"""STEP 13.1b — PAPER push through every configured channel carries exact links.py URLs (r=savipww).

No live Telegram send, no web push, no FOMO orders. We render the same payload agents publish on
bus "notify" and assert each channel's formatter keeps links.coin_url() verbatim.
"""
from __future__ import annotations

import asyncio

import links
import notifier
import webpush
from agents.executor import Executor, push_text
from fakes import MINT, make_ctx, pair
from telegram import is_owner, keyboard


def test_owner_only():
    assert is_owner({"message": {"chat": {"id": 8163306310}, "text": "/status"}}, 8163306310)
    assert not is_owner({"message": {"chat": {"id": 1}, "text": "/status"}}, 8163306310)
    assert not is_owner({"message": {"chat": {}}}, 8163306310)
    assert not is_owner({"message": {"chat": {"id": 8163306310}}}, None)


def test_webpush_disabled_planb():
    assert webpush.available() is False


def test_configured_channels_are_telegram_only(tmp_path):
    """Plan B + settings: desk.notifier=telegram; webpush unavailable → only telegram is set up."""
    ctx = make_ctx(tmp_path)
    assert ctx.cfg["desk"]["notifier"] == "telegram"
    assert webpush.available() is False


def test_paper_push_telegram_channel_carries_exact_links_url(tmp_path):
    """1 PAPER Buy push through the telegram channel: every FOMO URL is exactly links.coin_url."""
    ctx = make_ctx(tmp_path)
    want = links.coin_url(MINT, "solana")
    assert "r=savipww" in want and "t.co" not in want and "bit.ly" not in want

    n = ctx.bus.subscribe("notify")
    m = pair(liquidity_usd=50000)
    o = asyncio.run(Executor(ctx).handle(
        {"mint": MINT, "score": 80, "ctx": {"name": "Project Sphere", "priceUsd": m["price_usd"],
                                           "scanner": {"liquidityUsd": 50000}}},
        market=m,
    ))
    assert o["status"] == "bought" and o["mode"] == "PAPER"
    push = n.get_nowait()
    assert push["text"].startswith("PAPER · Buy Project Sphere")
    assert push["kind"] == "buy" and push["mint"] == MINT
    assert push["buttons"] == notifier.buy_buttons(MINT)
    assert push["buttons"][0]["url"] == want

    # Telegram keyboard keeps the URL exact (no rewrite / shorten)
    kb = keyboard(push["buttons"], mint=MINT)
    url_btn = kb["inline_keyboard"][0][0]
    assert url_btn == {"text": "Buy on FOMO", "url": want}
    assert url_btn["url"] == links.coin_url(MINT)


def test_paper_push_text_channel_fallback_appends_links_url():
    """Plain-text / email channel: with_url_fallback appends the exact links.py URL."""
    want = links.coin_url(MINT)
    text = push_text(True, "Project Sphere", 47.5, 23.8, 80, 6, 12)
    assert text.startswith("PAPER · ")
    out = notifier.with_url_fallback(text, MINT)
    assert out.endswith("\n" + want)
    assert out.count("https://fomo.family/") == 1
    assert "r=savipww" in out


def test_sell_push_buttons_exact():
    want = links.coin_url(MINT)
    assert notifier.sell_buttons(MINT)[0] == {"text": "Sell on FOMO", "url": want}
    kb = keyboard(notifier.sell_buttons(MINT), mint=MINT)
    assert kb["inline_keyboard"][0][0]["url"] == want
