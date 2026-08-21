"""Tests for dashboard-brief → LinkedIn DraftJob (Path B)."""

from __future__ import annotations

import pytest

from agents.marketing_agent.adapters.linkedin import LinkedInAdapter
from agents.marketing_agent.draft_job import (
    create_brief_task,
    process_approved_brief,
)
from agents.marketing_agent.pending_approval import (
    BRIEF_FINGERPRINT_PREFIX,
    DRAFT_FINGERPRINT_PREFIX,
    PLATFORM_LINKEDIN,
    STATUS_DRAFT_FAILED,
    STATUS_PENDING,
    PendingApprovalStore,
    draft_child_fingerprint,
    is_marketing_brief,
)


GENERATED = (
    "The Roster tab now surfaces each agent's queue without a new scheduler.\n\n"
    "Assign still writes into the existing pending_approval tables, and a human "
    "has to approve before anything ships."
)


@pytest.fixture
def store(tmp_path):
    db = PendingApprovalStore(
        db_path=tmp_path / "marketing_agent.db",
        enable_default_notifier=False,
    )
    yield db
    db.close()


def test_create_brief_task_tags_fingerprint_and_keeps_request_text(store):
    entry = create_brief_task(
        "Draft a post about the new roster view.",
        store=store,
    )
    assert entry.platform == PLATFORM_LINKEDIN
    assert entry.content_type == "post"
    assert entry.status == STATUS_PENDING
    assert entry.content == "Draft a post about the new roster view."
    assert is_marketing_brief(entry) is True
    assert entry.source_fingerprint.startswith(BRIEF_FINGERPRINT_PREFIX)


def test_process_approved_brief_writes_pending_child_with_generated_copy(store):
    parent = create_brief_task("Draft a post about the new roster view.", store=store)
    calls = {"n": 0}

    def draft_fn(platform: str, context: str, goal: str) -> str:
        calls["n"] += 1
        assert platform == "linkedin_post"
        assert "roster view" in context
        assert goal == "Draft a post about the new roster view."
        return GENERATED

    result = process_approved_brief(
        parent.entry_id,
        approval_store=store,
        draft_fn=draft_fn,
        adapter=LinkedInAdapter(store),
    )
    assert result.success is True
    assert result.skipped is False
    assert result.child_entry is not None
    assert result.child_entry.status == STATUS_PENDING
    assert result.child_entry.content == GENERATED
    assert result.child_entry.content != parent.content
    assert result.child_entry.source_fingerprint == draft_child_fingerprint(
        parent.entry_id
    )
    assert result.child_entry.source_fingerprint.startswith(DRAFT_FINGERPRINT_PREFIX)
    assert is_marketing_brief(result.child_entry) is False
    assert calls["n"] == 1


def test_process_approved_brief_is_idempotent(store):
    parent = create_brief_task("Write about the Roster tab.", store=store)

    def draft_fn(platform: str, context: str, goal: str) -> str:
        return GENERATED

    first = process_approved_brief(
        parent.entry_id,
        approval_store=store,
        draft_fn=draft_fn,
        adapter=LinkedInAdapter(store),
    )
    second = process_approved_brief(
        parent.entry_id,
        approval_store=store,
        draft_fn=lambda *args: (_ for _ in ()).throw(AssertionError("no second LLM")),
        adapter=LinkedInAdapter(store),
    )
    assert second.skipped is True
    assert second.reason == "already_has_child"
    assert second.child_entry is not None
    assert first.child_entry is not None
    assert second.child_entry.entry_id == first.child_entry.entry_id
    pending = [
        row
        for row in store.list_by_status(STATUS_PENDING)
        if (row.source_fingerprint or "").startswith(DRAFT_FINGERPRINT_PREFIX)
    ]
    assert len(pending) == 1


def test_non_brief_linkedin_row_does_not_start_draft_job(store):
    entry = store.create_entry(
        platform=PLATFORM_LINKEDIN,
        content_type="post",
        content="Already-formatted changelog post.",
    )
    result = process_approved_brief(
        entry.entry_id,
        approval_store=store,
        draft_fn=lambda *args: (_ for _ in ()).throw(AssertionError("no LLM")),
    )
    assert result.skipped is True
    assert result.reason == "not_brief"
    assert result.child_entry is None


def test_generation_failure_writes_draft_failed_child(store):
    parent = create_brief_task("Draft a post about tokens.", store=store)

    def draft_fn(platform: str, context: str, goal: str) -> str:
        raise RuntimeError("openai down")

    result = process_approved_brief(
        parent.entry_id,
        approval_store=store,
        draft_fn=draft_fn,
        adapter=LinkedInAdapter(store),
    )
    assert result.success is False
    assert result.reason == STATUS_DRAFT_FAILED
    assert result.child_entry is not None
    assert result.child_entry.status == STATUS_DRAFT_FAILED
    assert "openai down" in result.child_entry.content
    assert store.list_by_status(STATUS_PENDING) == [parent]


def test_create_brief_task_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        create_brief_task("  ")
