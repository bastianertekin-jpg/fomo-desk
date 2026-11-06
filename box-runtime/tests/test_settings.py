"""STEP 10 · SETTINGS: DEFAULTS literals, ranges/validate, ticket check, merge order, settings.json shape.
No network, no Telegram poll, no Jev, no FOMO orders."""
import asyncio
import json
import time

import pytest

import config
from agents.executor import Executor, ticket_check, ticket_text
from commands import Commands, coerce
from config import BOOLS, CHOICES, DEFAULTS, RANGES, STRINGS, get_path, leaf_keys, load_config, validate
from db import DB
from fakes import make_ctx

# STEP 10 literals, copied from the master (MASTER_PART_3 / specs/STEP10_SETTINGS.txt)
MASTER = {
    "scanner": {"min_liquidity_usd": 40000, "max_top10_pct": 25, "max_h1_change_pct": 80, "poll_s": 60,
                "min_pool_age_min": 10, "max_wait_min": 60, "runner_h24_pct": 300},
    "auditor": {"enabled": True, "max_dev_pct": 5, "max_top10_pct": 25, "min_lp_locked_pct": 99,
                "min_pool_age_min": 10, "max_bundled": 5, "max_delegated_accounts": 10, "retries": 3},
    "analyst": {"mode": "jev", "min_alive_6h": 0.70, "min_organic": 0.65, "max_chase": 0.30, "min_score": 70,
                "timeout_s": 10,
                "rules": {"alive_volume_h1": 25000, "organic_buys_h1": 150, "bundled_penalty": 0.5,
                          "bundled_penalty_from": 3, "flush_drop_m5_pct": 5, "chase_h1_pct": 80}},
    "executor": {"stack_pct": 8, "max_pool_pct": 2, "min_ticket_usd": 47.50, "below_min": "bump",
                 "recheck_liq_drop_pct": 20, "recheck_price_up_pct": 15, "confirm_window_min": 5},
    "risk": {"poll_s": 15, "stop_pct": 6, "trail_start_pct": 20, "trail_pct": 12, "timeout_h": 3,
             "timeout_min_gain_pct": 5, "confirm_window_min": 2},
    "desk": {"mode": "PAPER", "paper_start_stack": 200, "report_time": "08:00", "notifier": "telegram",
             "rpc_url": "https://api.mainnet-beta.solana.com", "push_rejects": False, "fees_ref_discount": 0.10,
             "fomo_handle": None},
}
E = DEFAULTS["executor"]


def test_defaults_match_master_literals():
    assert DEFAULTS == MASTER
    assert list(DEFAULTS) == ["scanner", "auditor", "analyst", "executor", "risk", "desk"]
    assert CHOICES["analyst.mode"] == ("jev", "rules") and CHOICES["executor.below_min"] == ("bump", "skip")
    assert CHOICES["desk.notifier"][0] == "telegram" and "webpush" in CHOICES["desk.notifier"]


def test_every_setting_has_a_rule_and_its_default_validates():
    for key in leaf_keys():
        assert key in RANGES or key in CHOICES or key in BOOLS or key in STRINGS, f"{key} has no min/max or shape"
        d = get_path(DEFAULTS, key)
        assert validate(key, d) == d, key
    for key in RANGES:
        if key != "live_stack":
            assert get_path(DEFAULTS, key) is not None, f"stray range {key}"
    for key, (lo, hi) in RANGES.items():
        assert lo < hi
        validate(key, lo), validate(key, hi), validate(key, (lo + hi) / 2)


@pytest.mark.parametrize("key", sorted(RANGES))
def test_out_of_range_refused_with_range_text(key):
    lo, hi = RANGES[key]
    for bad in (lo - 1, hi + 1, hi * 10 + 1):
        with pytest.raises(ValueError, match=rf"^{key} must be between {lo} and {hi}$"):
            validate(key, bad)
    with pytest.raises(ValueError, match=rf"^{key} must be a number between {lo} and {hi}$"):
        validate(key, "abc")
    with pytest.raises(ValueError):
        validate(key, float("nan"))


