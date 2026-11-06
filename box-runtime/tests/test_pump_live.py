"""Disabled pump.fun curve path. Offline only: no RPC, no signer, no desk history writes."""
import asyncio
from pathlib import Path

import pytest

from agents.common import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, bonding_curve
from agents.pump_live import PumpLive
from agents.risk import evaluate
from bus import Bus
from commands import Commands
from config import DEFAULTS
from config import test_config as desk_config
from db import DB
from agents.common import Ctx
from pump_curve import (
    BUY_V2,
    BUY_V3,
    SELL_V3,
    ZERO,
    Curve,
    FailClosed,
    MintView,
    PoolView,
    build_curve_buy,
    curve_accounts,
    decode_curve,
    discovery_request,
    encode_curve,
    quote_buy,
)
from pump_exec import DisabledSigner, Ledger, SignerDisabled, Ticket, evaluate_arm, ingest

MINT = "31SYoqWFhx3i9UsfnoXTjFp4ZwJA8F8UPrMxymfDpump"
USER = "HmcVp73ePenjn2JDHQBws6sYN56aBCmPuGDGouqzNUok"
CREATOR = "HN9YV4zbrsSjGwrLkr17ebbmCv99U34j7FuRcDtHjtZ9"
OTHER = "9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz"
ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
BLOCK = bytes(range(32))


def ctx(tmp_path):
    db = DB(tmp_path / "desk.db")
    return Ctx(cfg=desk_config(), db=db, bus=Bus(), http=None, offline=True)


def mint_view(program=TOKEN_PROGRAM, **kw):
    base = dict(mint=MINT, program=program, decimals=6, mint_authority=None, freeze_authority=None)
    base.update(kw)
    return MintView(**base)


def curve(**kw):
    base = dict(
        virtual_token=1_000_000_000_000, virtual_quote=30_000_000_000,
        real_token=800_000_000_000, real_quote=1_000_000_000, supply=1_000_000_000_000,
        complete=False, creator=CREATOR, mayhem=False, cashback=False, quote_mint=ZERO,
        creator_fee_bps=0, holder_reward=False, depth=0,
    )
    base.update(kw)
    return Curve(**base)


def limits(**kw):
    base = dict(
        max_position_usd=25, max_exposure_usd=50, max_positions=2, max_daily_loss_usd=10,
        max_quote_age_s=30, max_slippage_bps=100, max_price_impact_bps=500,
        fee_reserve_lamports=5_000, fee_cap_lamports=100_000,
        kill_switch=False, wallet=USER, network="devnet",
    )
    base.update(kw)
    return base


def ticket(tmp=None, **kw):
    base = dict(
        intent_id="buy-1", user=USER, mint=mint_view(), curve=curve(), pool=None,
        tokens=1_000_000, protocol_bps=100, creator_bps=30, buyback=OTHER,
        blockhash=BLOCK, observed_at=NOW, sol_usd=100.0, lamports=1_000_000_000, now=NOW,
    )
    base.update(kw)
    return Ticket(**base)


def arm_ok():
    return dict(who="cli", typed="LIVE", limits=limits(), night_done=True, reviewed=True)


def book(tmp_path):
    return Ledger(tmp_path / "pump_orders.db")


def dump(db: DB):
    return {name: db.q(f"SELECT * FROM {name}") for name in (
        "coins", "scans", "audits", "verdicts", "signals", "positions", "activity",
        "prices", "balance", "settings", "change_log", "skips")}


