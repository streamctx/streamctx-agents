"""Dataclasses for the legal/compliance agent (findings, DPDP checklist, drafts)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

KIND_MISSING_DOC = "missing_doc"
KIND_INCONSISTENCY = "inconsistency"
KIND_STALE_POLICY = "stale_policy"
KIND_DPDP_GAP = "dpdp_gap"
FINDING_KINDS = frozenset(
    {KIND_MISSING_DOC, KIND_INCONSISTENCY, KIND_STALE_POLICY, KIND_DPDP_GAP}
)

SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
SEVERITIES = frozenset({SEVERITY_LOW, SEVERITY_MEDIUM, SEVERITY_HIGH})

STATUS_NEW = "new"
STATUS_NEEDS_MANUAL_REVIEW = "needs_manual_review"
STATUS_DRAFTED = "drafted"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
FINDING_STATUSES = frozenset(
    {
        STATUS_NEW,
        STATUS_NEEDS_MANUAL_REVIEW,
        STATUS_DRAFTED,
        STATUS_APPROVED,
        STATUS_REJECTED,
    }
)

CHECKLIST_COVERED = "covered"
CHECKLIST_GAP = "gap"
CHECKLIST_UNKNOWN = "unknown"
CHECKLIST_STATUSES = frozenset(
    {CHECKLIST_COVERED, CHECKLIST_GAP, CHECKLIST_UNKNOWN}
)

AREA_LOCALIZATION = "data_localization"
AREA_CONSENT = "consent"
AREA_BREACH = "breach_notification"
AREA_RIGHTS = "data_principal_rights"
DPDP_AREAS = frozenset(
    {AREA_LOCALIZATION, AREA_CONSENT, AREA_BREACH, AREA_RIGHTS}
)

DISCLAIMER = (
    "NOT LEGAL ADVICE. Suggested language for founder + qualified counsel review. "
    "Do not publish or treat as StreamCtx policy until a lawyer signs off."
)


@dataclass(frozen=True)
class Finding:
    finding_id: str
    kind: str
    severity: str
    title: str
    evidence: str
    suggested_language: str
    source_path: str
    status: str
    draft_text: str
    draft_entry_id: Optional[str]
    source_fingerprint: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ScanFinding:
    """Pre-persist scan result."""

    kind: str
    severity: str
    title: str
    evidence: str
    suggested_language: str
    source_path: str
    source_fingerprint: str


@dataclass(frozen=True)
class ChecklistItem:
    item_id: str
    area: str
    requirement: str
    current_behavior: str
    gap: str
    status: str
    finding_id: Optional[str]
    updated_at: str


@dataclass(frozen=True)
class PendingApprovalEntry:
    """Suggested policy language awaiting founder + counsel. Never published."""

    entry_id: str
    finding_id: str
    title: str
    content: str
    target: Optional[str]
    mode: str
    status: str
    created_at: str
    reviewed_at: Optional[str] = None
    source_fingerprint: Optional[str] = None
    kind: str = ""


@dataclass(frozen=True)
class RunSummary:
    scanned: int = 0
    findings: int = 0
    drafted: int = 0
    needs_review: int = 0
    skipped: int = 0
    dpdp_gaps: int = 0
    errors: tuple[str, ...] = ()
    entry_ids: tuple[str, ...] = ()
    missing_docs: tuple[str, ...] = ()
