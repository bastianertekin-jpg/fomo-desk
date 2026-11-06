"""STEP 13: Risk exits (trail / stop / timeout), never loosening an open stop, PAPER close with fees.py."""
import asyncio
import time

import fees
import links
from agents.executor import Executor
from agents.risk import Risk, evaluate, pnl_of, stop_line
from config import DEFAULTS
from fakes import MINT, make_ctx, pair

R = DEFAULTS["risk"]
T0 = 1_800_000_000.0


def pos(**kw):
    p = {"id": 1, "mint": "M", "entry_price": 1.0, "size_usd": 47.5, "peak_price": 1.0,
         "stop_price": 1.0 * (1 - R["stop_pct"] / 100), "trail_active": 0, "opened_at": T0, "mode": "PAPER"}
    p.update(kw)
    return p


def test_up_25_then_13_off_top_trails_out():
    p = pos()
    upd, why = evaluate(p, 1.25, T0 + 60, R)
    assert why is None and upd["trail_active"] == 1 and upd["peak_price"] == 1.25
    p.update(upd)
    upd, why = evaluate(p, 1.25 * 0.87, T0 + 120, R)
    assert why == "trail"


def test_minus_6_1_is_stop():
    assert evaluate(pos(), 0.939, T0 + 60, R)[1] == "stop"
    assert evaluate(pos(), 0.941, T0 + 60, R)[1] is None


def test_3h_at_plus_4_is_timeout():
    assert evaluate(pos(), 1.04, T0 + 3 * 3600, R)[1] == "timeout"
    assert evaluate(pos(), 1.04, T0 + 3 * 3600 - 1, R)[1] is None
    assert evaluate(pos(), 1.06, T0 + 3 * 3600, R)[1] is None


def test_loosening_stop_never_moves_open_stop_down_tightening_does():
    p = pos()  # opened with stop 6% -> 0.94
    loose = {**R, "stop_pct": 10}
    assert stop_line(p, loose) == p["stop_price"]
    assert evaluate(p, 0.93, T0 + 60, loose)[1] == "stop"
    tight = {**R, "stop_pct": 4}
    assert abs(stop_line(p, tight) - 0.96) < 1e-12


def test_pnl_uses_fees_py_both_sides():
    r = pnl_of(pos(), 1.10, 0.10)
    gross = 47.5 * 1.10
    fee = fees.side_fee_usd(47.5, 0.10) + fees.side_fee_usd(gross, 0.10)
    assert abs(r["fees"] - round(fee, 4)) < 1e-9 and abs(r["pnl"] - round(gross - 47.5 - fee, 4)) < 1e-9


def test_new_position_minus_8_stopped_with_fees(tmp_path):
    """STEP 13: open a PAPER position, price drops 8%, Risk stops it within one poll."""
    ctx = make_ctx(tmp_path)
    ctx.db.upsert_coin(MINT, name="Project Sphere")
    ex, risk = Executor(ctx), Risk(ctx)
    fills = ctx.bus.subscribe("notify")
    m = pair(liquidity_usd=50000)
    o = asyncio.run(ex.handle({"mint": MINT, "score": 80, "ctx": {"name": "Project Sphere", "priceUsd": m["price_usd"]}},
                              market=m))
    assert o["status"] == "bought"
    p = ctx.db.open_positions()[0]
    out = risk.step(dict(p), {**m, "price_usd": m["price_usd"] * 0.92}, time.time())
    assert out["reason"] == "stop" and out["status"] == "closed" and out["pnlUsd"] < 0
    closed = ctx.db.one("SELECT * FROM positions WHERE id=?", (p["id"],))
    assert closed["closed_at"] and closed["exit_reason"] == "stop" and closed["fees_usd"] > 0
    act = ctx.db.one("SELECT * FROM activity WHERE kind='stopped'")
    assert act["title"] == "Stopped Project Sphere" and act["subtitle"] == "Risk · -6% stop"
    assert abs(ctx.db.cash_usd() - (200 + closed["pnl_usd"])) < 1e-6
    assert any(n["text"].startswith("PAPER · ") for n in [fills.get_nowait() for _ in range(fills.qsize())])


def test_pulled_liquidity_exits(tmp_path):
    ctx = make_ctx(tmp_path)
    ex, risk = Executor(ctx), Risk(ctx)
    m = pair()
    asyncio.run(ex.handle({"mint": MINT, "score": 80, "ctx": {"priceUsd": m["price_usd"]}}, market=m))
    p = ctx.db.open_positions()[0]
    assert risk.step(dict(p), {**m, "liquidity_usd": 0}, time.time())["reason"] == "pulled"


MINT_B = "So11111111111111111111111111111111111111112"