# ---------------------------------------------------------------- quotes and instructions
def test_buy_quote_matches_curve_math_and_v3_layout():
    c = curve()
    tokens = 1_000_000
    net = tokens * c.virtual_quote // (c.virtual_token - tokens) + 1
    fee = (net * 100 + 9_999) // 10_000 + (net * 30 + 9_999) // 10_000
    q = quote_buy(c, tokens, 100, 30, 100, 500)
    assert q.net_atoms == net and q.fee_atoms == fee
    assert q.limit_atoms == (net + fee) + (net + fee) * 100 // 10_000
    built = build_curve_buy(USER, mint_view(), c, tokens, 100, 30, 100, 500, OTHER, BLOCK)
    assert built.data[:8] == BUY_V3
    assert built.data[-1:] == b"\x00"  # partial fill stays off
    assert len(built.accounts) == 17
    assert built.accounts[1]["pubkey"] == MINT
    assert built.accounts[5]["pubkey"] == bonding_curve(MINT)
    assert built.accounts[8]["signer"] and built.accounts[16]["pubkey"].startswith("6EF8")
    assert built.message[0] == 1 and BLOCK in built.message
    again = curve_accounts(USER, MINT, ZERO, TOKEN_PROGRAM, OTHER)
    assert [a["pubkey"] for a in again] == [a["pubkey"] for a in built.accounts]


def test_structural_rejects_and_short_curve():
    view = mint_view()
    raw = encode_curve(curve())
    assert decode_curve(raw[:49]).quote_mint == ZERO
    assert decode_curve(raw[:82]).mayhem is False
    from pump_curve import classify
    short = decode_curve(encode_curve(curve(mayhem=True))[:82])
    with pytest.raises(FailClosed) as exc:
        classify(short, view)
    assert exc.value.reason == "mayhem"
    with pytest.raises(FailClosed) as tail:
        decode_curve(raw + b"\x01")
    assert tail.value.reason == "unknown_curve_tail"
    cases = [
        (curve(cashback=True), view, "cashback"),
        (curve(complete=True), view, "curve_complete"),
        (curve(depth=1), view, "quote_chain"),
        (curve(quote_mint=CREATOR), view, "non_sol_quote"),
        (curve(), mint_view(mint_authority=USER), "authority_set"),
        (curve(), mint_view(freeze_authority=USER), "authority_set"),
        (curve(), mint_view(program="not-a-program"), "unknown_token_program"),
    ]
    from pump_curve import classify
    for c, m, reason in cases:
        with pytest.raises(FailClosed) as err:
            classify(c, m)
        assert err.value.reason == reason
    with pytest.raises(FailClosed) as over:
        quote_buy(curve(), curve().real_token + 1, 100, 30, 100, 10_000)
    assert over.value.reason == "would_complete_curve"
    with pytest.raises(FailClosed) as clash:
        quote_buy(curve(creator_fee_bps=50), 1_000_000, 100, 30, 100, 500)
    assert clash.value.reason == "creator_fee_conflict"
    with pytest.raises(FailClosed) as unset:
        quote_buy(curve(), 1_000_000, None, 30, 100, 500)
    assert unset.value.reason == "fee_bps_unset"


def test_token2022_base_program_is_passed_through():
    built = build_curve_buy(USER, mint_view(program=TOKEN_2022_PROGRAM), curve(),
                            1_000_000, 100, 30, 100, 500, OTHER, BLOCK)
    assert built.accounts[3]["pubkey"] == TOKEN_2022_PROGRAM
    classic = build_curve_buy(USER, mint_view(), curve(), 1_000_000, 100, 30, 100, 500, OTHER, BLOCK)
    assert built.accounts[6]["pubkey"] != classic.accounts[6]["pubkey"]


def test_discovery_is_official_and_disabled_ingest_drops_rows():
    req = discovery_request()
    assert req["sent"] is False and req["method"] == "getProgramAccounts"
    assert req["unsupported"] == ["pumpportal"]
    assert all("dataSize" not in f for f in req["filters"])
    blob = {"data": encode_curve(curve()), "mint_view": mint_view()}
    assert ingest([blob], enabled=False) == {"enabled": False, "rows": []}
    seen = ingest([blob], enabled=True)
    assert seen["rows"][0]["tradable"] is False
    assert seen["rows"][0]["reason"] == "execution_disabled"
    bad = ingest([{"data": encode_curve(curve(cashback=True)), "mint_view": mint_view()}], enabled=True)
    assert bad["rows"][0]["reason"] == "cashback" and bad["rows"][0]["tradable"] is False


