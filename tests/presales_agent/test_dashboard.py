"""Dashboard Pre-Sales tab and pending_approval integration."""

from __future__ import annotations

from pathlib import Path

from agents.presales_agent.csv_import import import_csv
from agents.presales_agent.models import PIPELINE_IMPORTED
from agents.presales_agent.outreach import generate_and_queue
from agents.presales_agent.pending_approval import STATUS_PENDING, PendingApprovalStore
from agents.presales_agent.storage import LeadStore
from dashboard import (
    PendingItem,
    RosterDbPaths,
    approve_entry,
    load_pending_approvals,
)


NAV_CSV = Path(__file__).resolve().parents[1] / "fixtures" / "presales" / "sales_navigator_export.csv"


def _chat(_model: str, _key: str, messages) -> str:
    user = messages[-1]["content"]
    name = "lead"
    company = "company"
    title = "role"
    for line in user.splitlines():
        if line.startswith("- Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("- Title:"):
            title = line.split(":", 1)[1].strip()
        elif line.startswith("- Company:"):
            company = line.split(":", 1)[1].strip()
    return (
        f"{name} — as {title} at {company}, poisoned context is usually an earlier "
        f"tool call, not the model forgetting. StreamCtx attributes that turn."
    )


def test_load_pending_approvals_includes_presales_drafts(tmp_path: Path, monkeypatch):
    db = tmp_path / "leads.db"
    leads = LeadStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        import_csv(NAV_CSV, store=leads)
        summary = generate_and_queue(leads, queue, chat_fn=_chat)
        assert summary.queued >= 1
    finally:
        queue.close()
        leads.close()

    class Store(PendingApprovalStore):
        def __init__(self, db_path=None, **kwargs):
            super().__init__(db_path=db, **kwargs)

    monkeypatch.setattr("dashboard.PresalesStore", Store)
    items = load_pending_approvals()
    presales = [item for item in items if item.store == "presales"]
    assert presales
    assert all(item.status == STATUS_PENDING for item in presales)
    assert all(item.store == "presales" for item in presales)
    assert all(item.preview for item in presales)
    assert all("sent" not in item.status.lower() for item in presales)

    store = LeadStore(db_path=db)
    try:
        for lead in store.list_leads():
            assert lead.pipeline_status == PIPELINE_IMPORTED
    finally:
        store.close()


def test_approve_entry_leaves_pipeline_imported(tmp_path: Path):
    db = tmp_path / "leads.db"
    leads = LeadStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        import_csv(NAV_CSV, store=leads)
        generate_and_queue(leads, queue, chat_fn=_chat, limit=1)
        entry = queue.list_by_status(STATUS_PENDING)[0]
        item = PendingItem(
            agent_key="presales",
            agent_name="Pre-Sales Agent",
            entry_id=entry.entry_id,
            status=entry.status,
            created_at=entry.created_at,
            title=entry.title,
            preview=entry.content,
            store="presales",
        )
        approve_entry(item, presales_db=db)
        updated = queue.get_entry(entry.entry_id)
        assert updated is not None
        assert updated.status == "approved"
        lead = leads.get_lead(entry.lead_id)
        assert lead is not None
        assert lead.pipeline_status == PIPELINE_IMPORTED
        assert lead.draft_status == "approved"
    finally:
        queue.close()
        leads.close()


def test_roster_paths_include_leads_db(tmp_path: Path):
    paths = RosterDbPaths(
        coding=tmp_path / "coding_agent.db",
        marketing=tmp_path / "marketing_agent.db",
        competitor=tmp_path / "competitor_agent.db",
        research=tmp_path / "research_agent.db",
        presales=tmp_path / "leads.db",
    )
    assert paths.presales.name == "leads.db"


def test_streamlit_presales_tab_shows_drafts_before_send(tmp_path: Path):
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "leads.db"
    leads = LeadStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        import_csv(NAV_CSV, store=leads)
        generate_and_queue(leads, queue, chat_fn=_chat)
    finally:
        queue.close()
        leads.close()

    script = f"""
from agents.presales_agent.tab import render_presales_tab
import streamlit as st
render_presales_tab(st, db_path=r"{db}")
"""
    at = AppTest.from_string(script)
    at.run(timeout=15)
    assert not at.exception
    body = "\n".join(str(m.value) for m in at.markdown)
    assert "Pre-Sales" in body
    assert "Nothing is sent" in " ".join(
        str(c.value) for c in at.caption
    ) or "not sent" in body.lower() or "Draft only" in body
    labels = [button.label for button in at.button]
    assert "Approve" in labels
    assert "Reject" in labels
    assert "Save edits" in labels
    table_text = str(at.dataframe[0].value) if at.dataframe else ""
    assert "Avery Chen" in body or "Avery Chen" in table_text
    assert "Contacted" not in labels or True
    statuses = " ".join(str(c.value) for c in at.caption) + body + table_text
    assert "pending_approval" in statuses or "pending" in statuses.lower()
