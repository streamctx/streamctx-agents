"""Unit tests for the marketing pending_approval queue."""

from __future__ import annotations

import sqlite3

import pytest

from agents.marketing_agent.pending_approval import (
    MODE_DRAFT_ONLY,
    PLATFORM_LINKEDIN,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_REJECTED,
    PendingApprovalStore,
)


@pytest.fixture
def store(tmp_path):
    db = PendingApprovalStore(db_path=tmp_path / "marketing_agent.db")
    yield db
    db.close()


def test_pending_approval_table_schema(store, tmp_path):
    conn = sqlite3.connect(tmp_path / "marketing_agent.db")
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_approval'"
        ).fetchone()
        assert row is not None
        columns = {
            col[1] for col in conn.execute("PRAGMA table_info(pending_approval)").fetchall()
        }
        assert columns == {
            "entry_id",
            "platform",
            "content_type",
            "content",
            "target",
            "mode",
            "status",
            "created_at",
            "published_at",
        }
    finally:
        conn.close()


def test_create_entry_defaults_to_pending_draft_only(store):
    entry = store.create_entry(
        platform=PLATFORM_LINKEDIN,
        content_type="post",
        content="Draft LinkedIn post",
    )
    assert entry.status == STATUS_PENDING
    assert entry.mode == MODE_DRAFT_ONLY
    assert entry.published_at is None
    assert entry.target is None
    fetched = store.get_entry(entry.entry_id)
    assert fetched is not None
    assert fetched.content == "Draft LinkedIn post"


def test_create_entry_rejects_unknown_platform(store):
    with pytest.raises(ValueError, match="platform"):
        store.create_entry(platform="myspace", content_type="post", content="x")


def test_create_entry_rejects_empty_content(store):
    with pytest.raises(ValueError, match="empty"):
        store.create_entry(platform="hn", content_type="comment", content="   ")


def test_list_by_status_and_platform(store):
    store.create_entry(platform="hn", content_type="comment", content="hn draft", target="https://news.ycombinator.com/item?id=1")
    store.create_entry(platform="linkedin", content_type="post", content="li draft")
    pending = store.list_by_status(STATUS_PENDING)
    assert len(pending) == 2
    assert len(store.list_by_platform("hn")) == 1


def test_update_status_can_mark_published(store):
    entry = store.create_entry(platform="linkedin", content_type="post", content="x")
    updated = store.update_status(
        entry.entry_id,
        STATUS_PUBLISHED,
        published_at="2026-08-17T10:00:00+00:00",
    )
    assert updated is not None
    assert updated.status == STATUS_PUBLISHED
    assert updated.published_at == "2026-08-17T10:00:00+00:00"


def test_approve_and_reject(store):
    entry = store.create_entry(platform="twitter", content_type="post", content="tweet")
    approved = store.approve(entry.entry_id)
    assert approved.status == STATUS_APPROVED

    with pytest.raises(ValueError, match="cannot approve"):
        store.approve(entry.entry_id)

    rejected = store.reject(entry.entry_id)
    assert rejected.status == STATUS_REJECTED

    published = store.create_entry(platform="twitter", content_type="post", content="other")
    store.update_status(published.entry_id, STATUS_PUBLISHED, published_at="2026-08-17T00:00:00+00:00")
    with pytest.raises(ValueError, match="already-published"):
        store.reject(published.entry_id)
