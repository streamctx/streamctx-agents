"""Generate a LinkedIn draft from a human-approved dashboard brief.

Roster Assign writes a tagged brief (``dashboard-brief:…``), not finished copy.
Approving that row is the first gate: this module calls ``draft_post``, runs
``LinkedInAdapter.submit`` (SafetyGate), and writes a child ``pending`` row
for a second review. LinkedIn stays ``draft_only`` — nothing is posted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from agents.marketing_agent.adapters.base import DraftAdapter
from agents.marketing_agent.adapters.linkedin import LINKEDIN_MAX_CHARS, LinkedInAdapter
from agents.marketing_agent.models import PendingApprovalEntry
from agents.marketing_agent.pending_approval import (
    CONTENT_POST,
    MODE_DRAFT_ONLY,
    PLATFORM_LINKEDIN,
    STATUS_DRAFT_FAILED,
    STATUS_PENDING,
    PendingApprovalStore,
    draft_child_fingerprint,
    is_marketing_brief,
    new_brief_fingerprint,
)

DraftFn = Callable[[str, str, str], str]


@dataclass(frozen=True)
class DraftJob:
    """Human-approved Assign brief that should become a LinkedIn draft."""

    parent_entry_id: str
    brief: str
    platform: str = PLATFORM_LINKEDIN


@dataclass(frozen=True)
class DraftJobResult:
    parent_entry_id: str
    child_entry: Optional[PendingApprovalEntry]
    success: bool
    skipped: bool
    reason: str


def create_brief_task(
    text: str,
    *,
    store: Optional[PendingApprovalStore] = None,
    db_path: Optional[Path | str] = None,
) -> PendingApprovalEntry:
    """Queue a Roster brief. Content is the request, not generated copy."""
    body = (text or "").strip()
    if not body:
        raise ValueError("task is empty")
    owns_store = store is None
    db = store or PendingApprovalStore(
        db_path=db_path,
        enable_default_notifier=False,
    )
    try:
        return db.create_entry(
            platform=PLATFORM_LINKEDIN,
            content_type=CONTENT_POST,
            content=body,
            mode=MODE_DRAFT_ONLY,
            source_fingerprint=new_brief_fingerprint(),
        )
    finally:
        if owns_store:
            db.close()


def process_approved_brief(
    entry_id: str,
    *,
    db_path: Optional[Path | str] = None,
    approval_store: Optional[PendingApprovalStore] = None,
    draft_fn: Optional[DraftFn] = None,
    adapter: Optional[DraftAdapter] = None,
) -> DraftJobResult:
    """Entry point used by the dashboard executor after brief Approve."""
    owns_store = approval_store is None
    store = approval_store or PendingApprovalStore(
        db_path=db_path,
        enable_default_notifier=False,
    )
    try:
        parent = store.get_entry(entry_id)
        if parent is None:
            return DraftJobResult(
                parent_entry_id=entry_id,
                child_entry=None,
                success=False,
                skipped=True,
                reason="missing_entry",
            )
        if not is_marketing_brief(parent):
            return DraftJobResult(
                parent_entry_id=parent.entry_id,
                child_entry=None,
                success=False,
                skipped=True,
                reason="not_brief",
            )

        child_fp = draft_child_fingerprint(parent.entry_id)
        existing = store.get_by_source_fingerprint(child_fp)
        if existing is not None:
            return DraftJobResult(
                parent_entry_id=parent.entry_id,
                child_entry=existing,
                success=existing.status == STATUS_PENDING,
                skipped=True,
                reason="already_has_child",
            )

        job = DraftJob(parent_entry_id=parent.entry_id, brief=parent.content)
        writer = adapter or LinkedInAdapter(store)
        generate = draft_fn or _call_draft_post
        return _generate_child(store, job, child_fp, generate, writer)
    finally:
        if owns_store:
            store.close()


def _generate_child(
    store: PendingApprovalStore,
    job: DraftJob,
    child_fp: str,
    draft_fn: DraftFn,
    adapter: DraftAdapter,
) -> DraftJobResult:
    goal = job.brief.splitlines()[0].strip() or job.brief
    try:
        raw = draft_fn("linkedin_post", job.brief, goal)
        text = _clip_linkedin((raw or "").strip())
        if not text:
            raise ValueError("draft_post returned empty copy")
        child_id = adapter.submit(text, fingerprint=child_fp)
        child = store.get_entry(child_id)
        return DraftJobResult(
            parent_entry_id=job.parent_entry_id,
            child_entry=child,
            success=True,
            skipped=False,
            reason="pending",
        )
    except Exception as exc:
        child = _create_failed_child(store, job, child_fp, str(exc))
        return DraftJobResult(
            parent_entry_id=job.parent_entry_id,
            child_entry=child,
            success=False,
            skipped=False,
            reason=STATUS_DRAFT_FAILED,
        )


def _create_failed_child(
    store: PendingApprovalStore,
    job: DraftJob,
    child_fp: str,
    error: str,
) -> PendingApprovalEntry:
    return store.create_entry(
        platform=PLATFORM_LINKEDIN,
        content_type=CONTENT_POST,
        content=f"draft_failed: {error}",
        mode=MODE_DRAFT_ONLY,
        status=STATUS_DRAFT_FAILED,
        source_fingerprint=child_fp,
    )


def _call_draft_post(platform: str, context_text: str, goal: str) -> str:
    from openai import OpenAI

    from agents.marketing_agent.marketing_agent import draft_post
    from shared.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL

    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is required for draft generation.")
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
    return draft_post(platform, context_text, goal, client) or ""


def _clip_linkedin(text: str) -> str:
    if len(text) <= LINKEDIN_MAX_CHARS:
        return text
    clipped = text[: LINKEDIN_MAX_CHARS - 1].rsplit(" ", 1)[0]
    return clipped.rstrip(",;:") + "…"
