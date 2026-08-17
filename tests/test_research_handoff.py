"""Unit tests for Stage 6 coding_agent prototype handoff."""

from __future__ import annotations

import json

import pytest

from agents.coding_agent.models import PendingApprovalEntry
from agents.coding_agent.pending_approval import (
    ROOT_CAUSE_INTAKE,
    STATUS_NEEDS_HUMAN_REVIEW,
    PendingApprovalStore,
)
from agents.research_agent.handoff import (
    format_prototype_request,
    research_task_ref,
    run_handoff,
)
from agents.research_agent.models import (
    CLASSIFICATION_FEATURE,
    CLASSIFICATION_NEW_PRODUCT,
    HYPE_TECHNICAL,
    SOURCE_TYPE_ARXIV,
    STATUS_DISMISSED,
    STATUS_IN_BACKLOG,
    STATUS_NEW,
    STATUS_PROTOTYPED,
    STATUS_REVIEWED,
    ResearchIdea,
)
from agents.research_agent.settings import ResearchConfig
from agents.research_agent.storage import ResearchStore


@pytest.fixture
def store(tmp_path):
    db = ResearchStore(db_path=tmp_path / "research_agent.db")
    yield db
    db.close()


@pytest.fixture
def coding(tmp_path):
    db = PendingApprovalStore(
        db_path=tmp_path / "coding_agent.db",
        enable_default_notifier=False,
    )
    yield db
    db.close()


def _idea(
    store,
    *,
    url: str,
    title: str,
    classification: str = CLASSIFICATION_FEATURE,
    feasibility: int = 4,
    composite: float = 4.4,
    status: str = STATUS_REVIEWED,
    detected_at: str = "2026-08-17T12:00:00+00:00",
):
    return store.insert_idea(
        source_url=url,
        source_type=SOURCE_TYPE_ARXIV,
        title=title,
        gap_description="Fits existing attribution traces.",
        feasibility_score=feasibility,
        pain_match_score=5,
        novelty_score=4,
        composite_score=composite,
        classification=classification,
        hype_label=HYPE_TECHNICAL,
        status=status,
        detected_at=detected_at,
    )


def test_format_prototype_request_names_the_poc_and_idea_id():
    idea = ResearchIdea(
        idea_id="abc",
        source_url="https://arxiv.org/abs/1",
        source_type=SOURCE_TYPE_ARXIV,
        title="Struggle score",
        gap_description="Flag silent success on traces.",
        feasibility_score=5,
        pain_match_score=5,
        novelty_score=4,
        composite_score=4.8,
        classification=CLASSIFICATION_FEATURE,
        status=STATUS_REVIEWED,
        detected_at="2026-08-17T12:00:00+00:00",
    )
    request = format_prototype_request(idea)
    assert request.startswith("Prototype this as a proof-of-concept branch.")
    assert "idea_id: abc" in request
    assert "Struggle score" in request
    assert "Flag silent success on traces." in request
    assert "feasibility=5" in request
    assert "https://arxiv.org/abs/1" in request


