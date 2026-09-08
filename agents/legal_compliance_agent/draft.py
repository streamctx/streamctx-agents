"""Suggested policy language for founder + counsel. Never published as fact."""

from __future__ import annotations

from agents.legal_compliance_agent.models import DISCLAIMER, ScanFinding
from agents.legal_compliance_agent.pending_approval import (
    MODE_DRAFT_ONLY,
    PendingApprovalStore,
)
from agents.legal_compliance_agent.storage import FindingStore


def wrap_draft(finding: ScanFinding) -> str:
    target = finding.source_path or "(no existing policy file)"
    return (
        f"{DISCLAIMER}\n\n"
        f"## {finding.title}\n\n"
        f"**Kind:** `{finding.kind}` · **Severity:** `{finding.severity}`\n"
        f"**Target document (do not auto-write):** `{target}`\n\n"
        f"### Suggested language\n\n"
        f"{finding.suggested_language.strip()}\n\n"
        f"### Observed evidence\n\n"
        f"{finding.evidence.strip()}\n"
    )


def enqueue_draft(
    finding_id: str,
    finding: ScanFinding,
    *,
    findings: FindingStore,
    store: PendingApprovalStore,
    draft_text: str = "",
) -> str | None:
    """Queue suggested language. Returns None when a pending draft already exists."""
    fingerprint = finding.source_fingerprint
    existing = store.get_by_source_fingerprint(fingerprint)
    if existing is not None:
        findings.record_draft(
            finding_id,
            draft_text=existing.content,
            draft_entry_id=existing.entry_id,
            status="drafted" if existing.status == "pending" else existing.status,
        )
        return None
    body = (draft_text or wrap_draft(finding)).strip()
    entry = store.create_entry(
        finding_id=finding_id,
        content=body,
        title=finding.title,
        target=finding.source_path or None,
        mode=MODE_DRAFT_ONLY,
        source_fingerprint=fingerprint,
        kind=finding.kind,
    )
    findings.record_draft(
        finding_id,
        draft_text=body,
        draft_entry_id=entry.entry_id,
        status="drafted",
    )
    return entry.entry_id
