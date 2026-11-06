"""commands.py — STEP 9 · talking to the desk. Pure command layer shared by Telegram (and any other channel).

  handle(text) -> [Reply]     text = "/command args" or plain text ("why Sphere", "set stop to -8%")
  callback(data, message)     inline [Bought] / [Skip] / [Sold] -> bus "control" {cmd, mint}

X = name, symbol or mint (also a mint prefix). Unknown name/symbol -> ask for the mint; 2+ matches -> list each
with its coin_url and ask which. Every coin in every reply carries links.coin_url (via notifier/links only).
No command places an order; "buy X" only resends a push or audits read-only ("not sized, not on the desk").

CLI (reads/writes the same db; a running desk picks db settings up within ~10 s via main.sync_settings):
  .venv/bin/python commands.py "why Sphere"
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import links
import notifier
from agents.common import norm_name, short
from agents.executor import push_text, ticket_check, ticket_text
from config import ALIASES, BOOLS, CHOICES, DEFAULTS, RANGES, STRINGS, _set_path, get_path, validate
from db import night_start

AGENT_NAMES = ("scanner", "auditor", "analyst", "executor", "risk")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MINT_RE = re.compile(rf"^[{B58}]{{32,44}}$")
PREFIX_RE = re.compile(rf"^[{B58}]{{4,31}}$")
CALLBACKS = ("bought", "skip", "sold")
PUSHES_KEY = "_pushes"          # tonight's Buy pushes as sent: {mint: {"text", "ts"}} (Telegram notifier writes it)
REPORT_KEY = "_last_report_url"  # STEP 12 night-report writes the morning Page link here
LIVE_CONFIRM_S = 120
TELEGRAM_LIVE_REFUSAL = (
    "Telegram cannot activate LIVE. Desk stays PAPER. "
    "LIVE is typed outside Telegram, and only after a full PAPER night, "
    "reviewed results, approved limits, and an approved wallet."
)
NEG_DISPLAY = ("risk.stop_pct",)  # shown as "-6%"

HELP = """Talk to the desk (/command or plain text). X = name, symbol or mint.
buy X – resend tonight's Buy push, or audit X now (never an order)
sell X – [Sell on FOMO] for your open X
why X – which agent stopped X, the rule, the value, the time
pause Scanner · resume Scanner – any agent (Risk not while a position is open)
pause · resume – no new entries (whole desk)
set stop to -8% – any setting by name or alias
stack 1240 – LIVE stack (USD)
status – mode, balance, open positions
paper · live – switch mode (live needs you to type LIVE)
invite – FOMO signup link
report – last morning Page
also: balance · positions · kill X · close [X] · links · help"""


@dataclass
class Reply:
    text: str
    buttons: list = field(default_factory=list)  # flat row, or list of rows
    mint: str | None = None                       # callbacks (Bought/Skip/Sold) get ":mint" appended


# ---------------------------------------------------------------- pure helpers
def label(c: dict) -> str:
    n, s = c.get("name"), c.get("symbol")
    if n and s and n.lower() != s.lower():
        return f"{n} ({s})"
    return n or s or short(c.get("mint"))


def coin_line(c: dict) -> str:
    return f"{label(c)} · {links.coin_url(c['mint'])}"


def hhmm(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "?"
    now = now or time.time()
    same_day = time.strftime("%Y%m%d", time.localtime(ts)) == time.strftime("%Y%m%d", time.localtime(now))
    return time.strftime("%H:%M" if same_day else "%d %b %H:%M", time.localtime(ts))


def resolve(db, q: str) -> list[dict]:
    """X -> matching coins [{mint,name,symbol}] from db.coins + open positions. [] = unknown."""
    q = (q or "").strip().strip("\"'`").lstrip("$").strip()
    if not q:
        return []
    if MINT_RE.match(q):
        return [db.coin(q) or {"mint": q, "name": None, "symbol": None}]
    ql = q.lower()
    rows = db.q("SELECT mint,name,symbol FROM coins WHERE lower(name)=? OR lower(symbol)=? ORDER BY mint", (ql, ql))
    if not rows and (nq := norm_name(q)):
        rows = [c for c in db.q("SELECT mint,name,symbol FROM coins ORDER BY mint")
                if nq in (norm_name(c["name"]), norm_name(c["symbol"]))]
    if not rows and PREFIX_RE.match(q):
        mints = {r["mint"] for r in db.q("SELECT mint FROM coins WHERE mint LIKE ?", (q + "%",))}
        mints |= {p["mint"] for p in db.open_positions() if p["mint"].startswith(q)}
        rows = [db.coin(m) or {"mint": m, "name": None, "symbol": None} for m in sorted(mints)]
    seen, out = set(), []
    for r in rows:
        if r["mint"] not in seen:
            seen.add(r["mint"])
            out.append({"mint": r["mint"], "name": r.get("name"), "symbol": r.get("symbol")})
    return out


def ask_which(q: str, coins: list[dict]) -> Reply:
    if not coins:
        return Reply(f"I don't know \"{q}\". Send me its mint address.")
    lines = [f"\"{q}\" matches {len(coins)} coins, which one? Reply with the mint:"]
    lines += [f"• {label(c)} · {c['mint']}\n  {links.coin_url(c['mint'])}" for c in coins]
    return Reply("\n".join(lines))


def resolve_key(name: str):
    """setting name/alias -> (dotted key, shown name), or (None, [candidates]) if ambiguous, (None, "unknown")."""
    k = re.sub(r"[\s\-]+", "_", (name or "").strip().lower())
    if k in ALIASES:
        return ALIASES[k], k
    if "." in k:
        v = get_path(DEFAULTS, k, "__missing__")
        if v != "__missing__" and not isinstance(v, dict):
            return k, k
    leaves: list[str] = []

    def walk(d, pre=""):
        for kk, v in d.items():
            if isinstance(v, dict):
                walk(v, f"{pre}{kk}.")
            elif kk == k:
                leaves.append(f"{pre}{kk}")
    walk(DEFAULTS)
    if len(leaves) == 1:
        return leaves[0], k
    return None, leaves or "unknown"


def fmt_setting(dotted: str, v) -> str:
    if v is None:
        return "unset"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, (int, float)):
        if dotted in NEG_DISPLAY:
            return f"-{abs(v):g}%"
        if dotted.endswith("_pct"):
            return f"{v:g}%"
        if dotted.endswith("_usd") or dotted.endswith("_stack") or dotted == "live_stack":
            return f"${v:,.2f}"
        return f"{v:g}"
    return str(v)


def coerce(dotted: str, raw: str):
    """Owner text -> typed value, then config.validate (raises ValueError that names the range)."""
    s = raw.strip().strip("\"'")
    if dotted in BOOLS or dotted in STRINGS:
        return validate(dotted, s)  # STEP 10: on/off, HH:MM, http(s) URL, handle — refusals name the range
    if dotted in CHOICES:
        return validate(dotted, s.upper() if dotted == "desk.mode" else s.lower())
    num = s.replace(",", "").replace("$", "").rstrip("%").strip()
    try:
        f = float(num)
    except ValueError as e:
        r = RANGES.get(dotted)
        raise ValueError(f"{dotted} must be a number" + (f" between {r[0]} and {r[1]}" if r else "")) from e
    if dotted in NEG_DISPLAY or dotted == "risk.trail_pct":
        f = abs(f)  # "stop -8%" and "stop 8" mean the same
    return validate(dotted, f)


def why_events(db, mint: str) -> list[dict]:
    """Every 'stop' for this coin: {agent, rule, value, ts}, oldest first."""
    ev = []
    for s in db.q("SELECT * FROM scans WHERE mint=? AND verdict!='pass'", (mint,)):
        bits = []
        if s["liquidity_usd"] is not None:
            bits.append(f"liquidity ${s['liquidity_usd']:,.0f}")
        if s["top10_pct"] is not None:
            bits.append(f"top10 {s['top10_pct']:g}%")
        if s["h1_change_pct"] is not None:
            bits.append(f"h1 {s['h1_change_pct']:+g}%")
        if s["age_min"] is not None:
            bits.append(f"age {s['age_min']:g}m")
        ev.append({"agent": "Scanner", "rule": s["reason"] or s["verdict"], "value": " · ".join(bits) or "-", "ts": s["ts"]})
    for a in db.q("SELECT * FROM audits WHERE mint=? AND pass=0", (mint,)):
        ev.append({"agent": "Auditor", "rule": f"layer {a['layer']} · {a['name']}", "value": a["value_text"], "ts": a["ts"]})
    for v in db.q("SELECT * FROM verdicts WHERE mint=? AND pass=0", (mint,)):
        ev.append({"agent": "Analyst", "rule": v["reason"] or "rejected",
                   "value": f"score {v['score']:g}" if v["score"] is not None else "no score", "ts": v["ts"]})
    for a in db.q("SELECT * FROM activity WHERE mint=? AND agent='executor' AND kind='rejected'", (mint,)):
        rule = (a["subtitle"] or "").split("·", 1)[-1].strip() or "skipped"
        ev.append({"agent": "Executor", "rule": rule, "value": "not sized", "ts": a["ts"]})
    for s in db.q("SELECT * FROM signals WHERE mint=? AND status='missed'", (mint,)):
        ev.append({"agent": "Executor", "rule": "missed (no [Bought] tap in the confirm window)",
                   "value": f"${s['size_usd'] or 0:,.2f}", "ts": s["ts"]})
    for p in db.q("SELECT * FROM positions WHERE mint=? AND exit_reason IS NOT NULL", (mint,)):
        pnl = p["pnl_usd"]
        pct = (pnl / p["size_usd"] * 100) if pnl is not None and p["size_usd"] else None
        val = (f"{'+' if (pnl or 0) >= 0 else '-'}${abs(pnl or 0):,.2f}" + (f" ({pct:+.1f}%)" if pct is not None else "")
               + (f" at ${p['exit_price']:.10g}" if p["exit_price"] else ""))
        ev.append({"agent": "Risk", "rule": p["exit_reason"], "value": val, "ts": p["closed_at"] or p["opened_at"]})
    sk = db.one("SELECT until FROM skips WHERE mint=?", (mint,))
    if sk and sk["until"] > time.time():
        ev.append({"agent": "You", "rule": "skipped/killed",
                   "value": "until " + ("forever" if sk["until"] > 3e10 else hhmm(sk["until"])), "ts": None})
    return sorted(ev, key=lambda e: e["ts"] or 0)


def tonight_push(db, cfg, mint: str, now: float | None = None) -> str | None:
    """The Buy push text sent tonight for this mint, or None if it never went out."""
    ns = night_start(now, cfg["desk"]["report_time"])
    rec = (db.get_setting(PUSHES_KEY, {}) or {}).get(mint)
    if rec and rec.get("ts", 0) >= ns and rec.get("text"):
        return rec["text"]
    s = db.one("SELECT * FROM signals WHERE mint=? AND ts>=? AND status IN ('sent','bought','missed') "
               "ORDER BY id DESC LIMIT 1", (mint, ns))
    if not s:
        return None
    c = db.coin(mint) or {}
    v = db.one("SELECT score FROM verdicts WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,)) or {}
    score = v.get("score")
    r = cfg["risk"]
    return push_text((s["mode"] or "PAPER") != "LIVE", label({**c, "mint": mint}), s["size_usd"] or 0,
                     s["stack_pct"] or 0, f"{score:g}" if isinstance(score, (int, float)) else "?",
                     r["stop_pct"], r["trail_pct"])


def record_push(db, msg: dict, now: float | None = None):
    """Telegram notifier: remember tonight's Buy pushes exactly as sent (for the "buy X" resend)."""
    if msg.get("kind") != "buy" or not msg.get("mint"):
        return
    now = now or time.time()
    d = {k: v for k, v in (db.get_setting(PUSHES_KEY, {}) or {}).items() if now - v.get("ts", 0) < 2 * 86400}
    d[msg["mint"]] = {"text": msg["text"], "ts": now}
    db.set_setting(PUSHES_KEY, d)


