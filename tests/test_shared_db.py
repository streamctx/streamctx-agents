"""Tests for shared.db dual backend (SQLite default, Postgres when DATABASE_URL)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shared.db import (
    adapt_sql,
    connect,
    schema_for_path,
    uses_postgres,
)


def test_schema_for_path_maps_legacy_filenames():
    assert schema_for_path("~/.streamctx/coding_agent.db") == "coding"
    assert schema_for_path("leads.db") == "leads"
    assert schema_for_path("support_tickets.db") == "support_tickets"


def test_sqlite_backend_when_no_database_url(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert uses_postgres() is False
    db = tmp_path / "coding_agent.db"
    conn = connect(schema="coding", db_path=db)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_approval (
                entry_id TEXT PRIMARY KEY,
                status TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO pending_approval (entry_id, status) VALUES (?, ?)",
            ("a1", "pending"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT status FROM pending_approval WHERE entry_id = ?", ("a1",)
        ).fetchone()
        assert row["status"] == "pending"
        assert row[0] == "pending"
    finally:
        conn.close()


def test_upsert_sql_sqlite_and_postgres():
    from shared.db import upsert_sql

    sqlite_sql = upsert_sql("kv", ("key", "value"), conflict="key", postgres=False)
    assert "INSERT OR REPLACE" in sqlite_sql
    pg_sql = upsert_sql("kv", ("key", "value"), conflict="key", postgres=True)
    assert "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value" in pg_sql
    assert "INSERT OR REPLACE" not in pg_sql


def test_insert_or_replace_rewrite_triggers_on_original_sql():
    from shared.db import adapt_sql, _rewrite_insert_or_replace

    original = "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)"
    adapted = adapt_sql(original, postgres=True)
    assert "OR REPLACE" not in adapted.upper()
    rewritten = _rewrite_insert_or_replace(adapted)
    assert "ON CONFLICT (key)" in rewritten
    assert "EXCLUDED.value" in rewritten


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Set DATABASE_URL to run live Postgres store checks",
)
def test_postgres_coding_and_marketing_roundtrip(monkeypatch):
    assert uses_postgres()
    from agents.coding_agent.pending_approval import (
        STATUS_NEEDS_HUMAN_REVIEW,
        PendingApprovalStore as CodingStore,
    )
    from agents.marketing_agent.pending_approval import (
        CONTENT_POST,
        MODE_DRAFT_ONLY,
        PLATFORM_LINKEDIN,
        PendingApprovalStore as MarketingStore,
    )

    coding = CodingStore(enable_default_notifier=False)
    marketing = MarketingStore(enable_default_notifier=False)
    try:
        c = coding.create_entry(
            session_id="pg-test",
            root_cause="UNCLEAR",
            confidence=0.2,
            matched_pattern_id=None,
            diff=None,
            regression_test=None,
            test_results='{"failed_call_id": 1}',
            retries_used=0,
            status=STATUS_NEEDS_HUMAN_REVIEW,
        )
        assert coding.get_entry(c.entry_id) is not None
        coding.update_status(c.entry_id, "rejected")

        m = marketing.create_entry(
            platform=PLATFORM_LINKEDIN,
            content_type=CONTENT_POST,
            content="pg migration smoke",
            mode=MODE_DRAFT_ONLY,
        )
        assert marketing.get_entry(m.entry_id) is not None
        marketing.reject(m.entry_id)
    finally:
        coding.close()
        marketing.close()
