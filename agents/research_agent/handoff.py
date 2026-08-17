"""Stage 6: hand high-feasibility features to coding_agent as prototype intake.

Eligible ideas (classification=feature, high composite, feasibility >= 4)
become a structured ``create_intake_task`` request: prototype as a
proof-of-concept branch, referencing ``idea_id``. No diff is generated, so
coding_agent still requires human sign-off. Status becomes ``prototyped``
only after the intake row exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from agents.coding_agent.models import PendingApprovalEntry
from agents.coding_agent.pending_approval import PendingApprovalStore
from agents.research_agent.models import HandoffResult, ResearchIdea, STATUS_PROTOTYPED
from agents.research_agent.settings import ResearchConfig, default_config
from agents.research_agent.storage import ResearchStore


def research_task_ref(idea_id: str) -> str:
    return f"research:{idea_id}"


def format_prototype_request(idea: ResearchIdea) -> str:
    """Structured coding-agent request. No generated patch."""
    gap = (idea.gap_description or "").strip() or "(none)"
    scores = (
        f"feasibility={idea.feasibility_score} "
        f"pain_match={idea.pain_match_score} "
        f"novelty={idea.novelty_score} "
        f"composite={idea.composite_score}"
    )
    return (
        "Prototype this as a proof-of-concept branch.\n"
        f"idea_id: {idea.idea_id}\n"
        f"title: {idea.title}\n"
        f"gap: {gap}\n"
        f"scores: {scores}\n"
        f"source: {idea.source_url}"
    )


def run_handoff(
    store: ResearchStore,
    *,
    config: Optional[ResearchConfig] = None,
    intake_fn: Optional[Callable[[ResearchIdea], PendingApprovalEntry]] = None,
    approval_store: Optional[PendingApprovalStore] = None,
    coding_db: Optional[Path | str] = None,
    notify: bool = True,
    limit: Optional[int] = None,
) -> HandoffResult:
    spec = config or default_config()
    candidates = store.list_handoff_candidates(
        min_composite=spec.handoff_min_composite,
        min_feasibility=spec.handoff_min_feasibility,
        limit=limit,
    )
    owned_store: Optional[PendingApprovalStore] = None
    submit = intake_fn
    if submit is None:
        coding = approval_store or PendingApprovalStore(
            db_path=coding_db,
            enable_default_notifier=notify,
        )
        if approval_store is None:
            owned_store = coding

        def submit(idea: ResearchIdea) -> PendingApprovalEntry:
            return coding.create_intake_task(
                task_ref=research_task_ref(idea.idea_id),
                summary=idea.title,
                request=format_prototype_request(idea),
                payload=_intake_payload(idea),
                confidence=_confidence(idea),
            )

    handed_off: list[ResearchIdea] = []
    errors: list[tuple[str, str]] = []
    try:
        for idea in candidates:
            try:
                submit(idea)
                updated = store.set_status(idea.idea_id, STATUS_PROTOTYPED)
            except Exception as exc:
                errors.append((idea.idea_id, str(exc)))
                continue
            handed_off.append(updated)
    finally:
        if owned_store is not None:
            owned_store.close()

    return HandoffResult(handed_off=tuple(handed_off), errors=tuple(errors))


def summarize(result: HandoffResult) -> str:
    return f"handed_off={len(result.handed_off)} errors={len(result.errors)}"


def _intake_payload(idea: ResearchIdea) -> dict:
    return {
        "idea_id": idea.idea_id,
        "title": idea.title,
        "gap_description": idea.gap_description,
        "source_url": idea.source_url,
        "feasibility_score": idea.feasibility_score,
        "pain_match_score": idea.pain_match_score,
        "novelty_score": idea.novelty_score,
        "composite_score": idea.composite_score,
    }


def _confidence(idea: ResearchIdea) -> float:
    if idea.composite_score is None:
        return 0.0
    return max(0.0, min(1.0, float(idea.composite_score) / 5.0))
