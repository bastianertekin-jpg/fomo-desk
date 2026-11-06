"""db.py — SQLite WAL store, STEP 4 data model (literal). Single writer process; agents share one connection.

Units (documented, not in spec): ts = unix seconds; prices.price / positions.*_price = USD per token;
size_usd/pnl_usd/fees_usd = USD; balance.value = PAPER equity in USD (Home chart, starts at desk.paper_start_stack).
positions.mode / signals.mode = "PAPER" | "LIVE". A LIVE position with exit_reason set and closed_at NULL = "exiting".
Internal desk state lives in settings under keys starting with "_" (e.g. _paper_cash_usd, _sol_usd);
those are hidden from get_settings() so they never leak into config.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

ACTIVITY_KINDS = ("bought", "sold", "stopped", "rejected")
FOREVER = 32503680000.0  # year 3000 — skips.until for owner /kill

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS coins (
  mint TEXT PRIMARY KEY, name TEXT, symbol TEXT, pool TEXT, migrated_at REAL, creator TEXT,
  creator_balance_at_migration REAL, holders INTEGER
);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, ts REAL NOT NULL, liquidity_usd REAL,
  top10_pct REAL, h1_change_pct REAL, age_min REAL, verdict TEXT, reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_scans_mint_ts ON scans(mint, ts);
CREATE TABLE IF NOT EXISTS audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, ts REAL NOT NULL, layer TEXT, name TEXT,
  value_text TEXT, pass INTEGER, source TEXT
);
CREATE INDEX IF NOT EXISTS ix_audits_mint_ts ON audits(mint, ts);
CREATE TABLE IF NOT EXISTS verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, ts REAL NOT NULL, alive_6h REAL, organic REAL,
  flush REAL, chase REAL, score REAL, pass INTEGER, reason TEXT, input_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS ix_verdicts_mint_ts ON verdicts(mint, ts);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, ts REAL NOT NULL, price REAL, size_usd REAL,
  stack_pct REAL, link TEXT, mode TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, opened_at REAL NOT NULL, entry_price REAL,
  size_usd REAL, peak_price REAL, stop_price REAL, trail_active INTEGER DEFAULT 0,
  closed_at REAL, exit_price REAL, exit_reason TEXT, pnl_usd REAL, fees_usd REAL, mode TEXT
);
CREATE INDEX IF NOT EXISTS ix_positions_open ON positions(closed_at, mint);
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('bought','sold','stopped','rejected')),
  mint TEXT, agent TEXT, title TEXT, subtitle TEXT, value REAL
);
CREATE TABLE IF NOT EXISTS prices (mint TEXT NOT NULL, ts REAL NOT NULL, price REAL, volume_m5 REAL);
CREATE INDEX IF NOT EXISTS ix_prices_mint_ts ON prices(mint, ts);
CREATE TABLE IF NOT EXISTS balance (ts REAL NOT NULL, value REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_balance_ts ON balance(ts);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS change_log (ts REAL NOT NULL, key TEXT, old TEXT, new TEXT, who TEXT);
CREATE TABLE IF NOT EXISTS skips (mint TEXT PRIMARY KEY, until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS heartbeats (agent TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, p256dh TEXT, auth TEXT, created_at REAL);
"""

