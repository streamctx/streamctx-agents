"""Dashboard Legal tab, pending_approval integration, and roster path."""

from __future__ import annotations

from pathlib import Path

from agents.legal_compliance_agent.models import KIND_MISSING_DOC, ScanFinding
from agents.legal_compliance_agent.pending_approval import (
    STATUS_PENDING,
    PendingApprovalStore,
)
from agents.legal_compliance_agent.storage import FindingStore
from agents.legal_compliance_agent.legal_compliance_agent import persist_and_queue, run
from dashboard import (
    PendingItem,
    RosterDbPaths,
    approve_entry,
    load_pending_approvals,
)


def _empty_github(_url: str):
    if "/pulls/" in _url and _url.endswith("/files"):
        return []
    if "/commits/" in _url and "/commits?" not in _url:
        return {"files": []}
    return []


def test_run_queues_findings_into_pending_approval(tmp_path: Path):
    db = tmp_path / "compliance_findings.db"
    agents = tmp_path / "agents_root"
    agents.mkdir()
    (agents / "storage.py").write_text(
        "OPENROUTER_API_KEY\nSTREAMCTX_HOME\n", encoding="utf-8"
    )
    summary = run(
        db_path=db,
        enable_notifications=False,
        github=True,
        fetch_github_fn=_empty_github,
        agents_root=agents,
        product_root=tmp_path / "no-sdk",
    )
    assert summary.findings >= 4
    assert summary.drafted >= 4
    assert summary.dpdp_gaps >= 4
    assert "TERMS.md" in summary.missing_docs
    assert "PRIVACY.md" in summary.missing_docs
    findings = FindingStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        pending = queue.list_by_status(STATUS_PENDING)
        assert pending
        assert all("NOT LEGAL ADVICE" in entry.content for entry in pending)
        checklist = findings.list_checklist()
        assert len(checklist) == 4
        assert all(item.status != "covered" for item in checklist)
    finally:
        queue.close()
        findings.close()


def test_load_pending_approvals_includes_legal_drafts(tmp_path: Path, monkeypatch):
    db = tmp_path / "compliance_findings.db"
    findings = FindingStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        persist_and_queue(
            [
                ScanFinding(
                    kind=KIND_MISSING_DOC,
                    severity="high",
                    title="Missing PRIVACY.md",
                    evidence="absent",
                    suggested_language="Draft notice language.",
                    source_path="PRIVACY.md",
                    source_fingerprint="fp-dash-privacy",
                )
            ],
            findings=findings,
            store=queue,
        )
    finally:
        queue.close()
        findings.close()

    class Store(PendingApprovalStore):
        def __init__(self, db_path=None, **kwargs):
            super().__init__(db_path=db, **kwargs)

    monkeypatch.setattr(
        "dashboard.LegalStore",
        Store,
    )
    items = load_pending_approvals()
    legal = [item for item in items if item.store == "legal"]
    assert legal
    assert legal[0].status == STATUS_PENDING


def test_approve_entry_legal_does_not_write_docs(tmp_path: Path):
    db = tmp_path / "compliance_findings.db"
    findings = FindingStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        entry_ids, _, _ = persist_and_queue(
            [
                ScanFinding(
                    kind=KIND_MISSING_DOC,
                    severity="high",
                    title="Missing TERMS.md",
                    evidence="absent",
                    suggested_language="Draft terms.",
                    source_path="TERMS.md",
                    source_fingerprint="fp-dash-terms",
                )
            ],
            findings=findings,
            store=queue,
        )
        entry = queue.get_entry(entry_ids[0])
        assert entry is not None
        item = PendingItem(
            agent_key="legal",
            agent_name="Legal / Compliance Agent",
            entry_id=entry.entry_id,
            status=entry.status,
            created_at=entry.created_at,
            title=entry.title,
            preview=entry.content,
            store="legal",
        )
        approve_entry(item, legal_db=db)
        updated = queue.get_entry(entry.entry_id)
        assert updated is not None
        assert updated.status == "approved"
        assert not (tmp_path / "TERMS.md").exists()
    finally:
        queue.close()
        findings.close()


def test_roster_paths_include_compliance_findings_db(tmp_path: Path):
    paths = RosterDbPaths(
        coding=tmp_path / "coding_agent.db",
        marketing=tmp_path / "marketing_agent.db",
        competitor=tmp_path / "competitor_agent.db",
        research=tmp_path / "research_agent.db",
        presales=tmp_path / "leads.db",
        techsupport=tmp_path / "support_tickets.db",
        legal=tmp_path / "compliance_findings.db",
    )
    assert paths.legal.name == "compliance_findings.db"


def test_streamlit_legal_tab_shows_findings_and_dpdp(tmp_path: Path):
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "compliance_findings.db"
    agents = tmp_path / "root"
    agents.mkdir()
    (agents / "storage.py").write_text(
        "OPENROUTER_API_KEY\nSTREAMCTX_HOME\n", encoding="utf-8"
    )
    run(
        db_path=db,
        enable_notifications=False,
        github=False,
        agents_root=agents,
        product_root=tmp_path / "no-sdk",
    )
    script = f"""
from agents.legal_compliance_agent.tab import render_legal_tab
import streamlit as st
render_legal_tab(st, db_path=r"{db}")
"""
    at = AppTest.from_string(script)
    at.run(timeout=15)
    assert not at.exception
    parts: list[str] = []
    for attr in ("markdown", "subheader", "header", "caption", "success", "info"):
        for item in getattr(at, attr, []) or []:
            parts.append(str(getattr(item, "value", item)))
    body = "\n".join(parts)
    assert "Legal" in body
    assert "DPDP" in body
    assert "Pending Approval" in body
    assert "Findings" in body
    labels = [button.label for button in at.button]
    assert "Approve" in labels
    assert "Reject" in labels
    assert "Save edits" in labels
    joined = body + " ".join(str(c.value) for c in at.caption)
    assert "not legal advice" in joined.lower()
    assert "never edits" in joined.lower() or "does not write" in joined.lower() or "not write" in joined.lower()
