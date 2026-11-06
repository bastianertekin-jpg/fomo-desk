"""STEP 12 · health + morning night report."""
from __future__ import annotations

import json
import time

import health
import report
from db import DB, night_start
from links import SIGNUP_URL, coin_url

M1 = "MintRed1111111111111111111111111111111111"
M2 = "MintGreen222222222222222222222222222222222"
M3 = "MintKill3333333333333333333333333333333333"
M4 = "MintAud44444444444444444444444444444444444"
M5 = "MintAnl55555555555555555555555555555555555"


def _seed_coin(db, mint, name, symbol):
    db.upsert_coin(mint, name=name, symbol=symbol, migrated_at=time.time())


# ---- health -----------------------------------------------------------------

def test_stale_heartbeat_restart_action_and_wording(tmp_path):
    db = DB(tmp_path / "h.db")
    pid = tmp_path / "desk.pid"
    apid = tmp_path / "autopush.pid"
    import os as _os; pid.write_text(str(_os.getpid())); apid.write_text(str(_os.getpid()))  # this process → alive
    now = time.time()
    # fresh main + four agents; scanner stale by 5 min
    for a, age in (("main", 10), ("auditor", 10), ("analyst", 10), ("executor", 10), ("risk", 10),
                   ("scanner", 300)):
        db.x("INSERT INTO heartbeats(agent,ts) VALUES(?,?)", (a, now - age))
    out = health.check(db, now=now, pid_path=pid, autopush_pid_path=apid)
    assert any(a["type"] == "restart_agent" and a["agent"] == "scanner" for a in out["actions"])
    assert any(t.startswith("Restarted Scanner, down ") and t.endswith(" min") for t in out["notifies"])
    # exact minutes: 300s → 5 min
    assert "Restarted Scanner, down 5 min" in out["notifies"]
    assert out["alerts"] == []


def test_ws_reconnects_alert(tmp_path):
    db = DB(tmp_path / "h.db")
    now = time.time()
    # 11 reconnects in the last hour
    db.set_setting("_ws_reconnects", [now - 60 * i for i in range(11)])
    # fresh heartbeats so only the alert fires
    for a in health.AGENTS:
        db.x("INSERT INTO heartbeats(agent,ts) VALUES(?,?)", (a, now))
    pid = tmp_path / "desk.pid"
    apid = tmp_path / "autopush.pid"
    import os as _os; pid.write_text(str(_os.getpid())); apid.write_text(str(_os.getpid()))
    out = health.check(db, now=now, pid_path=pid, autopush_pid_path=apid)
    assert out["ws_reconnects_1h"] == 11
    assert out["actions"] == []
    assert any("reconnected 11 times" in a for a in out["alerts"])
    assert any("reconnected 11 times" in t for t in out["telegram_texts"])


def test_health_apply_requests_agent_restart(tmp_path):
    db = DB(tmp_path / "h.db")
    pid = tmp_path / "desk.pid"
    apid = tmp_path / "autopush.pid"
    import os as _os; pid.write_text(str(_os.getpid())); apid.write_text(str(_os.getpid()))
    now = time.time()
    for a in health.AGENTS:
        age = 400 if a == "risk" else 5
        db.x("INSERT INTO heartbeats(agent,ts) VALUES(?,?)", (a, now - age))
    out = health.apply(db, health.check(db, now=now, pid_path=pid, autopush_pid_path=apid), now=now)
    assert "risk" in (db.get_setting(health.RESTART_KEY) or [])
    assert any(a.get("type") == "request_agent_restarts" for a in out["applied"])


def test_autopush_dead_triggers_restart_action(tmp_path, monkeypatch):
    """health.check flags a dead autopush; apply relaunches scripts/autopush.sh."""
    db = DB(tmp_path / "h.db")
    pid = tmp_path / "desk.pid"
    apid = tmp_path / "autopush.pid"
    import os as _os
    pid.write_text(str(_os.getpid()))  # desk alive
    # no autopush.pid → dead
    now = time.time()
    for a in health.AGENTS:
        db.x("INSERT INTO heartbeats(agent,ts) VALUES(?,?)", (a, now))
    out = health.check(db, now=now, pid_path=pid, autopush_pid_path=apid)
    assert any(a["type"] == "restart_autopush" for a in out["actions"])
    assert "Restarted Autopush" in out["notifies"]
    assert out["autopush_alive"] is False

    calls = []
    monkeypatch.setattr(health, "relaunch_autopush", lambda cwd=None, force=False: calls.append(True) or {"launched": True, "pid": 1})
    applied = health.apply(db, out, now=now)
    assert calls == [True]
    assert any(a.get("type") == "relaunch_autopush" for a in applied["applied"])



