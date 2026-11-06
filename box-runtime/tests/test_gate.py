"""STEP 13: Analyst gate + rules mode; Auditor stops at the first red layer."""
import asyncio
import json
import time

from agents.analyst import Analyst, gate, rules_answers
from agents.auditor import Auditor, biggest_bundle, delegation_stats
from config import DEFAULTS
from fakes import RAW, CREATOR, MINT, POOL, FakeSources, make_ctx, mint_js, pair, token_acct

A = DEFAULTS["analyst"]


def test_gate_rejects_each_rule_and_low_score():
    # alive .69 / organic .64 / chase .31 -> each named, score round(100*(.69+.64+0+.69)/4) = 50
    ok, failed, score = gate({"alive_6h": .69, "organic": .64, "flush": 0, "chase": .31}, A)
    assert not ok and score == 50
    assert failed == ["alive_6h 0.69", "organic 0.64", "chase 0.31", "score 50"]


def test_gate_score_69_rejects_and_threshold_passes():
    ok, failed, s = gate({"alive_6h": .70, "organic": .65, "flush": .61, "chase": .20}, A)
    assert s == 69 and not ok and failed == ["score 69"]
    ok, failed, s = gate({"alive_6h": .70, "organic": .65, "flush": .75, "chase": .30}, A)
    assert s == 70 and ok and failed == []


def _passing_pair(**kw):
    return pair(liquidity_usd=50000, volume_h1=30000, buys_h1=200, change_m5=-6, buys_m5=300, sells_m5=100,
                change_h1=10, **kw)


def test_rules_mode_answers_and_activity_label(tmp_path):
    ctx = make_ctx(tmp_path, analyst__mode="rules")
    an = Analyst(ctx)
    ans = rules_answers(_passing_pair(), 0, A["rules"])
    assert ans == {"alive_6h": 1.0, "organic": 1.0, "flush": 1.0, "chase": 0.125}
    sig = ctx.bus.subscribe("signal")
    out = asyncio.run(an.analyse({"mint": MINT, "scanner": {"name": "Project Sphere"}, "facts": {"bundled": 0}},
                                 pair=_passing_pair()))
    assert out["passed"] and out["model"] == "rules" and out["score"] == 97 and sig.qsize() == 1
    # weak coin -> rejected, activity row says Analyst (rules)
    out = asyncio.run(Analyst(ctx).analyse({"mint": "WEAKmint", "scanner": {"name": "Weak"}},
                                           pair=pair(volume_h1=1000, buys_h1=10, change_h1=70)))
    assert not out["passed"]
    row = ctx.db.one("SELECT * FROM activity WHERE mint='WEAKmint'")
    assert row["kind"] == "rejected" and row["subtitle"].startswith("Analyst (rules) · ")
    v = ctx.db.one("SELECT reason FROM verdicts WHERE mint='WEAKmint'")
    assert v["reason"].startswith("rules · alive_6h")


def test_jev_mode_offline_never_calls_and_rejects_no_answer(tmp_path):
    ctx = make_ctx(tmp_path)  # mode jev, ctx.offline=True -> no paid call
    out = asyncio.run(Analyst(ctx).analyse({"mint": MINT, "scanner": {"name": "X"}}, pair=_passing_pair()))
    assert not out["passed"] and out["failed"] == ["no answer"]
    assert ctx.db.one("SELECT subtitle FROM activity")["subtitle"] == "Analyst · no answer"


def _coin(ctx, migrated_min_ago=15):
    ctx.db.upsert_coin(MINT, name="Project Sphere", pool=POOL, creator=CREATOR, creator_balance_at_migration=0.0,
                       migrated_at=time.time() - migrated_min_ago * 60)


SCAN = {"mint": MINT, "name": "Project Sphere", "pool": POOL, "liquidityUsd": 50000, "verdict": "pass"}


def test_auditor_freeze_authority_red_stops_at_layer_2(tmp_path):
    src = FakeSources(mint=mint_js(freeze_auth="SomeFreezer111"))
    ctx = make_ctx(tmp_path, sources=src)
    _coin(ctx)
    out = asyncio.run(Auditor(ctx).audit(SCAN))
    assert not out["passed"] and out["reason"] == "freeze not revoked" and out["layersChecked"] == 2
    rows = ctx.db.q("SELECT layer,value_text,pass FROM audits ORDER BY CAST(layer AS INTEGER)")
    assert [r["pass"] for r in rows] == [1, 0] + [None] * 6
    assert all(r["value_text"] == "not checked" for r in rows[2:])
    assert "report" not in src.calls and "gpa" not in src.calls  # nothing after the red layer is read
    assert ctx.db.one("SELECT subtitle FROM activity")["subtitle"] == "Auditor · freeze not revoked"


def test_auditor_all_green_publishes_audited(tmp_path):
    ctx = make_ctx(tmp_path, sources=FakeSources())
    _coin(ctx)
    q = ctx.bus.subscribe("audited")
    out = asyncio.run(Auditor(ctx).audit(SCAN))
    assert out["passed"] and out["layersChecked"] == 8, out
    msg = q.get_nowait()
    assert msg["scanner"]["pool"] == POOL and msg["facts"]["bundled"] == 0 and msg["facts"]["lp"] == "100% burned"


def test_auditor_layer8_down_kills_and_counts(tmp_path):
    ctx = make_ctx(tmp_path, sources=FakeSources(fail={"gpa"}))
    _coin(ctx)
    a = Auditor(ctx)
    a.retry_delay = 0
    n = ctx.bus.subscribe("notify")
    out = asyncio.run(a.audit(SCAN))
    assert not out["passed"] and out["reason"].startswith("no data") and out["layersChecked"] == 8
    assert ctx.db.get_setting("_layer8_kills") == 1 and n.qsize() == 1


def test_bundle_and_delegation_helpers():
    assert biggest_bundle([]) == 0
    assert biggest_bundle({"bad": 1}) is None
    g = [{"nodes": [{"id": x, "holdings": 1} for x in "abcd"], "links": [{"source": "a", "target": "b"},
                                                                                   {"source": "b", "target": "c"}]}]
    assert biggest_bundle(g) == 3
    assert biggest_bundle(json.loads((RAW / "rugcheck_insiders_graph_nonempty.json").read_text())) == 9  # BONK sample
    holders, d, cnt = delegation_stats([token_acct(1.0, "D1")] * 11 + [token_acct(0.0)])
    assert holders == 11 and d == "D1" and cnt == 11