def test_run_handoff_queues_eligible_features_and_marks_prototyped(store, coding):
    eligible = _idea(
        store,
        url="https://arxiv.org/abs/ok",
        title="Struggle score on existing traces",
        status=STATUS_REVIEWED,
        detected_at="2026-08-17T12:00:00+00:00",
    )
    backlog = _idea(
        store,
        url="https://arxiv.org/abs/backlog",
        title="Belief lineage on checkpoints",
        status=STATUS_IN_BACKLOG,
        composite=4.1,
        detected_at="2026-08-17T11:00:00+00:00",
    )
    _idea(
        store,
        url="https://arxiv.org/abs/product",
        title="Standalone ledger",
        classification=CLASSIFICATION_NEW_PRODUCT,
        detected_at="2026-08-17T13:00:00+00:00",
    )
    _idea(
        store,
        url="https://arxiv.org/abs/hard",
        title="Needs a rewrite",
        feasibility=3,
        detected_at="2026-08-17T14:00:00+00:00",
    )
    _idea(
        store,
        url="https://arxiv.org/abs/mid",
        title="Okay but not high",
        composite=3.9,
        detected_at="2026-08-17T15:00:00+00:00",
    )
    _idea(
        store,
        url="https://arxiv.org/abs/gone",
        title="Dismissed feature",
        status=STATUS_DISMISSED,
        detected_at="2026-08-17T10:00:00+00:00",
    )
    already = _idea(
        store,
        url="https://arxiv.org/abs/done",
        title="Already prototyped",
        status=STATUS_PROTOTYPED,
        detected_at="2026-08-17T09:00:00+00:00",
    )

    result = run_handoff(store, config=ResearchConfig(), approval_store=coding)
    ids = [idea.idea_id for idea in result.handed_off]
    assert ids == [eligible.idea_id, backlog.idea_id]
    assert store.get_idea(eligible.idea_id).status == STATUS_PROTOTYPED
    assert store.get_idea(backlog.idea_id).status == STATUS_PROTOTYPED
    assert store.get_idea(already.idea_id).status == STATUS_PROTOTYPED
    assert result.errors == ()

    queued = coding.list_by_status(STATUS_NEEDS_HUMAN_REVIEW)
    refs = {item.session_id for item in queued}
    assert refs == {
        research_task_ref(eligible.idea_id),
        research_task_ref(backlog.idea_id),
    }
    payload = json.loads(queued[0].test_results)
    assert payload["kind"] == "intake"
    assert "proof-of-concept branch" in payload["request"]
    assert queued[0].root_cause == ROOT_CAUSE_INTAKE
    assert queued[0].diff is None


def test_run_handoff_honors_limit_and_does_not_duplicate(store, coding):
    first = _idea(
        store,
        url="https://arxiv.org/abs/a",
        title="High A",
        composite=4.8,
        detected_at="2026-08-17T12:00:00+00:00",
    )
    second = _idea(
        store,
        url="https://arxiv.org/abs/b",
        title="High B",
        composite=4.5,
        detected_at="2026-08-17T13:00:00+00:00",
    )
    spec = ResearchConfig()
    limited = run_handoff(store, config=spec, approval_store=coding, limit=1)
    assert [idea.idea_id for idea in limited.handed_off] == [first.idea_id]
    assert store.get_idea(second.idea_id).status == STATUS_REVIEWED

    again = run_handoff(store, config=spec, approval_store=coding)
    assert [idea.idea_id for idea in again.handed_off] == [second.idea_id]
    queued = coding.list_by_status(STATUS_NEEDS_HUMAN_REVIEW)
    assert len(queued) == 2

    empty = run_handoff(store, config=spec, approval_store=coding)
    assert empty.handed_off == ()
    assert len(coding.list_by_status(STATUS_NEEDS_HUMAN_REVIEW)) == 2


def test_run_handoff_leaves_status_when_intake_fails(store):
    idea = _idea(
        store,
        url="https://arxiv.org/abs/fail",
        title="Would be a feature",
        status=STATUS_NEW,
    )

    def boom(_idea) -> PendingApprovalEntry:
        raise RuntimeError("coding db locked")

    result = run_handoff(store, intake_fn=boom)
    assert result.handed_off == ()
    assert result.errors[0][0] == idea.idea_id
    assert store.get_idea(idea.idea_id).status == STATUS_NEW


def test_run_handoff_reuses_existing_intake_after_partial_failure(store, coding):
    idea = _idea(
        store,
        url="https://arxiv.org/abs/retry",
        title="Retry after crash",
        status=STATUS_REVIEWED,
    )
    existing = coding.create_intake_task(
        task_ref=research_task_ref(idea.idea_id),
        summary=idea.title,
        request=format_prototype_request(store.get_idea(idea.idea_id)),
    )
    result = run_handoff(store, approval_store=coding)
    assert [item.idea_id for item in result.handed_off] == [idea.idea_id]
    assert store.get_idea(idea.idea_id).status == STATUS_PROTOTYPED
    queued = coding.list_by_status(STATUS_NEEDS_HUMAN_REVIEW)
    assert [item.entry_id for item in queued] == [existing.entry_id]
