"""Stage 4 fix generation and sandbox validation loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from streamctx.storage import SessionStorage, get_storage

from agents.coding_agent.fix_generator import (
    FixGenerator,
    build_fix_request,
    read_source_files,
)
from agents.coding_agent.models import DiagnosisResult, FixLoopResult, GateResult
from agents.coding_agent.pattern_memory import PatternMemory
from agents.coding_agent.pending_approval import (
    STATUS_AUTO_FIX_FAILED,
    STATUS_READY_FOR_APPROVAL,
    PendingApprovalStore,
)
from agents.coding_agent.sandbox import DiffApplyError, Sandbox, TestResult
from shared.config import BASE_DIR

# Maximum retry rounds after the first fix attempt (4 total attempts).
MAX_FIX_RETRIES = 3


class FixValidationLoop:
    """Generate fixes, validate in the sandbox, and write pending_approval rows."""

    def __init__(
        self,
        fix_generator: Optional[FixGenerator] = None,
        approval_store: Optional[PendingApprovalStore] = None,
        storage: Optional[SessionStorage] = None,
        source_root: Optional[Path | str] = None,
        pattern_memory: Optional[PatternMemory] = None,
    ) -> None:
        self.fix_generator = fix_generator or FixGenerator()
        self.approval_store = approval_store or PendingApprovalStore()
        self.storage = storage or get_storage()
        self.source_root = Path(source_root or BASE_DIR)
        self.pattern_memory = pattern_memory

    def run(self, gate_result: GateResult) -> FixLoopResult:
        if not gate_result.proceed_to_fix:
            return FixLoopResult(
                skipped=True,
                diagnosis=gate_result.diagnosis,
                pending_entry=gate_result.pending_entry,
                success=False,
            )

        diagnosis = gate_result.diagnosis
        error_message = _load_error_message(self.storage, diagnosis)
        source_paths = _resolve_source_paths(diagnosis, self.source_root)
        source_contents = read_source_files(self.source_root, source_paths)

        attempted_diffs: list[str] = []
        previous_failure_output: Optional[str] = None
        last_proposal = None
        last_test_result: Optional[TestResult] = None

        with Sandbox(source_root=self.source_root) as sandbox:
            for attempt in range(MAX_FIX_RETRIES + 1):
                rejection_hint = None
                if self.pattern_memory is not None:
                    rejection_hint = self.pattern_memory.get_rejection_hint(
                        diagnosis.signature_hash
                    )
                request = build_fix_request(
                    diagnosis,
                    error_message=error_message,
                    source_contents=source_contents,
                    attempt=attempt,
                    previous_failure_output=previous_failure_output,
                    regression_test_path=_regression_test_path(diagnosis),
                    rejection_hint=rejection_hint,
                )
                proposal = self.fix_generator.generate(request)
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

                test_result = sandbox.run_tests()
                last_test_result = test_result
                if test_result.passed:
                    entry = self._create_ready_entry(
                        diagnosis=diagnosis,
                        proposal=proposal,
                        test_result=test_result,
                        retries_used=attempt,
                    )
                    return FixLoopResult(
                        skipped=False,
                        diagnosis=diagnosis,
                        pending_entry=entry,
                        success=True,
                        attempts=attempt + 1,
                    )

                previous_failure_output = test_result.output

        entry = self._create_failed_entry(
            diagnosis=diagnosis,
            proposal=last_proposal,
            test_result=last_test_result,
            attempted_diffs=attempted_diffs,
            retries_used=MAX_FIX_RETRIES,
        )
        return FixLoopResult(
            skipped=False,
            diagnosis=diagnosis,
            pending_entry=entry,
            success=False,
            attempts=MAX_FIX_RETRIES + 1,
        )

    def _create_ready_entry(self, *, diagnosis, proposal, test_result, retries_used):
        existing = self.approval_store.get_by_failed_call_id(diagnosis.failed_call_id)
        if existing is not None:
            return existing
        return self.approval_store.create_entry(
            session_id=str(diagnosis.session_id),
            root_cause=diagnosis.root_cause,
            confidence=diagnosis.confidence,
            matched_pattern_id=(
                diagnosis.matched_pattern.signature_hash
                if diagnosis.matched_pattern
                else None
            ),
            diff=proposal.diff,
            regression_test=proposal.regression_test,
            test_results=_test_results_json(test_result, diagnosis),
            retries_used=retries_used,
            status=STATUS_READY_FOR_APPROVAL,
        )

    def _create_failed_entry(
        self,
        *,
        diagnosis,
        proposal,
        test_result,
        attempted_diffs,
        retries_used,
    ):
        existing = self.approval_store.get_by_failed_call_id(diagnosis.failed_call_id)
        if existing is not None:
            return existing
        return self.approval_store.create_entry(
            session_id=str(diagnosis.session_id),
            root_cause=diagnosis.root_cause,
            confidence=diagnosis.confidence,
            matched_pattern_id=(
                diagnosis.matched_pattern.signature_hash
                if diagnosis.matched_pattern
                else None
            ),
            diff=proposal.diff if proposal else None,
            regression_test=proposal.regression_test if proposal else None,
            test_results={
                "passed": False,
                "output": test_result.output if test_result else "",
                "coverage_delta": (
                    test_result.coverage_delta if test_result else 0.0
                ),
                "attempted_diffs": attempted_diffs,
                "failed_call_id": diagnosis.failed_call_id,
                "signature_hash": diagnosis.signature_hash,
                "error_type": diagnosis.error_type,
                "relevant_file": diagnosis.relevant_file,
            },
            retries_used=retries_used,
            status=STATUS_AUTO_FIX_FAILED,
        )


def _test_results_json(test_result: TestResult, diagnosis: DiagnosisResult) -> dict[str, Any]:
    return {
        "passed": test_result.passed,
        "output": test_result.output,
        "coverage_delta": test_result.coverage_delta,
        "failed_call_id": diagnosis.failed_call_id,
        "signature_hash": diagnosis.signature_hash,
        "error_type": diagnosis.error_type,
        "relevant_file": diagnosis.relevant_file,
    }


def _write_regression_test(
    sandbox: Sandbox,
    relative_path: str,
    content: str,
) -> None:
    if not content.strip():
        return
    target = sandbox.root / relative_path.replace("\\", "/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content.rstrip() + "\n", encoding="utf-8")


def _load_error_message(
    storage: SessionStorage,
    diagnosis: DiagnosisResult,
) -> Optional[str]:
    for call in storage.get_calls_for_session(diagnosis.session_id):
        if int(call["id"]) == int(diagnosis.failed_call_id):
            return call.get("error_message")
    return None


def _resolve_source_paths(diagnosis: DiagnosisResult, source_root: Path) -> list[str]:
    rel = diagnosis.relevant_file.replace("\\", "/")
    if rel != "unknown" and (source_root / rel).is_file():
        return [rel]
    return []


def _regression_test_path(diagnosis: DiagnosisResult) -> str:
    return f"tests/test_regression_session_{diagnosis.session_id}.py"
