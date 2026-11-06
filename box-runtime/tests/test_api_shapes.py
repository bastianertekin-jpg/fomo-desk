"""STEP 7 (Plan B): every GET shape published as JSON from a temp DB with seed data."""
import hashlib
import json
import time

import pytest

import config
from fakes import MINT, make_ctx

APP_TOKEN = "SANITIZED_TEST_VALUE"
DATA_PATH = "testDataPath_abcdefghijklmnop"
TG_TOKEN = "SANITIZED_TEST_VALUE"
MINT2 = "So11111111111111111111111111111111111111112"


@pytest.fixture
def writer(tmp_path, monkeypatch):
    sec = tmp_path / "secrets"
    sec.mkdir()
    for k, v in {"APP_TOKEN": APP_TOKEN, "DATA_PATH": DATA_PATH, "TG_TOKEN": TG_TOKEN}.items():
        (sec / k).write_text(v)
    monkeypatch.setattr(config, "SECRETS_DIR", sec)
    import api
    ctx = make_ctx(tmp_path)
    ctx.cfg["desk"]["fomo_handle"] = "@bastian"
    db = ctx.db
    now = time.time()
    db.seed_paper(200.0)
    db.upsert_coin(MINT, name="Project Sphere", symbol="SPHR", migrated_at=now - 3 * 3600, holders=420)
    db.upsert_coin(MINT2, name="Rug", symbol="RUG", migrated_at=now - 1800)
    db.add_scan(MINT, "pass", None, liquidity_usd=50000, top10_pct=12, h1_change_pct=10, age_min=30)
    db.add_scan(MINT2, "kill", "liquidity $3K < $40K")
    for n in range(1, 9):
        db.add_audit(MINT, str(n), f"L{n}", f"v{n}", True, "rpc", ts=now - 600)
    db.add_audit(MINT2, "1", "Mint authority", "Revoked", True, "rpc", ts=now - 500)
    db.add_audit(MINT2, "2", "Freeze authority", "not revoked", False, "rpc", ts=now - 500)
    db.add_verdict(MINT, {"alive_6h": .9, "organic": .8, "flush": .7, "chase": .1}, 82, True, "rules · ok")
    db.add_signal(MINT, 0.001, 16.0, 8, None, "PAPER", "bought")
    for i in range(40):  # 1m candles + volume delta
        db.add_price(MINT, 0.001 * (1 + i / 100), volume_m5=1000 + i * 10, ts=now - 2400 + i * 60)
    db.x("INSERT INTO positions(mint,opened_at,entry_price,size_usd,peak_price,stop_price,mode) VALUES(?,?,?,?,?,?,?)",
         (MINT, now - 900, 0.001, 16.0, 0.0012, 0.00094, "PAPER"))
    db.set_cash_usd(184.0)
    db.activity("bought", MINT, "executor", "Bought SPHR", "Executor · 12:00", 16.0)
    db.activity("rejected", MINT2, "auditor", "Rejected RUG", "Auditor · freeze not revoked")
    for i in range(45):
        db.activity("rejected", MINT2, "scanner", f"Rejected R{i}", "Scanner · liq")
    db.activity("stopped", MINT, "risk", "Stopped SPHR", "Risk · -6% stop", -1.2)
    db.add_balance(198.8)
    w = api.PlanBWriter(ctx, publish_dir=tmp_path / "publish", web_dir=config.WEB_DIR)
    w.publish_once()
    return w


def load(w, rel):
    return json.loads((w.data_dir / rel).read_text())