def test_pumpswap_adapter_is_off_until_asked_and_still_unsigned():
    pool = PoolView(
        pool=OTHER, base_vault=1_000_000_000, quote_vault=5_000_000_000, virtual_quote=0,
        base_mint=MINT, quote_mint=ZERO, pool_base=USER, pool_quote=CREATOR,
        protocol_bps=25, creator_bps=30, lp_bps=20, buyback=OTHER,
    )
    from pump_curve import build_amm
    with pytest.raises(FailClosed) as off:
        build_amm(USER, mint_view(), pool, 1_000_000, "buy", 100, 500, BLOCK, False)
    assert off.value.reason == "pumpswap_disabled"
    missing = PoolView(**{**pool.__dict__, "lp_bps": None})
    with pytest.raises(FailClosed) as fees:
        build_amm(USER, mint_view(), missing, 1_000_000, "buy", 100, 500, BLOCK, True)
    assert fees.value.reason == "fee_bps_unset"
    blocked = PoolView(**{**pool.__dict__, "disable_flags": 1 << 3})
    with pytest.raises(FailClosed) as flag:
        build_amm(USER, mint_view(), blocked, 1_000_000, "buy", 100, 500, BLOCK, True)
    assert flag.value.reason == "pool_disabled"
    built = build_amm(USER, mint_view(), pool, 1_000_000, "sell", 100, 500, BLOCK, True)
    assert built.data[:8] == bytes([93, 246, 130, 60, 231, 233, 64, 178])
    assert built.venue == "pumpswap_v2" and len(built.accounts) == 17
    buy = build_amm(USER, mint_view(), pool, 1_000_000, "buy", 100, 500, BLOCK, True)
    assert buy.data[:8] == BUY_V2


# ---------------------------------------------------------------- ledger gates
def test_arm_ignores_desk_mode_and_telegram_and_missing_limits():
    ok, bad = evaluate_arm("telegram", "LIVE", limits(), night_done=True, reviewed=True)
    assert not ok and "telegram" in bad
    ok, bad = evaluate_arm("cli", "LIVE", limits(kill_switch=True), night_done=True, reviewed=True)
    assert "kill_switch" in bad
    ok, bad = evaluate_arm("cli", "LIVE", {k: v for k, v in limits().items() if k != "kill_switch"},
                           night_done=True, reviewed=True)
    assert "kill_switch" in bad  # missing means engaged
    ok, bad = evaluate_arm("cli", "live", limits(), night_done=True, reviewed=True)
    assert "typed_live" in bad
    ok, _ = evaluate_arm(**arm_ok())
    assert ok


def test_prepare_builds_unsigned_then_signer_refuses(tmp_path):
    desk = ctx(tmp_path)
    desk.cfg["desk"]["mode"] = "LIVE"  # a generic mode must not be the gate
    led = book(tmp_path)
    before = dump(desk.db)
    refused = led.prepare(ticket(intent_id="nope"), who="cli", typed="LIVE",
                          limits=limits(max_position_usd=None), night_done=True, reviewed=True)
    assert refused["status"] == "rejected" and refused["message"] is None
    row = led.prepare(ticket(), **arm_ok())
    assert row["status"] == "built_unsigned" and row["provenance"] == "unsigned"
    assert row["message"][:1] == b"\x01" and bytes(row["message"]).find(BUY_V3) > 0
    again = led.prepare(ticket(), **arm_ok())
    assert again["attempts"] == 1 and again["message"] == row["message"]
    with pytest.raises(SignerDisabled):
        led.hand_off(row["intent_id"], DisabledSigner())
    assert led.get(row["intent_id"])["status"] == "built_unsigned"
    assert led.get(row["intent_id"])["signature_ok"] is None
    assert dump(desk.db) == before
    led.close()


def test_quote_age_slippage_impact_and_reserve_fail_closed(tmp_path):
    led = book(tmp_path)
    stale = led.prepare(ticket(intent_id="stale", observed_at=NOW - 31), **arm_ok())
    assert stale["reason"] == "quote_age"
    wide = led.prepare(ticket(intent_id="wide", tokens=100_000_000_000), **arm_ok())
    assert wide["reason"] == "price_impact"
    poor = led.prepare(ticket(intent_id="poor", lamports=5_000), **arm_ok())
    assert poor["reason"] == "fee_reserve"
    assert led.conn.execute("SELECT COUNT(*) AS n FROM mint_locks").fetchone()["n"] == 0
    led.close()