def parse_callback(data: str, message: dict | None = None):
    """'bought:<mint>' (or bare 'bought' + the mint from the message's FOMO button URL) -> (cmd, mint) | None."""
    cmd, _, mint = (data or "").partition(":")
    if cmd not in CALLBACKS:
        return None
    if not mint:
        for row in ((message or {}).get("reply_markup") or {}).get("inline_keyboard") or []:
            for b in row:
                m = re.search(r"[?&]address=([^&]+)", b.get("url") or "")
                if m:
                    mint = m.group(1)
                    break
            if mint:
                break
    return cmd, (mint or None)


NIGHT_MAX_GAP_S = 3600  # a desk restart inside the night is fine; a dead desk for over an hour is not a full night


def paper_night_status(db, cfg, now: float | None = None) -> dict:
    """STEP 11 "at least 1 full PAPER night in the db".

    A night = report_time to report_time (db.night_start). With N = the latest report_time <= now, the last
    closed night is [N - 24h, N). It counts as a full PAPER night when ALL hold:
      1. the current PAPER streak began at or before N - 24h (db "_paper_since", set by seed_paper / "paper",
         cleared by the LIVE switch; legacy dbs without it fall back to the first balance row), and
      2. db.balance (PAPER equity, written every minute by main + on every fill/exit) has points covering the
         whole night: one at or before N - 24h + 1h, one at or after N - 1h, and no gap over 1h between them.
    So the desk ran in PAPER through a full report-to-report night and its balance closed past report_time."""
    now = time.time() if now is None else now
    n_end = night_start(now, cfg["desk"]["report_time"])
    n_start = n_end - 86400
    since = db.paper_since()
    if since is None and not db.live_ok():
        first = db.one("SELECT MIN(ts) AS t FROM balance") or {}
        since = first.get("t")
    st = {"night_from": n_start, "night_to": n_end, "paper_since": since, "points": 0, "max_gap_s": None,
          "done": False, "why": None}
    if since is None or since > n_start:
        st["why"] = "PAPER streak started after the last night began" if since else "no PAPER history"
        return st
    lo = max(since, n_start - NIGHT_MAX_GAP_S)
    pts = [r["ts"] for r in db.q("SELECT ts FROM balance WHERE ts>=? AND ts<=? ORDER BY ts",
                                 (lo, n_end + NIGHT_MAX_GAP_S))]
    st["points"] = len(pts)
    if not pts or pts[0] > n_start + NIGHT_MAX_GAP_S or pts[-1] < n_end - NIGHT_MAX_GAP_S:
        st["why"] = "desk balance does not span the whole night"
        return st
    st["max_gap_s"] = max([b - a for a, b in zip(pts, pts[1:])] or [0])
    if st["max_gap_s"] > NIGHT_MAX_GAP_S:
        st["why"] = f"desk down {st['max_gap_s'] / 3600:.1f}h inside the night"
        return st
    st["done"] = True
    return st


