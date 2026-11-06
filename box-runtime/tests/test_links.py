"""STEP 6 / STEP 13.1 — every FOMO URL goes through links.py and carries r=savipww."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import links
import notifier


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "_legacy", "logs", "specs", "tests", "__pycache__", ".git", "data", "publish"}


def test_constants():
    assert links.CHAIN_ID == {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bnb": 56}
    assert links.SIGNUP_URL == "https://fomo.family/r/savipww"


def test_coin_url_has_ref():
    u = links.coin_url("31SYoqWFhx3i9UsfnoXTjFp4ZwJA8F8UPrMxymfDpump")
    p = urlparse(u)
    q = parse_qs(p.query)
    assert p.scheme == "https" and p.netloc == "fomo.family" and p.path == "/coin"
    assert q["r"] == ["savipww"] and q["chainId"] == ["1399811149"]
    assert q["source"] == ["share_link"]
    assert q["address"] == ["31SYoqWFhx3i9UsfnoXTjFp4ZwJA8F8UPrMxymfDpump"]
    # never shortened / wrapped
    assert "t.co" not in u and "bit.ly" not in u and "tinyurl" not in u


def test_coin_url_unknown_chain():
    assert links.coin_url("x", chain="nope") == links.SIGNUP_URL


def test_profile_url_has_ref():
    u = links.profile_url("@dishhh")
    assert u == "https://fomo.family/profile/dishhh?source=share_link&r=savipww"


def test_notifier_buttons_use_links_exactly():
    mint = "31SYoqWFhx3i9UsfnoXTjFp4ZwJA8F8UPrMxymfDpump"
    want = links.coin_url(mint, "solana")
    buy = notifier.buy_buttons(mint)
    sell = notifier.sell_buttons(mint)
    assert buy[0] == {"text": "Buy on FOMO", "url": want}
    assert sell[0] == {"text": "Sell on FOMO", "url": want}
    # exact string identity — no rewrite
    assert buy[0]["url"] is not None and buy[0]["url"] == want


def test_no_fomo_urls_outside_links():
    offenders = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix not in {".py", ".js", ".html", ".css", ".json", ".md", ".txt"}:
            continue
        if p.name in {"links.py", "README.md", "IPHONE_URL.txt"}:
            continue
        # published runtime JSON may contain links.py output; check r=savipww below
        if "publish/d/" in str(p).replace("\\", "/"):
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "fomo.family/" in txt:
            offenders.append(str(p.relative_to(ROOT)))
    assert offenders == [], f"FOMO URL literals outside links.py: {offenders}"


def test_published_fomo_urls_carry_ref():
    """Plan B JSON may embed URLs, but every one must look like links.py output."""
    pub = ROOT / "publish"
    if not pub.exists():
        return
    bad = []
    for p in pub.rglob("*.json"):
        txt = p.read_text(encoding="utf-8", errors="ignore")
        i = 0
        while True:
            j = txt.find("https://fomo.family/", i)
            if j < 0:
                break
            k = j
            while k < len(txt) and txt[k] not in '"\' <>':
                k += 1
            url = txt[j:k]
            if "/r/savipww" in url:
                # signup form
                if url != links.SIGNUP_URL and not url.startswith(links.SIGNUP_URL):
                    # allow trailing path-less signup
                    if "r=savipww" not in url and "/r/savipww" not in url:
                        bad.append((str(p.relative_to(ROOT)), url))
            else:
                if "r=savipww" not in url:
                    bad.append((str(p.relative_to(ROOT)), url))
                if any(x in url for x in ("t.co/", "bit.ly/", "tinyurl.com")):
                    bad.append((str(p.relative_to(ROOT)), url))
            i = k
    assert bad == [], f"published FOMO URLs missing r=savipww or shortened: {bad}"

def test_account_and_report_fomo_urls_carry_ref(tmp_path, monkeypatch):
    """STEP 13.1: Account payload + night-report footer only emit links.py URLs with r=savipww."""
    import json

    import config
    from api import PlanBWriter
    from fakes import MINT, make_ctx
    from report import SIGNUP_URL, build_night_report, coin_url as report_coin_url

    sec = tmp_path / "secrets"
    sec.mkdir()
    for k, v in {"APP_TOKEN": "SANITIZED_TEST_VALUE", "DATA_PATH": "acctLinkDataPath_abcdefghijk",
                 "TG_TOKEN": "1:tg"}.items():
        (sec / k).write_text(v)
    monkeypatch.setattr(config, "SECRETS_DIR", sec)

    ctx = make_ctx(tmp_path)
    ctx.cfg["desk"]["fomo_handle"] = "dishhh"
    ctx.db.seed_paper(200.0)
    ctx.db.upsert_coin(MINT, name="Sphere", symbol="SPH")
    w = PlanBWriter(ctx, publish_dir=tmp_path / "publish", web_dir=config.WEB_DIR)
    w.publish_once()
    acct = json.loads((w.data_dir / "account.json").read_text())
    assert acct["profile_url"] == links.profile_url("dishhh")
    assert acct["signup_url"] == links.SIGNUP_URL == SIGNUP_URL
    assert acct["invite"]["url"] == links.SIGNUP_URL
    assert "r=savipww" in acct["profile_url"] and "/r/savipww" in acct["signup_url"]
    assert "t.co/" not in json.dumps(acct) and "bit.ly/" not in json.dumps(acct)

    # report helper shares links.py
    assert report_coin_url(MINT) == links.coin_url(MINT)
    rep = build_night_report(ctx.db, ctx.cfg)
    assert rep["footer"] == f"New to FOMO? {links.SIGNUP_URL}"
    assert rep["signup_url"] == links.SIGNUP_URL