def test_choices_bools_and_strings_refuse_with_shape():
    with pytest.raises(ValueError, match="executor.below_min must be one of bump, skip"):
        validate("executor.below_min", "maybe")
    with pytest.raises(ValueError, match="analyst.mode must be one of jev, rules"):
        validate("analyst.mode", "gpt")
    assert validate("auditor.enabled", "off") is False and validate("desk.push_rejects", "on") is True
    with pytest.raises(ValueError, match="auditor.enabled must be on or off"):
        validate("auditor.enabled", "sometimes")
    assert validate("desk.report_time", "7:05") == "07:05"
    for bad in ("24:00", "08:60", "8am", "0800", ""):
        with pytest.raises(ValueError, match=r"desk.report_time must be HH:MM \(00:00 to 23:59\)"):
            validate("desk.report_time", bad)
    with pytest.raises(ValueError, match="desk.rpc_url must be an http"):
        validate("desk.rpc_url", "ftp://x")
    assert validate("desk.fomo_handle", "@dishhh") == "dishhh"
    with pytest.raises(ValueError):
        validate("desk.fomo_handle", "bad handle!")


def test_coerce_uses_validate_for_text_settings():
    assert coerce("desk.report_time", "9:30") == "09:30"
    assert coerce("auditor.enabled", "off") is False
    assert coerce("risk.stop_pct", "-8%") == 8
    with pytest.raises(ValueError, match="between 0.5 and 90"):
        coerce("risk.stop_pct", "-95%")


def test_ticket_check_min_stack_and_messages():
    min_stack, msg = ticket_check(200, E)
    assert min_stack == pytest.approx(593.75) == E["min_ticket_usd"] / (E["stack_pct"] / 100)
    assert msg == "tickets bumped to $47.50 = 23.8% of stack"
    assert ticket_check(200, {**E, "below_min": "skip"}) == (pytest.approx(593.75),
                                                            "every coin will be skipped, stack under $593.75")
    assert ticket_check(1000, E) == (pytest.approx(593.75), None)
    assert ticket_check(1000, {**E, "below_min": "skip"})[1] is None
    assert ticket_check(593.75, {**E, "below_min": "skip"})[1] is None  # exactly at min_stack is fine
    skip = ticket_text("every coin will be skipped, stack under $593.75", "PAPER · ")
    assert skip == ("PAPER · every coin will be skipped, stack under $593.75. "
                    "Switch to bump (set below_min to bump) or raise the stack.")


def test_night_start_never_silent_skip(tmp_path):
    ctx = make_ctx(tmp_path, executor__below_min="skip")
    n = ctx.bus.subscribe("notify")
    ex = Executor(ctx)
    assert ex.night_check() == "every coin will be skipped, stack under $593.75"
    push = n.get_nowait()
    assert push["kind"] == "ticket_check" and push["text"].startswith("PAPER · every coin will be skipped")
    assert "Switch to bump" in push["text"]
    hb = {h["agent"]: h for h in ctx.db.heartbeats()}["executor"]
    assert hb["status"] == "degraded" and "every coin skipped" in hb["detail"]
    tc = ctx.db.get_setting("_ticket_check")
    assert tc["min_stack"] == 593.75 and tc["stack"] == 200 and tc["below_min"] == "skip"
    # not in change_log (underscore key), not merged into cfg
    assert not ctx.db.one("SELECT 1 AS x FROM change_log WHERE key='_ticket_check'")
    assert "_ticket_check" not in ctx.db.get_settings()


def test_night_start_bump_tells_and_rich_stack_is_quiet(tmp_path):
    ctx = make_ctx(tmp_path)
    n = ctx.bus.subscribe("notify")
    assert Executor(ctx).night_check() == "tickets bumped to $47.50 = 23.8% of stack"
    assert n.get_nowait()["text"] == "PAPER · tickets bumped to $47.50 = 23.8% of stack"
    ctx2 = make_ctx(tmp_path / "b", desk__paper_start_stack=1000)
    n2 = ctx2.bus.subscribe("notify")
    assert Executor(ctx2).night_check() is None and n2.empty()


def test_run_does_ticket_check_before_first_coin(tmp_path):
    ctx = make_ctx(tmp_path, executor__below_min="skip")
    n = ctx.bus.subscribe("notify")
    ex = Executor(ctx)

    async def go():
        t = asyncio.create_task(ex.run())
        await asyncio.sleep(0.05)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(go())
    assert "every coin will be skipped" in n.get_nowait()["text"]


def test_commands_ticket_check_on_every_change(tmp_path):
    ctx = make_ctx(tmp_path)
    Executor(ctx)  # PAPER cash 200
    cmds = Commands(ctx)
    run = lambda t: asyncio.run(cmds.handle(t))[0].text  # noqa: E731
    t = run("set below_min to skip")
    assert t.startswith("below_min bump -> skip")
    assert "every coin will be skipped, stack under $593.75. Switch to bump (set below_min to bump) or raise the stack." in t
    assert "tickets bumped to $47.50" in run("set below_min to bump")
    assert "tickets bumped" in run("set ticket 50")
    t = run("set paper_stack 150")
    assert "PAPER · tickets bumped to $50.00 = 33.3% of stack" in t
    assert run("stack 5000").startswith("LIVE stack unset -> $5,000.00")
    assert "Refused: executor.stack_pct must be between 0.1 and 100" in run("set stack_pct 0")
    assert "Refused: desk.report_time must be HH:MM (00:00 to 23:59)" in run("set report_time 25:00")


