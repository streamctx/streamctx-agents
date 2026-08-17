"""Confidence gate — stops low-confidence diagnoses before auto-fix (Stage 3)."""

from __future__ import annotations

from typing import Optional

from agents.coding_agent.diagnose import FailureDiagnostician
from agents.coding_agent.models import DiagnosisResult, GateResult
from agents.coding_agent.pending_approval import PendingApprovalStore

# Minimum attribution confidence required before attempting an automatic fix.
MIN_CONFIDENCE_FOR_AUTO_FIX = 0.6


class ConfidenceGate:
    """
    After Stage 2 diagnosis, block auto-fix when confidence is too low or
    the root cause is unclear, writing a ``pending_approval`` row instead.
    """

    def __init__(
        self,
        diagnostician: Optional[FailureDiagnostician] = None,
        approval_store: Optional[PendingApprovalStore] = None,
    ) -> None:
        self.diagnostician = diagnostician or FailureDiagnostician()
        self.approval_store = approval_store or PendingApprovalStore()

    def evaluate(self, diagnosis: DiagnosisResult) -> GateResult:
        """Apply the confidence gate to a single diagnosis."""
        if _should_stop_for_human_review(diagnosis):
            entry = self.approval_store.create_needs_human_review(diagnosis)
            return GateResult(
                diagnosis=diagnosis,
                pending_entry=entry,
                proceed_to_fix=False,
            )

        return GateResult(
            diagnosis=diagnosis,
            pending_entry=None,
            proceed_to_fix=True,
        )

    def run(self, *, limit: Optional[int] = None) -> list[GateResult]:
        """Run Stage 2 diagnosis then apply the gate to each failure."""
        diagnoses = self.diagnostician.run(limit=limit)
        return [self.evaluate(diagnosis) for diagnosis in diagnoses]


def _should_stop_for_human_review(diagnosis: DiagnosisResult) -> bool:
    if diagnosis.root_cause == "UNCLEAR":
        return True
    if not diagnosis.replay_verified:
        return True
    if diagnosis.confidence < MIN_CONFIDENCE_FOR_AUTO_FIX:
        return True
    return False
