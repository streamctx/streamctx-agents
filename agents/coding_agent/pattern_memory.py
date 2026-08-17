"""Update fix-pattern memory when humans approve or reject fixes."""

from __future__ import annotations

import json
from typing import Any, Optional

from agents.coding_agent.fix_patterns import FixPatternStore
from agents.coding_agent.models import FixPatternMatch, PendingApprovalEntry
from agents.coding_agent.pending_approval import (
    STATUS_APPROVED,
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    STATUS_REJECTED,
    PendingApprovalStore,
)


class PatternMemory:
    """
    Stage 5: persist learned fix templates on approval and rejection feedback.
    """

    def __init__(
        self,
        pattern_store: Optional[FixPatternStore] = None,
        approval_store: Optional[PendingApprovalStore] = None,
    ) -> None:
        self.pattern_store = pattern_store or FixPatternStore()
        self.approval_store = approval_store or PendingApprovalStore()

    def approve(self, entry_id: str) -> FixPatternMatch:
        """
        Record human approval: increment ``success_count`` or insert a new pattern.
        """
        entry = _require_entry(self.approval_store, entry_id)
        signature_hash, error_type, relevant_file = _resolve_signature_fields(entry)
        diff_template = entry.diff or ""

        if not diff_template:
            raise ValueError(
                f"Approval entry {entry_id} has no diff to store as a fix template."
            )

        pattern = self.pattern_store.record_approval(
            signature_hash=signature_hash,
            root_cause_type=entry.root_cause,
            fix_diff_template=diff_template,
        )
        self.approval_store.update_status(entry_id, STATUS_APPROVED)
        return pattern

    def reject(self, entry_id: str, reason: str) -> FixPatternMatch:
        """
        Record human rejection: increment ``reject_count`` and store the reason.
        """
        if not reason.strip():
            raise ValueError("Rejection reason is required.")

        entry = _require_entry(self.approval_store, entry_id)
        signature_hash, _, _ = _resolve_signature_fields(entry)
        diff_template = entry.diff or ""

        pattern = self.pattern_store.record_rejection(
            signature_hash=signature_hash,
            root_cause_type=entry.root_cause,
            fix_diff_template=diff_template,
            reason=reason.strip(),
        )
        self.approval_store.update_status(entry_id, STATUS_REJECTED)
        return pattern

    def get_rejection_hint(self, signature_hash: str) -> Optional[str]:
        """Return the last rejection reason for a signature, if any."""
        pattern = self.pattern_store.get_pattern(signature_hash)
        if pattern is None:
            return None
        return pattern.last_rejection_reason


def _require_entry(store: PendingApprovalStore, entry_id: str) -> PendingApprovalEntry:
    entry = store.get_entry(entry_id)
    if entry is None:
        raise KeyError(f"pending_approval entry not found: {entry_id}")
    if entry.status not in {STATUS_READY_FOR_APPROVAL, STATUS_NEEDS_HUMAN_REVIEW}:
        raise ValueError(
            f"Entry {entry_id} is not awaiting approval (status={entry.status})."
        )
    return entry


def _resolve_signature_fields(
    entry: PendingApprovalEntry,
) -> tuple[str, str, str]:
    metadata = _parse_test_results(entry.test_results)
    signature_hash = (
        entry.matched_pattern_id
        or metadata.get("signature_hash")
        or FixPatternStore.compute_signature(
            str(metadata.get("error_type") or "UnknownError"),
            entry.root_cause,
            str(metadata.get("relevant_file") or "unknown"),
        )
    )
    error_type = str(metadata.get("error_type") or "UnknownError")
    relevant_file = str(metadata.get("relevant_file") or "unknown")
    return signature_hash, error_type, relevant_file


def _parse_test_results(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
