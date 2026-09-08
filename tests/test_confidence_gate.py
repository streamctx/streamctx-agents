"""Unit tests for Stage 3 confidence gate and pending_approval store."""

from __future__ import annotations

import json
import sqlite3

import pytest

from agents.coding_agent.confidence_gate import (
    MIN_CONFIDENCE_FOR_AUTO_FIX,
    ConfidenceGate,
)
from agents.coding_agent.diagnose import FailureDiagnostician
from agents.coding_agent.models import DiagnosisResult
from agents.coding_agent.pending_approval import (
    STATUS_NEEDS_HUMAN_REVIEW,
    PendingApprovalStore,
)
from streamctx.storage import SessionStorage
from tests.test_diagnose import _seed_drift_failure


@pytest.fixture
def approval_store(tmp_path):
    store = PendingApprovalStore(
        db_path=tmp_path / "coding_agent.db",
        notifier=lambda _entry: None,
        enable_default_notifier=False,
    )
    yield store
    store.close()


def _diagnosis(
    *,
    confidence: float = 0.8,
    root_cause: str = "DRIFT",
    replay_verified: bool = True,
) -> DiagnosisResult:
    return DiagnosisResult(
        session_id=42,
        failed_call_id=7,
        root_cause=root_cause,
        confidence=confidence,
        replay_verified=replay_verified,
        reason="test diagnosis",
        error_type="RuntimeError",
        relevant_file="foo.py",
        signature_hash="abc123",
    )


def test_pending_approval_table_schema(approval_store, tmp_path):
    conn = sqlite3.connect(tmp_path / "coding_agent.db")
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_approval'"
        ).fetchone()
        assert row is not None

        columns = {
            col[1] for col in conn.execute("PRAGMA table_info(pending_approval)").fetchall()
        }
        expected = {
            "entry_id",
            "session_id",
            "root_cause",
            "confidence",
            "matched_pattern_id",
            "diff",
            "regression_test",
            "test_results",
            "retries_used",
            "status",
            "created_at",
            "generation_mode",
            "auto_applied",
            "verification_mode",
        }
        assert expected.issubset(columns)
    finally:
        conn.close()


def test_create_needs_human_review_persists_entry(approval_store):
    diagnosis = _diagnosis(confidence=0.4)
    entry = approval_store.create_needs_human_review(diagnosis)

    assert entry.status == STATUS_NEEDS_HUMAN_REVIEW
    assert entry.session_id == "42"
    assert entry.root_cause == "DRIFT"
    assert entry.confidence == 0.4
    assert entry.diff is None
    assert entry.retries_used == 0

    stored = approval_store.get_entry(entry.entry_id)
    assert stored is not None
    assert stored.status == STATUS_NEEDS_HUMAN_REVIEW

    context = json.loads(stored.test_results or "{}")
    assert context["failed_call_id"] == 7
    assert context["reason"] == "test diagnosis"


def test_create_needs_human_review_is_idempotent_for_failed_call_id(approval_store):
    first = approval_store.create_needs_human_review(_diagnosis(confidence=0.4))
    second = approval_store.create_needs_human_review(_diagnosis(confidence=0.1))
    assert second.entry_id == first.entry_id
    assert approval_store.count_entries() == 1


def test_gate_evaluate_twice_does_not_insert_a_second_row(approval_store):
    gate = ConfidenceGate(approval_store=approval_store)
    first = gate.evaluate(_diagnosis(confidence=0.2))
    second = gate.evaluate(_diagnosis(confidence=0.2))
    assert first.pending_entry is not None
    assert second.pending_entry is not None
    assert second.pending_entry.entry_id == first.pending_entry.entry_id
    assert approval_store.count_entries() == 1


def test_gate_blocks_low_confidence(approval_store):
    gate = ConfidenceGate(approval_store=approval_store)
    result = gate.evaluate(_diagnosis(confidence=0.55))

    assert result.proceed_to_fix is False
    assert result.pending_entry is not None
    assert result.pending_entry.status == STATUS_NEEDS_HUMAN_REVIEW
    assert len(approval_store.list_by_status(STATUS_NEEDS_HUMAN_REVIEW)) == 1


def test_gate_blocks_unclear_root_cause(approval_store):
    gate = ConfidenceGate(approval_store=approval_store)
    result = gate.evaluate(_diagnosis(confidence=0.9, root_cause="UNCLEAR"))

    assert result.proceed_to_fix is False
    assert result.pending_entry is not None
    assert result.pending_entry.root_cause == "UNCLEAR"


def test_gate_blocks_unverified_replay(approval_store):
    gate = ConfidenceGate(approval_store=approval_store)
    result = gate.evaluate(_diagnosis(confidence=0.9, replay_verified=False))

    assert result.proceed_to_fix is False
    assert result.pending_entry is not None


def test_gate_allows_high_confidence_verified_diagnosis(approval_store):
    gate = ConfidenceGate(approval_store=approval_store)
    result = gate.evaluate(_diagnosis(confidence=MIN_CONFIDENCE_FOR_AUTO_FIX))

    assert result.proceed_to_fix is True
    assert result.pending_entry is None
    assert approval_store.list_by_status(STATUS_NEEDS_HUMAN_REVIEW) == []


def test_gate_allows_above_threshold(approval_store):
    gate = ConfidenceGate(approval_store=approval_store)
    result = gate.evaluate(_diagnosis(confidence=0.75))

    assert result.proceed_to_fix is True
    assert result.pending_entry is None


def test_gate_run_integration(storage, tmp_path):
    _seed_drift_failure(storage)
    approval_store = PendingApprovalStore(db_path=tmp_path / "agent.db")
    gate = ConfidenceGate(
        diagnostician=FailureDiagnostician(storage=storage),
        approval_store=approval_store,
    )

    results = gate.run()
    assert len(results) == 1

    result = results[0]
    if result.diagnosis.confidence >= MIN_CONFIDENCE_FOR_AUTO_FIX:
        assert result.proceed_to_fix is True
    else:
        assert result.proceed_to_fix is False
        assert result.pending_entry is not None

    approval_store.close()
