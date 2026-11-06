"""STEP 13: Executor sizing, ticket check, PAPER fill. No orders anywhere (PAPER only)."""
import asyncio

from agents.executor import Executor, size_ticket, ticket_check
from config import DEFAULTS, load_config
from fakes import MINT, make_ctx, pair

E = DEFAULTS["executor"]


def test_stack_200_8pct_is_16_bump_sends_47_50():
    sz = size_ticket(200, 200, 1_000_000, E)
    assert sz["raw"] == 16.0 and sz["size"] == 47.5 and sz["cap"] == "bump"


def test_skip_mode_skips_under_min():
    sz = size_ticket(200, 200, 1_000_000, {**E, "below_min": "skip"})
    assert sz["size"] is None and sz["skip"] == "under $47.50"


def test_ticket_check_messages():
    assert ticket_check(200, E)[1] == "tickets bumped to $47.50 = 23.8% of stack"
    assert ticket_check(200, {**E, "below_min": "skip"})[1] == "every coin will be skipped, stack under $593.75"
    assert ticket_check(1000, E)[1] is None


def test_paper_200_with_skip_warns_on_start(tmp_path):
    ctx = make_ctx(tmp_path, executor__below_min="skip")
    n = ctx.bus.subscribe("notify")
    msg = Executor(ctx).night_check()
    assert msg == "every coin will be skipped, stack under $593.75"
    # STEP 10: warn AND ask to switch to bump or raise the stack (PAPER pushes start with "PAPER · ")
    assert n.get_nowait()["text"] == ("PAPER · every coin will be skipped, stack under $593.75. "
                                      "Switch to bump (set below_min to bump) or raise the stack.")


def test_pool_40k_caps_at_800():
    sz = size_ticket(100_000, 100_000, 40_000, E)
    assert sz["size"] == 800.0 and sz["cap"] == "pool"


def test_no_free_stack():
    assert size_ticket(200, 10, 1_000_000, E)["skip"] == "no free stack"


def test_paper_fill_writes_rows_and_publishes_fill(tmp_path):
    ctx = make_ctx(tmp_path)
    ex = Executor(ctx)
    fill, notify = ctx.bus.subscribe("fill"), ctx.bus.subscribe("notify")
    m = pair(liquidity_usd=50000)
    sig = {"mint": MINT, "score": 80, "ctx": {"name": "Project Sphere", "priceUsd": m["price_usd"],
                                              "scanner": {"liquidityUsd": 50000}}}
    o = asyncio.run(ex.handle(sig, market=m))
    assert o["status"] == "bought" and o["sizeUsd"] == 47.5 and o["mode"] == "PAPER"
    assert o["link"].startswith("https://fomo.family/")
    s = ctx.db.one("SELECT * FROM signals")
    assert s["status"] == "bought" and s["size_usd"] == 47.5 and s["mode"] == "PAPER"
    p = ctx.db.open_positions()[0]
    assert abs(p["stop_price"] - m["price_usd"] * 0.94) < 1e-12 and p["fees_usd"] > 0
    a = ctx.db.one("SELECT * FROM activity")
    assert a["kind"] == "bought" and a["title"] == "Bought Project Sphere" and a["value"] == 47.5
    # STEP 11: ticket + fees.py buy side (flat 0.95 - 10% ref = 0.855) leave PAPER cash at the fill
    assert abs(ctx.db.cash_usd() - (200 - 47.5 - 0.855)) < 1e-9
    f = fill.get_nowait()
    assert f["positionId"] == p["id"]
    push = notify.get_nowait()
    assert push["text"].startswith("PAPER · Buy Project Sphere · $47.50") and len(push["buttons"]) == 3
    # second signal for the same coin tonight is ignored (one push per coin per night)
    assert asyncio.run(ex.handle(sig, market=m)) is None


def test_recheck_skips(tmp_path):
    ctx = make_ctx(tmp_path)
    ex = Executor(ctx)
    m = pair(liquidity_usd=30000)
    o = asyncio.run(ex.handle({"mint": MINT, "ctx": {"scanner": {"liquidityUsd": 50000}}}, market=m))
    assert o["status"] == "skipped" and o["reason"] == "liquidity dropping"
    assert ctx.db.one("SELECT subtitle FROM activity")["subtitle"] == "Executor · liquidity dropping"
    o = asyncio.run(Executor(ctx).handle({"mint": "Other", "ctx": {"priceUsd": m["price_usd"] / 1.2}}, market=m))
    assert o["reason"] == "ran before the push"


def test_paused_skips_entries(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.state["paused"] = True
    assert asyncio.run(Executor(ctx).handle({"mint": MINT}, market=pair()))["reason"] == "paused"


def test_live_is_never_reachable_from_settings(tmp_path):
    from db import DB
    db = DB(tmp_path / "x.db")
    db.set_setting("desk", {"mode": "LIVE"})
    assert load_config(db)["desk"]["mode"] == "PAPER"