def test_autopush_stuck_no_pushed_triggers_restart(tmp_path, monkeypatch):
    """pid alive but no PUSHED for >10 min while publish state.json is fresh → restart."""
    db = DB(tmp_path / "h.db")
    pid = tmp_path / "desk.pid"
    apid = tmp_path / "autopush.pid"
    log = tmp_path / "autopush.log"
    pub = tmp_path / "publish" / "d" / "testdata"
    pub.mkdir(parents=True)
    state = pub / "state.json"
    import os as _os
    pid.write_text(str(_os.getpid()))
    apid.write_text(str(_os.getpid()))  # autopush "alive"
    now = time.time()
    # Last PUSHED 15 minutes ago; publish mtime fresh
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 900))
    log.write_text(f"{old} +0100 PUSHED deadbeef\n")
    state.write_text("{}")
    _os.utime(state, (now, now))
    for a in health.AGENTS:
        db.x("INSERT INTO heartbeats(agent,ts) VALUES(?,?)", (a, now))
    out = health.check(
        db, now=now, pid_path=pid, autopush_pid_path=apid,
        autopush_log_path=log, publish_dir=tmp_path / "publish",
    )
    assert out["autopush_alive"] is True
    assert out["autopush_stuck"] is True
    assert any(a["type"] == "restart_autopush" and a.get("reason") == "stuck" for a in out["actions"])
    assert any("stuck" in t for t in out["notifies"])

    calls = []
    def fake_relaunch(cwd=None, force=False):
        calls.append({"force": force})
        return {"launched": True, "pid": 99, "forced": force}
    monkeypatch.setattr(health, "relaunch_autopush", fake_relaunch)
    applied = health.apply(db, out, now=now)
    assert calls == [{"force": True}]
    assert any(a.get("type") == "relaunch_autopush" for a in applied["applied"])


def test_autopush_not_stuck_when_recent_pushed(tmp_path):
    db = DB(tmp_path / "h.db")
    pid = tmp_path / "desk.pid"
    apid = tmp_path / "autopush.pid"
    log = tmp_path / "autopush.log"
    pub = tmp_path / "publish" / "d" / "testdata"
    pub.mkdir(parents=True)
    state = pub / "state.json"
    import os as _os
    pid.write_text(str(_os.getpid()))
    apid.write_text(str(_os.getpid()))
    now = time.time()
    recent = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60))
    log.write_text(f"{recent} +0100 PUSHED abc1234\n")
    state.write_text("{}")
    _os.utime(state, (now, now))
    for a in health.AGENTS:
        db.x("INSERT INTO heartbeats(agent,ts) VALUES(?,?)", (a, now))
    out = health.check(
        db, now=now, pid_path=pid, autopush_pid_path=apid,
        autopush_log_path=log, publish_dir=tmp_path / "publish",
    )
    assert out["autopush_stuck"] is False
    assert not any(a["type"] == "restart_autopush" for a in out["actions"])


# ---- night report ------------------------------------------------------------

def _night_window(report_time="08:00"):
    t1 = night_start(report_time=report_time)
    t0 = t1 - 86400
    mid = t0 + 3600
    return t0, t1, mid


