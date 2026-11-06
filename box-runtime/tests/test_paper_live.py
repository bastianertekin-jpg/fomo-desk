"""STEP 11 · PAPER, THEN LIVE. Fees table, PAPER fill + exit accounting, LIVE no auto-fill, the LIVE switch gates.
Offline fakes only: no FOMO orders (none exist), no Telegram poll, no Jev, no network."""
import asyncio
import re
import time
from pathlib import Path

import pytest

import config
import fees
from agents.executor import Executor, push_text
from agents.risk import Risk
from commands import Commands, paper_night_done, paper_night_status
from config import load_config
from db import night_start
from fakes import MINT, make_ctx, pair

ROOT = Path(__file__).resolve().parents[1]
MINT2 = "So11111111111111111111111111111111111111112"


def run(cmds, text):
    return asyncio.run(cmds.handle(text))


def drain(q):
    return [q.get_nowait() for _ in range(q.qsize())]


def signal(m, mint=MINT, name="Project Sphere"):
    return {"mint": mint, "score": 80, "ctx": {"name": name, "priceUsd": m["price_usd"]}}


# ---------------------------------------------------------------- fees.py
@pytest.mark.parametrize("ticket,raw,bracket", [
    (5.00, 0.10, "2%"), (20.00, 0.40, "2%"), (47.49, 0.9498, "2%"),
    (47.50, 0.95, "flat 0.95"), (100.00, 0.95, "flat 0.95"), (190.00, 0.95, "flat 0.95"),
    (190.01, 0.95005, "0.50%"), (500.00, 2.50, "0.50%"), (1000.00, 5.00, "0.50%"),
])
def test_fee_brackets_each_side(ticket, raw, bracket):
    assert fees.bracket(ticket) == bracket
    assert fees.raw_side_fee_usd(ticket) == pytest.approx(raw, abs=1e-12)
    assert fees.side_fee_usd(ticket, 0.0) == pytest.approx(raw, abs=1e-12)
    assert fees.side_fee_usd(ticket) == pytest.approx(raw * 0.90, abs=1e-12)  # default ref discount 10%


def test_fee_edges_are_continuous_discount_round_trip_gas():
    assert fees.raw_side_fee_usd(47.50) == pytest.approx(47.50 * 0.02)  # 2% and flat meet at 0.95
    assert fees.raw_side_fee_usd(190.0) == pytest.approx(190.0 * 0.005)  # flat and 0.50% meet at 0.95
    assert fees.side_fee_usd(47.50, 0.10) == pytest.approx(0.855)
    assert fees.side_fee_usd(47.50, 0.25) == pytest.approx(0.7125)
    assert fees.round_trip_fees_usd(47.50) == pytest.approx(1.71)
    assert fees.round_trip_fees_usd(47.50, 0.10, exit_usd=300) == pytest.approx(0.855 + 1.35)
    assert fees.gas_usd() == 0 == fees.GAS_USD


# ---------------------------------------------------------------- PAPER
def test_paper_seeds_at_paper_start_stack(tmp_path):
    ctx = make_ctx(tmp_path, desk__paper_start_stack=500)
    Executor(ctx)
    assert ctx.db.cash_usd() == 500 and ctx.db.equity_usd() == 500 and ctx.db.last_balance()["value"] == 500
    assert ctx.db.paper_since() is not None and not ctx.db.live_ok()


