"""Dataclasses for the tech-support agent (tickets, KB matches, draft queue)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SOURCE_GITHUB = "github"
SOURCE_DISCORD = "discord"
SOURCE_MANUAL = "manual"
SOURCES = frozenset({SOURCE_GITHUB, SOURCE_DISCORD, SOURCE_MANUAL})

TYPE_QUESTION = "question"
TYPE_BUG = "bug"
TYPE_FEATURE = "feature-request"
TICKET_TYPES = frozenset({TYPE_QUESTION, TYPE_BUG, TYPE_FEATURE})

SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
SEVERITIES = frozenset({SEVERITY_LOW, SEVERITY_MEDIUM, SEVERITY_HIGH})

STATUS_NEW = "new"
STATUS_CLASSIFIED = "classified"
STATUS_NEEDS_MANUAL_REVIEW = "needs_manual_review"
STATUS_DRAFTED = "drafted"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
TICKET_STATUSES = frozenset(
    {
        STATUS_NEW,
        STATUS_CLASSIFIED,
        STATUS_NEEDS_MANUAL_REVIEW,
        STATUS_DRAFTED,
        STATUS_APPROVED,
        STATUS_REJECTED,
    }
)

KB_MATCHED = "matched"
KB_UNMATCHED = "unmatched"
KB_PENDING = "pending"


@dataclass(frozen=True)
class Ticket:
    """One incoming support question stored in ``support_tickets.db``."""

    ticket_id: str
    source: str
    source_ref: str
    source_url: str
    title: str
    body: str
    author: str
    ticket_type: Optional[str]
    severity: Optional[str]
    classification_confidence: Optional[float]
    classification_rationale: str
    kb_match_path: str
    kb_match_heading: str
    kb_match_excerpt: str
    kb_match_confidence: Optional[float]
    status: str
    draft_text: str
    draft_entry_id: Optional[str]
    source_fingerprint: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class IncomingItem:
    """Raw poll payload before persistence."""

    source: str
    source_ref: str
    source_url: str
    title: str
    body: str
    author: str
    source_fingerprint: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class Classification:
    ticket_type: str
    severity: str
    confidence: float
    rationale: str


@dataclass(frozen=True)
class KbMatch:
    path: str
    heading: str
    excerpt: str
    confidence: float
    citation: str


@dataclass(frozen=True)
class GateResult:
    """Same shape as coding_agent: proceed or stop for a human."""

    ticket: Ticket
    kb_match: Optional[KbMatch]
    proceed_to_draft: bool
    reason: str


@dataclass(frozen=True)
class PendingApprovalEntry:
    """A reply draft awaiting founder review. Never posted by the agent."""

    entry_id: str
    ticket_id: str
    title: str
    content: str
    target: Optional[str]
    mode: str
    status: str
    created_at: str
    reviewed_at: Optional[str] = None
    source_fingerprint: Optional[str] = None
    kb_citation: str = ""


@dataclass(frozen=True)
class PollResult:
    inserted: tuple[Ticket, ...] = ()
    skipped_duplicate: tuple[str, ...] = ()
    skipped_interval: tuple[str, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RunSummary:
    ingested: int = 0
    classified: int = 0
    drafted: int = 0
    needs_review: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()
    entry_ids: tuple[str, ...] = ()
    model_used: str = ""
