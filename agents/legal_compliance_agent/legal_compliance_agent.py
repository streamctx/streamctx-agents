"""
Legal / Compliance Agent — scan policy docs vs code, flag DPDP gaps, draft language.

Never edits TERMS.md / PRIVACY.md / other legal docs. Never publishes. Never
states legal advice as fact. Every finding lands in pending_approval as suggested
language for founder + qualified counsel.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from streamctx import get_tracker

from agents.legal_compliance_agent.draft import enqueue_draft, wrap_draft
from agents.legal_compliance_agent.dpdp import build_dpdp_checklist
from agents.legal_compliance_agent.github_changes import scan_github_changes
from agents.legal_compliance_agent.models import (
    STATUS_APPROVED,
    STATUS_DRAFTED,
    STATUS_REJECTED,
    RunSummary,
    ScanFinding,
)
from agents.legal_compliance_agent.pending_approval import PendingApprovalStore
from agents.legal_compliance_agent.scan_docs import scan_docs
from agents.legal_compliance_agent.settings import LegalConfig, default_config
from agents.legal_compliance_agent.storage import FindingStore
from shared.audit_log import log_action
from shared.config import AGENT_IDS, STATUS_PENDING


def persist_and_queue(
    scans: list[ScanFinding],
    *,
    findings: FindingStore,
    store: PendingApprovalStore,
) -> tuple[list[str], int, int]:
    """Upsert findings and queue new drafts. Returns (entry_ids, drafted, skipped)."""
    entry_ids: list[str] = []
    drafted = 0
    skipped = 0
    for scan in scans:
        finding, _created = findings.upsert_finding(
            kind=scan.kind,
            severity=scan.severity,
            title=scan.title,
            evidence=scan.evidence,
            suggested_language=scan.suggested_language,
            source_path=scan.source_path,
            source_fingerprint=scan.source_fingerprint,
        )
        if finding.draft_entry_id:
            existing = store.get_entry(finding.draft_entry_id)
            if existing is not None and existing.status in {"pending", "approved"}:
                skipped += 1
                continue
        entry_id = enqueue_draft(
            finding.finding_id,
            scan,
            findings=findings,
            store=store,
            draft_text=wrap_draft(scan),
        )
        if entry_id is None:
            skipped += 1
            continue
        drafted += 1
        entry_ids.append(entry_id)
    return entry_ids, drafted, skipped


def run(
    *,
    db_path: Optional[Path | str] = None,
    config: Optional[LegalConfig] = None,
    enable_notifications: bool = True,
    github: bool = True,
    fetch_github_fn=None,
    agents_root: Optional[Path | str] = None,
    product_root: Optional[Path | str] = None,
) -> RunSummary:
    """Scan docs/code/PRs, refresh DPDP checklist, queue drafts. Does not edit policies."""
    spec = config or default_config(
        agents_root=agents_root,
        product_root=product_root,
    )
    if agents_root is not None or product_root is not None:
        spec = default_config(
            agents_root=agents_root if agents_root is not None else spec.agents_root,
            product_root=product_root if product_root is not None else spec.product_root,
            github_repo_name=spec.github_repo,
        )
    findings = FindingStore(db_path=db_path)
    store = PendingApprovalStore(
        db_path=db_path,
        enable_default_notifier=enable_notifications,
    )
    errors: list[str] = []
    try:
        doc_findings, docs, facts = scan_docs(
            agents_root=spec.agents_root,
            product_root=spec.product_root,
        )
        gh_findings: list[ScanFinding] = []
        if github:
            try:
                gh_findings, gh_errors = scan_github_changes(
                    spec, fetch_fn=fetch_github_fn
                )
                errors.extend(gh_errors)
            except Exception as exc:
                errors.append(f"github: {exc}")

        checklist_rows, dpdp_findings = build_dpdp_checklist(facts, docs)
        for row in checklist_rows:
            findings.upsert_checklist(
                item_id=row["item_id"],
                area=row["area"],
                requirement=row["requirement"],
                current_behavior=row["current_behavior"],
                gap=row["gap"],
                status=row["status"],
            )

        scans = doc_findings + gh_findings + dpdp_findings
        entry_ids, drafted, skipped = persist_and_queue(
            scans, findings=findings, store=store
        )
        missing = tuple(
            name for name, doc in docs.items() if not doc.found
        )
        dpdp_gaps = sum(1 for row in checklist_rows if row["status"] == "gap")
        summary = RunSummary(
            scanned=facts.scanned_files,
            findings=len(scans),
            drafted=drafted,
            needs_review=len(errors),
            skipped=skipped,
            dpdp_gaps=dpdp_gaps,
            errors=tuple(errors),
            entry_ids=tuple(entry_ids),
            missing_docs=missing,
        )
        print(
            f"[legal-compliance-agent] scanned={summary.scanned} "
            f"findings={summary.findings} drafted={summary.drafted} "
            f"skipped={summary.skipped} dpdp_gaps={summary.dpdp_gaps} "
            f"missing={','.join(summary.missing_docs) or 'none'}"
        )
        print(
            "[legal-compliance-agent] drafts only — not legal advice, "
            "policy files were not edited"
        )
        for entry_id in summary.entry_ids:
            print(f"  pending_approval id={entry_id}")
        for error in summary.errors:
            print(f"  error: {error}")
        return summary
    finally:
        store.close()
        findings.close()


def approve_draft(
    entry_id: str,
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    """Mark suggested language as founder-reviewed. Does not write TERMS.md or PRIVACY.md."""
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    findings = FindingStore(db_path=db_path)
    try:
        entry = store.approve(entry_id)
        findings.record_draft(
            entry.finding_id,
            draft_text=entry.content,
            draft_entry_id=entry.entry_id,
            status=STATUS_APPROVED,
        )
        print(
            f"[legal-compliance-agent] approved {entry_id} "
            "(copy only — TERMS.md/PRIVACY.md not written; not legal advice)"
        )
    finally:
        store.close()
        findings.close()


def reject_draft(
    entry_id: str,
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    findings = FindingStore(db_path=db_path)
    try:
        entry = store.reject(entry_id)
        findings.record_draft(
            entry.finding_id,
            draft_text=entry.content,
            draft_entry_id=entry.entry_id,
            status=STATUS_REJECTED,
        )
        print(f"[legal-compliance-agent] rejected {entry_id}")
    finally:
        store.close()
        findings.close()


def save_edited_draft(
    entry_id: str,
    content: str,
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    findings = FindingStore(db_path=db_path)
    try:
        entry = store.update_content(entry_id, content)
        if entry is None:
            raise KeyError(entry_id)
        finding = findings.get_finding(entry.finding_id)
        status = STATUS_DRAFTED
        if finding is not None and finding.status in {STATUS_DRAFTED, STATUS_APPROVED}:
            status = finding.status
        findings.record_draft(
            entry.finding_id,
            draft_text=entry.content,
            draft_entry_id=entry.entry_id,
            status=status,
        )
    finally:
        store.close()
        findings.close()


def _cmd_run(args: argparse.Namespace) -> int:
    tracker = get_tracker(AGENT_IDS["legal"])
    tracker.start()
    try:
        summary = run(
            enable_notifications=not args.no_notify,
            github=not args.no_github,
        )
        session_id = tracker.get_session_id()
        log_action(
            agent_name=AGENT_IDS["legal"],
            session_id=session_id,
            action_type="legal_compliance_drafts",
            payload={
                "scanned": summary.scanned,
                "findings": summary.findings,
                "drafted": summary.drafted,
                "dpdp_gaps": summary.dpdp_gaps,
                "missing_docs": list(summary.missing_docs),
            },
            status=STATUS_PENDING,
        )
    finally:
        tracker.stop()
    return 1 if summary.errors and not summary.drafted else 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "StreamCtx Legal / Compliance Agent — scan docs, draft DPDP checklist. "
            "Never edits legal files. Not legal advice."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Scan policies/code and queue suggested language")
    run_p.add_argument("--no-notify", action="store_true")
    run_p.add_argument("--no-github", action="store_true")
    run_p.set_defaults(func=_cmd_run)

    ap = sub.add_parser("approve", help="Approve copy (does not write policy files)")
    ap.add_argument("entry_id")
    ap.set_defaults(func=lambda args: (approve_draft(args.entry_id), 0)[1])

    rj = sub.add_parser("reject", help="Reject a draft")
    rj.add_argument("entry_id")
    rj.set_defaults(func=lambda args: (reject_draft(args.entry_id), 0)[1])
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
