"""Dataclasses for the pre-sales agent (CSV leads, scoring, draft queue)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


PIPELINE_IMPORTED = "imported"
PIPELINE_CONTACTED = "contacted"
PIPELINE_REPLIED = "replied"
PIPELINE_MEETING = "meeting"
PIPELINE_CLOSED = "closed"
PIPELINE_LOST = "lost"
PIPELINE_STATUSES = frozenset(
    {
        PIPELINE_IMPORTED,
        PIPELINE_CONTACTED,
        PIPELINE_REPLIED,
        PIPELINE_MEETING,
        PIPELINE_CLOSED,
        PIPELINE_LOST,
    }
)
PIPELINE_ORDER: tuple[str, ...] = (
    PIPELINE_IMPORTED,
    PIPELINE_CONTACTED,
    PIPELINE_REPLIED,
    PIPELINE_MEETING,
    PIPELINE_CLOSED,
    PIPELINE_LOST,
)

DRAFT_NONE = "none"
DRAFT_DRAFTED = "drafted"
DRAFT_PENDING = "pending_approval"
DRAFT_APPROVED = "approved"
DRAFT_REJECTED = "rejected"
DRAFT_STATUSES = frozenset(
    {
        DRAFT_NONE,
        DRAFT_DRAFTED,
        DRAFT_PENDING,
        DRAFT_APPROVED,
        DRAFT_REJECTED,
    }
)

FLAG_NONE = ""
FLAG_LEGAL = "Legal review"
FLAG_PRICING = "Pricing sign-off"
FLAGS = frozenset({FLAG_NONE, FLAG_LEGAL, FLAG_PRICING})


@dataclass(frozen=True)
class Lead:
    """One row in ``leads.db``."""

    lead_id: str
    name: str
    title: str
    company: str
    company_size: str
    industry: str
    linkedin_url: Optional[str]
    score: Optional[float]
    score_rationale: str
    pipeline_status: str
    draft_status: str
    draft_text: str
    draft_entry_id: Optional[str]
    flag: str
    source_fingerprint: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class LeadScore:
    """Role / company-size / industry fit for one lead."""

    score: float
    role_score: float
    size_score: float
    industry_score: float
    rationale: str


@dataclass(frozen=True)
class PendingApprovalEntry:
    """One outreach draft awaiting founder review. Never sent by the agent."""

    entry_id: str
    lead_id: str
    title: str
    content: str
    target: Optional[str]
    mode: str
    status: str
    created_at: str
    reviewed_at: Optional[str] = None
    source_fingerprint: Optional[str] = None
    flag: str = FLAG_NONE


@dataclass(frozen=True)
class ImportResult:
    imported: int
    skipped: int
    updated: int
    errors: tuple[str, ...]
    lead_ids: tuple[str, ...]


@dataclass(frozen=True)
class RunSummary:
    scored: int = 0
    queued: int = 0
    skipped: int = 0
    flagged: int = 0
    errors: tuple[str, ...] = ()
    entry_ids: tuple[str, ...] = ()
    model_used: str = ""