def test_mint_lock_attempt_cap_and_restart_hold(tmp_path):
    led = book(tmp_path)
    first = led.prepare(ticket(), **arm_ok())
    assert first["status"] == "built_unsigned"
    second = led.prepare(ticket(intent_id="buy-2"), **arm_ok())
    assert second["status"] == "rejected" and second["reason"] == "mint_locked"
    led.conn.execute("UPDATE orders SET status='confirmed_unfilled' WHERE intent_id='buy-1'")
    led.conn.execute("DELETE FROM mint_locks")
    for i in (2, 3):
        led.conn.execute(
            "INSERT INTO orders(intent_id,mint,side,status,attempts,provenance,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (f"old-{i}", MINT, "buy", "confirmed_unfilled", 1, "unsigned", NOW, NOW))
    capped = led.prepare(ticket(intent_id="buy-4"), **arm_ok())
    assert capped["reason"] == "attempts_exhausted"
    led.conn.execute("UPDATE orders SET status='awaiting_confirm', attempts=1, note=NULL WHERE intent_id='buy-1'")
    led.close()
    restarted = Ledger(tmp_path / "pump_orders.db")
    summary = restarted.reconcile()
    held = restarted.get("buy-1")
    assert summary == {"held": 1, "retried": 0, "filled": 0}
    assert held["status"] == "awaiting_confirm" and held["note"] == "reconcile_hold"
    assert held["attempts"] == 1
    unfilled = restarted.observe("buy-1", signature_ok=True, token_delta=None, quote_delta=None, now=NOW)
    assert unfilled["status"] == "confirmed_unfilled" and unfilled["provenance"] == "chain_unfilled"
    restarted.close()


def test_fill_needs_both_deltas_and_exit_follows_risk(tmp_path):
    desk = ctx(tmp_path)
    desk.db.x("INSERT INTO positions(mint,mode,opened_at) VALUES(?,?,?)", (MINT, "PAPER", NOW))
    before = dump(desk.db)
    led = book(tmp_path)
    row = led.prepare(ticket(), **arm_ok())
    led.conn.execute("UPDATE orders SET status='awaiting_confirm' WHERE intent_id=?", (row["intent_id"],))
    filled = led.observe(row["intent_id"], signature_ok=True, token_delta=1_000_000,
                         quote_delta=-30_000, now=NOW)
    assert filled["status"] == "filled" and filled["provenance"] == "chain_delta"
    pos = led.position(MINT)
    assert pos["status"] == "open" and pos["qty"] == 1_000_000
    # the desk book is a different file and stays as it was
    assert dump(desk.db) == before
    fired = led.plan_exit(MINT, 0.0, NOW, desk.cfg["risk"], ticket(intent_id="ignored"), **arm_ok())
    assert fired["status"] == "built_unsigned" and fired["side"] == "sell"
    assert bytes(fired["message"]).find(SELL_V3) > 0
    with pytest.raises(SignerDisabled):
        led.hand_off(fired["intent_id"])
    # same exit is not built twice
    assert led.plan_exit(MINT, 0.0, NOW, desk.cfg["risk"], ticket(intent_id="ignored"), **arm_ok())["intent_id"] == fired["intent_id"]
    led.conn.execute("UPDATE orders SET status='awaiting_confirm' WHERE intent_id=?", (fired["intent_id"],))
    closed = led.observe(fired["intent_id"], signature_ok=True, token_delta=-1_000_000,
                         quote_delta=40_000, now=NOW + 1)
    assert closed["status"] == "filled" and led.position(MINT)["status"] == "closed"
    nxt = led.prepare(ticket(intent_id="buy-2"), **arm_ok())
    assert nxt["status"] == "built_unsigned"
    assert dump(desk.db) == before
    led.close()


def test_failed_exit_when_phase_is_unsupported(tmp_path):
    desk = ctx(tmp_path)
    led = book(tmp_path)
    led.conn.execute(
        "INSERT INTO live_positions(mint,buy_intent,qty,entry_price,size_usd,peak_price,stop_price,"
        "trail_active,opened_at,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (MINT, "buy-1", 1_000_000, 1.0, 5.0, 1.0, None, 0, NOW, "open"))
    failed = led.plan_exit(
        MINT, 0.0, NOW, desk.cfg["risk"],
        ticket(curve=curve(complete=True), pool=None), **arm_ok())
    assert failed["status"] == "exit_failed" and failed["reason"] == "unsupported_phase"
    assert led.position(MINT)["status"] == "exit_failed"
    again = led.plan_exit(MINT, 0.0, NOW, desk.cfg["risk"], ticket(curve=curve(complete=True)), **arm_ok())
    assert again["intent_id"] == failed["intent_id"]
    led.close()


def test_agent_reconciles_without_touching_network_or_desk(tmp_path):
    desk = ctx(tmp_path)
    desk.db.x("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)", ("scanner.min_liquidity_usd", "1000", NOW))
    before = dump(desk.db)
    side = Ledger(tmp_path / "pump_orders.db")
    side.conn.execute(
        "INSERT INTO orders(intent_id,mint,side,status,attempts,provenance,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("held", MINT, "buy", "awaiting_confirm", 2, "unsigned", NOW, NOW))
    side.close()
    # the agent opens the sibling of the desk db; point the desk at that directory
    desk.db.path = str(tmp_path / "desk.db")

    class Boom:
        def __getattr__(self, name):
            raise AssertionError(name)

    desk.sources = Boom()
    agent = PumpLive(desk)

    async def once(_):
        raise asyncio.CancelledError

    orig = asyncio.sleep
    asyncio.sleep = once
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(agent.run())
    finally:
        asyncio.sleep = orig
    held = Ledger(tmp_path / "pump_orders.db").get("held")
    assert held["note"] == "reconcile_hold" and held["attempts"] == 2 and held["status"] == "awaiting_confirm"
    assert dump(desk.db) == before


def test_legacy_scanner_filters_and_settings_are_untouched():
    text = (ROOT / "agents" / "scanner.py").read_text()
    assert "subscribeMigration" in text and "PUMPPORTAL_WS" in text
    assert "min_liquidity_usd" in text and "max_top10_pct" in text and "max_h1_change_pct" in text
    assert set(DEFAULTS) == {"scanner", "auditor", "analyst", "executor", "risk", "desk"}
    assert "pump" not in DEFAULTS["scanner"]


def test_telegram_command_cannot_activate_and_cli_still_can_ask(tmp_path):
    desk = ctx(tmp_path)
    refused = asyncio.run(Commands(desk).handle("live"))[0].text
    assert refused.startswith("Telegram cannot activate LIVE")
    assert asyncio.run(Commands(desk).handle("LIVE"))[0].text.startswith("Telegram cannot activate LIVE")
    assert asyncio.run(Commands(desk).handle("paper"))[0].text == "Already PAPER."
    asked = asyncio.run(Commands(desk, who="cli").handle("live"))[0].text
    assert "Type LIVE (capitals) within 2 min to confirm" in asked
    assert desk.cfg["desk"]["mode"] == "PAPER"


def test_telegram_cannot_arm_and_curve_watch_is_not_a_setting(tmp_path):
    desk = ctx(tmp_path)
    led = book(tmp_path)
    row = led.prepare(ticket(), who="telegram", typed="LIVE", limits=limits(), night_done=True, reviewed=True)
    assert row["status"] == "rejected" and "telegram" in row["reason"] and row["message"] is None
    led.close()
    reply = asyncio.run(Commands(desk).handle("set curve_watch on"))[0].text
    assert reply.startswith("Unknown setting")
    assert desk.cfg["desk"]["mode"] == "PAPER"


def test_risk_helper_still_names_stop_before_timeout():
    pos = {"entry_price": 1.0, "peak_price": 1.0, "stop_price": None, "trail_active": 0, "opened_at": NOW}
    upd, reason = evaluate(pos, 0.9, NOW, desk_config()["risk"])
    assert reason == "stop" and upd["stop_price"] == pytest.approx(0.94)
