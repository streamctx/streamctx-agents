"""Dashboard Tech Support tab, pending_approval integration, and roster path."""

from __future__ import annotations

from pathlib import Path

from agents.techsupport_agent.kb import KnowledgeBase
from agents.techsupport_agent.models import STATUS_NEEDS_MANUAL_REVIEW
from agents.techsupport_agent.pending_approval import STATUS_PENDING, PendingApprovalStore
from agents.techsupport_agent.storage import TicketStore
from agents.techsupport_agent.techsupport_agent import process_open_tickets
from dashboard import (
    PendingItem,
    RosterDbPaths,
    approve_entry,
    load_pending_approvals,
)

KB_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "techsupport" / "kb"


def _chat(_model: str, _key: str, messages) -> str:
    user = messages[-1]["content"]
    cite = "README.md"
    heading = "Run the pipeline"
    for line in user.splitlines():
        if line.startswith("- Path:"):
            cite = line.split(":", 1)[1].strip()
        elif line.startswith("- Heading:"):
            heading = line.split(":", 1)[1].strip()
    return (
        f"Based on `{cite} § {heading}`: run "
        "`python -m agents.coding_agent.coding_agent run`."
    )


def _seed(db: Path) -> None:
    tickets = TicketStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        tickets.insert_ticket(
            source="github",
            source_ref="42",
            source_url="https://github.com/streamctx/streamctx-agents/issues/42",
            title="How do I run the coding agent pipeline?",
            body="How do I run the coding agent pipeline and review pending_approval?",
            author="alex",
            source_fingerprint="gh:42",
        )
        tickets.insert_ticket(
            source="github",
            source_ref="44",
            source_url="https://github.com/streamctx/streamctx-agents/issues/44",
            title="Istio sidecar drops HTTP/2 when an eBPF kprobe is attached",
            body=(
                "Our GKE cluster's Istio sidecar drops HTTP/2 streams when we "
                "attach a custom eBPF kprobe to the CNI."
            ),
            author="ops",
            source_fingerprint="gh:44",
        )
        process_open_tickets(
            tickets,
            queue,
            kb=KnowledgeBase(root=KB_ROOT),
            chat_fn=_chat,
        )
    finally:
        queue.close()
        tickets.close()


def test_load_pending_approvals_includes_only_drafts(tmp_path: Path, monkeypatch):
    db = tmp_path / "support_tickets.db"
    _seed(db)

    class Store(PendingApprovalStore):
        def __init__(self, db_path=None, **kwargs):
            super().__init__(db_path=db, **kwargs)

    monkeypatch.setattr("dashboard.TechsupportStore", Store)
    items = load_pending_approvals()
    support = [item for item in items if item.store == "techsupport"]
    assert support
    assert all(item.status == STATUS_PENDING for item in support)
    assert all(item.preview for item in support)
    tickets = TicketStore(db_path=db)
    try:
        review = [
            t for t in tickets.list_tickets() if t.status == STATUS_NEEDS_MANUAL_REVIEW
        ]
        assert review
        pending_ticket_ids = {item.entry_id for item in support}
        for ticket in review:
            assert ticket.draft_entry_id not in pending_ticket_ids
            assert ticket.draft_entry_id is None
    finally:
        tickets.close()


def test_approve_entry_does_not_post(tmp_path: Path):
    db = tmp_path / "support_tickets.db"
    _seed(db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    tickets = TicketStore(db_path=db)
    try:
        entry = queue.list_by_status(STATUS_PENDING)[0]
        item = PendingItem(
            agent_key="techsupport",
            agent_name="Tech Support Agent",
            entry_id=entry.entry_id,
            status=entry.status,
            created_at=entry.created_at,
            title=entry.title,
            preview=entry.content,
            store="techsupport",
        )
        approve_entry(item, techsupport_db=db)
        updated = queue.get_entry(entry.entry_id)
        assert updated is not None
        assert updated.status == "approved"
        ticket = tickets.get_ticket(entry.ticket_id)
        assert ticket is not None
        assert ticket.status == "approved"
    finally:
        queue.close()
        tickets.close()


def test_roster_paths_include_support_tickets_db(tmp_path: Path):
    paths = RosterDbPaths(
        coding=tmp_path / "coding_agent.db",
        marketing=tmp_path / "marketing_agent.db",
        competitor=tmp_path / "competitor_agent.db",
        research=tmp_path / "research_agent.db",
        presales=tmp_path / "leads.db",
        techsupport=tmp_path / "support_tickets.db",
    )
    assert paths.techsupport.name == "support_tickets.db"


def test_streamlit_tab_separates_review_from_approval_queue(tmp_path: Path):
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "support_tickets.db"
    _seed(db)
    script = f"""
from agents.techsupport_agent.tab import render_techsupport_tab
import streamlit as st
render_techsupport_tab(st, db_path=r"{db}")
"""
    at = AppTest.from_string(script)
    at.run(timeout=15)
    assert not at.exception
    parts: list[str] = []
    for attr in ("markdown", "subheader", "header", "caption", "success", "info"):
        for item in getattr(at, attr, []) or []:
            parts.append(str(getattr(item, "value", item)))
    body = "\n".join(parts)
    assert "Tech Support" in body
    assert "Needs Manual Review" in body
    assert "Pending Approval" in body
    labels = [button.label for button in at.button]
    assert "Approve" in labels
    assert "Reject" in labels
    assert "Save edits" in labels
    joined = body + " ".join(str(c.value) for c in at.caption)
    assert "not mixed" in joined.lower() or "not in the pending_approval" in joined.lower()
    assert "Nothing is posted" in joined or "not posted" in joined.lower()
    df = at.dataframe[0].value
    records = df.to_dict("records") if hasattr(df, "to_dict") else df
    table_blob = str(records).lower()
    assert "github" in table_blob
    assert "unmatched" in table_blob or "needs_manual_review" in table_blob
    assert "matched" in table_blob or "drafted" in table_blob
