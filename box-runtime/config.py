"""config.py — STEP 10 DEFAULTS (nested by agent), secrets loader, settings merge.

Merge order: DEFAULTS <- settings.json <- db settings table (db wins, so app/Telegram edits persist).
Overrides may be nested ({"risk": {"stop_pct": 8}}) or dotted ("risk.stop_pct": 8). A few legacy flat
keys from STEP 1/2 settings.json are mapped (analyst_mode -> analyst.mode, fomo_handle -> desk.fomo_handle,
rpc_url -> desk.rpc_url, notifier -> desk.notifier). Money is USD everywhere (stack, sizes, balance).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SECRETS_DIR = Path(os.environ.get("DESK_SECRETS_DIR", "/home/box/.desk-secrets"))
SETTINGS_FILE = ROOT / "settings.json"
DB_PATH = Path(os.environ.get("DESK_DB", ROOT / "data" / "desk.db"))
WEB_DIR = ROOT / "web"
PUBLISH_DIR = Path(os.environ.get("DESK_PUBLISH", ROOT / "publish"))
LOG_DIR = Path(os.environ.get("DESK_LOG_DIR", ROOT / "logs"))

SECRET_NAMES = ("TYPESAFE_API_KEY", "TG_TOKEN", "VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "APP_TOKEN", "DATA_PATH")

# ---- STEP 10, literal
DEFAULTS: dict = {
    "scanner": {
        "min_liquidity_usd": 40000, "max_top10_pct": 25, "max_h1_change_pct": 80, "poll_s": 60,
        "min_pool_age_min": 10, "max_wait_min": 60, "runner_h24_pct": 300,
    },
    "auditor": {
        "enabled": True, "max_dev_pct": 5, "max_top10_pct": 25, "min_lp_locked_pct": 99,
        "min_pool_age_min": 10, "max_bundled": 5, "max_delegated_accounts": 10, "retries": 3,
    },
    "analyst": {
        "mode": "jev", "min_alive_6h": 0.70, "min_organic": 0.65, "max_chase": 0.30, "min_score": 70,
        "timeout_s": 10,
        "rules": {"alive_volume_h1": 25000, "organic_buys_h1": 150, "bundled_penalty": 0.5,
                  "bundled_penalty_from": 3, "flush_drop_m5_pct": 5, "chase_h1_pct": 80},
    },
    "executor": {
        "stack_pct": 8, "max_pool_pct": 2, "min_ticket_usd": 47.50, "below_min": "bump",
        "recheck_liq_drop_pct": 20, "recheck_price_up_pct": 15, "confirm_window_min": 5,
    },
    "risk": {
        "poll_s": 15, "stop_pct": 6, "trail_start_pct": 20, "trail_pct": 12, "timeout_h": 3,
        "timeout_min_gain_pct": 5, "confirm_window_min": 2,
    },
    "desk": {
        "mode": "PAPER", "paper_start_stack": 200, "report_time": "08:00", "notifier": "telegram",
        "rpc_url": "https://api.mainnet-beta.solana.com", "push_rejects": False, "fees_ref_discount": 0.10,
        "fomo_handle": None,
    },
}

# Sane ranges (STEP 10 "out of range = refuse and say the range"). Choices for string settings.
RANGES: dict = {
    "scanner.min_liquidity_usd": (1000, 10_000_000), "scanner.max_top10_pct": (1, 100),
    "scanner.max_h1_change_pct": (1, 10_000), "scanner.poll_s": (10, 600), "scanner.min_pool_age_min": (0, 240),
    "scanner.max_wait_min": (5, 720), "scanner.runner_h24_pct": (10, 100_000),
    "auditor.max_dev_pct": (0, 100), "auditor.max_top10_pct": (1, 100), "auditor.min_lp_locked_pct": (0, 100),
    "auditor.min_pool_age_min": (0, 240), "auditor.max_bundled": (1, 1000),
    "auditor.max_delegated_accounts": (1, 10_000), "auditor.retries": (1, 10),
    "analyst.min_alive_6h": (0, 1), "analyst.min_organic": (0, 1), "analyst.max_chase": (0, 1),
    "analyst.min_score": (0, 100), "analyst.timeout_s": (1, 60),
    "analyst.rules.alive_volume_h1": (1, 10_000_000), "analyst.rules.organic_buys_h1": (1, 100_000),
    "analyst.rules.bundled_penalty": (0, 1), "analyst.rules.bundled_penalty_from": (1, 1000),
    "analyst.rules.flush_drop_m5_pct": (0, 100), "analyst.rules.chase_h1_pct": (1, 10_000),
    "executor.stack_pct": (0.1, 100), "executor.max_pool_pct": (0.1, 100), "executor.min_ticket_usd": (1, 100_000),
    "executor.recheck_liq_drop_pct": (1, 100), "executor.recheck_price_up_pct": (1, 1000),
    "executor.confirm_window_min": (1, 60),
    "risk.poll_s": (5, 300), "risk.stop_pct": (0.5, 90), "risk.trail_start_pct": (1, 1000), "risk.trail_pct": (0.5, 90),
    "risk.timeout_h": (0.1, 72), "risk.timeout_min_gain_pct": (-100, 1000), "risk.confirm_window_min": (1, 60),
    "desk.paper_start_stack": (10, 10_000_000), "desk.fees_ref_discount": (0, 1),
    "live_stack": (1, 10_000_000),  # STEP 9 "stack N" (USD, LIVE sizing; FOMO balance can't be read)
}
BOOLS = ("auditor.enabled", "desk.push_rejects")

# STEP 9 "set <name> to <value>": short names -> STEP 10 dotted keys. A full dotted key or a unique leaf
# name (e.g. trail_start_pct) also works; an ambiguous leaf (poll_s, max_top10_pct...) asks which.
ALIASES: dict = {
    "stop": "risk.stop_pct", "trail": "risk.trail_pct", "trail_start": "risk.trail_start_pct",
    "timeout": "risk.timeout_h", "timeout_gain": "risk.timeout_min_gain_pct", "risk_poll": "risk.poll_s",
    "sell_window": "risk.confirm_window_min",
    "stack_pct": "executor.stack_pct", "ticket": "executor.min_ticket_usd", "min_ticket": "executor.min_ticket_usd",
    "pool_cap": "executor.max_pool_pct", "max_pool": "executor.max_pool_pct", "below_min": "executor.below_min",
    "buy_window": "executor.confirm_window_min", "liq_drop": "executor.recheck_liq_drop_pct",
    "price_up": "executor.recheck_price_up_pct",
    "liquidity": "scanner.min_liquidity_usd", "min_liq": "scanner.min_liquidity_usd", "liq": "scanner.min_liquidity_usd",
    "top10": "scanner.max_top10_pct", "h1": "scanner.max_h1_change_pct", "pool_age": "scanner.min_pool_age_min",
    "max_wait": "scanner.max_wait_min", "runner": "scanner.runner_h24_pct", "scanner_poll": "scanner.poll_s",
    "auditor": "auditor.enabled", "dev": "auditor.max_dev_pct", "lp_locked": "auditor.min_lp_locked_pct",
    "bundled": "auditor.max_bundled", "bundles": "auditor.max_bundled", "delegations": "auditor.max_delegated_accounts",
    "audit_top10": "auditor.max_top10_pct", "audit_pool_age": "auditor.min_pool_age_min", "retries": "auditor.retries",
    "analyst": "analyst.mode", "analyst_mode": "analyst.mode", "score": "analyst.min_score",
    "min_score": "analyst.min_score", "alive": "analyst.min_alive_6h", "organic": "analyst.min_organic",
    "chase": "analyst.max_chase", "jev_timeout": "analyst.timeout_s",
    "report_time": "desk.report_time", "notifier": "desk.notifier", "rpc": "desk.rpc_url", "rpc_url": "desk.rpc_url",
    "push_rejects": "desk.push_rejects", "fees_discount": "desk.fees_ref_discount",
    "paper_stack": "desk.paper_start_stack", "handle": "desk.fomo_handle", "fomo_handle": "desk.fomo_handle",
}
# STEP 11: LIVE is reachable only through the switch gate (commands.Commands.confirm_live): at least 1 full PAPER
# night in the db (commands.paper_night_done) AND the owner typing LIVE. That gate writes db "_live_ok"; without it
# load_config keeps PAPER, whatever settings.json / the db / the app say for desk.mode. No auto LIVE.
CHOICES: dict = {
    "analyst.mode": ("jev", "rules"), "executor.below_min": ("bump", "skip"), "desk.mode": ("PAPER", "LIVE"),
    "desk.notifier": ("telegram", "webpush", "channel"),
}

# Runtime knobs that are not STEP 10 settings (process plumbing only).
RUNTIME: dict = {
    "owner_chat_id": None, "hosting": "planb", "publish_every_s": 15, "heartbeat_every_s": 30,
    "balance_every_s": 60, "api_price_every_s": 60, "sample_mint": None,
}

LEGACY_KEYS = {"analyst_mode": "analyst.mode", "fomo_handle": "desk.fomo_handle", "rpc_url": "desk.rpc_url",
               "notifier": "desk.notifier", "mode": "desk.mode"}

log = logging.getLogger("desk.config")


def read_secret(name: str) -> str | None:
    """Read a secret from SECRETS_DIR. Never log or print the value."""
    p = SECRETS_DIR / name
    try:
        v = p.read_text().strip()
        return v or None
    except FileNotFoundError:
        return None


def all_secret_values() -> list[str]:
    out = []
    for n in SECRET_NAMES:
        v = read_secret(n)
        if v and len(v) >= 8:
            out.append(v)
    return out


def load_settings_file() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def _set_path(d: dict, dotted: str, value):
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def get_path(d: dict, dotted: str, default=None):
    cur = d
    for p in dotted.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


STRINGS = ("desk.report_time", "desk.rpc_url", "desk.fomo_handle")  # free text, shape-checked in validate()
GROUPS = ("scanner", "auditor", "analyst", "executor", "risk", "desk")  # Account tab order (pipeline + desk)
_HHMM = re.compile(r"(\d{1,2}):(\d{2})")


def leaf_keys(d: dict | None = None, prefix: str = "") -> list[str]:
    """Every STEP 10 setting as a dotted key, nested ones included (analyst.rules.alive_volume_h1)."""
    out = []
    for k, v in (DEFAULTS if d is None else d).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out += leaf_keys(v, key + ".")
        else:
            out.append(key)
    return out


def _fmt_n(x) -> str:
    return f"{x:,g}" if isinstance(x, (int, float)) and abs(x) < 1e6 else f"{x:,.0f}"


def range_text(dotted: str) -> str:
    """Human range for refusals: 'between 0.5 and 90', 'one of bump, skip', 'on or off', 'HH:MM (00:00 to 23:59)'."""
    if dotted in CHOICES:
        return "one of " + ", ".join(CHOICES[dotted])
    if dotted in BOOLS:
        return "on or off"
    if dotted in RANGES:
        lo, hi = RANGES[dotted]
        return f"between {lo} and {hi}"
    if dotted == "desk.report_time":
        return "HH:MM (00:00 to 23:59)"
    if dotted == "desk.rpc_url":
        return "an http(s) URL"
    return "valid"


def validate(dotted: str, value):
    """STEP 10: return the coerced value or raise ValueError naming the range, e.g.
    'risk.stop_pct must be between 0.5 and 90'. Covers ranges, choices, bools, report_time, rpc_url, fomo_handle."""
    if dotted in CHOICES:
        if value not in CHOICES[dotted]:
            raise ValueError(f"{dotted} must be one of {', '.join(CHOICES[dotted])}")
        return value
    if dotted in BOOLS:
        if isinstance(value, bool):
            return value
        lo = str(value).strip().lower()
        if lo in ("on", "true", "yes", "1", "enable", "enabled"):
            return True
        if lo in ("off", "false", "no", "0", "disable", "disabled"):
            return False
        raise ValueError(f"{dotted} must be on or off")
    if dotted in RANGES:
        lo, hi = RANGES[dotted]
        if isinstance(value, bool):
            raise ValueError(f"{dotted} must be a number between {lo} and {hi}")
        try:
            v = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{dotted} must be a number between {lo} and {hi}") from e
        if v != v or not lo <= v <= hi:  # NaN never passes
            raise ValueError(f"{dotted} must be between {lo} and {hi}")
        return int(v) if float(v).is_integer() and isinstance(get_path(DEFAULTS, dotted), int) else v
    if dotted == "desk.report_time":
        m = _HHMM.fullmatch(str(value).strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError("desk.report_time must be HH:MM (00:00 to 23:59)")
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    if dotted == "desk.rpc_url":
        s = str(value or "").strip()
        if not s.startswith(("https://", "http://")) or " " in s:
            raise ValueError("desk.rpc_url must be an http(s) URL")
        return s
    if dotted == "desk.fomo_handle":
        if value is None:
            return None
        s = str(value).strip().lstrip("@")
        if s and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", s):
            raise ValueError("desk.fomo_handle must be a FOMO handle (letters, digits, _ . -)")
        return s or None
    return value


def sanitize(cfg: dict) -> list[str]:
    """Out-of-range values from settings.json / db never reach the agents: revert to DEFAULTS and log it."""
    bad = []
    for key in leaf_keys():
        v = get_path(cfg, key)
        if v is None and get_path(DEFAULTS, key) is None:
            continue
        try:
            nv = validate(key, v)
        except ValueError as e:
            bad.append(key)
            log.warning("setting refused, back to default: %s", e)
            nv = copy.deepcopy(get_path(DEFAULTS, key))
        _set_path(cfg, key, nv)
    return bad


def merge(cfg: dict, overrides: dict, top: bool = True):
    for k, v in (overrides or {}).items():
        if v is None:
            continue
        if top:
            k = LEGACY_KEYS.get(k, k)
        if "." in k:
            _set_path(cfg, k, v)
        elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
            merge(cfg[k], v, top=False)
        else:
            cfg[k] = v


class Config(dict):
    """cfg["risk"]["stop_pct"], cfg.path("risk.stop_pct"), cfg.paper."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:
            raise AttributeError(k) from e

    def path(self, dotted, default=None):
        return get_path(self, dotted, default)

    @property
    def paper(self) -> bool:
        return str(get_path(self, "desk.mode", "PAPER")).upper() != "LIVE"


