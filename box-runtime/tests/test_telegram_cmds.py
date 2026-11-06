"""STEP 9: commands + resolvers with fakes. No network, no Telegram poll, no Jev, no FOMO orders."""
import asyncio
import time

import links
from agents.executor import Executor
from commands import HELP, Commands, coerce, parse_callback, record_push, resolve, resolve_key, why_events
from fakes import MINT, make_ctx, pair
from telegram import Telegram, is_owner, keyboard

MINT2 = "So11111111111111111111111111111111111111112"
MINT3 = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


def run(cmds, text):
    return asyncio.run(cmds.handle(text))


def setup(tmp_path, **kw):
    ctx = make_ctx(tmp_path, **kw)
    ctx.db.upsert_coin(MINT, name="Project Sphere", symbol="SPHERE", pool="HmcVp73ePenjn2JDHQBws6sYN56aBCmPuGDGouqzNUok")
    ctx.db.upsert_coin(MINT2, name="Doge Two", symbol="DOGE")
    ctx.db.upsert_coin(MINT3, name="Doge Three", symbol="DOGE")
    return ctx, Commands(ctx)


def open_position(ctx, mint=MINT):
    ctx.db.x("INSERT INTO positions(mint,opened_at,entry_price,size_usd,peak_price,stop_price,mode) VALUES(?,?,?,?,?,?,?)",
             (mint, time.time(), 0.001, 47.5, 0.001, 0.00094, "PAPER"))


# ---- resolvers
def test_resolve_name_symbol_mint_prefix(tmp_path):
    ctx, _ = setup(tmp_path)
    assert [c["mint"] for c in resolve(ctx.db, "project sphere")] == [MINT]
    assert [c["mint"] for c in resolve(ctx.db, "$sphere")] == [MINT]
    assert [c["mint"] for c in resolve(ctx.db, MINT)] == [MINT]
    assert [c["mint"] for c in resolve(ctx.db, MINT[:6])] == [MINT]
    assert len(resolve(ctx.db, "DOGE")) == 2
    assert resolve(ctx.db, "nothinghere") == []


def test_resolve_key_aliases():
    assert resolve_key("stop") == ("risk.stop_pct", "stop")
    assert resolve_key("trail")[0] == "risk.trail_pct"
    assert resolve_key("stack_pct")[0] == "executor.stack_pct"
    assert resolve_key("trail start")[0] == "risk.trail_start_pct"
    assert resolve_key("risk.timeout_h")[0] == "risk.timeout_h"
    k, cands = resolve_key("poll_s")
    assert k is None and set(cands) == {"scanner.poll_s", "risk.poll_s"}
    assert coerce("risk.stop_pct", "-8%") == 8


# ---- buy
def test_buy_unknown_asks_for_mint(tmp_path):
    _, cmds = setup(tmp_path)
    r = run(cmds, "buy Nonexistent")
    assert len(r) == 1 and "Send me its mint" in r[0].text


def test_buy_multi_match_lists_coin_urls(tmp_path):
    _, cmds = setup(tmp_path)
    r = run(cmds, "/buy doge")[0]
    assert "which one" in r.text
    for m in (MINT2, MINT3):
        assert links.coin_url(m) in r.text and "r=savipww" in links.coin_url(m)


def test_buy_pushed_tonight_resends_push(tmp_path):
    ctx, cmds = setup(tmp_path)
    record_push(ctx.db, {"kind": "buy", "mint": MINT, "text": "PAPER · Buy Project Sphere · $47.50 · 23.8% of stack"})
    r = run(cmds, "buy sphere")[0]
    assert r.text.startswith("PAPER · Buy Project Sphere") and links.coin_url(MINT) in r.text
    assert r.buttons[0] == {"text": "Buy on FOMO", "url": links.coin_url(MINT)} and r.mint == MINT


def test_buy_pushed_tonight_from_signals_row(tmp_path):
    ctx, cmds = setup(tmp_path)
    ctx.db.add_signal(MINT, 0.001, 47.5, 23.8, links.coin_url(MINT), "PAPER", "bought")
    r = run(cmds, "buy SPHERE")[0]
    assert r.text.startswith("PAPER · Buy Project Sphere (SPHERE) · $47.50")
    assert r.buttons[0]["url"] == links.coin_url(MINT)


def test_buy_never_pushed_runs_auditor_read_only(tmp_path):
    ctx, cmds = setup(tmp_path)
    ctx.sources.pairs = {MINT: pair()}
    fills, cands, audited = ctx.bus.subscribe("fill"), ctx.bus.subscribe("candidate"), ctx.bus.subscribe("audited")
    r = run(cmds, f"buy {MINT}")[0]
    assert "not sized, not on the desk" in r.text and links.coin_url(MINT) in r.text
    for n in range(1, 9):
        assert f"\n{n} " in r.text
    assert r.buttons == [{"text": "Buy on FOMO", "url": links.coin_url(MINT)}]
    # nothing written, nothing on the desk, no order
    assert ctx.db.one("SELECT COUNT(*) n FROM audits")["n"] == 0
    assert ctx.db.one("SELECT COUNT(*) n FROM signals")["n"] == 0 and not ctx.db.open_positions()
    assert fills.empty() and cands.empty() and audited.empty()