def test_paper_fill_at_signal_price_then_trail_exit_at_polled_price(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.db.upsert_coin(MINT, name="Project Sphere")
    ex, risk = Executor(ctx), Risk(ctx)
    n = ctx.bus.subscribe("notify")
    m = pair(liquidity_usd=50000)
    p0 = m["price_usd"]
    o = asyncio.run(ex.handle(signal(m), market=m))
    assert o["status"] == "bought" and o["mode"] == "PAPER" and o["signalPrice"] == p0
    pos = ctx.db.open_positions()[0]
    assert pos["entry_price"] == p0 and pos["size_usd"] == 47.5 and pos["mode"] == "PAPER"
    assert pos["fees_usd"] == pytest.approx(0.855)
    cash_open = 200 - 47.5 - 0.855
    assert ctx.db.cash_usd() == pytest.approx(cash_open)
    assert ctx.db.equity_usd() == pytest.approx(cash_open + 47.5)  # cash + open value (marked at entry)
    push = drain(n)[0]
    assert push["text"].startswith("PAPER · Buy Project Sphere · $47.50")
    # +25% arms the trail, then 13% off the top fires it: exit at THAT polled price
    assert risk.step(dict(pos), {**m, "price_usd": p0 * 1.25}, time.time()) is None
    pos = ctx.db.open_positions()[0]
    ex_price = p0 * 1.25 * 0.87
    out = risk.step(dict(pos), {**m, "price_usd": ex_price}, time.time())
    assert out["reason"] == "trail" and out["status"] == "closed" and out["exitPrice"] == ex_price
    gross = 47.5 * 1.25 * 0.87
    pnl = gross - 47.5 - 0.855 - fees.side_fee_usd(gross)
    closed = ctx.db.one("SELECT * FROM positions WHERE id=?", (pos["id"],))
    assert closed["exit_price"] == ex_price and closed["pnl_usd"] == pytest.approx(pnl, abs=1e-4)
    assert closed["fees_usd"] == pytest.approx(1.71, abs=1e-4)
    assert ctx.db.cash_usd() == pytest.approx(cash_open + gross - 0.855)
    assert ctx.db.cash_usd() == pytest.approx(200 + pnl, abs=1e-4)
    assert ctx.db.equity_usd() == ctx.db.cash_usd() == pytest.approx(ctx.db.last_balance()["value"])
    kinds = [a["kind"] for a in ctx.db.q("SELECT kind FROM activity ORDER BY id")]
    assert kinds == ["bought", "sold"]
    assert drain(n)[-1]["text"].startswith("PAPER · Sold Project Sphere · +$2.45")


def test_paper_stop_exit_moves_cash_after_fees(tmp_path):
    ctx = make_ctx(tmp_path)
    ex, risk = Executor(ctx), Risk(ctx)
    m = pair(liquidity_usd=50000)
    asyncio.run(ex.handle(signal(m), market=m))
    pos = ctx.db.open_positions()[0]
    out = risk.step(dict(pos), {**m, "price_usd": m["price_usd"] * 0.939}, time.time())
    gross = 47.5 * 0.939  # 44.6025: the sell side drops into the 2% bracket
    assert fees.side_fee_usd(gross) == pytest.approx(gross * 0.02 * 0.9)
    pnl = gross - 47.5 - 0.855 - fees.side_fee_usd(gross)
    assert out["reason"] == "stop" and out["pnlUsd"] == pytest.approx(pnl, abs=1e-4)
    assert ctx.db.cash_usd() == pytest.approx(200 + pnl, abs=1e-4)  # 200 - 47.5 - .855 + 44.6025 - .802845


def test_ticket_check_runs_before_first_coin(tmp_path):
    ctx = make_ctx(tmp_path)
    ex = Executor(ctx)
    n = ctx.bus.subscribe("notify")
    seen = []
    orig = ex.handle

    async def handle(sig, market=None):
        seen.append(ctx.db.get_setting("_ticket_check"))
        return await orig(sig, market=pair(liquidity_usd=50000))
    ex.handle = handle

    async def go():
        t = asyncio.create_task(ex.run())
        await asyncio.sleep(0)
        ctx.bus.publish("signal", signal(pair()))
        for _ in range(20):
            await asyncio.sleep(0)
        t.cancel()
    asyncio.run(go())
    msgs = drain(n)
    assert msgs[0]["kind"] == "ticket_check" and msgs[0]["text"].startswith("PAPER · tickets bumped")
    assert seen and seen[0]["msg"].startswith("tickets bumped")
    assert msgs[1]["kind"] == "buy"


# ---------------------------------------------------------------- LIVE pipeline
def test_live_push_never_auto_fills_missed_window_is_no_position(tmp_path):
    ctx = make_ctx(tmp_path, desk__mode="LIVE", live_stack=1000)
    ex = Executor(ctx)
    n = ctx.bus.subscribe("notify")
    m = pair(liquidity_usd=50000)
    o = asyncio.run(ex.handle(signal(m), market=m))
    assert o["status"] == "sent" and o["mode"] == "LIVE" and ctx.db.open_positions() == []
    assert ctx.db.cash_usd() == 200  # PAPER cash untouched in LIVE
    push = drain(n)[0]
    assert push["text"].startswith("Buy Project Sphere · $80.00") and "PAPER" not in push["text"]
    assert [b["text"] for b in push["buttons"]] == ["Buy on FOMO", "Bought", "Skip"]
    ex.expire(time.time() + 5 * 60 + 1)
    assert ctx.db.one("SELECT status FROM signals WHERE mint=?", (MINT,))["status"] == "missed"
    ex.on_control({"cmd": "bought", "mint": MINT})  # too late: nothing pending
    assert ctx.db.open_positions() == []


def test_live_bought_opens_sold_closes(tmp_path):
    ctx = make_ctx(tmp_path, desk__mode="LIVE", live_stack=1000)
    ctx.db.upsert_coin(MINT, name="Project Sphere")
    ex, risk = Executor(ctx), Risk(ctx)
    n = ctx.bus.subscribe("notify")
    m = pair(liquidity_usd=50000)
    asyncio.run(ex.handle(signal(m), market=m))
    ex.on_control({"cmd": "bought", "mint": MINT})
    pos = ctx.db.open_positions()[0]
    assert pos["mode"] == "LIVE" and pos["entry_price"] == m["price_usd"] and pos["size_usd"] == 80.0
    assert ctx.db.cash_usd() == 200 and ctx.db.equity_usd() == 200  # LIVE money sits in FOMO, not in PAPER
    out = risk.step(dict(pos), {**m, "price_usd": m["price_usd"] * 0.93}, time.time())
    assert out["status"] == "exiting" and ctx.db.open_positions()  # still open until [Sold]
    ex_push = drain(n)[-1]
    assert ex_push["text"].startswith("Project Sphere hit the -6% stop") and "PAPER" not in ex_push["text"]
    assert [b["text"] for b in ex_push["buttons"]] == ["Sell on FOMO", "Sold"]
    risk.confirm_sold(MINT)
    closed = ctx.db.one("SELECT * FROM positions WHERE id=?", (pos["id"],))
    assert closed["closed_at"] and closed["exit_reason"] == "stop"
    assert closed["fees_usd"] == pytest.approx(0.855 + 0.855, abs=1e-4)  # 80 and 74.40 both in the flat bracket
    assert ctx.db.cash_usd() == 200


# ---------------------------------------------------------------- LIVE switch gates
def full_night(db, report_time="08:00", step=1800):
    now = time.time()
    n_end = night_start(now, report_time)
    t0 = n_end - 86400 - 3600
    db.set_setting("_paper_since", t0)
    t = t0
    while t <= now:
        db.add_balance(200.0, ts=t)
        t += step
    db.add_balance(200.0, ts=now)
    return n_end


def test_paper_night_needs_streak_and_coverage(tmp_path):
    ctx = make_ctx(tmp_path)
    Executor(ctx)  # seeds: streak starts now -> not a full night
    st = paper_night_status(ctx.db, ctx.cfg)
    assert not st["done"] and "after the last night began" in st["why"]
    n_end = full_night(ctx.db)
    assert paper_night_done(ctx.db, ctx.cfg)
    # 2h hole inside the night -> not full
    ctx.db.x("DELETE FROM balance WHERE ts>? AND ts<?", (n_end - 12 * 3600, n_end - 10 * 3600))
    st = paper_night_status(ctx.db, ctx.cfg)
    assert not st["done"] and st["why"].startswith("desk down")


def test_live_switch_refuses_without_night(tmp_path):
    ctx = make_ctx(tmp_path, live_stack=1000)
    Executor(ctx)
    cmds = Commands(ctx, who="cli")
    assert "Type LIVE (capitals) within 2 min to confirm" in run(cmds, "live")[0].text
    t = run(cmds, "LIVE")[0].text
    assert t.startswith("Not yet: LIVE needs at least 1 full PAPER night in the db. Desk stays PAPER.")
    assert ctx.cfg["desk"]["mode"] == "PAPER" and load_config(ctx.db)["desk"]["mode"] == "PAPER"


def test_live_switch_with_night_and_typed_LIVE(tmp_path):
    ctx = make_ctx(tmp_path, live_stack=1000)
    Executor(ctx)
    full_night(ctx.db)
    blocked = Commands(ctx)  # telegram cannot arm the manual FOMO switch either
    assert run(blocked, "live")[0].text.startswith("Telegram cannot activate LIVE")
    assert run(blocked, "LIVE")[0].text.startswith("Telegram cannot activate LIVE")
    assert ctx.cfg["desk"]["mode"] == "PAPER"
    cmds = Commands(ctx, who="cli")
    assert run(cmds, "LIVE")[0].text.startswith("Switching to LIVE")  # no prompt pending -> just asks
    assert ctx.cfg["desk"]["mode"] == "PAPER"
    run(cmds, "live")
    assert run(cmds, "live")[0].text.startswith("Switching to LIVE")  # lowercase never confirms
    assert ctx.cfg["desk"]["mode"] == "PAPER"
    t = run(cmds, "LIVE")[0].text
    assert t.startswith("PAPER -> LIVE.") and "places no orders" in t
    assert ctx.cfg["desk"]["mode"] == "LIVE" and ctx.db.live_ok()
    assert load_config(ctx.db)["desk"]["mode"] == "LIVE"  # unlocked: load_config no longer forces PAPER
    assert run(cmds, "status")[0].text.startswith("LIVE desk")
    # back to PAPER: marker cleared, new streak -> LIVE needs a fresh full PAPER night
    assert run(cmds, "paper")[0].text.startswith("LIVE -> PAPER.")
    assert load_config(ctx.db)["desk"]["mode"] == "PAPER" and not ctx.db.live_ok()
    assert not paper_night_done(ctx.db, ctx.cfg)


def test_live_confirm_window_expires(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, live_stack=1000)
    Executor(ctx)
    full_night(ctx.db)
    cmds = Commands(ctx, who="cli")
    run(cmds, "live")
    ctx.state["live_pending"] -= 121
    assert run(cmds, "LIVE")[0].text.startswith("Switching to LIVE") and ctx.cfg["desk"]["mode"] == "PAPER"


def test_live_needs_a_stack(tmp_path):
    ctx = make_ctx(tmp_path)
    Executor(ctx)
    full_night(ctx.db)
    cmds = Commands(ctx, who="cli")
    run(cmds, "live")
    assert run(cmds, "LIVE")[0].text.startswith("Set your LIVE stack first") and ctx.cfg["desk"]["mode"] == "PAPER"


def test_no_auto_live_from_file_or_raw_setting(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.db.set_setting("desk.mode", "LIVE")
    full_night(ctx.db)  # even with a night in the db, nobody typed LIVE
    assert load_config(ctx.db)["desk"]["mode"] == "PAPER"
    assert "Switch mode with: paper  or  live" in run(Commands(ctx), "set desk.mode LIVE")[0].text


def test_live_locked_is_gone():
    assert not hasattr(config, "LIVE_LOCKED")
    hits = [p for p in ROOT.rglob("*.py") if not ({".venv", "_legacy", "tests"} & set(p.parts))
            and "LIVE_LOCKED" in p.read_text()]
    assert hits == []


# ---------------------------------------------------------------- PAPER badge on pushes + screens
def test_paper_prefix_only_in_paper():
    assert push_text(True, "X", 47.5, 23.8, 80, 6, 12).startswith("PAPER · Buy X")
    assert push_text(False, "X", 47.5, 23.8, 80, 6, 12).startswith("Buy X")


def test_screens_show_paper_flag():
    js, html = (ROOT / "web" / "app.js").read_text(), (ROOT / "web" / "index.html").read_text()
    assert 'id="paper-flag"' in html and 'id="paper-badge"' in html
    assert js.count("paperFlag();") >= 4 and 'st.paper !== false && st.mode !== "LIVE"' in js
    assert "LIVE is locked in this build" not in js


def test_api_paper_flags_follow_mode(tmp_path, monkeypatch):
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "DATA_PATH").write_text("testDataPath_abcdefghijklmnop")
    (sec / "APP_TOKEN").write_text("app-token-SECRET-value-123456")
    monkeypatch.setattr(config, "SECRETS_DIR", sec)
    import api
    ctx = make_ctx(tmp_path)
    Executor(ctx)
    w = api.PlanBWriter(ctx, publish_dir=tmp_path / "publish", web_dir=config.WEB_DIR)
    st, ac = w.build_state(), w.build_account()
    assert st["paper"] is True and st["mode"] == "PAPER" and ac["paper"] is True
    assert ac["mode_switch"]["live_gate"]["paper_night_done"] is False
    ctx.cfg["desk"]["mode"] = "LIVE"
    assert w.build_state()["paper"] is False and w.build_account()["paper"] is False


# ---------------------------------------------------------------- no FOMO order code anywhere
ORDER_RE = re.compile(r"place_order|placeOrder|buy_tx|sell_tx|send_transaction|sendTransaction|sendRawTransaction|"
                      r"sign_transaction|signTransaction|signAndSend|\bKeypair\b|private_key|secretKey|"
                      r"jup\.ag|quote-api|/swap\b|def swap|\bswap\(|create_order|submit_order")


def test_no_fomo_order_execution_code():
    hits = []
    for p in ROOT.rglob("*"):
        if p.suffix not in (".py", ".js", ".html", ".txt", ".json") or not p.is_file():
            continue
        if {".venv", "_legacy", "tests", "specs", "logs", "data", "__pycache__", ".pytest_cache"} & set(p.parts):
            continue
        for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            if ORDER_RE.search(line):
                hits.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:80]}")
    assert hits == []
    reqs = (ROOT / "requirements.txt").read_text().lower()
    assert not any(lib in reqs for lib in ("solana", "solders", "anchorpy", "jupiter", "web3"))
