"""Regression: desk freeze (hung pid self-heal) + top10 when getTokenLargestAccounts is 429ing."""
from __future__ import annotations

import asyncio
import os
import time

import health
from agents.common import top10_pct
from db import DB
from tests.fakes import MINT, POOL, FakeSources, token_acct


def _gpa_acct(pubkey, owner, amount):
    a = token_acct(amount)
    a["pubkey"] = pubkey
    a["account"]["data"]["parsed"]["info"]["owner"] = owner
    return a


def test_top10_falls_back_to_gpa_when_largest_rate_limited():
    gpa = [_gpa_acct("poolacct", POOL, 500_000_000.0)] + [_gpa_acct(f"a{i}", f"o{i}", 10_000_000.0) for i in range(15)]
    src = FakeSources(fail={"largest"}, gpa=gpa)
    v = asyncio.run(top10_pct(src, MINT, POOL))
    assert v == 10.0  # 10 x 1% (pool excluded by owner)


def test_top10_falls_back_to_rugcheck_when_rpc_down():
    rep = {"topHolders": [{"address": "x", "owner": POOL, "pct": 60}] +
                         [{"address": f"a{i}", "owner": f"o{i}", "pct": 2} for i in range(12)]}
    src = FakeSources(fail={"largest", "gpa"}, report=rep)
    assert asyncio.run(top10_pct(src, MINT, POOL)) == 20.0


def _db_with_beats(tmp_path, ages):
    db = DB(tmp_path / "h.db")
    now = time.time()
    for a, age in ages.items():
        db.x("INSERT INTO heartbeats(agent,ts) VALUES(?,?)", (a, now - age))
    pid = tmp_path / "desk.pid"
    apid = tmp_path / "autopush.pid"
    pid.write_text(str(os.getpid()))
    apid.write_text(str(os.getpid()))
    return db, now, pid, apid


def test_hung_main_with_live_pid_kills_and_relaunches(tmp_path):
    ages = {a: 5 for a in health.AGENTS}
    ages.update(main=1900, publisher=1800, telegram=1950)
    db, now, pid, apid = _db_with_beats(tmp_path, ages)
    out = health.check(db, now=now, pid_path=pid, autopush_pid_path=apid)
    kills = [a for a in out["actions"] if a["type"] == "kill_relaunch_desk"]
    assert kills and out["hung"]
    assert not any(a["type"] == "restart_agent" for a in out["actions"])


def test_unconsumed_restart_request_means_hung(tmp_path):
    db, now, pid, apid = _db_with_beats(tmp_path, {a: 5 for a in health.AGENTS})
    db.set_setting(health.RESTART_KEY, ["publisher"])
    db.set_setting(health.RESTART_TS_KEY, now - 600)
    out = health.check(db, now=now, pid_path=pid, autopush_pid_path=apid)
    assert any(a["type"] == "kill_relaunch_desk" for a in out["actions"])


def test_kill_relaunch_never_kills_self(tmp_path):
    pid = tmp_path / "desk.pid"
    pid.write_text(str(os.getpid()))
    out = health.kill_relaunch_desk(None, pid_path=pid)
    assert out["killed"] is False and out["launched"] is False
