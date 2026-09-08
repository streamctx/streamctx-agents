"""pending_approval for suggested language — copy only, never writes policy files."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agents.legal_compliance_agent.models import (
    KIND_MISSING_DOC,
    STATUS_APPROVED,
    ScanFinding,
)
from agents.legal_compliance_agent.pending_approval import (
    MODE_DRAFT_ONLY,
    STATUS_PENDING,
    STATUS_REJECTED,
    PendingApprovalStore,
)
from agents.legal_compliance_agent.storage import FindingStore
from agents.legal_compliance_agent.legal_compliance_agent import (
    approve_draft,
    persist_and_queue,
    reject_draft,
)


def test_pending_approval_schema(tmp_path: Path):
    db = tmp_path / "compliance_findings.db"
    findings = FindingStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        conn = sqlite3.connect(db)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "findings" in tables
        assert "pending_approval" in tables
        assert "dpdp_checklist" in tables
        columns = {
            col[1]
            for col in conn.execute("PRAGMA table_info(pending_approval)").fetchall()
        }
        assert {
            "entry_id",
            "finding_id",
            "title",
            "content",
            "target",
            "mode",
            "status",
            "created_at",
            "reviewed_at",
            "source_fingerprint",
            "kind",
        } <= columns
        conn.close()
    finally:
        queue.close()
        findings.close()


def test_queue_and_approve_does_not_write_policy_files(tmp_path: Path):
    db = tmp_path / "compliance_findings.db"
    terms = tmp_path / "TERMS.md"
    privacy = tmp_path / "PRIVACY.md"
    findings = FindingStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        scan = ScanFinding(
            kind=KIND_MISSING_DOC,
            severity="high",
            title="Missing policy document: TERMS.md",
            evidence="not found",
            suggested_language="Draft TERMS.md for counsel.",
            source_path="TERMS.md",
            source_fingerprint="fp-terms-missing",
        )
        entry_ids, drafted, _skipped = persist_and_queue(
            [scan], findings=findings, store=queue
        )
        assert drafted == 1
        entry_id = entry_ids[0]
        entry = queue.get_entry(entry_id)
        assert entry is not None
        assert entry.status == STATUS_PENDING
        assert entry.mode == MODE_DRAFT_ONLY
        assert "NOT LEGAL ADVICE" in entry.content
        approve_draft(entry_id, db_path=db)
        assert queue.get_entry(entry_id).status == "approved"
        finding = findings.get_by_fingerprint("fp-terms-missing")
        assert finding is not None
        assert finding.status == STATUS_APPROVED
        assert not terms.exists()
        assert not privacy.exists()
    finally:
        queue.close()
        findings.close()


def test_reject_and_dedupe(tmp_path: Path):
    db = tmp_path / "compliance_findings.db"
    findings = FindingStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        scan = ScanFinding(
            kind=KIND_MISSING_DOC,
            severity="high",
            title="Missing PRIVACY.md",
            evidence="absent",
            suggested_language="Draft PRIVACY.md",
            source_path="PRIVACY.md",
            source_fingerprint="fp-privacy",
        )
        first, drafted, _ = persist_and_queue([scan], findings=findings, store=queue)
        assert drafted == 1
        second, drafted2, skipped = persist_and_queue(
            [scan], findings=findings, store=queue
        )
        assert drafted2 == 0
        assert skipped == 1
        assert second == []
        reject_draft(first[0], db_path=db)
        assert queue.get_entry(first[0]).status == STATUS_REJECTED
    finally:
        queue.close()
        findings.close()