LEGACY_TABLES = ("snapshots", "analyses", "orders", "kills")


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.hb_detail: dict[str, tuple[str, str | None]] = {}  # in-memory only (heartbeats is agent, ts)
        with self.lock:
            old = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if old & set(LEGACY_TABLES):
                raise RuntimeError(f"{self.path} has the pre-STEP4 schema; move it aside (see README) and restart")
            self.conn.executescript(SCHEMA)

    # ---- low level
    def x(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def q(self, sql, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()) -> dict | None:
        r = self.q(sql, args)
        return r[0] if r else None

    # ---- settings + change_log
    def get_settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.q("SELECT key, value FROM settings") if not r["key"].startswith("_")}

    def get_setting(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_setting(self, key, value, who="desk"):
        old = self.one("SELECT value FROM settings WHERE key=?", (key,))
        new = json.dumps(value)
        now = time.time()
        self.x("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
               (key, new, now))
        if not key.startswith("_") and (old is None or old["value"] != new):
            self.x("INSERT INTO change_log(ts,key,old,new,who) VALUES(?,?,?,?,?)", (now, key, old and old["value"], new, who))

    # ---- activity / heartbeats
    def activity(self, kind, mint=None, agent=None, title=None, subtitle=None, value=None):
        if kind not in ACTIVITY_KINDS:
            raise ValueError(f"activity.kind must be one of {ACTIVITY_KINDS}")
        self.x("INSERT INTO activity(ts,kind,mint,agent,title,subtitle,value) VALUES(?,?,?,?,?,?,?)",
               (time.time(), kind, mint, agent, title, subtitle, value))

    def beat(self, agent, status="ok", detail=None):
        self.hb_detail[agent] = (status, detail)
        self.x("INSERT INTO heartbeats(agent,ts) VALUES(?,?) ON CONFLICT(agent) DO UPDATE SET ts=excluded.ts", (agent, time.time()))

    def heartbeats(self) -> list[dict]:
        out = []
        for h in self.q("SELECT agent, ts FROM heartbeats ORDER BY agent"):
            st, det = self.hb_detail.get(h["agent"], ("unknown", None))
            out.append({**h, "status": st, "detail": det})
        return out

    # ---- coins
    COIN_COLS = ("name", "symbol", "pool", "migrated_at", "creator", "creator_balance_at_migration", "holders")

    def upsert_coin(self, mint, **kw) -> bool:
        kw = {k: v for k, v in kw.items() if k in self.COIN_COLS and v is not None}
        if self.one("SELECT 1 AS e FROM coins WHERE mint=?", (mint,)) is None:
            cols = ("mint", *kw)
            self.x(f"INSERT INTO coins({','.join(cols)}) VALUES({','.join('?' * len(cols))})", (mint, *kw.values()))
            return True
        if kw:
            self.x(f"UPDATE coins SET {', '.join(f'{k}=?' for k in kw)} WHERE mint=?", (*kw.values(), mint))
        return False

    def coin(self, mint) -> dict | None:
        return self.one("SELECT * FROM coins WHERE mint=?", (mint,))

    # ---- scans
    def add_scan(self, mint, verdict, reason=None, liquidity_usd=None, top10_pct=None, h1_change_pct=None, age_min=None):
        self.x("INSERT INTO scans(mint,ts,liquidity_usd,top10_pct,h1_change_pct,age_min,verdict,reason) VALUES(?,?,?,?,?,?,?,?)",
               (mint, time.time(), liquidity_usd, top10_pct, h1_change_pct, age_min, verdict, reason))

    def last_scan(self, mint):
        return self.one("SELECT * FROM scans WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))

    def coin_status(self, mint) -> str:
        """Derived status for the app: skipped|traded|signal|rejected|analysed|audited|passed|killed|waiting."""
        if self.is_skipped(mint):
            return "skipped"
        if self.one("SELECT 1 AS e FROM positions WHERE mint=?", (mint,)):
            return "traded"
        sig = self.one("SELECT status FROM signals WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if sig:
            return "signal" if sig["status"] in ("sent", "bought") else "rejected"
        v = self.one("SELECT pass FROM verdicts WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if v:
            return "analysed" if v["pass"] else "rejected"
        if self.one("SELECT 1 AS e FROM audits WHERE mint=? AND pass=0", (mint,)):
            return "rejected"
        if self.one("SELECT 1 AS e FROM audits WHERE mint=?", (mint,)):
            return "audited"
        s = self.last_scan(mint)
        return {"pass": "passed", "kill": "killed"}.get(s["verdict"], s["verdict"]) if s else "waiting"

    # ---- audits / verdicts / signals
    def add_audit(self, mint, layer, name, value_text, passed, source, ts=None):
        self.x("INSERT INTO audits(mint,ts,layer,name,value_text,pass,source) VALUES(?,?,?,?,?,?,?)",
               (mint, ts or time.time(), layer, name, value_text, None if passed is None else int(bool(passed)), source))

    def add_verdict(self, mint, scores: dict, score, passed, reason, input_tokens=None):
        self.x("INSERT INTO verdicts(mint,ts,alive_6h,organic,flush,chase,score,pass,reason,input_tokens) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (mint, time.time(), scores.get("alive_6h"), scores.get("organic"), scores.get("flush"), scores.get("chase"),
                score, int(bool(passed)), reason, input_tokens))

    def add_signal(self, mint, price, size_usd, stack_pct, link, mode, status):
        cur = self.x("INSERT INTO signals(mint,ts,price,size_usd,stack_pct,link,mode,status) VALUES(?,?,?,?,?,?,?,?)",
                     (mint, time.time(), price, size_usd, stack_pct, link, mode, status))
        return cur.lastrowid

    # ---- skips (replaces kills)
    def skip(self, mint, until=FOREVER):
        self.x("INSERT INTO skips(mint,until) VALUES(?,?) ON CONFLICT(mint) DO UPDATE SET until=excluded.until", (mint, until))

    def is_skipped(self, mint, now=None) -> bool:
        return self.one("SELECT 1 AS e FROM skips WHERE mint=? AND until>?", (mint, now or time.time())) is not None

    # ---- prices
    def add_price(self, mint, price, volume_m5=None, ts=None):
        if price is None:
            return
        self.x("INSERT INTO prices(mint,ts,price,volume_m5) VALUES(?,?,?,?)", (mint, ts or time.time(), price, volume_m5))

    def last_price(self, mint) -> dict | None:
        return self.one("SELECT * FROM prices WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,))

    # ---- paper cash / balance (USD)
    def sol_usd(self, fallback: float = 0.0) -> float:
        return float(self.get_setting("_sol_usd", fallback) or fallback)

    def cash_usd(self) -> float:
        return float(self.get_setting("_paper_cash_usd", 0.0))

    def set_cash_usd(self, v: float):
        self.set_setting("_paper_cash_usd", round(float(v), 6))

    def seed_paper(self, start_usd: float):
        """Empty DB -> PAPER cash + first balance point at start_usd (desk.paper_start_stack) + PAPER streak start."""
        if self.get_setting("_paper_cash_usd") is None:
            self.set_cash_usd(start_usd)
        if self.last_balance() is None:
            self.add_balance(start_usd)
        if self.get_setting("_paper_since") is None and self.get_setting("_live_ok") is None:
            first = self.one("SELECT MIN(ts) AS t FROM balance") or {}
            self.set_setting("_paper_since", float(first.get("t") or time.time()))

    # ---- STEP 11 mode markers (internal "_" keys, never in config)
    def paper_since(self) -> float | None:
        v = self.get_setting("_paper_since")
        return float(v) if v is not None else None

    def mark_paper(self, ts: float | None = None):
        """Desk is (back) in PAPER: a new PAPER streak starts now; LIVE confirmation dropped."""
        self.set_setting("_paper_since", float(ts or time.time()))
        self.set_setting("_live_ok", None)

    def mark_live(self, proof: dict):
        """Owner typed LIVE with a full PAPER night in the db: load_config may now honour desk.mode=LIVE."""
        self.set_setting("_live_ok", {**proof, "ts": time.time()})
        self.set_setting("_paper_since", None)

    def live_ok(self) -> bool:
        return bool(self.get_setting("_live_ok"))

    def add_balance(self, value, ts=None):
        self.x("INSERT INTO balance(ts,value) VALUES(?,?)", (ts or time.time(), value))

    def last_balance(self) -> dict | None:
        return self.one("SELECT ts, value FROM balance ORDER BY ts DESC, rowid DESC LIMIT 1")

    # ---- positions
    def open_positions(self):
        return self.q("SELECT * FROM positions WHERE closed_at IS NULL ORDER BY opened_at")

    def open_position(self, mint):
        return self.one("SELECT * FROM positions WHERE mint=? AND closed_at IS NULL ORDER BY id DESC LIMIT 1", (mint,))

    @staticmethod
    def qty(p) -> float:
        return (p["size_usd"] or 0) / p["entry_price"] if p.get("entry_price") else 0.0

    def mark(self, p) -> float:
        lp = self.last_price(p["mint"])
        return (lp and lp["price"]) or p["entry_price"]

    def _opens(self, mode: str | None):
        ps = self.open_positions()
        return ps if mode is None else [p for p in ps if (p.get("mode") or "PAPER").upper() == mode]

    def open_value_usd(self, mode: str | None = "PAPER") -> float:
        """Open positions marked to last price, USD. Default PAPER only (LIVE money sits in FOMO, not here)."""
        return sum(self.qty(p) * self.mark(p) for p in self._opens(mode))

    def open_cost_usd(self, mode: str | None = "PAPER") -> float:
        return sum(p["size_usd"] or 0 for p in self._opens(mode))

    def equity_usd(self) -> float:
        """STEP 11 simulated PAPER balance: cash + open PAPER value."""
        return self.cash_usd() + self.open_value_usd("PAPER")

    def realized_today_usd(self) -> float:
        t0 = time.time() - (time.time() % 86400)
        r = self.one("SELECT COALESCE(SUM(pnl_usd),0) AS p FROM positions WHERE closed_at>=?", (t0,))
        return float(r["p"]) if r else 0.0


def night_start(now: float | None = None, report_time: str = "08:00") -> float:
    """Start of the current 'night' = the latest report_time (box-local) at or before now."""
    import datetime as _dt
    now = time.time() if now is None else now
    hh, mm = (int(x) for x in str(report_time).split(":")[:2])
    d = _dt.datetime.fromtimestamp(now)
    start = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if start > d:
        start -= _dt.timedelta(days=1)
    return start.timestamp()


def next_report(now: float | None = None, report_time: str = "08:00") -> float:
    return night_start(now, report_time) + 86400
