"""Generate a code fix from a human-approved dashboard intake ticket.

Path A (``coding_agent.run``) diagnoses failed LLM calls, then runs
``FixValidationLoop``. Path B (Roster Assign) only writes an INTAKE row with
no diff. Approving that row is the confidence gate: this module parses pytest
nodeids from the ticket, reuses ``FixGenerator`` + sandbox retries, and writes
a child ``pending_approval`` row. A full-suite regression check is mandatory
before ``ready_for_approval``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

from agents.coding_agent.diagnose import extract_error_type, extract_relevant_file
from agents.coding_agent.fix_generator import (
    FixGenerator,
    build_fix_request,
    read_source_files,
)
from agents.coding_agent.fix_loop import MAX_FIX_RETRIES, _write_regression_test
from agents.coding_agent.models import (
    DiagnosisResult,
    IntakeFixJob,
    IntakeFixResult,
    PendingApprovalEntry,
)
from agents.coding_agent.pending_approval import (
    FIX_STATUS_CANNOT_PARSE,
    FIX_STATUS_FAILED,
    FIX_STATUS_GENERATING,
    FIX_STATUS_READY,
    INTAKE_FIX_KIND,
    ROOT_CAUSE_INTAKE,
    STATUS_AUTO_FIX_FAILED,
    STATUS_READY_FOR_APPROVAL,
    PendingApprovalStore,
    intake_child_session_id,
    is_intake_task,
    parse_test_results,
)
from agents.coding_agent.sandbox import DiffApplyError, Sandbox, TestResult
from shared.config import BASE_DIR

# ``pytest tests/ --ignore=tests/fixtures`` must pass before review.
FULL_SUITE_ARGS = ["tests/", "--ignore=tests/fixtures"]

# tests/foo.py::test_bar  or  tests/foo.py::TestClass::test_bar
_NODEID_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+[/\\])*test[A-Za-z0-9_.-]*\.py(?:::[A-Za-z_][A-Za-z0-9_]*)+"
)
_TRACE_FILE_RE = re.compile(
    r'(?:File "([^"]+\.py)"|((?:[A-Za-z]:)?[A-Za-z0-9_./\\-]+\.py)(?::\d+)?)'
)
_SKIP_PATH_MARKERS = ("site-packages", "dist-packages", "<frozen", "/pytest/", "\\pytest\\")
_MAX_SOURCE_FILES = 12

SandboxFactory = Callable[[], Sandbox]


class IntakeFixLoop:
    """Parse an approved intake ticket and generate a child fix row."""

    def __init__(
        self,
        *,
        approval_store: PendingApprovalStore,
        fix_generator: Optional[FixGenerator] = None,
        source_root: Optional[Path | str] = None,
        sandbox_factory: Optional[SandboxFactory] = None,
    ) -> None:
        self.approval_store = approval_store
        self.fix_generator = fix_generator or FixGenerator()
        self.source_root = Path(source_root or BASE_DIR)
        self.sandbox_factory = sandbox_factory or (
            lambda: Sandbox(source_root=self.source_root)
        )

    def run(self, parent: PendingApprovalEntry) -> IntakeFixResult:
        if not is_intake_task(parent):
            return IntakeFixResult(
                parent_entry_id=parent.entry_id,
                child_entry=None,
                success=False,
                skipped=True,
                reason="not_intake",
            )

        existing = self.approval_store.get_latest_by_session_id(
            intake_child_session_id(parent.entry_id)
        )
        if existing is not None:
            return IntakeFixResult(
                parent_entry_id=parent.entry_id,
                child_entry=existing,
                success=existing.status == STATUS_READY_FOR_APPROVAL,
                skipped=True,
                reason="already_has_child",
                nodeids=_nodeids_from_child(existing),
            )

        request_text = str(parse_test_results(parent).get("request") or "")
        nodeids = tuple(parse_pytest_nodeids(request_text))
        if not nodeids:
            child = self._create_failed_child(
                parent=parent,
                nodeids=(),
                proposal=None,
                test_result=None,
                attempted_diffs=[],
                retries_used=0,
                extra={"parse_error": FIX_STATUS_CANNOT_PARSE},
            )
            self._link_parent(parent.entry_id, child, FIX_STATUS_CANNOT_PARSE)
            return IntakeFixResult(
                parent_entry_id=parent.entry_id,
                child_entry=child,
                success=False,
                skipped=False,
                reason=FIX_STATUS_CANNOT_PARSE,
            )

        self.approval_store.merge_test_results(
            parent.entry_id,
            {"fix_status": FIX_STATUS_GENERATING, "nodeids": list(nodeids)},
        )

        job = IntakeFixJob(
            parent_entry_id=parent.entry_id,
            request=request_text,
            nodeids=nodeids,
        )
        return self._generate(parent, job)

    def _generate(
        self,
        parent: PendingApprovalEntry,
        job: IntakeFixJob,
    ) -> IntakeFixResult:
        attempted_diffs: list[str] = []
        previous_failure_output: Optional[str] = None
        last_proposal = None
        last_test_result: Optional[TestResult] = None
        source_contents: dict[str, str] = {}
        error_output = ""

        with self.sandbox_factory() as sandbox:
            baseline = sandbox.run_pytest(list(job.nodeids), coverage=False)
            error_output = baseline.output
            source_paths = collect_source_paths(
                self.source_root,
                job.nodeids,
                error_output,
            )
            source_contents = read_source_files(self.source_root, list(source_paths))
            job = IntakeFixJob(
                parent_entry_id=job.parent_entry_id,
                request=job.request,
                nodeids=job.nodeids,
                error_output=error_output,
                source_paths=source_paths,
            )
            diagnosis = _diagnosis_for_job(job)

            for attempt in range(MAX_FIX_RETRIES + 1):
                request = build_fix_request(
                    diagnosis,
                    error_message=error_output if attempt == 0 else (
                        previous_failure_output or error_output
                    ),
                    source_contents=source_contents,
                    attempt=attempt,
                    previous_failure_output=previous_failure_output,
                    regression_test_path=_intake_regression_path(parent.entry_id),
                )
                try:
                    proposal = self.fix_generator.generate(request)
                except Exception as exc:
                    previous_failure_output = f"Fix generation error: {exc}"
                    last_proposal = None
                    continue

                last_proposal = proposal
                attempted_diffs.append(proposal.diff)
                sandbox.reset()
                try:
                    sandbox.apply_diff(proposal.diff)
                    _write_regression_test(
                        sandbox,
                        proposal.regression_test_path,
                        proposal.regression_test,
                    )
                except (DiffApplyError, ValueError) as exc:
                    previous_failure_output = f"Diff apply/validation error: {exc}"
                    continue

                scoped = sandbox.run_pytest(list(job.nodeids), coverage=False)
                last_test_result = scoped
                if not scoped.passed:
                    previous_failure_output = scoped.output
                    continue

                full = sandbox.run_pytest(list(FULL_SUITE_ARGS), coverage=False)
                last_test_result = full
                if not full.passed:
                    previous_failure_output = (
                        "Named tests passed but full-suite regression failed.\n"
                        + full.output
                    )
                    continue

                child = self._create_ready_child(
                    parent=parent,
                    job=job,
                    proposal=proposal,
                    test_result=full,
                    retries_used=attempt,
                )
                self._link_parent(parent.entry_id, child, FIX_STATUS_READY)
                return IntakeFixResult(
                    parent_entry_id=parent.entry_id,
                    child_entry=child,
                    success=True,
                    skipped=False,
                    reason=FIX_STATUS_READY,
                    nodeids=job.nodeids,
                    attempts=attempt + 1,
                )

        child = self._create_failed_child(
            parent=parent,
            nodeids=job.nodeids,
            proposal=last_proposal,
            test_result=last_test_result,
            attempted_diffs=attempted_diffs,
            retries_used=MAX_FIX_RETRIES,
        )
        self._link_parent(parent.entry_id, child, FIX_STATUS_FAILED)
        return IntakeFixResult(
            parent_entry_id=parent.entry_id,
            child_entry=child,
            success=False,
            skipped=False,
            reason=FIX_STATUS_FAILED,
            nodeids=job.nodeids,
            attempts=MAX_FIX_RETRIES + 1,
        )

    def _create_ready_child(
        self,
        *,
        parent: PendingApprovalEntry,
        job: IntakeFixJob,
        proposal,
        test_result: TestResult,
        retries_used: int,
    ) -> PendingApprovalEntry:
        return self.approval_store.create_entry(
            session_id=intake_child_session_id(parent.entry_id),
            root_cause=ROOT_CAUSE_INTAKE,
            confidence=parent.confidence,
            matched_pattern_id=None,
            diff=proposal.diff,
            regression_test=proposal.regression_test,
            test_results={
                "kind": INTAKE_FIX_KIND,
                "parent_entry_id": parent.entry_id,
                "nodeids": list(job.nodeids),
                "passed": True,
                "output": test_result.output,
                "coverage_delta": test_result.coverage_delta,
                "full_suite": " ".join(FULL_SUITE_ARGS),
            },
            retries_used=retries_used,
            status=STATUS_READY_FOR_APPROVAL,
        )

    def _create_failed_child(
        self,
        *,
        parent: PendingApprovalEntry,
        nodeids: tuple[str, ...],
        proposal,
        test_result: Optional[TestResult],
        attempted_diffs: list[str],
        retries_used: int,
        extra: Optional[dict[str, Any]] = None,
    ) -> PendingApprovalEntry:
        payload: dict[str, Any] = {
            "kind": INTAKE_FIX_KIND,
            "parent_entry_id": parent.entry_id,
            "nodeids": list(nodeids),
            "passed": False,
            "output": test_result.output if test_result else "",
            "coverage_delta": test_result.coverage_delta if test_result else 0.0,
            "attempted_diffs": attempted_diffs,
            "full_suite": " ".join(FULL_SUITE_ARGS),
        }
        if extra:
            payload.update(extra)
        return self.approval_store.create_entry(
            session_id=intake_child_session_id(parent.entry_id),
            root_cause=ROOT_CAUSE_INTAKE,
            confidence=parent.confidence,
            matched_pattern_id=None,
            diff=proposal.diff if proposal else None,
            regression_test=proposal.regression_test if proposal else None,
            test_results=payload,
            retries_used=retries_used,
            status=STATUS_AUTO_FIX_FAILED,
        )

    def _link_parent(
        self,
        parent_entry_id: str,
        child: PendingApprovalEntry,
        fix_status: str,
    ) -> None:
        self.approval_store.merge_test_results(
            parent_entry_id,
            {
                "child_entry_id": child.entry_id,
                "fix_status": fix_status,
            },
        )


def process_approved_intake(
    entry_id: str,
    *,
    db_path: Optional[Path | str] = None,
    source_root: Optional[Path | str] = None,
    approval_store: Optional[PendingApprovalStore] = None,
    fix_generator: Optional[FixGenerator] = None,
    sandbox_factory: Optional[SandboxFactory] = None,
) -> IntakeFixResult:
    """Entry point used by the dashboard executor after INTAKE Approve."""
    owns_store = approval_store is None
    store = approval_store or PendingApprovalStore(
        db_path=db_path,
        enable_default_notifier=False,
    )
    try:
        entry = store.get_entry(entry_id)
        if entry is None:
            return IntakeFixResult(
                parent_entry_id=entry_id,
                child_entry=None,
                success=False,
                skipped=True,
                reason="missing_entry",
            )
        loop = IntakeFixLoop(
            approval_store=store,
            fix_generator=fix_generator,
            source_root=source_root,
            sandbox_factory=sandbox_factory,
        )
        return loop.run(entry)
    finally:
        if owns_store:
            store.close()


def parse_pytest_nodeids(text: str) -> list[str]:
    """Extract pytest nodeids from free-form intake text, preserving order."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _NODEID_RE.finditer(text or ""):
        node = match.group(0).replace("\\", "/")
        if node not in seen:
            seen.add(node)
            found.append(node)
    return found


