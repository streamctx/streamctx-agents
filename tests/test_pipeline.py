"""Integration tests for the top-level coding-agent pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agents.coding_agent.coding_agent import run as coding_agent_run
from agents.coding_agent.fix_generator import FixGenerator
from agents.coding_agent.models import FixProposal, FixRequest
from agents.coding_agent.pending_approval import (
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    PendingApprovalStore,
)
from agents.coding_agent.pipeline import CodingAgentPipeline
from tests.test_diagnose import _seed_drift_failure
from tests.test_fix_loop import FIX_DIFF, REGRESSION_TEST

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "sandbox_project"

@pytest.fixture
def pipeline(tmp_path, storage):
    db_path = tmp_path / "agent.db"

    def llm_fn(request: FixRequest) -> FixProposal:
        return FixProposal(
            diff=FIX_DIFF,
            regression_test=REGRESSION_TEST,
            regression_test_path=f"tests/test_regression_session_{request.diagnosis.session_id}.py",
        )

    with CodingAgentPipeline(
        storage=storage,
        source_root=FIXTURE_ROOT,
        db_path=db_path,
        fix_generator=FixGenerator(llm_fn=llm_fn),
        enable_notifications=False,
    ) as pipe:
        yield pipe


def test_pipeline_run_empty_when_no_failures(pipeline):
    result = pipeline.run()
    assert result.failures_detected == 0
    assert result.blocked_for_review == 0
    assert result.fixes_ready == 0


def test_pipeline_run_blocks_low_confidence(pipeline, storage):
    _seed_drift_failure(storage)
    with patch(
        "agents.coding_agent.confidence_gate.MIN_CONFIDENCE_FOR_AUTO_FIX",
        0.99,
    ):
        result = pipeline.run()

    assert result.failures_detected == 1
    assert result.blocked_for_review == 1
    assert result.fixes_ready == 0
    assert pipeline.approval_store.list_by_status(STATUS_NEEDS_HUMAN_REVIEW)


def test_pipeline_run_produces_ready_for_approval(pipeline, storage):
    _seed_drift_failure(storage)
    result = pipeline.run()

    assert result.failures_detected == 1
    assert result.fixes_ready == 1
    assert result.fixes_failed == 0
    ready = pipeline.approval_store.list_by_status(STATUS_READY_FOR_APPROVAL)
    assert len(ready) == 1
    assert ready[0].diff == FIX_DIFF


def test_pipeline_approve_and_reject(pipeline, storage):
    _seed_drift_failure(storage)
    pipeline.run()
    entry = pipeline.approval_store.list_by_status(STATUS_READY_FOR_APPROVAL)[0]

    pattern = pipeline.approve(entry.entry_id, applied_commit="cafebabe")
    assert pattern.success_count == 1
    assert pipeline.approval_store.get_entry(entry.entry_id).applied_commit == "cafebabe"

    entry2 = pipeline.approval_store.create_entry(
        session_id=entry.session_id,
        root_cause=entry.root_cause,
        confidence=entry.confidence,
        matched_pattern_id=entry.matched_pattern_id,
        diff=entry.diff,
        regression_test=entry.regression_test,
        test_results=entry.test_results,
        retries_used=0,
        status=STATUS_READY_FOR_APPROVAL,
    )
    rejected = pipeline.reject(entry2.entry_id, reason="Not safe enough.")
    assert rejected.reject_count == 1
    assert rejected.last_rejection_reason == "Not safe enough."


def test_coding_agent_run_twice_does_not_duplicate_pending(tmp_path, storage):
    _seed_drift_failure(storage)
    db_path = tmp_path / "agent.db"

    def llm_fn(request: FixRequest) -> FixProposal:
        return FixProposal(
            diff=FIX_DIFF,
            regression_test=REGRESSION_TEST,
            regression_test_path=f"tests/test_regression_session_{request.diagnosis.session_id}.py",
        )

    first = coding_agent_run(
        source_root=str(FIXTURE_ROOT),
        enable_notifications=False,
        storage=storage,
        db_path=db_path,
        fix_generator=FixGenerator(llm_fn=llm_fn),
    )
    store = PendingApprovalStore(
        db_path=db_path,
        enable_default_notifier=False,
    )
    try:
        after_first = store.count_entries()
        assert after_first >= 1
        known = store.known_failed_call_ids()
        assert known
    finally:
        store.close()

    second = coding_agent_run(
        source_root=str(FIXTURE_ROOT),
        enable_notifications=False,
        storage=storage,
        db_path=db_path,
        fix_generator=FixGenerator(llm_fn=llm_fn),
    )
    store = PendingApprovalStore(
        db_path=db_path,
        enable_default_notifier=False,
    )
    try:
        after_second = store.count_entries()
        assert after_second == after_first
    finally:
        store.close()

    assert first.failures_detected == 1
    assert second.failures_detected == 0


def test_pipeline_summarize(pipeline):
    result = pipeline.run()
    summary = pipeline.summarize(result)
    assert "failures=" in summary
    assert "ready=" in summary
