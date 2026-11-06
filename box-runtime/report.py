"""report.py — STEP 12 night / morning report (desk-side).

Builds a full night report for the last finished night (report_time → report_time):
  red trades first, then green; coins seen; Scanner kills by reason; Auditor refusals by layer
  with times; Analyst rejects with failed rule; Jev tokens + cost; fees paid.
Every coin carries links.coin_url (r=savipww). Footer: "New to FOMO? " + SIGNUP_URL.
Clear db.skips after the report. Save the Page URL into setting `_last_report_url` once a Page exists.

CLI (from /workspace/desk):
  .venv/bin/python report.py build              # JSON summary of last finished night
  .venv/bin/python report.py clear-skips        # empty skips table
  .venv/bin/python report.py set-url <url>      # store Page URL for Telegram `report`
  .venv/bin/python report.py daily              # legacy text stub (kept for callers)

The Grok Bot "night-report" routine calls build, writes the morning Page, then set-url + clear-skips.
main.daily() at report_time rollover: build → notify text → clear_skips (Page URL left for the routine).
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict

from db import DB, night_start
from links import SIGNUP_URL, coin_url

REPORT_KEY = "_last_report_url"
# TypeSafe System One list price used for the morning Page (input tokens only). Documented, not billed here.
JEV_USD_PER_MILLION_INPUT = 2.0


def last_finished_night(now: float | None = None, report_time: str = "08:00") -> tuple[float, float]:
    """[t0, t1) of the last closed night = previous report_time → current night_start."""
    t1 = night_start(now, report_time)
    return t1 - 86400.0, t1


def _coin_meta(db: DB, mint: str) -> dict:
    c = db.coin(mint) or {}
    return {
        "mint": mint,
        "name": c.get("name") or c.get("symbol") or mint[:8],
        "symbol": c.get("symbol"),
        "url": coin_url(mint, "solana"),
    }


def _pct(pnl: float | None, size: float | None) -> float | None:
    if pnl is None or not size:
        return None
    return round(100.0 * float(pnl) / float(size), 2)


def _trade_row(db: DB, p: dict) -> dict:
    meta = _coin_meta(db, p["mint"])
    pnl = float(p["pnl_usd"] or 0)
    size = float(p["size_usd"] or 0)
    return {
        **meta,
        "entry_price": p["entry_price"],
        "exit_price": p["exit_price"],
        "size_usd": size,
        "pnl_usd": pnl,
        "pnl_pct": _pct(pnl, size),
        "fees_usd": float(p["fees_usd"] or 0),
        "exit_reason": p["exit_reason"] or "?",
        "opened_at": p["opened_at"],
        "closed_at": p["closed_at"],
        "mode": p.get("mode") or "PAPER",
        "color": "red" if pnl < 0 else "green",
    }


def build_night_report(db: DB, cfg: dict | None = None, now: float | None = None) -> dict:
    """Full night report dict for the last finished night. Does NOT clear skips or write the Page URL."""
    cfg = cfg or {}
    desk = cfg.get("desk") or {}
    report_time = desk.get("report_time") or db.get_setting("desk.report_time") or "08:00"
    if isinstance(report_time, dict):  # shouldn't happen
        report_time = "08:00"
    now = time.time() if now is None else now
    t0, t1 = last_finished_night(now, str(report_time))

    closed = db.q(
        "SELECT * FROM positions WHERE closed_at IS NOT NULL AND closed_at>=? AND closed_at<? ORDER BY closed_at",
        (t0, t1),
    )
    trades = [_trade_row(db, p) for p in closed]
    red = [t for t in trades if t["color"] == "red"]
    green = [t for t in trades if t["color"] == "green"]
    ordered = red + green
    fees_paid = round(sum(t["fees_usd"] for t in trades), 6)
    pnl_total = round(sum(t["pnl_usd"] for t in trades), 6)

    # Coins seen = distinct mints scanned this night
    scan_rows = db.q("SELECT mint, verdict, reason, ts FROM scans WHERE ts>=? AND ts<?", (t0, t1))
    coins_seen_mints = list(dict.fromkeys(r["mint"] for r in scan_rows))
    coins_seen = [_coin_meta(db, m) for m in coins_seen_mints]

    kills = Counter()
    for r in scan_rows:
        if r["verdict"] == "kill":
            kills[r["reason"] or "unknown"] += 1
    scanner_kills = [{"reason": k, "count": n} for k, n in kills.most_common()]

    # Auditor refusals by layer (pass=0), with times
    refusals_raw = db.q(
        "SELECT mint, layer, name, value_text, ts FROM audits WHERE pass=0 AND ts>=? AND ts<? ORDER BY ts",
        (t0, t1),
    )
    by_layer: dict[str, list] = defaultdict(list)
    for r in refusals_raw:
        meta = _coin_meta(db, r["mint"])
        by_layer[str(r["layer"])].append({
            **meta,
            "layer": str(r["layer"]),
            "layer_name": r["name"],
            "value": r["value_text"],
            "ts": r["ts"],
        })
    auditor_refusals = [
        {"layer": layer, "layer_name": (rows[0]["layer_name"] if rows else None), "count": len(rows), "rows": rows}
        for layer, rows in sorted(by_layer.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 99)
    ]

    # Analyst rejects with failed rule (verdicts.pass=0; reason holds "model · rule")
    rej = db.q(
        "SELECT mint, reason, score, ts, input_tokens FROM verdicts WHERE pass=0 AND ts>=? AND ts<? ORDER BY ts",
        (t0, t1),
    )
    analyst_rejects = []
    for r in rej:
        reason = r["reason"] or ""
        # "jev · chase 0.31" / "rules · alive_6h 0.69" → failed rule after the ·
        failed = reason.split("·", 1)[-1].strip() if "·" in reason else reason
        analyst_rejects.append({**_coin_meta(db, r["mint"]), "failed_rule": failed, "reason": reason,
                                "score": r["score"], "ts": r["ts"]})

    tok_row = db.one(
        "SELECT COALESCE(SUM(input_tokens),0) AS t FROM verdicts WHERE ts>=? AND ts<?", (t0, t1),
    ) or {}
    jev_tokens = int(tok_row.get("t") or 0)
    jev_cost_usd = round(jev_tokens * JEV_USD_PER_MILLION_INPUT / 1_000_000.0, 6)

    layer8 = db.get_setting("_layer8_kills", 0) or 0
    footer = f"New to FOMO? {SIGNUP_URL}"

    telegram_text = _telegram_text(
        red=len(red), green=len(green), pnl=pnl_total, fees=fees_paid,
        coins=len(coins_seen), kills=sum(k["count"] for k in scanner_kills),
        refusals=sum(a["count"] for a in auditor_refusals), rejects=len(analyst_rejects),
        tokens=jev_tokens, cost=jev_cost_usd, layer8=layer8, t0=t0, t1=t1,
    )

    return {
        "night_from": t0,
        "night_to": t1,
        "report_time": str(report_time),
        "red_trades": red,
        "green_trades": green,
        "trades": ordered,  # red before green
        "coins_seen": coins_seen,
        "scanner_kills": scanner_kills,
        "auditor_refusals": auditor_refusals,
        "analyst_rejects": analyst_rejects,
        "jev_tokens": jev_tokens,
        "jev_cost_usd": jev_cost_usd,
        "jev_usd_per_million": JEV_USD_PER_MILLION_INPUT,
        "fees_paid": fees_paid,
        "pnl_usd": pnl_total,
        "layer8_kills": layer8,
        "footer": footer,
        "signup_url": SIGNUP_URL,
        "telegram_text": telegram_text,
        "last_report_url": db.get_setting(REPORT_KEY),
    }


def _telegram_text(*, red, green, pnl, fees, coins, kills, refusals, rejects, tokens, cost, layer8, t0, t1) -> str:
    import datetime as _dt
    fmt = lambda ts: _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    head = f"📊 Night report · {fmt(t0)} → {fmt(t1)}"
    if layer8:
        head = f"⚠️ layer 8 down killed {layer8} coins\n" + head
    return (
        f"{head}\n"
        f"Trades {red + green} (red {red} · green {green}) · pnl {pnl:+.2f} USD · fees {fees:.2f} USD\n"
        f"Coins seen {coins} · Scanner kills {kills} · Auditor refusals {refusals} · Analyst rejects {rejects}\n"
        f"Jev tokens {tokens} · ~${cost:.4f}\n"
        f"{f'New to FOMO? {SIGNUP_URL}'}"
    )


def clear_skips(db: DB) -> int:
    """Empty db.skips after the morning report. Returns rows deleted."""
    n = db.one("SELECT COUNT(*) AS n FROM skips") or {"n": 0}
    db.x("DELETE FROM skips")
    return int(n["n"] or 0)


def set_last_report_url(db: DB, url: str) -> str:
    """Routine hook: store the morning Page URL for Telegram `report`."""
    db.set_setting(REPORT_KEY, url)
    return url


def daily_report(db, cfg: dict | None = None) -> str:
    """Backward-compatible text summary (main.daily / older callers)."""
    return build_night_report(db, cfg)["telegram_text"]


def cli(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(prog="report.py", description="STEP 12 night report")
    ap.add_argument("cmd", choices=("build", "clear-skips", "set-url", "daily"))
    ap.add_argument("url", nargs="?", help="Page URL for set-url")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    from config import DB_PATH, load_config
    db = DB(args.db or DB_PATH)
    if args.cmd == "build":
        cfg = load_config(db)
        print(json.dumps(build_night_report(db, cfg), indent=2, default=str))
    elif args.cmd == "clear-skips":
        n = clear_skips(db)
        print(json.dumps({"cleared": n}))
    elif args.cmd == "set-url":
        if not args.url:
            raise SystemExit("usage: report.py set-url <url>")
        print(json.dumps({"url": set_last_report_url(db, args.url)}))
    elif args.cmd == "daily":
        print(daily_report(db, load_config(db)))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
