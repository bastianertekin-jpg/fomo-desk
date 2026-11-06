"""STEP 4 data model: every table/column exactly as specified."""
import sqlite3

import pytest

from db import DB

STEP4 = {
    "coins": ["mint", "name", "symbol", "pool", "migrated_at", "creator", "creator_balance_at_migration", "holders"],
    "scans": ["id", "mint", "ts", "liquidity_usd", "top10_pct", "h1_change_pct", "age_min", "verdict", "reason"],
    "audits": ["id", "mint", "ts", "layer", "name", "value_text", "pass", "source"],
    "verdicts": ["id", "mint", "ts", "alive_6h", "organic", "flush", "chase", "score", "pass", "reason", "input_tokens"],
    "signals": ["id", "mint", "ts", "price", "size_usd", "stack_pct", "link", "mode", "status"],
    "positions": ["id", "mint", "opened_at", "entry_price", "size_usd", "peak_price", "stop_price", "trail_active",
                  "closed_at", "exit_price", "exit_reason", "pnl_usd", "fees_usd", "mode"],
    "activity": ["id", "ts", "kind", "mint", "agent", "title", "subtitle", "value"],
    "prices": ["mint", "ts", "price", "volume_m5"],
    "balance": ["ts", "value"],
    "settings": ["key", "value", "updated_at"],
    "change_log": ["ts", "key", "old", "new", "who"],
    "skips": ["mint", "until"],
    "heartbeats": ["agent", "ts"],
    "push_subs": ["endpoint", "p256dh", "auth", "created_at"],
}


def test_schema_exact(tmp_path):
    db = DB(tmp_path / "s.db")
    tables = {r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert tables == set(STEP4)
    for t, cols in STEP4.items():
        assert [r["name"] for r in db.q(f"PRAGMA table_info({t})")] == cols, t
    assert db.one("PRAGMA journal_mode")["journal_mode"] == "wal"
    pk = {t: [r["name"] for r in db.q(f"PRAGMA table_info({t})") if r["pk"]] for t in ("coins", "settings", "push_subs")}
    assert pk == {"coins": ["mint"], "settings": ["key"], "push_subs": ["endpoint"]}


def test_activity_kind_enforced(tmp_path):
    db = DB(tmp_path / "s.db")
    with pytest.raises(ValueError):
        db.activity("candidate")
    with pytest.raises(sqlite3.IntegrityError):
        db.x("INSERT INTO activity(ts,kind) VALUES(1,'nope')")


def test_price_activity_roundtrip_and_settings_log(tmp_path):
    db = DB(tmp_path / "s.db")
    db.add_price("M1", 0.0012, 345.0)
    db.activity("bought", mint="M1", agent="executor", title="Bought TST", subtitle="Executor · 21:04", value=37.5)
    assert db.last_price("M1")["price"] == 0.0012
    assert db.one("SELECT kind,title,value FROM activity") == {"kind": "bought", "title": "Bought TST", "value": 37.5}
    db.set_setting("paused", True, who="telegram")
    assert db.one("SELECT key,old,new,who FROM change_log") == {"key": "paused", "old": None, "new": "true", "who": "telegram"}
    db.seed_paper(200.0)  # desk.paper_start_stack, USD
    assert db.last_balance()["value"] == 200.0 and db.cash_usd() == 200.0 and db.equity_usd() == 200.0
    db.skip("M1")
    assert db.is_skipped("M1") and not db.is_skipped("M2")


def test_legacy_db_refused(tmp_path):
    p = tmp_path / "old.db"
    sqlite3.connect(p).execute("CREATE TABLE snapshots(id INTEGER)")
    with pytest.raises(RuntimeError):
        DB(p)
