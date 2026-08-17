"""Unit tests for Stage 4 fix generation and validation loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.coding_agent.fix_generator import FixGenerator, parse_fix_proposal
from agents.coding_agent.fix_loop import FixValidationLoop
from agents.coding_agent.models import (
    DiagnosisResult,
    FixPatternMatch,
    FixProposal,
    FixRequest,
    GateResult,
)
from agents.coding_agent.pending_approval import (
    STATUS_AUTO_FIX_FAILED,
    STATUS_READY_FOR_APPROVAL,
    PendingApprovalStore,
)

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "sandbox_project"

FIX_DIFF = """\
diff --git a/broken_math.py b/broken_math.py
--- a/broken_math.py
+++ b/broken_math.py
@@ -4,3 +4,3 @@
 
 def add(a: int, b: int) -> int:
-    return a - b
+    return a + b
"""

BAD_DIFF = """\
diff --git a/broken_math.py b/broken_math.py
--- a/broken_math.py
+++ b/broken_math.py
@@ -4,3 +4,3 @@
 
 def add(a: int, b: int) -> int:
-    return a - b
+    return a - b
"""

REGRESSION_TEST = """\
from broken_math import add


def test_add_regression():
    assert add(2, 3) == 5
"""


def _diagnosis(**overrides) -> DiagnosisResult:
    base = dict(
        session_id=99,
        failed_call_id=1,
        root_cause="DRIFT",
        confidence=0.85,
        replay_verified=True,
        reason="drift detected",
        error_type="AssertionError",
        relevant_file="broken_math.py",
        signature_hash="sig-99",
        matched_pattern=None,
        skip_to_stage4=False,
        signal_breakdown={"drift": 0.8},
    )
    base.update(overrides)
    return DiagnosisResult(**base)


def _gate(proceed_to_fix: bool = True, **diag_overrides) -> GateResult:
    return GateResult(
        diagnosis=_diagnosis(**diag_overrides),
        pending_entry=None,
        proceed_to_fix=proceed_to_fix,
    )


@pytest.fixture
def approval_store(tmp_path):
    store = PendingApprovalStore(db_path=tmp_path / "agent.db")
    yield store
    store.close()


def test_parse_fix_proposal_from_json():
    raw = json.dumps(
        {
            "diff": FIX_DIFF,
            "regression_test": REGRESSION_TEST,
            "regression_test_path": "tests/test_regression_session_99.py",
        }
    )
    proposal = parse_fix_proposal(raw)
    assert proposal.diff.startswith("diff --git")
    assert "test_add_regression" in proposal.regression_test


def test_fix_loop_skips_when_gate_blocks(approval_store):
    gate = _gate(proceed_to_fix=False, confidence=0.2)
    loop = FixValidationLoop(
        approval_store=approval_store,
        source_root=FIXTURE_ROOT,
        fix_generator=FixGenerator(llm_fn=_should_not_be_called),
    )

    result = loop.run(gate)
    assert result.skipped is True
    assert result.success is False
    assert approval_store.list_by_status(STATUS_READY_FOR_APPROVAL) == []


def test_fix_loop_success_writes_ready_for_approval(approval_store):
    attempts = {"count": 0}

    def llm_fn(request: FixRequest) -> FixProposal:
        attempts["count"] += 1
        return FixProposal(
            diff=FIX_DIFF,
            regression_test=REGRESSION_TEST,
            regression_test_path="tests/test_regression_session_99.py",
        )

    loop = FixValidationLoop(
        approval_store=approval_store,
        source_root=FIXTURE_ROOT,
        fix_generator=FixGenerator(llm_fn=llm_fn),
    )

    result = loop.run(_gate())
    assert result.skipped is False
    assert result.success is True
    assert result.pending_entry is not None
    assert result.pending_entry.status == STATUS_READY_FOR_APPROVAL
    assert result.pending_entry.diff == FIX_DIFF
    assert attempts["count"] == 1

    stored = json.loads(result.pending_entry.test_results or "{}")
    assert stored["passed"] is True


def test_fix_loop_retries_until_success(approval_store):
    attempts = {"count": 0}

    def llm_fn(request: FixRequest) -> FixProposal:
        attempts["count"] += 1
        diff = FIX_DIFF if attempts["count"] >= 2 else BAD_DIFF
        return FixProposal(
            diff=diff,
            regression_test=REGRESSION_TEST,
            regression_test_path="tests/test_regression_session_99.py",
        )

    loop = FixValidationLoop(
        approval_store=approval_store,
        source_root=FIXTURE_ROOT,
        fix_generator=FixGenerator(llm_fn=llm_fn),
    )

    result = loop.run(_gate())
    assert result.success is True
    assert attempts["count"] == 2
    assert result.attempts == 2
    assert result.pending_entry.retries_used == 1


def test_fix_loop_auto_fix_failed_after_max_retries(approval_store):
    def llm_fn(request: FixRequest) -> FixProposal:
        return FixProposal(
            diff=BAD_DIFF,
            regression_test=REGRESSION_TEST,
            regression_test_path="tests/test_regression_session_99.py",
        )

    loop = FixValidationLoop(
        approval_store=approval_store,
        source_root=FIXTURE_ROOT,
        fix_generator=FixGenerator(llm_fn=llm_fn),
    )

    result = loop.run(_gate())
    assert result.success is False
    assert result.pending_entry is not None
    assert result.pending_entry.status == STATUS_AUTO_FIX_FAILED
    assert result.attempts == 4

    payload = json.loads(result.pending_entry.test_results or "{}")
    assert payload["passed"] is False
    assert len(payload["attempted_diffs"]) == 4


def test_fix_loop_uses_pattern_template_without_llm(approval_store):
    pattern = FixPatternMatch(
        signature_hash="sig-99",
        root_cause_type="DRIFT",
        fix_diff_template=FIX_DIFF,
        success_count=3,
        reject_count=0,
    )

    def llm_fn(request: FixRequest) -> FixProposal:
        raise AssertionError("LLM should not be called when a pattern template matches.")

    loop = FixValidationLoop(
        approval_store=approval_store,
        source_root=FIXTURE_ROOT,
        fix_generator=FixGenerator(llm_fn=llm_fn),
    )

    gate = _gate(matched_pattern=pattern, skip_to_stage4=True)
    result = loop.run(gate)
    assert result.success is True
    assert result.pending_entry.status == STATUS_READY_FOR_APPROVAL


def _should_not_be_called(request: FixRequest) -> FixProposal:
    raise AssertionError("LLM must not run when gate blocks fix generation.")
