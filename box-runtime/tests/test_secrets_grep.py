"""STEP 13.7 — secret values never appear in web/, logs/, the repo, publish/, or Pages JSON."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRETS = Path(os.environ.get("DESK_SECRETS", "/home/box/.desk-secrets"))
# Names the acceptance grep must empty-check
SECRET_NAMES = ("TYPESAFE_API_KEY", "TG_TOKEN", "VAPID_PRIVATE_KEY", "APP_TOKEN", "DATA_PATH")
SKIP_DIRS = {".venv", "_legacy", ".git", ".pytest_cache", "__pycache__"}
SKIP_FILES = {"test_secrets_grep.py", "IPHONE_LINK_FOR_USER.txt"}  # local private deep-link (gitignored); never committed
TEXT_SUFFIXES = {".py", ".js", ".html", ".css", ".json", ".md", ".txt", ".jsonl", ".svg", ".xml", ".yml", ".yaml", ".toml", ".sh", ".cfg", ".ini", ".log", ".url"}


def _secret_values() -> dict[str, str]:
    out = {}
    if not SECRETS.is_dir():
        pytest.skip(f"secrets dir missing: {SECRETS}")
    for name in SECRET_NAMES:
        p = SECRETS / name
        if p.is_file():
            v = p.read_text(encoding="utf-8", errors="ignore").strip()
            if v:
                out[name] = v
    if not out:
        pytest.skip("no secret values to grep")
    return out


def _iter_repo_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name in SKIP_FILES:
            continue
        # secrets dir itself is outside ROOT; publish/ is scanned (Plan B JSON)
        if p.suffix.lower() not in TEXT_SUFFIXES and p.name not in {"IPHONE_URL.txt", "serve.sh"}:
            # still scan extensionless text-ish files under logs/
            if "logs" not in p.parts:
                continue
        yield p


def test_secret_values_absent_from_repo_web_logs_publish():
    secrets = _secret_values()
    hits: list[tuple[str, str]] = []
    for p in _iter_repo_files():
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for name, val in secrets.items():
            if val and val in txt:
                hits.append((name, str(p.relative_to(ROOT))))
    assert hits == [], f"secret values leaked into: {hits}"


def test_iphone_url_file_has_no_token_or_data_path():
    """IPHONE_URL.txt in the desk tree must not carry APP_TOKEN or DATA_PATH values."""
    secrets = _secret_values()
    p = ROOT / "IPHONE_URL.txt"
    if not p.exists():
        return
    txt = p.read_text(encoding="utf-8", errors="ignore")
    for name in ("APP_TOKEN", "DATA_PATH"):
        val = secrets.get(name)
        if val:
            assert val not in txt, f"{name} value found in IPHONE_URL.txt"
