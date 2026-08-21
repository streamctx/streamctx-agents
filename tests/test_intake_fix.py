"""Tests for dashboard-intake → fix-generation (Path B)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.coding_agent.fix_generator import FixGenerator
from agents.coding_agent.intake_fix import (
    FULL_SUITE_ARGS,
    collect_source_paths,
    parse_pytest_nodeids,
    process_approved_intake,
)
from agents.coding_agent.models import FixProposal, FixRequest
from agents.coding_agent.pending_approval import (
    FIX_STATUS_CANNOT_PARSE,
    FIX_STATUS_FAILED,
    FIX_STATUS_READY,
    INTAKE_FIX_KIND,
    STATUS_AUTO_FIX_FAILED,
    STATUS_READY_FOR_APPROVAL,
    PendingApprovalStore,
    intake_child_session_id,
    is_intake_task,
    parse_test_results,
)
from agents.coding_agent.sandbox import DiffApplyError
from agents.coding_agent.sandbox import TestResult as SandboxTestResult

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


class FakeSandbox:
    """In-memory sandbox: scoped tests fail until a good diff is applied."""

    def __init__(self, root: Path, *, full_pass: bool = True) -> None:
        self.root = root
        self.full_pass = full_pass
        self.applied: list[str] = []
        self._has_good_diff = False

    def __enter__(self) -> FakeSandbox:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def reset(self) -> None:
        self._has_good_diff = False

    def apply_diff(self, diff: str) -> None:
        if not (diff or "").strip():
            raise DiffApplyError("Diff is empty.")
        self.applied.append(diff)
        self._has_good_diff = "return a + b" in diff

    def run_pytest(
        self,
        extra_args: list[str] | None = None,
        *,
        coverage: bool = True,
    ) -> SandboxTestResult:
        del coverage
        args = list(extra_args or [])
        is_full = args == list(FULL_SUITE_ARGS)
        trace = (
            'FAILED tests/test_broken_math.py::test_add\n'
            'File "broken_math.py", line 4\n'
            "AssertionError"
        )
        if is_full:
            if not self.full_pass:
                return SandboxTestResult(passed=False, output="unrelated test failed", coverage_delta=0.0)
            if self._has_good_diff:
                return SandboxTestResult(passed=True, output="full suite passed", coverage_delta=0.0)
            return SandboxTestResult(passed=False, output=trace, coverage_delta=0.0)
        if self._has_good_diff:
            return SandboxTestResult(passed=True, output="scoped passed", coverage_delta=0.0)
        return SandboxTestResult(passed=False, output=trace, coverage_delta=0.0)


@pytest.fixture
def store(tmp_path: Path) -> PendingApprovalStore:
    db = PendingApprovalStore(
        db_path=tmp_path / "coding_agent.db",
        enable_default_notifier=False,
    )
    yield db
    db.close()


def _intake(store: PendingApprovalStore, request: str):
    return store.create_intake_task(
        task_ref="dashboard:test-intake",
        summary=request.splitlines()[0][:80],
        request=request,
        payload={"source": "dashboard_roster"},
    )


def test_parse_pytest_nodeids_from_ticket_text():
    text = """
    Please fix these 4 failing tests:
    tests/test_auto_adapters.py::test_reddit_publish_respects_approval_karma_and_interval
    `tests/test_safety.py::test_duplicate_guard_allows_same_story_next_utc_day`
    tests\\test_safety.py::test_rejected_entries_do_not_block_duplicates
    also tests/test_story.py::TestParse::test_parse_real_product_changelog
    ignore tests/test_foo.py without a function
    """
    nodes = parse_pytest_nodeids(text)
    assert nodes == [
        "tests/test_auto_adapters.py::test_reddit_publish_respects_approval_karma_and_interval",
        "tests/test_safety.py::test_duplicate_guard_allows_same_story_next_utc_day",
        "tests/test_safety.py::test_rejected_entries_do_not_block_duplicates",
        "tests/test_story.py::TestParse::test_parse_real_product_changelog",
    ]


def test_parse_pytest_nodeids_returns_empty_when_none():
    assert parse_pytest_nodeids("please make the agent smarter") == []


def test_collect_source_paths_includes_test_file_and_traceback(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_broken_math.py").write_text("def test_add():\n    pass\n")
    (tmp_path / "broken_math.py").write_text("def add(a, b):\n    return a - b\n")
    paths = collect_source_paths(
        tmp_path,
        ["tests/test_broken_math.py::test_add"],
        'File "broken_math.py", line 4\nAssertionError',
    )
    assert "tests/test_broken_math.py" in paths
    assert "broken_math.py" in paths


def test_cannot_parse_tests_records_status_and_failed_child(store, tmp_path):
    parent = _intake(store, "Fix whatever is broken in the coding agent.")
    result = process_approved_intake(
        parent.entry_id,
        approval_store=store,
        source_root=tmp_path,
        sandbox_factory=lambda: FakeSandbox(tmp_path),
        fix_generator=FixGenerator(llm_fn=_should_not_be_called),
    )
    assert result.success is False
    assert result.reason == FIX_STATUS_CANNOT_PARSE
    assert result.child_entry is not None
    assert result.child_entry.status == STATUS_AUTO_FIX_FAILED
    payload = parse_test_results(result.child_entry)
    assert payload["parse_error"] == FIX_STATUS_CANNOT_PARSE
    parent = store.get_entry(parent.entry_id)
    linked = parse_test_results(parent)
    assert linked["fix_status"] == FIX_STATUS_CANNOT_PARSE
    assert linked["child_entry_id"] == result.child_entry.entry_id


def test_intake_fix_writes_ready_child_when_scoped_and_full_suite_pass(store, tmp_path):
    parent = _intake(
        store,
        "fix tests/test_broken_math.py::test_add please",
    )
    calls = {"n": 0}

    def llm_fn(request: FixRequest) -> FixProposal:
        calls["n"] += 1
        assert request.diagnosis.root_cause == "INTAKE"
        return FixProposal(
            diff=FIX_DIFF,
            regression_test="",
            regression_test_path="tests/test_regression_intake.py",
        )

    result = process_approved_intake(
        parent.entry_id,
        approval_store=store,
        source_root=FIXTURE_ROOT,
        sandbox_factory=lambda: FakeSandbox(tmp_path),
        fix_generator=FixGenerator(llm_fn=llm_fn),
    )
    assert result.success is True
    assert result.reason == FIX_STATUS_READY
    assert result.child_entry is not None
    assert result.child_entry.status == STATUS_READY_FOR_APPROVAL
    assert result.child_entry.diff == FIX_DIFF
    assert result.child_entry.session_id == intake_child_session_id(parent.entry_id)
    assert is_intake_task(result.child_entry) is False
    child_payload = parse_test_results(result.child_entry)
    assert child_payload["kind"] == INTAKE_FIX_KIND
    assert child_payload["parent_entry_id"] == parent.entry_id
    assert child_payload["passed"] is True
    parent = store.get_entry(parent.entry_id)
    linked = parse_test_results(parent)
    assert linked["child_entry_id"] == result.child_entry.entry_id
    assert linked["fix_status"] == FIX_STATUS_READY
    assert calls["n"] == 1


def test_full_suite_failure_never_creates_ready_for_approval(store, tmp_path):
    parent = _intake(store, "fix tests/test_broken_math.py::test_add")

    def llm_fn(request: FixRequest) -> FixProposal:
        return FixProposal(
            diff=FIX_DIFF,
            regression_test="",
            regression_test_path="tests/test_regression_intake.py",
        )

    result = process_approved_intake(
        parent.entry_id,
        approval_store=store,
        source_root=FIXTURE_ROOT,
        sandbox_factory=lambda: FakeSandbox(tmp_path, full_pass=False),
        fix_generator=FixGenerator(llm_fn=llm_fn),
    )
    assert result.success is False
    assert result.reason == FIX_STATUS_FAILED
    assert result.child_entry is not None
    assert result.child_entry.status == STATUS_AUTO_FIX_FAILED
    assert store.list_by_status(STATUS_READY_FOR_APPROVAL) == []
    parent = store.get_entry(parent.entry_id)
    assert parse_test_results(parent)["fix_status"] == FIX_STATUS_FAILED


def test_intake_fix_is_idempotent_when_child_exists(store, tmp_path):
    parent = _intake(store, "fix tests/test_broken_math.py::test_add")
    existing = store.create_entry(
        session_id=intake_child_session_id(parent.entry_id),
        root_cause="INTAKE",
        confidence=0.0,
        matched_pattern_id=None,
        diff=FIX_DIFF,
        regression_test=None,
        test_results={"kind": INTAKE_FIX_KIND, "parent_entry_id": parent.entry_id},
        retries_used=0,
        status=STATUS_READY_FOR_APPROVAL,
    )
    result = process_approved_intake(
        parent.entry_id,
        approval_store=store,
        source_root=tmp_path,
        sandbox_factory=lambda: FakeSandbox(tmp_path),
        fix_generator=FixGenerator(llm_fn=_should_not_be_called),
    )
    assert result.skipped is True
    assert result.reason == "already_has_child"
    assert result.child_entry.entry_id == existing.entry_id
    ready = store.list_by_status(STATUS_READY_FOR_APPROVAL)
    assert len(ready) == 1


def test_bad_diffs_retry_then_succeed(store, tmp_path):
    parent = _intake(store, "fix tests/test_broken_math.py::test_add")
    attempts = {"n": 0}

    def llm_fn(request: FixRequest) -> FixProposal:
        attempts["n"] += 1
        diff = FIX_DIFF if attempts["n"] >= 2 else BAD_DIFF
        return FixProposal(
            diff=diff,
            regression_test="",
            regression_test_path="tests/test_regression_intake.py",
        )

    result = process_approved_intake(
        parent.entry_id,
        approval_store=store,
        source_root=FIXTURE_ROOT,
        sandbox_factory=lambda: FakeSandbox(tmp_path),
        fix_generator=FixGenerator(llm_fn=llm_fn),
    )
    assert result.success is True
    assert attempts["n"] == 2
    assert result.attempts == 2


def _should_not_be_called(request: FixRequest) -> FixProposal:
    raise AssertionError("LLM must not run for this case.")