# ---- sell
def test_sell_with_and_without_position(tmp_path):
    ctx, cmds = setup(tmp_path)
    r = run(cmds, "sell sphere")[0]
    assert r.text.startswith("No open position on Project Sphere") and links.coin_url(MINT) in r.text
    open_position(ctx)
    r = run(cmds, "sell sphere")[0]
    assert r.buttons[0] == {"text": "Sell on FOMO", "url": links.coin_url(MINT)}
    assert links.coin_url(MINT) in r.text and r.mint == MINT


# ---- why
def test_why_rejected_audit(tmp_path):
    ctx, cmds = setup(tmp_path)
    ctx.db.add_scan(MINT, "pass", None, liquidity_usd=50000)
    ctx.db.add_audit(MINT, "3", "Dev wallet", "dev sold", False, "rpc")
    ctx.db.add_audit(MINT, "4", "Top 10 holders", "not checked", None, None)
    r = run(cmds, "why Project Sphere")[0]
    assert r.text.startswith("Auditor stopped Project Sphere (SPHERE) at ")
    assert "rule: layer 3 · Dev wallet" in r.text and "value: dev sold" in r.text
    assert links.coin_url(MINT) in r.text
    assert why_events(ctx.db, MINT)[-1]["agent"] == "Auditor"


def test_why_scanner_kill_and_nothing(tmp_path):
    ctx, cmds = setup(tmp_path)
    assert "Nothing stopped" in run(cmds, "why sphere")[0].text
    ctx.db.add_scan(MINT, "kill", "liquidity under 40K", liquidity_usd=12000)
    t = run(cmds, "why sphere")[0].text
    assert "Scanner stopped" in t and "liquidity under 40K" in t and "liquidity $12,000" in t


# ---- set
def test_set_stop_validates_and_logs(tmp_path):
    ctx, cmds = setup(tmp_path)
    r = run(cmds, "set stop to -8%")[0]
    assert r.text == "stop -6% -> -8%"
    assert ctx.cfg["risk"]["stop_pct"] == 8 and ctx.db.get_setting("risk.stop_pct") == 8
    row = ctx.db.one("SELECT key,old,new,who FROM change_log WHERE key='risk.stop_pct'")
    assert row == {"key": "risk.stop_pct", "old": None, "new": "8", "who": "telegram"}
    r = run(cmds, "set stop to -95%")[0]
    assert r.text.startswith("Refused: risk.stop_pct must be between 0.5 and 90")
    assert ctx.cfg["risk"]["stop_pct"] == 8


def test_set_alias_trail_and_stack_pct_ticket_check(tmp_path):
    ctx, cmds = setup(tmp_path)
    Executor(ctx)  # seeds PAPER cash 200
    assert run(cmds, "set trail 10")[0].text == "trail 12% -> 10%"
    t = run(cmds, "set stack_pct to 10%")[0].text
    assert t.startswith("stack_pct 8% -> 10%") and "tickets bumped to $47.50" in t
    assert "ambiguous" in run(cmds, "set poll_s 30")[0].text


def test_stack_sets_live_stack(tmp_path):
    ctx, cmds = setup(tmp_path)
    t = run(cmds, "stack 1240")[0].text
    assert t.startswith("LIVE stack unset -> $1,240.00") and ctx.cfg["live_stack"] == 1240
    assert ctx.db.one("SELECT key FROM change_log WHERE key='live_stack'")
    assert "tickets bumped to $47.50 = 15.8% of stack" in run(cmds, "stack 300")[0].text


# ---- pause / resume
def test_pause_agent_and_risk_blocked_with_open_position(tmp_path):
    ctx, cmds = setup(tmp_path)
    assert "Scanner paused" in run(cmds, "pause Scanner")[0].text
    assert ctx.state["paused_agents"] == {"scanner"} and ctx.db.get_setting("paused_agents") == ["scanner"]
    assert "Scanner resumed" in run(cmds, "/resume scanner")[0].text and ctx.state["paused_agents"] == set()
    open_position(ctx)
    r = run(cmds, "pause Risk")[0]
    assert "Risk can't be paused while a position is open" in r.text and links.coin_url(MINT) in r.text
    assert "risk" not in ctx.state["paused_agents"]


def test_risk_paused_executor_skips(tmp_path):
    ctx, cmds = setup(tmp_path)
    ex = Executor(ctx)
    run(cmds, "pause risk")
    o = asyncio.run(ex.handle({"mint": MINT, "score": 80, "ctx": {}}, market=pair(liquidity_usd=50000)))
    assert o["status"] == "skipped" and o["reason"] == "risk paused" and not ctx.db.open_positions()