def paper_night_done(db, cfg, now: float | None = None) -> bool:
    """STEP 11: at least 1 full PAPER night in the db (see paper_night_status)."""
    return paper_night_status(db, cfg, now)["done"]


# ---------------------------------------------------------------- commands
class Commands:
    def __init__(self, ctx, who: str = "telegram"):
        self.ctx, self.db, self.bus, self.who = ctx, ctx.db, ctx.bus, who
        self._auditor = None

    @property
    def cfg(self):
        return self.ctx.cfg

    @property
    def paper(self) -> bool:
        return str(self.cfg["desk"].get("mode", "PAPER")).upper() != "LIVE"

    def _pick(self, q, coins=None):
        coins = resolve(self.db, q) if coins is None else coins
        return coins[0] if len(coins) == 1 else ask_which(q, coins)

    def stack_now(self) -> float:
        if self.paper:
            return self.db.cash_usd() + self.db.open_cost_usd("PAPER")
        return float(self.cfg.get("live_stack") or 0)

    def _write(self, dotted: str, value):
        self.db.set_setting(dotted, value, who=self.who)  # + change_log row
        _set_path(self.cfg, dotted, value)

    # ---- dispatch
    async def handle(self, text: str) -> list[Reply]:
        t = (text or "").strip()
        if not t:
            return []
        if self.who == "telegram":
            head = t.lstrip("/").split()[0].split("@")[0]
            if head.upper() == "LIVE":
                self.ctx.state.pop("live_pending", None)
                return [Reply(TELEGRAM_LIVE_REFUSAL)]
        pending = self.ctx.state.get("live_pending")
        if t == "LIVE" and pending and time.time() - pending <= LIVE_CONFIRM_S:
            return self.confirm_live()
        self.ctx.state.pop("live_pending", None)
        first, _, rest = t.partition(" ")
        cmd = first.lstrip("/").split("@")[0].lower()
        rest = rest.strip()
        if cmd == "buy":
            return await self.buy(rest)
        fn = {
            "sell": self.sell, "why": self.why, "set": self.set, "stack": self.stack, "status": self.status,
            "paper": self.to_paper, "live": self.to_live, "invite": self.invite, "report": self.report,
            "help": self.help, "start": self.help, "balance": self.balance, "positions": self.positions,
            "kill": self.kill, "close": self.close, "links": self.links,
            "pause": lambda a: self.pause(a, True), "resume": lambda a: self.pause(a, False),
        }.get(cmd)
        if fn is None:
            return [Reply("I didn't get that.\n\n" + HELP)]
        return fn(rest)

    def help(self, _=""):
        return [Reply(HELP)]

    # ---- buy X
    def auditor(self):
        if self._auditor is None:
            from agents.auditor import Auditor
            self._auditor = Auditor(self.ctx, subscribe=False, dry=True)
        return self._auditor

    async def buy(self, q):
        if not q:
            return [Reply("buy what? Send a name, symbol or mint.")]
        c = self._pick(q)
        if isinstance(c, Reply):
            return [c]
        mint = c["mint"]
        url = links.coin_url(mint)
        pushed = tonight_push(self.db, self.cfg, mint)
        if pushed:
            return [Reply(pushed if url in pushed else f"{pushed}\n{url}", notifier.buy_buttons(mint), mint)]
        coin = self.db.coin(mint) or {}
        try:
            market = await self.ctx.src.market(mint)
        except Exception:
            market = None
        m = market or {}
        scan = {"name": coin.get("name") or m.get("name"), "pool": coin.get("pool") or m.get("pair_address"),
                "liquidityUsd": m.get("liquidity_usd")}
        res = await self.auditor().owner_check(mint, scan)
        who = label({"mint": mint, "name": c.get("name") or m.get("name"), "symbol": c.get("symbol") or m.get("symbol")})
        lines = [f"{who} was not pushed tonight. Auditor now: {res['green']}/8 green"
                 + ("" if res["passed"] else f" · red at {res['reason']}")]
        lines += [f"{x['n']} {x['name']}: {'✅' if x['pass'] else '❌'} {x['value']}" for x in res["layers"]]
        lines += ["not sized, not on the desk", url]
        return [Reply("\n".join(lines), [notifier.buy_link(mint)], mint)]

    # ---- sell X
    def sell(self, q):
        if not q:
            return [Reply("sell what? Send a name, symbol or mint.")]
        coins = resolve(self.db, q)
        open_ = [c for c in coins if self.db.open_position(c["mint"])]
        c = open_[0] if len(open_) == 1 else self._pick(q, coins)
        if isinstance(c, Reply):
            return [c]
        p = self.db.open_position(c["mint"])
        if not p:
            return [Reply(f"No open position on {coin_line(c)}")]
        return [Reply(self.pos_line(p, c), notifier.sell_buttons(c["mint"]), c["mint"])]

    def pos_line(self, p, c=None) -> str:
        if c is None:
            row = self.db.coin(p["mint"]) or {}
            c = {"mint": p["mint"], "name": row.get("name"), "symbol": row.get("symbol")}
        mark = self.db.mark(p)
        pct = (mark / p["entry_price"] - 1) * 100 if p["entry_price"] else 0
        mode = (p["mode"] or "PAPER").upper()
        ex = f" · exiting ({p['exit_reason']})" if p["exit_reason"] else ""
        return (f"{'PAPER · ' if mode == 'PAPER' else ''}{label(c)} · ${p['size_usd'] or 0:,.2f} · {pct:+.1f}% · "
                f"since {hhmm(p['opened_at'])}{ex}\n{links.coin_url(p['mint'])}")

    # ---- why X
    def why(self, q):
        if not q:
            return [Reply("why what? Send a name, symbol or mint.")]
        c = self._pick(q)
        if isinstance(c, Reply):
            return [c]
        p = self.db.open_position(c["mint"])
        if p and not p["exit_reason"]:
            return [Reply(f"Nothing stopped it, it's an open position.\n{self.pos_line(p, c)}",
                          notifier.sell_buttons(c["mint"]), c["mint"])]
        ev = why_events(self.db, c["mint"])
        if not ev:
            return [Reply(f"Nothing stopped {label(c)} (status: {self.db.coin_status(c['mint'])}).\n"
                          f"{links.coin_url(c['mint'])}")]
        e = ev[-1]
        when = f" at {hhmm(e['ts'])}" if e["ts"] else ""
        return [Reply(f"{e['agent']} stopped {label(c)}{when}\nrule: {e['rule']}\nvalue: {e['value']}\n"
                      f"{links.coin_url(c['mint'])}")]

    # ---- pause / resume
    def pause(self, arg, on: bool):
        a = (arg or "").strip().lower()
        if not a or a in ("desk", "all", "entries"):
            self.ctx.state["paused"] = on
            self.db.set_setting("paused", on, who=self.who)
            return [Reply("⏸ Paused new entries (agents keep running; Risk keeps guarding exits)." if on
                          else "▶️ Resumed new entries.")]
        if a not in AGENT_NAMES:
            return [Reply(f"No agent \"{arg}\". Agents: {', '.join(n.capitalize() for n in AGENT_NAMES)}")]
        if on and a == "risk" and self.db.open_positions():
            ps = "\n".join(self.pos_line(p) for p in self.db.open_positions())
            return [Reply(f"Risk can't be paused while a position is open:\n{ps}")]
        s = set(self.ctx.state.get("paused_agents") or ())
        was = a in s
        if on:
            s.add(a)
        else:
            s.discard(a)
        self.ctx.state["paused_agents"] = s
        self.db.set_setting("paused_agents", sorted(s), who=self.who)
        name = a.capitalize()
        if on:
            extra = " (Executor skips every signal while Risk is paused)" if a == "risk" else ""
            return [Reply(f"⏸ {name} {'already ' if was else ''}paused{extra}.")]
        return [Reply(f"▶️ {name} {'resumed' if was else 'was not paused'}.")]

    # ---- set
    def set(self, rest):
        m = re.match(r"^(?P<k>.+?)\s*(?:=|\s+to\s+|\s+)(?P<v>\S+)$", rest or "")
        if not m:
            return [Reply("Usage: set <name> to <value>, e.g. set stop to -8%")]
        key, shown = resolve_key(m.group("k"))
        if key is None:
            if isinstance(shown, list):
                return [Reply(f"\"{m.group('k')}\" is ambiguous: {' or '.join(shown)}. Say which.")]
            return [Reply(f"Unknown setting \"{m.group('k')}\". Try stop, trail, stack_pct, ticket, liquidity, top10, "
                          f"bundled, score, report_time, or a full key like risk.timeout_h.")]
        if key == "desk.mode":
            return [Reply("Switch mode with: paper  or  live")]
        try:
            new = coerce(key, m.group("v"))
        except ValueError as e:
            return [Reply(f"Refused: {e}. {shown} stays {fmt_setting(key, get_path(self.cfg, key))}.")]
        old = get_path(self.cfg, key)
        if old == new:
            return [Reply(f"{shown} already {fmt_setting(key, new)}")]
        self._write(key, new)
        out = f"{shown} {fmt_setting(key, old)} -> {fmt_setting(key, new)}"
        if key in ("executor.stack_pct", "executor.min_ticket_usd", "executor.below_min"):
            out += self.ticket_note()
        elif key == "desk.paper_start_stack":  # the stack the next PAPER night starts with
            out += self.ticket_note(float(new), tag="PAPER · ")
        return [Reply(out)]

    def ticket_note(self, stack: float | None = None, tag: str | None = None) -> str:
        _, msg = ticket_check(self.stack_now() if stack is None else stack, self.cfg["executor"])
        if not msg:
            return ""
        if tag is None:
            tag = "PAPER · " if self.paper and stack is None else ""
        return "\n" + ticket_text(msg, tag, icon=True)

    # ---- stack N
    def stack(self, rest):
        if not rest:
            return [Reply(f"LIVE stack {fmt_setting('live_stack', self.cfg.get('live_stack'))}. Set it with: stack 1240")]
        try:
            v = validate("live_stack", rest.replace("$", "").replace(",", "").strip())
        except ValueError as e:
            return [Reply(f"Refused: {e}")]
        old = self.cfg.get("live_stack")
        self.db.set_setting("live_stack", v, who=self.who)
        self.cfg["live_stack"] = v
        out = f"LIVE stack {fmt_setting('live_stack', old)} -> {fmt_setting('live_stack', v)}" + self.ticket_note(float(v))
        if self.paper:
            out += "\n(desk is PAPER: this stack sizes tickets once LIVE)"
        return [Reply(out)]

    # ---- status / balance / positions
    def balance_line(self) -> str:
        if self.paper:
            return f"PAPER cash ${self.db.cash_usd():,.2f} · equity ${self.db.equity_usd():,.2f}"
        st, used = float(self.cfg.get("live_stack") or 0), self.db.open_cost_usd("LIVE")
        return f"LIVE stack ${st:,.2f} · in positions ${used:,.2f} · free ${max(0.0, st - used):,.2f}"

    def status(self, _=""):
        pa = sorted(self.ctx.state.get("paused_agents") or ())
        ps = self.db.open_positions()
        lines = [f"{'PAPER' if self.paper else 'LIVE'} desk · "
                 f"{'PAUSED (no new entries)' if self.ctx.state.get('paused') else 'active'}"
                 + (f" · paused: {', '.join(a.capitalize() for a in pa)}" if pa else ""),
                 self.balance_line(), f"today {self.db.realized_today_usd():+.2f} USD · open {len(ps)}"]
        lines += [f"• {self.pos_line(p)}" for p in ps]
        hb = self.db.heartbeats()
        if hb:
            lines.append(" ".join(f"{h['agent']}:{h['status']}" for h in hb))
        rows = [[notifier.sell_link(p["mint"], label=label({**(self.db.coin(p["mint"]) or {}), "mint": p["mint"]}))]
                for p in ps]
        return [Reply("\n".join(lines), rows)]

    def balance(self, _=""):
        return [Reply(self.balance_line())]

    def positions(self, _=""):
        ps = self.db.open_positions()
        if not ps:
            return [Reply("No open positions.")]
        return [Reply(self.pos_line(p), notifier.sell_buttons(p["mint"]), p["mint"]) for p in ps]

    # ---- paper / live
    def to_paper(self, _=""):
        if self.paper:
            return [Reply("Already PAPER.")]
        self._write("desk.mode", "PAPER")
        self.db.mark_paper()  # new PAPER streak; LIVE needs a fresh full PAPER night again
        return [Reply("LIVE -> PAPER. New pushes start with \"PAPER · \"." + self.ticket_note(tag="PAPER · "))]

    def to_live(self, _=""):
        if self.who == "telegram":
            self.ctx.state.pop("live_pending", None)
            return [Reply(TELEGRAM_LIVE_REFUSAL)]
        if not self.paper:
            return [Reply("Already LIVE.")]
        self.ctx.state["live_pending"] = time.time()
        return [Reply("Switching to LIVE: money moves only when you tap [Buy on FOMO] and buy inside FOMO; "
                      "[Bought] opens, [Sold] closes. Type LIVE (capitals) within 2 min to confirm.")]

    def confirm_live(self):
        self.ctx.state.pop("live_pending", None)
        if self.who == "telegram":
            return [Reply(TELEGRAM_LIVE_REFUSAL)]
        night = paper_night_status(self.db, self.cfg)
        if not night["done"]:
            return [Reply("Not yet: LIVE needs at least 1 full PAPER night in the db. Desk stays PAPER."
                          + (f" ({night['why']})" if night.get("why") else ""))]
        if not self.cfg.get("live_stack"):
            return [Reply("Set your LIVE stack first (e.g. stack 1240), then type live again.")]
        self.db.mark_live({"night_from": night["night_from"], "night_to": night["night_to"], "who": self.who})
        self._write("desk.mode", "LIVE")
        return [Reply("PAPER -> LIVE. Pushes no longer start with \"PAPER · \". The desk places no orders: "
                      "tap [Buy on FOMO], buy inside FOMO, then [Bought]; [Sold] after you sell." + self.ticket_note())]

    # ---- invite / report / links / kill / close
    def invite(self, _=""):
        return [Reply(f"New to FOMO? {links.SIGNUP_URL}")]

    def links(self, _=""):
        h = self.cfg["desk"].get("fomo_handle")
        return [Reply(f"Sign up: {links.SIGNUP_URL}" + (f"\nProfile: {links.profile_url(h)}" if h else ""))]

    def report(self, _=""):
        url = self.db.get_setting(REPORT_KEY)
        return [Reply(f"Last morning Page: {url}" if url else "no report yet")]

    def kill(self, q):
        if not q:
            return [Reply("kill what? Send a name, symbol or mint.")]
        c = self._pick(q)
        if isinstance(c, Reply):
            return [c]
        self.db.skip(c["mint"])
        return [Reply(f"☠️ Killed {coin_line(c)}\n(never pushed again)")]

    def close(self, q):
        if not q:
            self.bus.publish("control", {"cmd": "close", "mint": None})
            return [Reply("Closing all open PAPER positions…")]
        c = self._pick(q)
        if isinstance(c, Reply):
            return [c]
        self.bus.publish("control", {"cmd": "close", "mint": c["mint"]})
        return [Reply(f"Closing (PAPER) {coin_line(c)}")]

    # ---- inline buttons
    def callback(self, data: str, message: dict | None = None) -> str:
        parsed = parse_callback(data, message)
        if not parsed:
            return "unknown button"
        cmd, mint = parsed
        if not mint:
            return "no coin on that button"
        self.bus.publish("control", {"cmd": cmd, "mint": mint})
        return {"bought": "Bought ✓", "skip": "Skipped until the morning report", "sold": "Sold ✓"}[cmd]


def _cli():  # pragma: no cover - manual tool
    import asyncio
    import sys

    from agents.common import Ctx
    from bus import Bus
    from config import DB_PATH, load_config
    from db import DB

    db = DB(DB_PATH)
    ctx = Ctx(cfg=load_config(db), db=db, bus=Bus(), offline="--online" not in sys.argv)
    ctx.state["paused"] = bool(db.get_setting("paused", False))
    ctx.state["paused_agents"] = set(db.get_setting("paused_agents", []) or [])
    text = " ".join(a for a in sys.argv[1:] if a != "--online")
    for r in asyncio.run(Commands(ctx, who="cli").handle(text)):
        print(r.text)
        rows = r.buttons if r.buttons and isinstance(r.buttons[0], list) else [r.buttons]
        for b in (b for row in rows for b in row):
            if b.get("url"):
                print(f"[{b['text']}] {b['url']}")
        print()


if __name__ == "__main__":
    _cli()
