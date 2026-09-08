"""Confidence gate — stop low-confidence / unmatched tickets before drafting.

Reuses ``MIN_CONFIDENCE_FOR_AUTO_FIX`` from coding_agent so the numeric bar
stays the same (0.6). Unlike coding_agent, a blocked ticket is marked
``needs_manual_review`` on the tickets table and is **not** inserted into
``pending_approval`` — drafts and manual-review items stay in separate queues.
"""

from __future__ import annotations

from typing import Optional

from agents.coding_agent.confidence_gate import MIN_CONFIDENCE_FOR_AUTO_FIX
from agents.techsupport_agent.models import GateResult, KbMatch, Ticket

MIN_CONFIDENCE_FOR_DRAFT = MIN_CONFIDENCE_FOR_AUTO_FIX


class ConfidenceGate:
    """Block reply drafting when classification or KB match is below the bar."""

    def __init__(self, *, min_confidence: float = MIN_CONFIDENCE_FOR_DRAFT) -> None:
        self.min_confidence = float(min_confidence)

    def evaluate(
        self,
        ticket: Ticket,
        kb_match: Optional[KbMatch],
    ) -> GateResult:
        classification_confidence = ticket.classification_confidence
        if classification_confidence is None or classification_confidence < self.min_confidence:
            return GateResult(
                ticket=ticket,
                kb_match=kb_match,
                proceed_to_draft=False,
                reason="low_classification_confidence",
            )
        if kb_match is None or kb_match.confidence < self.min_confidence:
            return GateResult(
                ticket=ticket,
                kb_match=kb_match,
                proceed_to_draft=False,
                reason="no_kb_match",
            )
        return GateResult(
            ticket=ticket,
            kb_match=kb_match,
            proceed_to_draft=True,
            reason="matched",
        )