def test_set_stop_8_new_position_drops_8_1_old_keeps_6(tmp_path, monkeypatch):
    """STEP 13.3: set stop to -8%; new PAPER pos drop 8.1% → -8% in push/Activity/mark; Risk pill stop -8%;
    a position opened at -6% still shows -6%. Loosening never moves an open stop down."""
    import hashlib
    import json

    import config
    from api import PlanBWriter
    from commands import Commands
    from agents.risk import stop_pct_of

    sec = tmp_path / "secrets"
    sec.mkdir()
    for k, v in {"APP_TOKEN": "SANITIZED_TEST_VALUE", "DATA_PATH": "riskTestDataPath_abcdefghij",
                 "TG_TOKEN": "SANITIZED_TEST_VALUE"}.items():
        (sec / k).write_text(v)
    monkeypatch.setattr(config, "SECRETS_DIR", sec)

    ctx = make_ctx(tmp_path)
    ctx.db.upsert_coin(MINT, name="Project Sphere", symbol="SPHR")
    ctx.db.upsert_coin(MINT_B, name="Second Coin", symbol="SEC")
    cmds = Commands(ctx)
    ex, risk = Executor(ctx), Risk(ctx)
    notify = ctx.bus.subscribe("notify")
    m = pair(liquidity_usd=50000)

    # Position A opened under default stop -6%
    o_a = asyncio.run(ex.handle(
        {"mint": MINT, "score": 80, "ctx": {"name": "Project Sphere", "priceUsd": m["price_usd"]}}, market=m))
    assert o_a["status"] == "bought"
    pos_a = [p for p in ctx.db.open_positions() if p["mint"] == MINT][0]
    assert abs(stop_pct_of(pos_a) - 6) < 1e-9

    # set stop to -8%
    r = asyncio.run(cmds.handle("set stop to -8%"))[0]
    assert "stop -6% -> -8%" in r.text
    assert ctx.cfg["risk"]["stop_pct"] == 8

    # Loosening never moves open stop down
    from agents.risk import stop_line
    assert stop_line(dict(pos_a), ctx.cfg["risk"]) == pos_a["stop_price"]
    assert abs(stop_pct_of(dict(ctx.db.one("SELECT * FROM positions WHERE id=?", (pos_a["id"],)))) - 6) < 1e-9

    # New PAPER position B inherits -8%
    o_b = asyncio.run(ex.handle(
        {"mint": MINT_B, "score": 81, "ctx": {"name": "Second Coin", "priceUsd": m["price_usd"]}}, market=m))
    assert o_b["status"] == "bought"
    pos_b = [p for p in ctx.db.open_positions() if p["mint"] == MINT_B][0]
    assert abs(stop_pct_of(pos_b) - 8) < 1e-9

    # Drop B 8.1% → stop
    entry_b = pos_b["entry_price"]
    out = risk.step(dict(pos_b), {**m, "price_usd": entry_b * (1 - 0.081)}, time.time())
    assert out["reason"] == "stop" and out["status"] == "closed" and abs(out["stopPct"] - 8) < 1e-9

    act = ctx.db.one("SELECT * FROM activity WHERE mint=? AND kind='stopped'", (MINT_B,))
    assert act["subtitle"] == "Risk · -8% stop"
    assert "-8%" in act["subtitle"]

    # Drain notify queue; last exit push must read -8%
    pushes = []
    while notify.qsize():
        pushes.append(notify.get_nowait())
    stop_push = [p for p in pushes if p.get("kind") == "exit_stop" and p.get("mint") == MINT_B][-1]
    assert stop_push["text"].startswith("PAPER · ") and "-8% stop" in stop_push["text"]
    assert stop_push["buttons"][0]["url"] == links.coin_url(MINT_B)

    # Position A still carries -6%
    still_a = ctx.db.one("SELECT * FROM positions WHERE id=?", (pos_a["id"],))
    assert still_a["closed_at"] is None and abs(stop_pct_of(still_a) - 6) < 1e-9

    # Risk pill + chart mark
    w = PlanBWriter(ctx, publish_dir=tmp_path / "publish", web_dir=config.WEB_DIR)
    w.publish_once()
    st = json.loads((w.data_dir / "state.json").read_text())
    assert st["risk_pills"]["stop"] == "stop -8%"
    risk_card = next(a for a in st["agents"] if a["agent"] == "risk")
    assert any(p["text"] == "stop -8%" for p in risk_card["pills"])
    # open position A view still -6%
    pos_views = [p for p in st["positions"] if p["mint"] == MINT]
    assert pos_views and abs(pos_views[0]["stop_pct"] - 6) < 1e-9

    marks = json.loads((w.data_dir / "chart/ALL.json").read_text())["marks"]
    stopped = [m for m in marks if m["kind"] == "stopped" and m["mint"] == MINT_B]
    assert stopped and stopped[0]["label"].startswith("-8%")
