"""Unit tests for Stage 5 fix-pattern memory."""

from __future__ import annotations

import json

import pytest

from agents.coding_agent.fix_patterns import FixPatternStore
from agents.coding_agent.pattern_memory import PatternMemory
from agents.coding_agent.pending_approval import (
    STATUS_APPROVED,
    STATUS_READY_FOR_APPROVAL,
    STATUS_REJECTED,
    PendingApprovalStore,
)

FIX_DIFF = """\
diff --git a/broken_math.py b/broken_math.py
--- a/broken_math.py
+++ b/broken_math.py
@@ -4,3 +4,3 @@
 
 def add(a: int, b: int) -> int:
-    return a - b
+    return a + b
"""


@pytest.fixture
def stores(tmp_path):
    db_path = tmp_path / "coding_agent.db"
    approval_store = PendingApprovalStore(
        db_path=db_path,
        notifier=lambda _entry: None,
        enable_default_notifier=False,
    )
    pattern_store = FixPatternStore(db_path=db_path)
    memory = PatternMemory(
        pattern_store=pattern_store,
        approval_store=approval_store,
    )
    yield memory, approval_store, pattern_store
    approval_store.close()
    pattern_store.close()


def _create_ready_entry(approval_store: PendingApprovalStore, *, signature_hash: str):
    return approval_store.create_entry(
        session_id="42",
        root_cause="DRIFT",
        confidence=0.85,
        matched_pattern_id=None,
        diff=FIX_DIFF,
        regression_test="def test_x(): pass",
        test_results={
            "passed": True,
            "signature_hash": signature_hash,
            "error_type": "AssertionError",
            "relevant_file": "broken_math.py",
        },
        retries_used=0,
        status=STATUS_READY_FOR_APPROVAL,
    )


def test_approve_inserts_new_pattern(stores):
    memory, approval_store, pattern_store = stores
    entry = _create_ready_entry(approval_store, signature_hash="sig-new")

    pattern = memory.approve(entry.entry_id, applied_commit="deadbeef")

    assert pattern.success_count == 1
    assert pattern.reject_count == 0
    assert pattern.fix_diff_template == FIX_DIFF
    assert approval_store.get_entry(entry.entry_id).status == STATUS_APPROVED
    assert approval_store.get_entry(entry.entry_id).applied_commit == "deadbeef"
    assert pattern_store.find_match("sig-new") is not None


def test_approve_increments_existing_pattern(stores):
    memory, approval_store, pattern_store = stores
    pattern_store.upsert_pattern(
        signature_hash="sig-existing",
        root_cause_type="DRIFT",
        fix_diff_template=FIX_DIFF,
        success_count=2,
        reject_count=1,
    )
    entry = _create_ready_entry(approval_store, signature_hash="sig-existing")

    pattern = memory.approve(entry.entry_id)

    assert pattern.success_count == 3
    assert pattern.reject_count == 1
    assert pattern_store.find_match("sig-existing") is not None


def test_reject_increments_reject_count_and_stores_reason(stores):
    memory, approval_store, pattern_store = stores
    entry = _create_ready_entry(approval_store, signature_hash="sig-reject")

    pattern = memory.reject(entry.entry_id, reason="Fix changes unrelated behavior.")

    assert pattern.success_count == 0
    assert pattern.reject_count == 1
    assert pattern.last_rejection_reason == "Fix changes unrelated behavior."
    assert approval_store.get_entry(entry.entry_id).status == STATUS_REJECTED
    assert pattern_store.find_match("sig-reject") is None


def test_reject_makes_pattern_untrusted_for_future_matches(stores):
    memory, approval_store, pattern_store = stores
    entry = _create_ready_entry(approval_store, signature_hash="sig-blocked")
    memory.approve(entry.entry_id)

    entry2 = _create_ready_entry(approval_store, signature_hash="sig-blocked")
    memory.reject(entry2.entry_id, reason="Regression test insufficient.")

    pattern = pattern_store.get_pattern("sig-blocked")
    assert pattern.success_count == 1
    assert pattern.reject_count == 1
    assert pattern_store.find_match("sig-blocked") is None


def test_get_rejection_hint(stores):
    memory, approval_store, _ = stores
    entry = _create_ready_entry(approval_store, signature_hash="sig-hint")
    memory.reject(entry.entry_id, reason="Do not patch token_utils directly.")

    assert memory.get_rejection_hint("sig-hint") == "Do not patch token_utils directly."


def test_reject_requires_reason(stores):
    memory, approval_store, _ = stores
    entry = _create_ready_entry(approval_store, signature_hash="sig-no-reason")

    with pytest.raises(ValueError, match="Rejection reason is required"):
        memory.reject(entry.entry_id, reason="  ")


def test_approve_requires_diff(stores):
    memory, approval_store, _ = stores
    entry = approval_store.create_entry(
        session_id="42",
        root_cause="DRIFT",
        confidence=0.85,
        matched_pattern_id=None,
        diff=None,
        regression_test=None,
        test_results=json.dumps({"signature_hash": "sig-no-diff"}),
        retries_used=0,
        status=STATUS_READY_FOR_APPROVAL,
    )

    with pytest.raises(ValueError, match="no diff"):
        memory.approve(entry.entry_id)
