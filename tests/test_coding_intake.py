"""Unit tests for coding_agent task intake (human-gated, no auto-commit)."""

from __future__ import annotations

import json

import pytest

from agents.coding_agent.pending_approval import (
    INTAKE_KIND,
    ROOT_CAUSE_INTAKE,
    STATUS_NEEDS_HUMAN_REVIEW,
    PendingApprovalStore,
)


@pytest.fixture
def store(tmp_path):
    db = PendingApprovalStore(
        db_path=tmp_path / "coding_agent.db",
        enable_default_notifier=False,
    )
    yield db
    db.close()


def test_create_intake_task_queues_review_without_a_diff(store):
    entry = store.create_intake_task(
        task_ref="research:idea-1",
        summary="Struggle score on traces",
        request="Prototype this as a proof-of-concept branch.\nidea_id: idea-1",
        payload={"idea_id": "idea-1", "composite_score": 4.4},
        confidence=0.88,
    )
    assert entry.status == STATUS_NEEDS_HUMAN_REVIEW
    assert entry.root_cause == ROOT_CAUSE_INTAKE
    assert entry.diff is None
    assert entry.session_id == "research:idea-1"
    assert entry.confidence == pytest.approx(0.88)
    payload = json.loads(entry.test_results)
    assert payload["kind"] == INTAKE_KIND
    assert payload["summary"] == "Struggle score on traces"
    assert "proof-of-concept branch" in payload["request"]
    assert payload["idea_id"] == "idea-1"
    listed = store.list_by_status(STATUS_NEEDS_HUMAN_REVIEW)
    assert [item.entry_id for item in listed] == [entry.entry_id]


def test_create_intake_task_is_idempotent_for_the_same_task_ref(store):
    first = store.create_intake_task(
        task_ref="research:idea-1",
        summary="First",
        request="Prototype this as a proof-of-concept branch.",
    )
    second = store.create_intake_task(
        task_ref="research:idea-1",
        summary="Second",
        request="Prototype this as a proof-of-concept branch again.",
    )
    other = store.create_intake_task(
        task_ref="research:idea-2",
        summary="Other",
        request="Prototype this as a proof-of-concept branch.",
    )
    assert second.entry_id == first.entry_id
    assert other.entry_id != first.entry_id
    assert store.get_latest_by_session_id("research:idea-1").entry_id == first.entry_id


def test_create_intake_task_rejects_empty_fields(store):
    with pytest.raises(ValueError, match="task_ref"):
        store.create_intake_task(task_ref="  ", summary="t", request="r")
    with pytest.raises(ValueError, match="summary"):
        store.create_intake_task(task_ref="ref", summary=" ", request="r")
    with pytest.raises(ValueError, match="request"):
        store.create_intake_task(task_ref="ref", summary="t", request="")
