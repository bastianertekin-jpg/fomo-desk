"""Replay raw samples + the whole STEP 5 bus (Scanner -> Auditor -> Analyst(rules) -> Executor -> Risk), offline."""
import asyncio
import json
import time

import fields as F
from agents.analyst import Analyst, gate, parse_jev, rules_answers
from agents.auditor import Auditor
from agents.common import bonding_curve, parse_pair, pumpswap_pair
from agents.executor import Executor
from agents.risk import Risk
from agents.scanner import Scanner, copycat_of, h1_kill, liq_kill, top10_kill
from bus import TOPICS, Bus
from config import DEFAULTS
from fakes import CREATOR, MINT, POOL, RAW, FakeSources, make_ctx, pair

S = DEFAULTS["scanner"]


def test_migration_sample():
    m = json.loads((RAW / "pumpportal_migration.json").read_text())
    assert F.get(m, F.PUMPPORTAL_MIGRATION, "mint").endswith("pump")


def test_dexscreener_samples_pick_pumpswap():
    raw = json.loads((RAW / "dexscreener_pair.json").read_text())
    pairs = raw if isinstance(raw, list) else raw.get("pairs") or [raw]
    p = pumpswap_pair(pairs)
    assert p is None or p["dex_id"] == "pumpswap"
    one = parse_pair(json.loads((RAW / "dexscreener_pair_one.json").read_text()))
    assert one["pair_address"] == POOL and one["price_usd"] > 0


def test_bonding_curve_pda_matches_chain():
    assert bonding_curve(MINT) == "52tWxXYWfexXDaHyY3jDcGD5DTkLbSxM1ekB1t1tXnxt"


def test_scanner_kill_rules_in_order():
    assert liq_kill(39_999, S) and not liq_kill(40_000, S)
    assert top10_kill(None, S) is None and top10_kill(25.1, S) == "top 10 hold 25%" and not top10_kill(25, S)
    assert h1_kill(81, S) and not h1_kill(80, S)
    assert copycat_of("Project Sphere", "SPHERE", {"Other": ("PROJECT SPHERE!", "SPH")}, MINT)
    assert copycat_of("Sphere", "SPH", {MINT: ("Sphere", "SPH")}, MINT) is None  # itself is no copycat


def test_jev_sample_and_rules():
    js = json.loads((RAW / "jev_systemone_test.json").read_text())
    ans = parse_jev(js)
    assert ans is None or set(ans) == {"alive_6h", "organic", "flush", "chase"}
    assert parse_jev({"answers": {"alive_6h": {"noul": 2}}}) is None
    a = rules_answers(pair(), 9, DEFAULTS["analyst"]["rules"])  # 9 bundled -> organic halved
    assert a["organic"] == 0.5 and gate(a, DEFAULTS["analyst"])[2] >= 0


def test_bus_topics_and_unknown_topic():
    assert TOPICS == ("candidate", "audited", "signal", "fill", "notify", "control", "ui")
    b = Bus()
    try:
        b.publish("order", {})
        raise AssertionError("order topic must not exist")
    except ValueError:
        pass


def test_full_pipeline_offline_over_bus(tmp_path):
    good = pair(liquidity_usd=50000, volume_h1=30000, buys_h1=200, change_m5=-6, buys_m5=300, sells_m5=100,
                change_h1=10)
    ctx = make_ctx(tmp_path, sources=FakeSources(pairs={MINT: good}), analyst__mode="rules")
    sc, au, an, ex, rk = Scanner(ctx), Auditor(ctx), Analyst(ctx), Executor(ctx), Risk(ctx)
    now = time.time()
    ctx.db.upsert_coin(MINT, migrated_at=now - 15 * 60, creator=CREATOR, creator_balance_at_migration=0.0)
    sc.room[MINT] = {"migrated_at": now - 15 * 60}

    async def flow():
        cand = await sc.evaluate(MINT, good, now)
        assert cand["verdict"] == "pass" and cand["top10Pct"] == 10.0
        audited = await au.audit(await au.inbox.get())
        assert audited["passed"]
        verdict = await an.analyse(await an.inbox.get())
        assert verdict["passed"] and verdict["model"] == "rules"
        o = await ex.handle(await ex.inbox.get())
        assert o["status"] == "bought" and o["sizeUsd"] == 47.5
        f = await rk.fills.get() if hasattr(rk, "fills") else None
        return f

    asyncio.run(flow())
    assert ctx.db.one("SELECT verdict FROM scans")["verdict"] == "pass"
    assert ctx.db.one("SELECT COUNT(*) n FROM audits WHERE pass=1")["n"] == 8
    assert ctx.db.one("SELECT reason FROM verdicts")["reason"] == "rules · pass"
    assert ctx.db.coin_status(MINT) == "traded"
    # Risk owns the exit: trail after +25% then -13% off the top
    p = dict(ctx.db.open_positions()[0])
    e = p["entry_price"]
    assert rk.step(p, {**good, "price_usd": e * 1.25}, now) is None
    out = rk.step(p, {**good, "price_usd": e * 1.25 * 0.87}, now)
    assert out["reason"] == "trail" and ctx.db.one("SELECT kind,subtitle FROM activity WHERE kind='sold'")["subtitle"] == "Risk · trailing stop"