def load_config(db=None, overrides: dict | None = None) -> Config:
    cfg = Config(copy.deepcopy(DEFAULTS))
    cfg.update(copy.deepcopy(RUNTIME))
    merge(cfg, load_settings_file())
    if db is not None:
        try:
            merge(cfg, db.get_settings())
        except Exception as e:  # db not ready
            log.debug("db settings unavailable: %s", type(e).__name__)
    merge(cfg, overrides or {})
    sanitize(cfg)  # STEP 10: every setting inside its range, else DEFAULTS (logged)
    # STEP 11 safety: desk.mode=LIVE counts only after the typed-LIVE gate wrote db "_live_ok" (never from a file
    # or a raw settings write). The desk places no FOMO orders in either mode.
    if str(cfg["desk"].get("mode", "PAPER")).upper() == "LIVE" and not live_confirmed(db):
        cfg["desk"]["mode"] = "PAPER"
    return cfg


def live_confirmed(db) -> bool:
    try:
        return db is not None and bool(db.live_ok())
    except Exception as e:  # db not ready -> PAPER
        log.debug("live marker unavailable: %s", type(e).__name__)
        return False


def test_config(**overrides) -> Config:
    """Pure DEFAULTS (+ dotted overrides), no settings.json/db. For tests and offline tools."""
    cfg = Config(copy.deepcopy(DEFAULTS))
    cfg.update(copy.deepcopy(RUNTIME))
    for k, v in overrides.items():
        _set_path(cfg, k.replace("__", "."), v)
    return cfg


class RedactFilter(logging.Filter):
    """Redact any secret value from every log record."""

    def __init__(self):
        super().__init__()
        self.secrets = all_secret_values()

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        hit = False
        for s in self.secrets:
            if s in msg:
                msg = msg.replace(s, "***")
                hit = True
        if hit:
            record.msg, record.args = msg, ()
        return True


def setup_logging(level=logging.INFO):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    f = RedactFilter()
    for h in (logging.StreamHandler(), logging.FileHandler(LOG_DIR / "desk.log")):
        h.setFormatter(fmt)
        h.addFilter(f)
        root.addHandler(h)
    # httpx logs full URLs (Telegram token lives in the URL) — keep it quiet
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_decision_loggers: dict[str, logging.Logger] = {}


def decision_log(agent: str) -> logging.Logger:
    """logs/{agent}.log — 1 line per decision (STEP 5 LOG sections). Secrets redacted."""
    if agent in _decision_loggers:
        return _decision_loggers[agent]
    lg = logging.getLogger(f"decisions.{agent}")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(LOG_DIR / f"{agent}.log")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        h.addFilter(RedactFilter())
        lg.addHandler(h)
    except OSError:
        lg.addHandler(logging.NullHandler())
    _decision_loggers[agent] = lg
    return lg