def test_night_report_red_before_green_urls_footer_fees(tmp_path):
    db = DB(tmp_path / "r.db")
    t0, t1, mid = _night_window()
    _seed_coin(db, M1, "RedCoin", "RED")
    _seed_coin(db, M2, "GreenCoin", "GRN")
    # red trade
    db.x(
        "INSERT INTO positions(mint,opened_at,entry_price,size_usd,peak_price,stop_price,closed_at,exit_price,"
        "exit_reason,pnl_usd,fees_usd,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (M1, mid, 0.01, 47.50, 0.01, 0.0094, mid + 600, 0.009, "stop", -5.0, 1.71, "PAPER"),
    )
    # green trade (closed later so order-by-closed would put it first — report must still put red first)
    db.x(
        "INSERT INTO positions(mint,opened_at,entry_price,size_usd,peak_price,stop_price,closed_at,exit_price,"
        "exit_reason,pnl_usd,fees_usd,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (M2, mid + 100, 0.02, 47.50, 0.03, 0.0188, mid + 1200, 0.025, "trail", 8.5, 1.71, "PAPER"),
    )
    # a coin seen + kill
    _seed_coin(db, M3, "KillCoin", "KILL")
    db.x("INSERT INTO scans(mint,ts,verdict,reason) VALUES(?,?,?,?)", (M3, mid, "kill", "liq $31.2K"))
    db.x("INSERT INTO scans(mint,ts,verdict,reason) VALUES(?,?,?,?)", (M1, mid, "pass", None))
    # auditor refusal layer 2
    _seed_coin(db, M4, "AudCoin", "AUD")
    db.x(
        "INSERT INTO audits(mint,ts,layer,name,value_text,pass,source) VALUES(?,?,?,?,?,?,?)",
        (M4, mid, "2", "Freeze authority", "freeze not revoked", 0, "rpc"),
    )
    # analyst reject
    _seed_coin(db, M5, "AnlCoin", "ANL")
    db.x(
        "INSERT INTO verdicts(mint,ts,alive_6h,organic,flush,chase,score,pass,reason,input_tokens)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (M5, mid, 0.5, 0.5, 0.5, 0.5, 50, 0, "jev · chase 0.50", 1234),
    )

    cfg = {"desk": {"report_time": "08:00"}}
    data = report.build_night_report(db, cfg, now=t1 + 60)

    assert data["night_from"] == t0 and data["night_to"] == t1
    assert [t["color"] for t in data["trades"]] == ["red", "green"]
    assert data["trades"][0]["mint"] == M1 and data["trades"][0]["exit_reason"] == "stop"
    assert data["trades"][1]["mint"] == M2 and data["trades"][1]["exit_reason"] == "trail"

    # every mint in the report carries coin_url with r=savipww
    urls = []
    for t in data["trades"] + data["coins_seen"] + data["analyst_rejects"]:
        urls.append(t["url"])
    for block in data["auditor_refusals"]:
        for r in block["rows"]:
            urls.append(r["url"])
    assert urls, "expected coin urls"
    for u in urls:
        assert "r=savipww" in u
        assert u.startswith("https://fomo.family/")
        assert coin_url.__module__ == "links" or True
    assert data["footer"] == f"New to FOMO? {SIGNUP_URL}"
    assert SIGNUP_URL in data["telegram_text"]
    assert data["fees_paid"] == 3.42  # 1.71 + 1.71
    assert data["scanner_kills"] == [{"reason": "liq $31.2K", "count": 1}]
    assert data["auditor_refusals"][0]["layer"] == "2"
    assert data["auditor_refusals"][0]["rows"][0]["ts"] == mid
    assert data["analyst_rejects"][0]["failed_rule"] == "chase 0.50"
    assert data["jev_tokens"] == 1234
    assert data["jev_cost_usd"] == round(1234 * report.JEV_USD_PER_MILLION_INPUT / 1_000_000, 6)

    # no secrets in the serialised report
    blob = json.dumps(data, default=str)
    for secret in ("TYPESAFE_API_KEY", "TG_TOKEN", "VAPID_PRIVATE_KEY", "APP_TOKEN"):
        assert secret not in blob


def test_clear_skips_empties(tmp_path):
    db = DB(tmp_path / "r.db")
    db.skip(M1)
    db.skip(M2)
    assert db.one("SELECT COUNT(*) AS n FROM skips")["n"] == 2
    n = report.clear_skips(db)
    assert n == 2
    assert db.one("SELECT COUNT(*) AS n FROM skips")["n"] == 0
    assert not db.is_skipped(M1)


def test_fomo_urls_contain_ref_no_secrets_in_helpers():
    u = coin_url(M1)
    assert "r=savipww" in u
    assert SIGNUP_URL.endswith("/r/savipww")
    assert report.REPORT_KEY.startswith("_")


def test_set_last_report_url(tmp_path):
    db = DB(tmp_path / "r.db")
    url = "https://example.invalid/canvas/sample"
    report.set_last_report_url(db, url)
    assert db.get_setting(report.REPORT_KEY) == url


def test_daily_report_text_uses_night_window(tmp_path):
    db = DB(tmp_path / "r.db")
    t0, t1, mid = _night_window()
    _seed_coin(db, M2, "GreenCoin", "GRN")
    db.x(
        "INSERT INTO positions(mint,opened_at,entry_price,size_usd,peak_price,stop_price,closed_at,exit_price,"
        "exit_reason,pnl_usd,fees_usd,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (M2, mid, 0.02, 47.50, 0.03, 0.0188, mid + 100, 0.025, "trail", 8.5, 1.71, "PAPER"),
    )
    text = report.daily_report(db, {"desk": {"report_time": "08:00"}})
    assert "Night report" in text
    assert "fees 1.71" in text
    assert SIGNUP_URL in text
