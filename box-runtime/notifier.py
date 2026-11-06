"""notifier.py — take URLs from links.py and put them on channel buttons (STEP 6).

Agents publish bus "notify" with optional mint / kind / buttons already carrying links.py
URLs. This module is the single helper that *builds* those button rows from a mint, so no
channel invents a FOMO URL. A channel without buttons gets the full URL as a text line.
Channels that can't carry a full URL are not allowed.
"""
from __future__ import annotations

from typing import Any

import links


def buy_buttons(mint: str, chain: str = "solana") -> list[dict[str, Any]]:
    """[Buy on FOMO] url + [Bought] / [Skip] callbacks. URL is exactly links.coin_url()."""
    return [
        {"text": "Buy on FOMO", "url": links.coin_url(mint, chain)},
        {"text": "Bought", "callback": "bought"},
        {"text": "Skip", "callback": "skip"},
    ]


def sell_buttons(mint: str, chain: str = "solana") -> list[dict[str, Any]]:
    """[Sell on FOMO] url + [Sold] callback."""
    return [
        {"text": "Sell on FOMO", "url": links.coin_url(mint, chain)},
        {"text": "Sold", "callback": "sold"},
    ]


def buy_link(mint: str, chain: str = "solana") -> dict[str, Any]:
    """[Buy on FOMO] only (STEP 9 "buy X" for a coin that's not on the desk: no Bought/Skip)."""
    return {"text": "Buy on FOMO", "url": links.coin_url(mint, chain)}


def sell_link(mint: str, chain: str = "solana", label: str | None = None) -> dict[str, Any]:
    """[Sell on FOMO] only (STEP 9 status: one per open position)."""
    return {"text": f"Sell on FOMO · {label}" if label else "Sell on FOMO", "url": links.coin_url(mint, chain)}


def with_url_fallback(text: str, mint: str | None = None, chain: str = "solana") -> str:
    """For channels without buttons (email, plain text): append the full links.py URL."""
    if not mint:
        return text
    url = links.coin_url(mint, chain)
    if url in text:
        return text
    return f"{text}\n{url}"


def signup_line() -> str:
    return links.SIGNUP_URL


def profile_line(handle: str | None) -> str | None:
    if not handle:
        return None
    return links.profile_url(handle)
