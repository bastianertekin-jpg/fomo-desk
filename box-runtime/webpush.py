"""webpush.py — Web Push is UNAVAILABLE in Plan B (static GitHub Pages, no server to store
subscriptions or receive them). Kept as a stub so Plan A can enable it later with pywebpush.
Notifications in Plan B go to Telegram only."""
from __future__ import annotations

AVAILABLE = False
REASON = "Plan B (static hosting): no endpoint to register push subscriptions. Telegram only."


def available() -> bool:
    return AVAILABLE


async def send(*_a, **_kw) -> bool:
    return False