def test_merge_order_db_wins_and_live_needs_gate(tmp_path, monkeypatch):
    sf = tmp_path / "settings.json"
    sf.write_text(json.dumps({"risk": {"stop_pct": 7, "trail_pct": 11}, "fomo_handle": "fromfile",
                              "desk.mode": "LIVE", "scanner": {"poll_s": 9999}}))
    monkeypatch.setattr(config, "SETTINGS_FILE", sf)
    db = DB(tmp_path / "t.db")
    db.set_setting("risk.stop_pct", 8, who="test")
    cfg = load_config(db)
    assert cfg["risk"]["stop_pct"] == 8           # db wins over settings.json
    assert cfg["risk"]["trail_pct"] == 11         # settings.json wins over DEFAULTS
    assert cfg["risk"]["timeout_h"] == 3          # DEFAULTS
    assert cfg["desk"]["fomo_handle"] == "fromfile"
    assert cfg["desk"]["mode"] == "PAPER"         # STEP 11: LIVE from a file without the typed-LIVE gate -> PAPER
    assert cfg["scanner"]["poll_s"] == 60         # out of range in the file -> refused, back to default


def test_settings_json_publish_shape(tmp_path, monkeypatch):
    sec = tmp_path / "secrets"
    sec.mkdir()
    secrets = {"APP_TOKEN": "SANITIZED_TEST_VALUE", "DATA_PATH": "testDataPath_abcdefghijklmnop",
               "TG_TOKEN": "SANITIZED_TEST_VALUE"}
    for k, v in secrets.items():
        (sec / k).write_text(v)
    monkeypatch.setattr(config, "SECRETS_DIR", sec)
    import api
    ctx = make_ctx(tmp_path, executor__below_min="skip")
    ctx.db.seed_paper(200.0)
    w = api.PlanBWriter(ctx, publish_dir=tmp_path / "publish", web_dir=config.WEB_DIR)
    w.publish_once()
    raw = (w.data_dir / "settings.json").read_text()
    s = json.loads(raw)
    assert s["groups"] == ["scanner", "auditor", "analyst", "executor", "risk", "desk"]
    assert set(s["settings"]) == set(DEFAULTS) and s["settings"]["analyst"]["rules"]["alive_volume_h1"] == 25000
    assert s["keys"] == leaf_keys() and "analyst.rules.chase_h1_pct" in s["keys"]
    assert s["ranges"]["risk.stop_pct"] == [0.5, 90] and s["choices"]["executor.below_min"] == ["bump", "skip"]
    assert set(s["bools"]) == set(BOOLS) and s["planb_readonly"] is True
    assert s["range_text"]["risk.stop_pct"] == "between 0.5 and 90"
    for key in s["keys"]:  # every key renderable: a range, a choice, a bool or a string shape
        assert key in s["ranges"] or key in s["choices"] or key in s["bools"] or key in s["strings"], key
    tc = s["ticket_check"]
    assert tc["min_stack_usd"] == 593.75 and tc["status"] == "skip" and tc["stack_usd"] == 200
    assert tc["msg"] == "every coin will be skipped, stack under $593.75"
    meta = json.loads((w.data_dir / "settings_meta.json").read_text())
    assert "analyst.rules.alive_volume_h1" in meta["keys"] and meta["planb_readonly"] is True
    acc = json.loads((w.data_dir / "account.json").read_text())
    assert acc["ticket_check"]["status"] == "skip"
    for v in secrets.values():
        assert v not in raw
        assert v not in json.dumps(meta) and v not in json.dumps(acc)


def test_account_ui_renders_every_key_readonly():
    js = (config.WEB_DIR / "app.js").read_text()
    assert "set.groups" in js and "set.ranges" in js and "set.choices" in js and "set.bools" in js
    assert 'type="range"' in js and " disabled " in js and "readonly" in js  # Plan B read-only
    assert "Ticket check" in js
    for k in ("min_liquidity_usd", "stop_pct", "below_min"):  # values come from the payload, not hard-coded
        assert k not in js.split("function setRow")[1].split("function renderAccount")[0]
