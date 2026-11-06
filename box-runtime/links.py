"""links.py — THE ONLY place FOMO URLs are built (STEP 6)."""
from __future__ import annotations

import logging

log = logging.getLogger("links")

CHAIN_ID = {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bnb": 56}
SIGNUP_URL = "https://fomo.family/r/savipww"


def coin_url(address: str, chain: str = "solana") -> str:
    if chain not in CHAIN_ID:
        log.warning("unknown chain %r for coin_url; returning SIGNUP_URL", chain)
        return SIGNUP_URL
    return (
        f"https://fomo.family/coin?address={address}"
        f"&chainId={CHAIN_ID[chain]}&r=savipww&source=share_link"
    )


def profile_url(handle: str) -> str:
    h = (handle or "").lstrip("@")
    return f"https://fomo.family/profile/{h}?source=share_link&r=savipww"