def test_agent_pause_property(tmp_path):
    from agents.scanner import Scanner
    ctx, cmds = setup(tmp_path)
    sc = Scanner(ctx)
    assert not sc.paused
    run(cmds, "pause scanner")
    assert sc.paused


# ---- status / modes / invite / report / help
def test_status_has_sell_button_per_position(tmp_path):
    ctx, cmds = setup(tmp_path)
    Executor(ctx)
    open_position(ctx)
    open_position(ctx, MINT2)
    r = run(cmds, "status")[0]
    assert r.text.startswith("PAPER desk") and "PAPER cash $200.00" in r.text
    assert links.coin_url(MINT) in r.text and links.coin_url(MINT2) in r.text
    assert [row[0]["url"] for row in r.buttons] == [links.coin_url(MINT), links.coin_url(MINT2)]
    assert all(row[0]["text"].startswith("Sell on FOMO") for row in r.buttons)


def test_live_needs_typed_LIVE_and_a_paper_night(tmp_path):
    ctx, cmds = setup(tmp_path)
    assert run(cmds, "live")[0].text.startswith("Telegram cannot activate LIVE")
    assert run(cmds, "LIVE")[0].text.startswith("Telegram cannot activate LIVE")
    assert ctx.cfg["desk"]["mode"] == "PAPER"
    ctx.state["live_pending"] = 1  # a stale prompt must not confirm from Telegram
    assert run(cmds, "LIVE")[0].text.startswith("Telegram cannot activate LIVE")
    assert ctx.cfg["desk"]["mode"] == "PAPER"
    assert run(cmds, "paper")[0].text == "Already PAPER."


def test_invite_report_unknown(tmp_path):
    ctx, cmds = setup(tmp_path)
    assert links.SIGNUP_URL in run(cmds, "invite")[0].text
    assert run(cmds, "/report")[0].text == "no report yet"
    ctx.db.set_setting("_last_report_url", "https://example.org/page")
    assert "https://example.org/page" in run(cmds, "report")[0].text
    r = run(cmds, "hello there desk")[0]
    assert HELP in r.text and "buy X" in r.text and "set stop to -8%" in r.text


# ---- callbacks / telegram glue
def test_callback_publishes_control(tmp_path):
    ctx, cmds = setup(tmp_path)
    q = ctx.bus.subscribe("control")
    assert cmds.callback(f"bought:{MINT}") == "Bought ✓"
    assert q.get_nowait() == {"cmd": "bought", "mint": MINT}
    msg = {"reply_markup": {"inline_keyboard": [[{"text": "Sell on FOMO", "url": links.coin_url(MINT2)}]]}}
    cmds.callback("sold", msg)
    assert q.get_nowait() == {"cmd": "sold", "mint": MINT2}
    assert parse_callback("nope:x") is None


def test_keyboard_appends_mint_and_rows():
    from notifier import buy_buttons
    kb = keyboard(buy_buttons(MINT), MINT)["inline_keyboard"]
    assert kb[0][0]["url"] == links.coin_url(MINT)
    assert kb[0][1]["callback_data"] == f"bought:{MINT}" and len(kb[0][2]["callback_data"]) <= 64
    rows = keyboard([[{"text": "a", "url": "u1"}], [{"text": "b", "url": "u2"}]])["inline_keyboard"]
    assert len(rows) == 2


def test_owner_only_callbacks():
    assert is_owner({"callback_query": {"message": {"chat": {"id": 5}}, "data": "bought"}}, 5)
    assert not is_owner({"callback_query": {"message": {"chat": {"id": 6}}, "data": "bought"}}, 5)


def test_telegram_dispatch_plain_text_and_callback(tmp_path, monkeypatch):
    ctx, _ = setup(tmp_path, owner_chat_id=5)
    tg = Telegram(ctx)
    sent, calls = [], []

    async def fake_send(text, buttons=None, mint=None):
        sent.append((text, buttons, mint))
        return {}

    async def fake_call(method, **params):
        calls.append((method, params))
        return {}
    monkeypatch.setattr(tg, "send", fake_send)
    monkeypatch.setattr(tg, "call", fake_call)
    q = ctx.bus.subscribe("control")
    asyncio.run(tg.dispatch({"message": {"chat": {"id": 5}, "text": "invite"}}))
    asyncio.run(tg.dispatch({"message": {"chat": {"id": 9}, "text": "invite"}}))  # not owner: ignored
    asyncio.run(tg.dispatch({"callback_query": {"id": "1", "data": f"skip:{MINT}", "message": {"chat": {"id": 5}}}}))
    assert len(sent) == 1 and links.SIGNUP_URL in sent[0][0]
    assert q.get_nowait() == {"cmd": "skip", "mint": MINT}
    assert calls[0][0] == "answerCallbackQuery"
