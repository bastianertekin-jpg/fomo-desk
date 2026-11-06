"""api.py — STEP 7 API, Plan B: the same GET shapes written as static JSON files (no server).

Plan B = static GitHub Pages. There is NO HTTP server, NO SSE, NO working POST endpoint and NO 401:
GitHub Pages serves files to anyone holding the URL. Access control is (1) the unguessable
DATA_PATH directory and (2) a soft gate: state.json carries sha256("fomo-desk-gate:"+APP_TOKEN)
and the app compares it with the token from #t=. APP_TOKEN itself is never written.

Every `publish_every_s` (15 s) publish_once() writes, under publish/d/{DATA_PATH}/:

  Plan A endpoint                      Plan B file
  GET /api/state                       state.json
  GET /api/chart?range=R               chart.json (R=LIVE) + chart/{LIVE,1H,4H,1D,1W,ALL}.json
  GET /api/activity                    activity.json (30 newest, next_before)
  GET /api/activity?before=ts          activity/page_{n}.json (n>=1, 30 per page, chained by next_before)
  GET /api/coin/{mint}                 coin/{mint}.json
  GET /api/audit/{mint}                audit/{mint}.json
  GET /api/agents/{name}/decisions     agents/{name}/decisions.json (last 20)
  GET /api/account                     account.json
  GET /api/settings                    settings.json  (+ settings_meta.json: planb_readonly=true)
  GET /api/stream (SSE)                stream/last.json — buffered bus events since start (poll, not push)
  POST /api/settings|mode|push|agents/{name}/pause|resume   — Plan A only. Plan B: use Telegram commands.
  (index.json lists this map for the app.)

It also mirrors web/ -> publish/ (the static app). Nothing is written at bare publish/d/.
A secret guard refuses to write any file containing a secret value.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path

import links
import webpush
from config import (BOOLS, CHOICES, GROUPS, PUBLISH_DIR, RANGES, STRINGS, WEB_DIR, all_secret_values, leaf_keys,
                    range_text, read_secret)
from db import night_start

log = logging.getLogger("desk.api")
SAFE_PATH = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
WEB_EXT = {".html", ".css", ".js", ".json", ".png", ".svg", ".ico", ".webmanifest", ".txt"}
GATE_SALT = "fomo-desk-gate:"

AGENTS = ("scanner", "auditor", "analyst", "executor", "risk")  # pipeline order = the 5 cards
AGENT_COLORS = {"scanner": "#64d2ff", "auditor": "#ffffff", "analyst": "#ffd60a", "executor": "#00d35a", "risk": "#ff6b9a"}
CHART_RANGES = ("LIVE", "1H", "4H", "1D", "1W", "ALL")
RANGE_S = {"1H": 3600, "4H": 4 * 3600, "1D": 86400, "1W": 7 * 86400, "ALL": None}  # LIVE = since night start
MAX_POINTS = 600
PAGE = 30
MAX_PAGES = 20           # activity/page_1..page_20 (= 630 rows reachable)
DECISIONS = 20
STREAM_TOPICS = {"candidate": "scan", "audited": "audited", "signal": "signal", "fill": "fill", "ui": None}
STREAM_KEEP = 200
COLORS = {"blue": "#0a84ff", "green": "#00d35a", "red": "#ff453a", "orange": "#ff9f0a"}


class SecretLeak(RuntimeError):
    pass


def gate_hash(app_token: str | None) -> str | None:
    """Soft gate: app compares sha256(salt+t) with this. Hash only — never the token."""
    return hashlib.sha256((GATE_SALT + app_token).encode()).hexdigest() if app_token else None


def _usd(v) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def _coin_label(title: str | None, prefix: str) -> str:
    t = title or ""
    return t[len(prefix):].strip() if t.startswith(prefix) else t


def _small(msg: dict) -> dict:
    """Trim a bus message for stream/last.json (drop nested ctx blobs)."""
    out = {}
    for k, v in (msg or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
    return out


class PlanBWriter:
    name = "publisher"

    def __init__(self, ctx, publish_dir: Path = PUBLISH_DIR, web_dir: Path = WEB_DIR):
        self.ctx, self.db, self.cfg = ctx, ctx.db, ctx.cfg
        self.root = Path(publish_dir)
        self.web = Path(web_dir)
        dp = read_secret("DATA_PATH")
        if not dp or not SAFE_PATH.match(dp):
            raise RuntimeError("DATA_PATH secret missing or not URL-safe [A-Za-z0-9_-]{16,128}")
        self.data_dir = self.root / "d" / dp
        self.secrets = all_secret_values()
        self.gate = gate_hash(read_secret("APP_TOKEN"))
        self.writes = 0
        # stream buffer (Plan B stand-in for SSE): observe bus topics without consuming other subscribers' copies
        self.stream: deque = deque(maxlen=STREAM_KEEP)
        self._subs = {}
        bus = getattr(ctx, "bus", None)
        if bus is not None:
            for topic in STREAM_TOPICS:
                try:
                    self._subs[topic] = bus.subscribe(topic)
                except ValueError:
                    pass

    # ---------------------------------------------------------------- io
    def _guard(self, blob: bytes, where: str):
        for s in self.secrets:
            if s.encode() in blob:
                raise SecretLeak(f"refusing to write {where}: contains a secret value")

    def dumps(self, obj) -> bytes:
        return json.dumps(obj, separators=(",", ":"), default=str).encode()

    def write_json(self, rel: str, obj):
        blob = self.dumps(obj)
        self._guard(blob, rel)
        p = self.data_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, p)

    def mirror_web(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".nojekyll").write_text("")
        for src in self.web.rglob("*"):
            if src.is_dir() or src.suffix.lower() not in WEB_EXT:
                continue
            blob = src.read_bytes()
            self._guard(blob, str(src))
            dst = self.root / src.relative_to(self.web)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() or dst.read_bytes() != blob:
                dst.write_bytes(blob)
        # nothing listable at bare /d/
        for stray in ("index.html", "index.json"):
            (self.root / "d" / stray).unlink(missing_ok=True)

    # ---------------------------------------------------------------- helpers
    def night_t0(self) -> float:
        return night_start(time.time(), self.cfg["desk"].get("report_time", "08:00"))

    def coin_name(self, mint: str | None) -> str:
        if not mint:
            return "?"
        c = self.db.coin(mint) or {}
        return c.get("symbol") or c.get("name") or f"{mint[:4]}…{mint[-4:]}"

    def night_start_equity(self, t0: float) -> float:
        start = float(self.cfg["desk"]["paper_start_stack"])
        r = (self.db.one("SELECT value FROM balance WHERE ts<=? ORDER BY ts DESC LIMIT 1", (t0,))
             or self.db.one("SELECT value FROM balance WHERE ts>=? ORDER BY ts LIMIT 1", (t0,)))
        return float(r["value"]) if r else start

    def exit_lines(self, p: dict) -> dict:
        r = self.cfg["risk"]
        now = time.time()
        entry = p["entry_price"]
        trail = p["peak_price"] * (1 - r["trail_pct"] / 100) if p["trail_active"] and p["peak_price"] else None
        stop = p["stop_price"]
        cands = [x for x in (stop, trail) if x is not None]
        timeout_at = p["opened_at"] + r["timeout_h"] * 3600
        return {
            "stop_price": stop,
            "stop_pct": round((1 - stop / entry) * 100, 2) if entry and stop else None,
            "trail_active": bool(p["trail_active"]),
            "trail_price": trail,
            "trail_pct": r["trail_pct"],
            "trail_arms_at_price": entry * (1 + r["trail_start_pct"] / 100) if entry else None,
            "exit_line": max(cands) if cands else None,
            "exit_line_label": "Risk trailing stop" if trail is not None and (stop is None or trail >= stop) else "Risk stop",
            "timeout_at": timeout_at,
            "timeout_remaining_s": max(0, round(timeout_at - now)),
            "timeout_min_gain_price": entry * (1 + r["timeout_min_gain_pct"] / 100) if entry else None,
        }

    def position_view(self, p: dict) -> dict:
        c = self.db.coin(p["mint"]) or {}
        last = self.db.mark(p)
        qty = self.db.qty(p)
        ex = self.exit_lines(p)
        return {
            "id": p["id"], "mint": p["mint"], "symbol": c.get("symbol"), "name": c.get("name"), "qty": qty, "mode": p["mode"],
            "entry_price": p["entry_price"], "last_price": last, "size_usd": p["size_usd"], "peak_price": p["peak_price"],
            **ex,
            "status": "exiting" if p["exit_reason"] else "open", "exit_reason": p["exit_reason"],
            "value_usd": qty * last, "pnl_usd": qty * last - (p["size_usd"] or 0),
            "pnl_pct": (last / p["entry_price"] - 1) * 100 if p["entry_price"] else None,
            "opened_at": p["opened_at"], "agent": "executor", "label": f"Your position · ${p['size_usd'] or 0:,.2f} in · Executor",
            "coin_url": links.coin_url(p["mint"]), "fomo_url": links.coin_url(p["mint"]),
        }

    # ---------------------------------------------------------------- decisions (GET /api/agents/{name}/decisions)
    def decisions(self, agent: str, limit: int = DECISIONS) -> list[dict]:
        rows: list[dict] = []
        if agent == "scanner":
            for s in self.db.q("SELECT mint,ts,verdict,reason,liquidity_usd,top10_pct,h1_change_pct,age_min FROM scans "
                               "ORDER BY id DESC LIMIT ?", (limit,)):
                rows.append({"ts": s["ts"], "mint": s["mint"], "verdict": "pass" if s["verdict"] == "pass" else (s["verdict"] or "?"),
                             "reason": s["reason"] or ("→ auditor" if s["verdict"] == "pass" else None),
                             "facts": {k: s[k] for k in ("liquidity_usd", "top10_pct", "h1_change_pct", "age_min")}})
        elif agent == "auditor":
            for a in self.db.q("SELECT mint,ts FROM audits GROUP BY mint,ts ORDER BY ts DESC LIMIT ?", (limit,)):
                lay = self.db.q("SELECT layer,name,value_text,pass FROM audits WHERE mint=? AND ts=? ORDER BY CAST(layer AS INTEGER)",
                                (a["mint"], a["ts"]))
                red = [x for x in lay if x["pass"] == 0]
                ok = not red and lay and all(x["pass"] for x in lay)
                rows.append({"ts": a["ts"], "mint": a["mint"], "verdict": "pass" if ok else "reject",
                             "reason": "8/8 → Executor" if ok else (f"{red[0]['name']}: {red[0]['value_text']}" if red else "incomplete"),
                             "layers_passed": sum(1 for x in lay if x["pass"] == 1)})
        elif agent == "analyst":
            for v in self.db.q("SELECT mint,ts,score,pass,reason,alive_6h,organic,flush,chase FROM verdicts ORDER BY id DESC LIMIT ?", (limit,)):
                rows.append({"ts": v["ts"], "mint": v["mint"], "verdict": "pass" if v["pass"] else "reject", "reason": v["reason"],
                             "score": v["score"], "answers": {k: v[k] for k in ("alive_6h", "organic", "flush", "chase")}})
        elif agent == "executor":
            for s in self.db.q("SELECT mint,ts,price,size_usd,stack_pct,mode,status FROM signals ORDER BY id DESC LIMIT ?", (limit,)):
                rows.append({"ts": s["ts"], "mint": s["mint"], "verdict": s["status"],
                             "reason": f"${s['size_usd'] or 0:,.2f} · {s['stack_pct'] or 0:g}% of stack · {s['mode']}",
                             "price": s["price"], "size_usd": s["size_usd"]})
            for a in self.db.q("SELECT mint,ts,subtitle FROM activity WHERE agent='executor' AND kind='rejected' ORDER BY id DESC LIMIT ?", (limit,)):
                rows.append({"ts": a["ts"], "mint": a["mint"], "verdict": "reject", "reason": (a["subtitle"] or "").replace("Executor · ", "")})
        elif agent == "risk":
            for p in self.db.q("SELECT mint,opened_at,closed_at,exit_reason,exit_price,pnl_usd,entry_price,size_usd FROM positions "
                               "WHERE exit_reason IS NOT NULL ORDER BY COALESCE(closed_at,opened_at) DESC LIMIT ?", (limit,)):
                pct = (p["exit_price"] / p["entry_price"] - 1) * 100 if p["exit_price"] and p["entry_price"] else None
                rows.append({"ts": p["closed_at"] or p["opened_at"], "mint": p["mint"],
                             "verdict": {"stop": "stopped", "trail": "trailed out"}.get(p["exit_reason"], p["exit_reason"]),
                             "reason": (f"{_usd(p['pnl_usd'])}" if p["pnl_usd"] is not None else "exiting")
                                       + (f" ({pct:+.1f}%)" if pct is not None else ""), "pnl_usd": p["pnl_usd"]})
            for p in self.db.open_positions():
                if not p["exit_reason"]:
                    rows.append({"ts": p["opened_at"], "mint": p["mint"], "verdict": "guarding", "reason": "open position"})
        rows.sort(key=lambda r: r["ts"] or 0, reverse=True)
        rows = rows[:limit]
        for r in rows:
            r["time"] = time.strftime("%H:%M", time.localtime(r["ts"])) if r["ts"] else None
            r["coin"] = self.coin_name(r["mint"])
            r["coin_url"] = r["fomo_url"] = links.coin_url(r["mint"]) if r["mint"] else None
        return rows

    def build_decisions(self, agent: str) -> dict:
        return {"generated_at": time.time(), "agent": agent, "limit": DECISIONS, "items": self.decisions(agent)}

    # ---------------------------------------------------------------- agent cards
    def agent_cards(self, hb: dict, t0: float) -> list[dict]:
        now = time.time()
        paused = bool(self.ctx.state.get("paused", False))
        r = self.cfg["risk"]
        n = lambda sql, a=(): (self.db.one(sql, a) or {}).get("n") or 0  # noqa: E731
        cards = []
        for name in AGENTS:
            h = hb.get(name) or {"agent": name, "ts": None, "status": "unknown", "detail": None}
            age = round(now - h["ts"]) if h["ts"] else None
            card = {"agent": name, "name": name.capitalize(), "color": AGENT_COLORS[name], "status": h["status"],
                    "detail": h["detail"], "ts": h["ts"], "age_s": age,
                    "running": bool(h["ts"]) and age is not None and age <= 180 and h["status"] != "error",
                    "paused": paused or name in (self.ctx.state.get("paused_agents") or ()), "locked": False}
            if name == "scanner":
                card["today_count"] = n("SELECT COUNT(DISTINCT mint) n FROM scans WHERE ts>=?", (t0,))
                card["headline"] = f"{card['today_count']} pools"
                feed = self.decisions("scanner", 6)
                card["feed"] = [{"mint": d["mint"], "coin": d["coin"], "verdict": d["verdict"], "reason": d["reason"],
                                 "killed": d["verdict"] != "pass", "coin_url": d["coin_url"]} for d in feed]
                card["empty"] = "Listening for graduations" if not feed else None
            elif name == "auditor":
                card["today_count"] = n("SELECT COUNT(DISTINCT mint||ts) n FROM audits WHERE ts>=?", (t0,))
                card["rejected"] = n("SELECT COUNT(DISTINCT mint||ts) n FROM audits WHERE ts>=? AND pass=0", (t0,))
                card["headline"] = f"{card['today_count']} taken apart"
                last = self.decisions("auditor", 1)
                if last:
                    lay = self.db.q("SELECT pass FROM audits WHERE mint=? AND ts=? ORDER BY CAST(layer AS INTEGER)",
                                    (last[0]["mint"], last[0]["ts"]))
                    card["strips"] = [x["pass"] for x in lay][:8]
                    card["last"] = {"mint": last[0]["mint"], "coin": last[0]["coin"], "result": last[0]["reason"],
                                    "passed": last[0]["verdict"] == "pass", "coin_url": last[0]["coin_url"]}
                card["sub"] = f"{card['rejected']} rejected"
            elif name == "analyst":
                card["today_count"] = n("SELECT COUNT(*) n FROM verdicts WHERE ts>=?", (t0,))
                card["headline"] = "Scoring"
                v = self.db.one("SELECT mint,score,pass FROM verdicts ORDER BY id DESC LIMIT 1")
                card["ring"] = {"score": v["score"] if v else None, "green_from": self.cfg["analyst"]["min_score"],
                                "mint": v and v["mint"], "coin": v and self.coin_name(v["mint"]), "pass": bool(v and v["pass"])}
                card["mode"] = self.cfg["analyst"]["mode"]
            elif name == "executor":
                card["today_count"] = n("SELECT COUNT(*) n FROM signals WHERE ts>=? AND status='bought'", (t0,))
                card["headline"] = f"{card['today_count']} trades"
                s = self.db.one("SELECT mint,size_usd,stack_pct,status FROM signals ORDER BY id DESC LIMIT 1")
                card["last"] = s and {"mint": s["mint"], "coin": self.coin_name(s["mint"]), "status": s["status"],
                                      "ticket": f"${s['size_usd'] or 0:,.2f} · {s['stack_pct'] or 0:g}% of stack",
                                      "filled": s["status"] == "bought", "coin_url": links.coin_url(s["mint"])}
            elif name == "risk":
                opens = self.db.open_positions()
                card["today_count"] = n("SELECT COUNT(*) n FROM positions WHERE closed_at>=?", (t0,))
                card["guarding"] = len(opens)
                card["headline"] = "Guarding"
                card["locked"] = bool(opens)  # pause switch locked while a position is open
                last = self.db.one("SELECT exit_reason FROM positions WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1")
                card["last_exit"] = last and {"trail": "Trailed out", "stop": "Stopped"}.get(last["exit_reason"], last["exit_reason"])
                card["pills"] = [{"text": f"stop -{r['stop_pct']:g}%", "color": COLORS["red"]},
                                 {"text": f"trail {r['trail_pct']:g}%", "color": COLORS["green"]}]
            cards.append(card)
        return cards

    # ---------------------------------------------------------------- GET /api/state
    def build_state(self) -> dict:
        now = time.time()
        t0 = self.night_t0()
        bal = self.db.last_balance() or {}
        start = float(self.cfg["desk"]["paper_start_stack"])
        eq = self.db.equity_usd()
        cash = self.db.cash_usd()
        n0 = self.night_start_equity(t0)
        positions = [self.position_view(p) for p in self.db.open_positions()]
        hb = {h["agent"]: h for h in self.db.heartbeats()}
        cards = self.agent_cards(hb, t0)
        extra = [{**h, "age_s": round(now - h["ts"]), "extra": True} for a, h in hb.items() if a not in AGENTS]
        coins = []
        for c in self.db.q("SELECT c.mint,c.symbol,c.name,c.migrated_at,COALESCE((SELECT MIN(ts) FROM scans s WHERE s.mint=c.mint),c.migrated_at) first_seen "
                           "FROM coins c ORDER BY first_seen DESC LIMIT 40"):
            scan = self.db.last_scan(c["mint"]) or {}
            coins.append({**c, "status": self.db.coin_status(c["mint"]),
                          "source": scan.get("reason") or scan.get("verdict") or "waiting", "fomo_url": links.coin_url(c["mint"])})
        closed = self.db.q("SELECT mint,pnl_usd,exit_reason,closed_at FROM positions WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 20")
        for c in closed:
            c["fomo_url"] = links.coin_url(c["mint"])
        r = self.cfg["risk"]
        mult = eq / start if start else None
        mode = self.cfg["desk"]["mode"]
        return {
            "v": 3, "generated_at": now, "mode": mode, "paper": mode != "LIVE", "hosting": "planb",
            "poll_s": self.cfg["publish_every_s"], "paused": self.ctx.state.get("paused", False), "gate": self.gate,
            "night_start": t0,
            "balance": {
                "unit": "USD", "mode": mode,
                "cash": cash, "equity": eq, "start": start, "pnl": eq - start,
                "today_pct": (eq / n0 - 1) * 100 if n0 else None, "today_usd": eq - n0, "night_start_usd": n0,
                "multiplier": round(mult, 4) if mult is not None else None,
                "multiplier_text": f"{mult:.2f}×" if mult is not None else None,
                # shims (pre-STEP7 names app.js reads)
                "cash_usd": cash, "equity_usd": eq, "start_usd": start, "pnl_usd": eq - start,
                "pnl_pct": (eq / start - 1) * 100 if start else None,
                "realized_today_usd": self.db.realized_today_usd(), "updated_at": bal.get("ts")},
            "risk_pills": {"stop": f"stop -{r['stop_pct']:g}%", "trail": f"trail {r['trail_pct']:g}%"},
            "positions": positions, "closed": closed,
            "agents": cards,              # 5 cards, pipeline order (app.js reads .agent/.status/.detail/.ts/.age_s)
            "agents_extra": extra,        # publisher / main / telegram heartbeats
            "agents_running": sum(1 for c in cards if c["running"]),
            "coins": coins,
            "account": self.build_account(compact=True),
        }

    # ---------------------------------------------------------------- GET /api/chart?range=
    def marks(self, since: float | None) -> list[dict]:
        rows = self.db.q("SELECT id,ts,kind,mint,title,subtitle,value FROM activity WHERE kind IN ('bought','sold','stopped') "
                         + ("AND ts>=? " if since else "") + "ORDER BY ts", (since,) if since else ())
        out = []
        for a in rows:
            v = a["value"]
            if a["kind"] == "bought":
                label, color = f"Bought {_coin_label(a['title'], 'Bought ')}", COLORS["blue"]
            elif a["kind"] == "sold":
                label, color = (_usd(v) if v is not None else "Sold"), (COLORS["green"] if (v or 0) >= 0 else COLORS["red"])
            else:
                m = re.search(r"-(\d+(?:\.\d+)?)% stop", a["subtitle"] or "")
                pct = m.group(1) if m else f"{self.cfg['risk']['stop_pct']:g}"
                label, color = f"-{pct}% · {_usd(v) if v is not None else ''}".rstrip(" ·"), COLORS["red"]
            y = self.db.one("SELECT value FROM balance WHERE ts<=? ORDER BY ts DESC LIMIT 1", (a["ts"],))
            out.append({"ts": a["ts"], "kind": a["kind"], "mint": a["mint"], "label": label, "color": color, "value": v,
                        "y": y and y["value"], "fomo_url": links.coin_url(a["mint"]) if a["mint"] else None})
        return out

    def build_chart(self, rng: str = "LIVE") -> dict:
        rng = rng.upper()
        if rng not in CHART_RANGES:
            raise ValueError(f"range must be one of {', '.join(CHART_RANGES)}")
        now = time.time()
        since = self.night_t0() if rng == "LIVE" else (now - RANGE_S[rng] if RANGE_S[rng] else None)
        rows = self.db.q("SELECT ts,value FROM balance " + ("WHERE ts>=? " if since else "") + "ORDER BY ts", (since,) if since else ())
        pts = [[r["ts"], r["value"]] for r in rows]
        if since is not None:  # carry the last value before the window so the line starts at the window edge
            prev = self.db.one("SELECT ts,value FROM balance WHERE ts<? ORDER BY ts DESC LIMIT 1", (since,))
            if prev:
                pts.insert(0, [since, prev["value"]])
        if len(pts) > MAX_POINTS:  # downsample, keep last point exact
            step = len(pts) / MAX_POINTS
            pts = [pts[int(i * step)] for i in range(MAX_POINTS - 1)] + [pts[-1]]
        start = float(self.cfg["desk"]["paper_start_stack"])
        base = pts[0][1] if pts else start
        return {"generated_at": now, "unit": "USD", "range": rng, "ranges": list(CHART_RANGES), "since": since,
                "start_usd": start, "base": base, "points": pts, "marks": self.marks(since),
                "empty": "Waiting for the first trade" if not self.db.one("SELECT 1 e FROM activity WHERE kind='bought'") else None,
                "equity": pts}  # shim: app.js reads chart.equity

    # ---------------------------------------------------------------- GET /api/activity?before=
    def activity_row(self, r: dict) -> dict:
        r["msg"] = r["title"] + (f" · {r['subtitle']}" if r["subtitle"] else "")  # shim: app.js renders .msg
        r["fomo_url"] = links.coin_url(r["mint"]) if r["mint"] else None
        r["opens"] = "audit" if r["kind"] == "rejected" else "coin"
        r["right"] = None
        if r["kind"] == "rejected":
            r["right"] = "Skipped"
        elif r["kind"] == "bought" and r["value"] is not None:
            s = self.db.one("SELECT stack_pct FROM signals WHERE mint=? AND status='bought' ORDER BY id DESC LIMIT 1", (r["mint"],))
            r["right"] = f"${r['value']:,.2f}" + (f" · {s['stack_pct']:g}% of stack" if s and s["stack_pct"] is not None else "")
        elif r["value"] is not None:
            r["right"] = _usd(r["value"])
            if r["kind"] == "sold":
                p = self.db.one("SELECT size_usd FROM positions WHERE mint=? AND pnl_usd IS NOT NULL ORDER BY closed_at DESC LIMIT 1", (r["mint"],))
                if p and p["size_usd"]:
                    r["right"] += f" · {r['value'] / p['size_usd'] * 100:+.1f}%"
        return r

    def activity_pages(self) -> list[dict]:
        rows = self.db.q("SELECT id,ts,kind,mint,agent,title,subtitle,value FROM activity ORDER BY ts DESC, id DESC LIMIT ?",
                         (PAGE * (MAX_PAGES + 1) + 1,))
        total = (self.db.one("SELECT COUNT(*) n FROM activity") or {}).get("n", 0)
        pages = []
        for i in range(MAX_PAGES + 1):
            chunk = rows[i * PAGE:(i + 1) * PAGE]
            if not chunk and i:
                break
            more = len(rows) > (i + 1) * PAGE
            nxt = chunk[-1]["ts"] if chunk and more else None
            pages.append({"generated_at": time.time(), "page": i, "page_size": PAGE, "total": total,
                          "before": pages[-1]["next_before"] if pages else None,
                          "items": [self.activity_row(r) for r in chunk], "next_before": nxt,
                          "next": f"activity/page_{i + 1}.json" if nxt is not None and i < MAX_PAGES else None,
                          "empty": "Nothing yet tonight" if not chunk else None})
        return pages

    def build_activity(self) -> dict:
        return self.activity_pages()[0]

    # ---------------------------------------------------------------- GET /api/coin/{mint}
    def candles(self, mint: str, limit: int = 2000) -> list[dict]:
        """1-minute OHLC from db.prices (no other candle source). volume = positive change of DexScreener volume.m5."""
        pts = self.db.q("SELECT ts,price,volume_m5 FROM prices WHERE mint=? ORDER BY ts DESC LIMIT ?", (mint, limit))[::-1]
        out: list[dict] = []
        prev_v = None
        for p in pts:
            if p["price"] is None:
                continue
            b = int(p["ts"] // 60) * 60
            dv = 0.0
            if p["volume_m5"] is not None:
                dv = max(0.0, p["volume_m5"] - prev_v) if prev_v is not None else 0.0
                prev_v = p["volume_m5"]
            if out and out[-1]["ts"] == b:
                c = out[-1]
                c["h"], c["l"], c["c"] = max(c["h"], p["price"]), min(c["l"], p["price"]), p["price"]
                c["volume"] += dv
                c["n"] += 1
            else:
                out.append({"ts": b, "o": p["price"], "h": p["price"], "l": p["price"], "c": p["price"], "volume": dv, "n": 1})
        return out

    def audit_view(self, mint: str) -> dict | None:
        last = self.db.one("SELECT ts FROM audits WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if not last:
            return None
        rows = self.db.q("SELECT layer,name,value_text,pass,source FROM audits WHERE mint=? AND ts=? ORDER BY CAST(layer AS INTEGER)",
                         (mint, last["ts"]))
        from agents.auditor import LAYERS
        by_n = {int(r["layer"]): r for r in rows if str(r["layer"]).isdigit()}
        layers = []
        for n_ in range(1, 9):
            r = by_n.get(n_) or {"name": LAYERS.get(n_), "value_text": "not checked", "pass": None, "source": None}
            layers.append({"n": n_, "layer": str(n_), "name": r["name"] or LAYERS.get(n_), "value": r["value_text"],
                           "value_text": r["value_text"], "pass": None if r["pass"] is None else bool(r["pass"]),
                           "status": "not_checked" if r["pass"] is None else ("pass" if r["pass"] else "fail"),
                           "source": r["source"]})
        red = [x for x in layers if x["pass"] is False]
        passed = not red and all(x["pass"] for x in layers)
        return {"ts": last["ts"], "passed": passed, "layers": layers,
                "reasons": [red[0]["value_text"]] if red else [],
                "checked": sum(1 for x in layers if x["pass"] is not None),
                "passed_count": sum(1 for x in layers if x["pass"]),
                "shield": f"{sum(1 for x in layers if x['pass'])}/8"}

    def build_coin(self, mint: str) -> dict:
        now = time.time()
        coin = dict(self.db.coin(mint) or {"mint": mint})
        coin["status"] = self.db.coin_status(mint)
        name = coin.get("symbol") or coin.get("name") or mint[:6]
        age_h = (now - coin["migrated_at"]) / 3600 if coin.get("migrated_at") else None
        pts = self.db.q("SELECT ts,price,volume_m5 FROM prices WHERE mint=? ORDER BY ts DESC LIMIT 500", (mint,))[::-1]
        lp = self.db.last_price(mint)
        audit = self.audit_view(mint)
        scan = self.db.last_scan(mint) or {}
        v = self.db.one("SELECT * FROM verdicts WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        an = None
        if v:
            model, _, reason = (v["reason"] or "").partition(" · ")
            an = {"ts": v["ts"], "mode": "rules" if model == "rules" else "jev", "model": model,
                  "verdict": "pass" if v["pass"] else "reject", "reason": reason or None,
                  "decision": "pass" if v["pass"] else f"reject: {reason}", "score": v["score"],
                  "answers": {k: v[k] for k in ("alive_6h", "organic", "flush", "chase")}, "input_tokens": v["input_tokens"]}
        pos_row = self.db.one("SELECT * FROM positions WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        openp = self.db.open_position(mint)
        position = self.position_view(openp) if openp else None
        price = lp["price"] if lp else None
        since_entry = (price / openp["entry_price"] - 1) * 100 if openp and price and openp["entry_price"] else None
        stats = {"market_cap": None, "liquidity_usd": scan.get("liquidity_usd"), "volume_h24": None,
                 "holders": coin.get("holders"), "top10_pct": scan.get("top10_pct"), "h1_change_pct": scan.get("h1_change_pct"),
                 "sources": {"liquidity_usd": "scanner (DexScreener)", "holders": "auditor layer 8",
                             "market_cap": "not stored in db (n/a)", "volume_h24": "not stored in db (n/a)"}}
        url = links.coin_url(mint)
        return {"generated_at": now, "mint": mint, "coin": coin, "name": name, "chain": "solana",
                "subtitle": f"{name} · Solana" + (f" · {age_h:.0f}h old" if age_h is not None else ""),
                "paper": self.cfg["desk"]["mode"] != "LIVE",
                "price": price, "price_ts": lp and lp["ts"], "since_entry_pct": since_entry,
                "candles": self.candles(mint), "candle_interval_s": 60,
                "stats": stats, "position": position, "exit": position and {k: position[k] for k in (
                    "stop_price", "stop_pct", "trail_active", "trail_price", "trail_arms_at_price", "exit_line",
                    "exit_line_label", "timeout_at", "timeout_remaining_s")},
                "last_position": pos_row,
                "audit": audit and {**audit, "facts": {"liquidity_usd": stats["liquidity_usd"], "market_cap": stats["market_cap"]}},
                "analysis": an, "scan": scan,
                "signals": self.db.q("SELECT ts,price,size_usd,stack_pct,mode,status FROM signals WHERE mint=? ORDER BY ts", (mint,)),
                "button": {"text": "Sell on FOMO" if openp else "Buy on FOMO", "url": url},
                "coin_url": url, "fomo_url": url,
                # shims for app.js: series = [ts, price_usd, price_native(None), liquidity(None), mcap(None)]
                "series": [[r["ts"], r["price"], None, None, None] for r in pts],
                "candles_src": [[r["ts"], r["price"], r["volume_m5"]] for r in pts]}

    # ---------------------------------------------------------------- GET /api/audit/{mint}
    def build_audit(self, mint: str) -> dict:
        a = self.audit_view(mint)
        name = self.coin_name(mint)
        url = links.coin_url(mint)
        out = {"generated_at": time.time(), "mint": mint, "coin": name, "enabled": bool(self.cfg["auditor"]["enabled"]),
               "toggle": {"key": "auditor.enabled", "label": "Take apart before buying", "confirm_twice_to_disable": True},
               "button": {"text": "Open on FOMO", "url": url}, "coin_url": url, "fomo_url": url}
        if not a:
            return {**out, "audit": None, "layers": [], "empty": "No coin taken apart yet"}
        if a["passed"]:
            s = self.db.one("SELECT size_usd FROM signals WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
            res = {"title": "Cleared · 8/8 parts",
                   "subtitle": f"Sent to Executor · ${s['size_usd']:,.2f}" if s and s["size_usd"] else "Sent to Analyst"}
        else:
            res = {"title": f"Rejected · {a['reasons'][0] if a['reasons'] else 'incomplete'}", "subtitle": f"Grok Bot won't buy {name}"}
        return {**out, "audit": {k: a[k] for k in ("ts", "passed", "checked", "passed_count", "shield", "reasons")},
                "layers": a["layers"], "result": res}

    # ---------------------------------------------------------------- GET /api/account
    def build_account(self, compact: bool = False) -> dict:
        d = self.cfg["desk"]
        handle = d.get("fomo_handle")
        mode = d["mode"]
        tg = bool(read_secret("TG_TOKEN")) and bool(self.cfg.get("owner_chat_id"))
        base = {"fomo_handle": handle, "profile_url": links.profile_url(handle) if handle else None,
                "signup_url": links.SIGNUP_URL, "analyst_mode": self.cfg["analyst"]["mode"],
                "notifier": d["notifier"], "webpush": webpush.available(), "webpush_note": webpush.REASON}
        if compact:
            return base
        return {"generated_at": time.time(), **base, "mode": mode, "paper": mode != "LIVE",
                "SIGNUP_URL": links.SIGNUP_URL,
                "stack": {"unit": "USD", "paper_cash_usd": self.db.cash_usd(), "paper_start_stack": float(d["paper_start_stack"]),
                          "equity_usd": self.db.equity_usd()},
                "ticket_check": self.ticket_check(),
                "invite": {"label": "Invite a friend", "url": links.SIGNUP_URL, "share": "navigator.share, fallback copy + 'Link copied'"},
                "telegram": {"connected": tg, "test_push": "not available in Plan B (no POST); Telegram has no /test command yet"},
                "push": {"webpush_available": webpush.available(), "plan": "A only",
                         "note": "Plan B (static GitHub Pages) has no endpoint to POST a subscription to: no web push, Telegram only."},
                "mode_switch": {"choices": ["PAPER", "LIVE"], "live_confirm": "type LIVE", "live_gate": self.live_gate(),
                                "planb": "read-only here: switch with Telegram \"live\" then type LIVE (needs 1 full PAPER night in the db)"},
                "hosting": {"plan": "B", "poll_s": self.cfg["publish_every_s"], "sse": False, "post": False}}

    def live_gate(self) -> dict:
        """STEP 11: the two gates for PAPER -> LIVE (read-only view; the switch itself is commands.confirm_live)."""
        from commands import paper_night_status
        st = paper_night_status(self.db, self.cfg)
        return {"paper_night_done": st["done"], "why": st["why"], "night_from": st["night_from"],
                "night_to": st["night_to"], "typed_live_required": True, "live_confirmed": bool(self.db.live_ok())}

    # ---------------------------------------------------------------- GET /api/settings
    def ticket_check(self) -> dict:
        """STEP 10 ticket check for the Account tab: PAPER stack = equity, LIVE = settings.live_stack."""
        from agents.executor import ticket_check, ticket_text
        e = self.cfg["executor"]
        paper = str(self.cfg["desk"]["mode"]).upper() != "LIVE"
        stack = self.db.equity_usd() if paper else float(self.cfg.get("live_stack") or 0)
        min_stack, msg = ticket_check(stack, e)
        return {"stack_usd": round(stack, 2), "min_stack_usd": round(min_stack, 2), "below_min": e["below_min"],
                "min_ticket_usd": float(e["min_ticket_usd"]), "stack_pct": float(e["stack_pct"]),
                "formula": "min_stack = min_ticket_usd / (stack_pct / 100)", "status": (
                    "ok" if not msg else ("skip" if "skipped" in msg else "bump")),
                "msg": msg, "text": ticket_text(msg, "PAPER · " if paper else "") if msg else None,
                "night_start": self.db.get_setting("_ticket_check")}

    def build_settings(self) -> dict:
        """STEP 10: every setting grouped per agent + the ranges / choices / bools the Account tab renders from."""
        s = {k: json.loads(json.dumps(self.cfg[k], default=str)) for k in GROUPS}
        log_rows = self.db.q("SELECT ts,key,old,new,who FROM change_log ORDER BY ts DESC LIMIT 50")
        keys = leaf_keys()
        return {"generated_at": time.time(), "settings": s, "groups": list(GROUPS), "keys": keys,
                "ranges": {k: list(v) for k, v in RANGES.items()}, "choices": {k: list(v) for k, v in CHOICES.items()},
                "bools": list(BOOLS), "strings": list(STRINGS), "range_text": {k: range_text(k) for k in keys},
                "ticket_check": self.ticket_check(),
                "edit": "Plan B: read-only here. Telegram: set <name> to <value> (e.g. set stop to -8%), stack N.",
                "change_log": log_rows, "planb_readonly": True}

    def build_settings_meta(self) -> dict:
        return {"generated_at": time.time(), "planb_readonly": True,
                "post": "POST /api/settings is Plan A only (validated by config.validate, written to change_log). "
                        "Plan B: settings change only via db.set_setting (Telegram / commands.py: set, stack, pause, resume, paper, live). The next publish reflects them.",
                "keys": sorted(leaf_keys())}

    # ---------------------------------------------------------------- GET /api/stream (buffered)
    def drain_stream(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # called from the publish worker thread: asyncio queues are drained on the loop instead
        for topic, q in self._subs.items():
            while True:
                try:
                    m = q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                ev = _small(m)
                ev["type"] = ev.get("type") or STREAM_TOPICS[topic] or topic
                ev.setdefault("ts", time.time())
                self.stream.append(ev)

    def build_stream(self) -> dict:
        self.drain_stream()
        return {"generated_at": time.time(), "sse": False,
                "note": "Plan B has no SSE; this is a poll buffer of recent bus events. Exit/balance events are in activity.json/state.json.",
                "events": list(self.stream)[-STREAM_KEEP:]}

    # ---------------------------------------------------------------- publish
    def published_mints(self) -> set[str]:
        mints = {p["mint"] for p in self.db.open_positions()}
        mints |= {r["mint"] for r in self.db.q("SELECT mint FROM scans GROUP BY mint ORDER BY MIN(ts) DESC LIMIT 30")}
        mints |= {r["mint"] for r in self.db.q("SELECT mint FROM coins ORDER BY migrated_at DESC LIMIT 10")}
        mints |= {r["mint"] for r in self.db.q("SELECT mint FROM audits GROUP BY mint ORDER BY MAX(ts) DESC LIMIT 30")}
        mints |= {r["mint"] for r in self.db.q("SELECT mint FROM activity WHERE mint IS NOT NULL GROUP BY mint ORDER BY MAX(ts) DESC LIMIT 30")}
        return {m for m in mints if m and SAFE_PATH.match(m)}

    def index(self) -> dict:
        return {"v": 3, "plan": "B", "poll_s": self.cfg["publish_every_s"], "map": {
            "GET /api/state": "state.json", "GET /api/chart?range=R": "chart/{R}.json (chart.json = LIVE)",
            "GET /api/activity": "activity.json", "GET /api/activity?before=": "activity/page_{n}.json via next",
            "GET /api/coin/{mint}": "coin/{mint}.json", "GET /api/audit/{mint}": "audit/{mint}.json",
            "GET /api/agents/{name}/decisions": "agents/{name}/decisions.json", "GET /api/account": "account.json",
            "GET /api/settings": "settings.json", "GET /api/stream": "stream/last.json (poll)",
            "POST *": "Plan A only — Telegram /pause, /resume"}, "agents": list(AGENTS), "ranges": list(CHART_RANGES)}

    def publish_once(self):
        self.mirror_web()
        self.write_json("state.json", self.build_state())
        for rng in CHART_RANGES:
            ch = self.build_chart(rng)
            self.write_json(f"chart/{rng}.json", ch)
            if rng == "LIVE":
                self.write_json("chart.json", ch)
        pages = self.activity_pages()
        self.write_json("activity.json", pages[0])
        for p in pages[1:]:
            self.write_json(f"activity/page_{p['page']}.json", p)
        mints = self.published_mints()
        for d in ("coin", "audit"):  # dirs exist even before the first coin (empty on a quiet/offline night)
            (self.data_dir / d).mkdir(parents=True, exist_ok=True)
        for m in mints:
            self.write_json(f"coin/{m}.json", self.build_coin(m))
            self.write_json(f"audit/{m}.json", self.build_audit(m))
        for a in AGENTS:
            self.write_json(f"agents/{a}/decisions.json", self.build_decisions(a))
        self.write_json("account.json", self.build_account())
        self.write_json("settings.json", self.build_settings())
        self.write_json("settings_meta.json", self.build_settings_meta())
        self.write_json("stream/last.json", self.build_stream())
        self.write_json("index.json", self.index())
        self.writes += 1
        self.db.beat("publisher", "ok", f"{self.writes} publishes; {len(mints)} coins")

    def uncovered_mints(self) -> list[str]:
        """Coins shown in the app tonight that neither Scanner (waiting room) nor Risk (open positions) price."""
        t0 = time.time() - 12 * 3600
        tonight = {r["mint"] for r in self.db.q(
            "SELECT mint FROM signals WHERE ts>=? UNION SELECT mint FROM audits WHERE ts>=? UNION SELECT mint FROM positions WHERE opened_at>=?",
            (t0, t0, t0))}
        opened = {p["mint"] for p in self.db.open_positions()}
        waiting = {r["mint"] for r in self.db.q(  # Scanner's waiting room = coins without a scan decision yet
            "SELECT mint FROM coins WHERE mint NOT IN (SELECT mint FROM scans)")}
        return sorted(tonight - opened - waiting)[:30]

    async def price_poll(self):
        from agents.common import RateLimited, SourceError, store_price
        while True:
            try:
                mints = self.uncovered_mints()
                for m, pair in ((await self.ctx.src.dex_batch(mints)) if mints and not self.ctx.offline else {}).items():
                    store_price(self.db, pair, m)
            except (RateLimited, SourceError) as e:
                log.info("api price poll: %s", e)
            except Exception:
                log.exception("api price poll failed")
            await asyncio.sleep(self.cfg.get("api_price_every_s", 60))

    def on_restart(self):
        t = getattr(self, "_price_task", None)
        if t is not None and not t.done():
            t.cancel()

    async def guarded(self):
        self.on_restart()  # never leave a second price_poll running after an in-process restart
        self._price_task = asyncio.ensure_future(self.price_poll())
        while True:
            try:
                t0 = time.monotonic()
                self.drain_stream()  # touches asyncio queues: stay on the loop
                await asyncio.to_thread(self.publish_once)  # file + sqlite work off the event loop
                took = time.monotonic() - t0
                if took > 10:
                    log.warning("publish took %.1fs", took)
            except SecretLeak as e:
                self.db.beat("publisher", "error", "secret guard tripped")
                log.error("%s", e)
            except Exception as e:
                self.db.beat("publisher", "error", type(e).__name__)
                log.exception("publish failed")
            await asyncio.sleep(self.cfg["publish_every_s"])
