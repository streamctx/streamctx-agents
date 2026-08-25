"""Path B Assign with nodeids reuses ConfidenceGate + FixValidationLoop."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.coding_agent.assign_fix import (
    failed_call_id_for_nodeids,
    process_assigned_nodeids,
)
from agents.coding_agent.confidence_gate import MIN_CONFIDENCE_FOR_AUTO_FIX, ConfidenceGate
from agents.coding_agent.fix_generator import FixGenerator
from agents.coding_agent.models import FixProposal, FixRequest
from agents.coding_agent.pending_approval import (
    STATUS_READY_FOR_APPROVAL,
    PendingApprovalStore,
)

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "sandbox_project"

FOUR_FAILING = """
Please fix these 4 failing tests:
tests/test_auto_adapters.py::test_reddit_publish_respects_approval_karma_and_interval
tests/test_safety.py::test_duplicate_guard_allows_same_story_next_utc_day
tests/test_safety.py::test_rejected_entries_do_not_block_duplicates
tests/test_story.py::test_parse_real_product_changelog
"""

BROKEN_MATH = "fix tests/test_broken_math.py::test_add"

FIX_DIFF = """\
diff --git a/broken_math.py b/broken_math.py
--- a/broken_math.py
+++ b/broken_math.py
@@ -4,3 +4,3 @@
 
 def add(a: int, b: int) -> int:
-    return a - b
+    return a + b
"""

REGRESSION_TEST = """\
from broken_math import add


def test_add_regression():
    assert add(2, 3) == 5