def test_replay_two_hours_of_migrations_through_paper_shows_activity(tmp_path):
    """STEP 13.6: replay a 2h window of migrations offline through PAPER; Activity shows the night."""
    from agents.scanner import Scanner
    from fakes import mint_js

    now = time.time()
    t0 = now - 2 * 3600
    mints = [
        ("MintAAAA1111111111111111111111111111111111", t0 + 10 * 60, "Alpha"),
        ("MintBBBB2222222222222222222222222222222222", t0 + 70 * 60, "Beta"),
        ("MintCCCC3333333333333333333333333333333333", t0 + 110 * 60, "Gamma"),
    ]
    good = pair(liquidity_usd=50000, volume_h1=30000, buys_h1=200, change_m5=-6, buys_m5=300, sells_m5=100,
                change_h1=10)
    src = FakeSources(pairs={m: good for m, _, _ in mints}, mint=mint_js())
    ctx = make_ctx(tmp_path, sources=src, analyst__mode="rules")
    sc, au, an, ex, rk = Scanner(ctx), Auditor(ctx), Analyst(ctx), Executor(ctx), Risk(ctx)

    async def run_night():
        for i, (mint, migrated_at, name) in enumerate(mints):
            ctx.db.upsert_coin(mint, name=name, migrated_at=migrated_at, creator=CREATOR,
                               creator_balance_at_migration=0.0)
            sc.room[mint] = {"migrated_at": migrated_at}
            src.mint = mint_js(freeze_auth="FreezerXYZ111") if i == 2 else mint_js()
            cand = await sc.evaluate(mint, good, migrated_at + 15 * 60)
            assert cand["verdict"] == "pass", cand
            audited = await au.audit(await au.inbox.get())
            if i == 2:
                assert not audited["passed"] and audited["reason"] == "freeze not revoked"
                continue
            assert audited["passed"]
            assert (await an.analyse(await an.inbox.get()))["passed"]
            o = await ex.handle(await ex.inbox.get())
            assert o["status"] == "bought" and o["mode"] == "PAPER"

    asyncio.run(run_night())

    migr = [r["migrated_at"] for r in ctx.db.q("SELECT migrated_at FROM coins ORDER BY migrated_at")]
    assert migr[-1] - migr[0] >= 100 * 60

    assert [r["kind"] for r in ctx.db.q("SELECT kind FROM activity ORDER BY id")].count("bought") == 2
    assert any(r["subtitle"] == "Auditor · freeze not revoked"
               for r in ctx.db.q("SELECT subtitle FROM activity WHERE kind='rejected'"))

    alpha = dict([p for p in ctx.db.open_positions() if p["mint"] == mints[0][0]][0])
    e = alpha["entry_price"]
    assert rk.step(alpha, {**good, "price_usd": e * 1.25}, now) is None
    alpha = dict([p for p in ctx.db.open_positions() if p["mint"] == mints[0][0]][0])
    out = rk.step(alpha, {**good, "price_usd": e * 1.25 * 0.87}, now)
    assert out["reason"] == "trail"
    acts = ctx.db.q("SELECT kind,title,subtitle,mint FROM activity ORDER BY id")
    assert any(a["kind"] == "sold" and a["subtitle"] == "Risk · trailing stop" for a in acts)
    assert len(ctx.db.open_positions()) == 1
    assert ctx.db.open_positions()[0]["mint"] == mints[1][0]