def collect_source_paths(
    source_root: Path,
    nodeids: tuple[str, ...] | list[str],
    pytest_output: str,
) -> tuple[str, ...]:
    """Test files from nodeids plus Python files mentioned in pytest output."""
    root = source_root.resolve()
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        rel = _relative_source_path(root, raw)
        if rel is None or rel in seen:
            return
        seen.add(rel)
        ordered.append(rel)

    for nodeid in nodeids:
        _add(nodeid.split("::", 1)[0])
    relevant = extract_relevant_file(pytest_output)
    if relevant != "unknown":
        _add(relevant)
    for match in _TRACE_FILE_RE.finditer(pytest_output or ""):
        _add(match.group(1) or match.group(2) or "")
    return tuple(ordered[:_MAX_SOURCE_FILES])


def _relative_source_path(root: Path, raw: str) -> Optional[str]:
    text = (raw or "").strip().replace("\\", "/")
    if not text.endswith(".py"):
        return None
    lowered = text.lower()
    if any(marker in lowered or marker.replace("/", "\\") in raw for marker in _SKIP_PATH_MARKERS):
        return None
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(root)
        except ValueError:
            return None
        path = root / rel
    else:
        path = root / text
        if not path.is_file():
            # Tracebacks sometimes include only a basename.
            matches = list(root.rglob(Path(text).name))
            matches = [m for m in matches if "site-packages" not in str(m)]
            if len(matches) != 1:
                return None
            path = matches[0]
    if not path.is_file():
        return None
    return path.resolve().relative_to(root).as_posix()


def _diagnosis_for_job(job: IntakeFixJob) -> DiagnosisResult:
    return DiagnosisResult(
        session_id=0,
        failed_call_id=0,
        root_cause=ROOT_CAUSE_INTAKE,
        confidence=1.0,
        replay_verified=True,
        reason=job.request,
        error_type=extract_error_type(job.error_output) or "AssertionError",
        relevant_file=job.source_paths[0] if job.source_paths else "unknown",
        signature_hash=f"intake:{job.parent_entry_id}",
        matched_pattern=None,
        skip_to_stage4=True,
    )


def _intake_regression_path(parent_entry_id: str) -> str:
    slug = parent_entry_id.replace("-", "")[:12]
    return f"tests/test_regression_intake_{slug}.py"


def _nodeids_from_child(entry: PendingApprovalEntry) -> tuple[str, ...]:
    payload = parse_test_results(entry)
    raw = payload.get("nodeids") or []
    if isinstance(raw, list):
        return tuple(str(item) for item in raw)
    return ()