"""


@pytest.fixture
def store(tmp_path: Path) -> PendingApprovalStore:
    db = PendingApprovalStore(
        db_path=tmp_path / "coding_agent.db",
        enable_default_notifier=False,
    )
    yield db
    db.close()


def _good_fix(request: FixRequest) -> FixProposal:
    return FixProposal(
        diff=FIX_DIFF,
        regression_test=REGRESSION_TEST,
        regression_test_path="tests/test_regression_session_assign.py",
    )


def test_failed_call_id_is_stable_negative_and_order_independent():
    four = [
        "tests/test_auto_adapters.py::test_reddit_publish_respects_approval_karma_and_interval",
        "tests/test_safety.py::test_duplicate_guard_allows_same_story_next_utc_day",
        "tests/test_safety.py::test_rejected_entries_do_not_block_duplicates",
        "tests/test_story.py::test_parse_real_product_changelog",
    ]
    first = failed_call_id_for_nodeids(four)
    second = failed_call_id_for_nodeids(list(reversed(four)))
    assert first == second
    assert first < 0
    other = failed_call_id_for_nodeids(["tests/test_broken_math.py::test_add"])
    assert other != first
    assert other < 0


def test_process_assigned_no_nodeids_is_skipped(store):
    result = process_assigned_nodeids(
        "please make the agent smarter",
        approval_store=store,
        error_output="",
        fix_generator=FixGenerator(llm_fn=_should_not_be_called),
    )
    assert result.skipped is True
    assert result.reason == "no_nodeids"
    assert result.pending_entry is None
    assert store.count_entries() == 0


def test_process_assigned_produces_diff_and_calls_evaluate(store):
    evaluated: list = []

    class SpyGate(ConfidenceGate):
        def evaluate(self, diagnosis):
            evaluated.append(diagnosis)
            return super().evaluate(diagnosis)

    result = process_assigned_nodeids(
        BROKEN_MATH,
        approval_store=store,
        source_root=FIXTURE_ROOT,
        error_output='File "broken_math.py", line 5\nAssertionError: 1 != 3',
        confidence_gate=SpyGate(approval_store=store),
        fix_generator=FixGenerator(llm_fn=_good_fix),
    )
    assert len(evaluated) == 1
    assert evaluated[0].failed_call_id == failed_call_id_for_nodeids(
        ["tests/test_broken_math.py::test_add"]
    )
    assert evaluated[0].confidence == MIN_CONFIDENCE_FOR_AUTO_FIX
    assert evaluated[0].replay_verified is True
    assert result.pending_entry is not None
    assert result.pending_entry.diff == FIX_DIFF
    assert result.pending_entry.status == STATUS_READY_FOR_APPROVAL
    assert result.pending_entry.confidence == MIN_CONFIDENCE_FOR_AUTO_FIX
    payload = result.pending_entry.test_results or ""
    assert str(evaluated[0].failed_call_id) in payload


def test_process_assigned_repeated_does_not_duplicate(store):
    first = process_assigned_nodeids(
        BROKEN_MATH,
        approval_store=store,
        source_root=FIXTURE_ROOT,
        error_output='File "broken_math.py", line 5\nAssertionError: 1 != 3',
        fix_generator=FixGenerator(llm_fn=_good_fix),
    )
    assert first.pending_entry is not None
    second = process_assigned_nodeids(
        BROKEN_MATH,
        approval_store=store,
        source_root=FIXTURE_ROOT,
        error_output='File "broken_math.py", line 5\nAssertionError: 1 != 3',
        fix_generator=FixGenerator(llm_fn=_should_not_be_called),
    )
    assert second.skipped is True
    assert second.reason == "already_recorded"
    assert second.pending_entry.entry_id == first.pending_entry.entry_id
    assert store.count_entries() == 1


def test_process_assigned_four_failures_dedup_without_sandbox(store, tmp_path):
    """The four dashboard-assigned nodeids share one failed_call_id."""
    call_id = failed_call_id_for_nodeids(
        [
            "tests/test_auto_adapters.py::test_reddit_publish_respects_approval_karma_and_interval",
            "tests/test_safety.py::test_duplicate_guard_allows_same_story_next_utc_day",
            "tests/test_safety.py::test_rejected_entries_do_not_block_duplicates",
            "tests/test_story.py::test_parse_real_product_changelog",
        ]
    )
    first = process_assigned_nodeids(
        FOUR_FAILING,
        approval_store=store,
        source_root=tmp_path,
        error_output="AssertionError: four failures",
        fix_loop=_ImmediateLoop(store),
        fix_generator=FixGenerator(llm_fn=_should_not_be_called),
    )
    assert first.pending_entry is not None
    assert first.pending_entry.diff
    assert first.failed_call_id == call_id
    second = process_assigned_nodeids(
        FOUR_FAILING,
        approval_store=store,
        source_root=tmp_path,
        error_output="AssertionError: four failures",
        fix_loop=_ImmediateLoop(store, explode=True),
        fix_generator=FixGenerator(llm_fn=_should_not_be_called),
    )
    assert second.pending_entry.entry_id == first.pending_entry.entry_id
    assert store.count_entries() == 1


class _ImmediateLoop:
    """Writes a Path A-style ready row without opening a sandbox."""

    def __init__(self, store: PendingApprovalStore, *, explode: bool = False) -> None:
        self.store = store
        self.explode = explode

    def run(self, gate_result):
        from agents.coding_agent.models import FixLoopResult

        if self.explode:
            raise AssertionError("fix loop must not run on a duplicate assign")
        if not gate_result.proceed_to_fix:
            return FixLoopResult(
                skipped=True,
                diagnosis=gate_result.diagnosis,
                pending_entry=gate_result.pending_entry,
                success=False,
            )
        diagnosis = gate_result.diagnosis
        entry = self.store.create_entry(
            session_id=str(diagnosis.session_id),
            root_cause=diagnosis.root_cause,
            confidence=diagnosis.confidence,
            matched_pattern_id=None,
            diff="--- a/x.py\n+++ b/x.py\n",
            regression_test=None,
            test_results={
                "passed": True,
                "failed_call_id": diagnosis.failed_call_id,
            },
            retries_used=0,
            status=STATUS_READY_FOR_APPROVAL,
        )
        return FixLoopResult(
            skipped=False,
            diagnosis=diagnosis,
            pending_entry=entry,
            success=True,
        )


def _should_not_be_called(request: FixRequest) -> FixProposal:
    raise AssertionError("LLM must not run for this Path B case.")