def all_urls(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("fomo_url", "coin_url", "profile_url", "signup_url", "SIGNUP_URL", "url") and v:
                yield v
            yield from all_urls(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from all_urls(v)


def test_state_shape(writer):
    st = load(writer, "state.json")
    b = st["balance"]
    for k in ("cash", "equity", "start", "today_pct", "pnl", "multiplier", "equity_usd", "pnl_pct"):
        assert k in b, k
    assert b["multiplier"] == pytest.approx(b["equity"] / b["start"], rel=1e-3)
    assert st["mode"] == "PAPER" and "paused" in st and st["risk_pills"]["stop"] == "stop -6%"
    assert [a["agent"] for a in st["agents"]] == ["scanner", "auditor", "analyst", "executor", "risk"]
    assert st["gate"] == hashlib.sha256(("fomo-desk-gate:" + APP_TOKEN).encode()).hexdigest()
    p = st["positions"][0]
    assert p["fomo_url"] == p["coin_url"] and "exit_line" in p and "timeout_remaining_s" in p


def test_chart_ranges_and_marks(writer):
    ch = load(writer, "chart.json")
    assert ch["range"] == "LIVE" and ch["points"] and ch["equity"] == ch["points"]
    for r in ("LIVE", "1H", "4H", "1D", "1W", "ALL"):
        c = load(writer, f"chart/{r}.json")
        assert c["range"] == r and isinstance(c["points"], list)
    labels = {m["kind"]: m for m in load(writer, "chart/ALL.json")["marks"]}
    assert labels["bought"]["label"] == "Bought SPHR" and labels["bought"]["color"] == "#0a84ff"
    assert labels["stopped"]["label"] == "-6% · -$1.20" and labels["stopped"]["color"] == "#ff453a"
    with pytest.raises(ValueError):
        writer.build_chart("2H")


def test_activity_pages(writer):
    a = load(writer, "activity.json")
    assert a["page_size"] == 30 and len(a["items"]) == 30 and a["total"] == 48
    i = a["items"][0]
    for k in ("ts", "kind", "mint", "agent", "title", "subtitle", "value", "right", "fomo_url", "msg"):
        assert k in i, k
    assert i["kind"] == "stopped"  # newest first
    p1 = load(writer, a["next"])
    assert len(p1["items"]) == 18 and p1["next_before"] is None
    assert all(x["ts"] <= a["next_before"] for x in p1["items"])


def test_coin_audit_decisions(writer):
    c = load(writer, f"coin/{MINT}.json")
    for k in ("price", "candles", "stats", "position", "exit", "coin_url", "fomo_url", "audit", "analysis", "series"):
        assert k in c, k
    cd = c["candles"][1]
    assert set(cd) >= {"ts", "o", "h", "l", "c", "volume"} and cd["volume"] == 10
    assert c["stats"]["holders"] == 420 and c["button"]["text"] == "Sell on FOMO"
    au = load(writer, f"audit/{MINT2}.json")
    assert len(au["layers"]) == 8 and [x["n"] for x in au["layers"]] == list(range(1, 9))
    assert au["layers"][1]["pass"] is False and au["layers"][5]["status"] == "not_checked"
    for x in au["layers"]:
        assert {"n", "name", "value", "value_text", "pass", "source"} <= set(x)
    assert au["result"]["title"].startswith("Rejected")
    assert load(writer, f"audit/{MINT}.json")["result"]["title"] == "Cleared · 8/8 parts"
    for a in ("scanner", "auditor", "analyst", "executor", "risk"):
        d = load(writer, f"agents/{a}/decisions.json")
        assert len(d["items"]) <= 20
        for it in d["items"]:
            assert {"time", "coin", "verdict", "reason", "coin_url"} <= set(it)


def test_account_settings(writer):
    ac = load(writer, "account.json")
    assert ac["mode"] == "PAPER" and ac["stack"]["paper_start_stack"] == 200
    assert ac["profile_url"].startswith("https://fomo.family/profile/bastian") and ac["SIGNUP_URL"].endswith("/r/savipww")
    assert "push" in ac and ac["push"]["webpush_available"] is False
    s = load(writer, "settings.json")
    assert set(s["settings"]) == set(config.DEFAULTS) and s["settings"]["risk"]["stop_pct"] == 6
    assert load(writer, "settings_meta.json")["planb_readonly"] is True
    assert "events" in load(writer, "stream/last.json")


def test_links_and_no_secrets(writer):
    blobs = [p.read_text() for p in writer.data_dir.rglob("*.json")]
    assert len(blobs) > 15
    for b in blobs:
        for s in (APP_TOKEN, DATA_PATH, TG_TOKEN):
            assert s not in b
        for u in all_urls(json.loads(b)):
            if "fomo.family" in u:
                assert "r=savipww" in u or u.endswith("/r/savipww"), u
    assert not list((writer.root / "d").glob("*.json"))  # nothing at bare d/


def test_secret_guard_refuses(writer):
    import api
    with pytest.raises(api.SecretLeak):
        writer.write_json("x.json", {"oops": APP_TOKEN})
