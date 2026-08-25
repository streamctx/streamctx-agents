"""Path B Assign with pytest nodeids: reuse Path A's gate + fix loop.

Free-text Assign (no nodeids) stays an INTAKE placeholder in the dashboard.
Approved INTAKE rows (research handoff) still go through ``intake_fix``.
"""

from __future__ import annotations

import subprocess
import sys
import zlib
from pathlib import Path
from typing import Optional, Sequence

from agents.coding_agent.confidence_gate import MIN_CONFIDENCE_FOR_AUTO_FIX, ConfidenceGate
from agents.coding_agent.diagnose import extract_error_type, extract_relevant_file
from agents.coding_agent.fix_generator import FixGenerator
from agents.coding_agent.fix_loop import FixValidationLoop
from agents.coding_agent.fix_patterns import FixPatternStore
from agents.coding_agent.intake_fix import parse_pytest_nodeids
from agents.coding_agent.models import AssignFixResult, DiagnosisResult, GateResult
from agents.coding_agent.pending_approval import (
    ROOT_CAUSE_INTAKE,
    PendingApprovalStore,
)
from shared.config import BASE_DIR


class _NoSessionStorage:
    """Path B has no StreamCtx failed call; the fix loop must not open sessions.db."""

    def get_calls_for_session(self, session_id: int) -> list:
        return []


def failed_call_id_for_nodeids(nodeids: Sequence[str]) -> int:
    """Stable negative id so Path B reuses ``get_by_failed_call_id`` without colliding."""
    normalized = "\n".join(sorted({item.replace("\\", "/") for item in nodeids}))
    digest = zlib.crc32(normalized.encode("utf-8")) & 0x7FFFFFFF
    return -(digest or 1)


def process_assigned_nodeids(
    text: str,
    *,
    db_path: Optional[Path | str] = None,
    source_root: Optional[Path | str] = None,
    approval_store: Optional[PendingApprovalStore] = None,
    fix_generator: Optional[FixGenerator] = None,
    confidence_gate: Optional[ConfidenceGate] = None,
    fix_loop: Optional[FixValidationLoop] = None,
    pattern_store: Optional[FixPatternStore] = None,
    error_output: Optional[str] = None,
    storage: Optional[object] = None,
) -> AssignFixResult:
    """Run ``ConfidenceGate.evaluate`` then ``FixValidationLoop`` for Assign nodeids."""
    nodeids = tuple(parse_pytest_nodeids(text))
    if not nodeids:
        return AssignFixResult(
            nodeids=(),
            failed_call_id=None,
            pending_entry=None,
            gate=None,
            fix=None,
            skipped=True,
            reason="no_nodeids",
        )

    call_id = failed_call_id_for_nodeids(nodeids)
    owns_store = approval_store is None
    store = approval_store or PendingApprovalStore(
        db_path=db_path,
        enable_default_notifier=False,
    )
    owns_patterns = pattern_store is None
    patterns = pattern_store or FixPatternStore(db_path=store.db_path)
    try:
        existing = store.get_by_failed_call_id(call_id)
        if existing is not None:
            return AssignFixResult(
                nodeids=nodeids,
                failed_call_id=call_id,
                pending_entry=existing,
                gate=GateResult(
                    diagnosis=_diagnosis_from_nodeids(
                        nodeids,
                        error_output or "",
                        patterns,
                    ),
                    pending_entry=existing,
                    proceed_to_fix=False,
                ),
                fix=None,
                skipped=True,
                reason="already_recorded",
            )

        output = error_output if error_output is not None else _collect_pytest_output(
            Path(source_root or BASE_DIR),
            nodeids,
        )
        diagnosis = _diagnosis_from_nodeids(nodeids, output, patterns)
        gate = confidence_gate or ConfidenceGate(approval_store=store)
        gate_result = gate.evaluate(diagnosis)
        loop = fix_loop or FixValidationLoop(
            fix_generator=fix_generator,
            approval_store=store,
            storage=storage or _NoSessionStorage(),
            source_root=source_root or BASE_DIR,
        )
        fix_result = loop.run(gate_result)
        entry = fix_result.pending_entry or gate_result.pending_entry
        return AssignFixResult(
            nodeids=nodeids,
            failed_call_id=call_id,
            pending_entry=entry,
            gate=gate_result,
            fix=fix_result,
            skipped=fix_result.skipped,
            reason=_reason_from_results(gate_result, fix_result),
        )
    finally:
        if owns_patterns:
            patterns.close()
        if owns_store:
            store.close()


def _diagnosis_from_nodeids(
    nodeids: tuple[str, ...],
    error_output: str,
    pattern_store: FixPatternStore,
) -> DiagnosisResult:
    call_id = failed_call_id_for_nodeids(nodeids)
    listed = "\n".join(nodeids)
    reason = f"Assigned pytest nodeids:\n{listed}"
    if error_output.strip():
        reason = f"{reason}\n\n{error_output.strip()}"
    error_type = extract_error_type(error_output)
    relevant_file = extract_relevant_file(error_output)
    if relevant_file == "unknown":
        relevant_file = nodeids[0].split("::", 1)[0]
    signature_hash = FixPatternStore.compute_signature(
        error_type,
        ROOT_CAUSE_INTAKE,
        relevant_file,
    )
    matched = pattern_store.find_match(signature_hash)
    return DiagnosisResult(
        session_id=abs(call_id),
        failed_call_id=call_id,
        root_cause=ROOT_CAUSE_INTAKE,
        confidence=MIN_CONFIDENCE_FOR_AUTO_FIX,
        replay_verified=True,
        reason=reason,
        error_type=error_type,
        relevant_file=relevant_file,
        signature_hash=signature_hash,
        matched_pattern=matched,
        skip_to_stage4=matched is not None,
    )


def _collect_pytest_output(source_root: Path, nodeids: Sequence[str]) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *nodeids, "--tb=short", "-q", "--no-header"],
        cwd=source_root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return f"{result.stdout or ''}{result.stderr or ''}"


def _reason_from_results(gate: GateResult, fix: FixLoopResult) -> str:
    entry = fix.pending_entry or gate.pending_entry
    if entry is not None:
        return entry.status
    if not gate.proceed_to_fix:
        return "blocked_for_review"
    return "completed"
