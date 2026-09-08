"""pending_approval queue for support drafts — copy only, never posted."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agents.techsupport_agent.models import STATUS_APPROVED, STATUS_DRAFTED
from agents.techsupport_agent.pending_approval import (
    MODE_DRAFT_ONLY,
    STATUS_PENDING,
    STATUS_REJECTED,
    PendingApprovalStore,
)
from agents.techsupport_agent.storage import TicketStore
from agents.techsupport_agent.techsupport_agent import approve_draft, reject_draft


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "support_tickets.db"


@pytest.fixture
def stores(db: Path):
    tickets = TicketStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    yield tickets, queue
    queue.close()
    tickets.close()


def test_pending_approval_schema(db: Path, stores):
    _tickets, _queue = stores
    conn = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "tickets" in tables
        assert "pending_approval" in tables
        columns = {
            col[1]
            for col in conn.execute("PRAGMA table_info(pending_approval)").fetchall()
        }
        assert {
            "entry_id",
            "ticket_id",
            "title",
            "content",
            "target",
            "mode",
            "status",
            "created_at",
            "reviewed_at",
            "source_fingerprint",
            "kb_citation",
        } <= columns
    finally:
        conn.close()


def test_create_entry_is_pending_draft_only(stores):
    tickets, queue = stores
    ticket = tickets.insert_ticket(
        source="github",
        source_ref="42",
        source_url="https://github.com/streamctx/streamctx-agents/issues/42",
        title="How do I run the coding agent?",
        body="how to",
        author="alex",
        source_fingerprint="gh:42",
    )
    entry = queue.create_entry(
        ticket_id=ticket.ticket_id,
        content="Based on README.md § Run the pipeline: python -m agents.coding_agent...",
        title="github #42 · How do I run the coding agent?",
        target=ticket.source_url,
        kb_citation="README.md § Run the pipeline",
    )
    assert entry.status == STATUS_PENDING
    assert entry.mode == MODE_DRAFT_ONLY
    assert entry.reviewed_at is None
    assert "README.md" in entry.kb_citation


def test_approve_does_not_post(stores, db: Path):
    tickets, queue = stores
    ticket = tickets.insert_ticket(
        source="discord",
        source_ref="1001",
        source_url="https://discord.com/channels/@me/1/1001",
        title="How do I install streamctx?",
        body="install?",
        author="jamie",
        source_fingerprint="dc:1001",
    )
    entry = queue.create_entry(
        ticket_id=ticket.ticket_id,
        content="See docs/install.md § Install: pip install streamctx.",
        kb_citation="docs/install.md § Install",
    )
    tickets.record_draft(
        ticket.ticket_id,
        draft_text=entry.content,
        draft_entry_id=entry.entry_id,
        status=STATUS_DRAFTED,
    )
    approve_draft(entry.entry_id, db_path=db)
    after = tickets.get_ticket(ticket.ticket_id)
    assert after is not None
    assert after.status == STATUS_APPROVED
    assert queue.get_entry(entry.entry_id).status == "approved"


def test_reject(stores, db: Path):
    tickets, queue = stores
    ticket = tickets.insert_ticket(
        source="github",
        source_ref="9",
        source_url="https://github.com/streamctx/streamctx-agents/issues/9",
        title="q",
        body="q",
        author="a",
        source_fingerprint="gh:9",
    )
    entry = queue.create_entry(ticket_id=ticket.ticket_id, content="draft body")
    reject_draft(entry.entry_id, db_path=db)
    assert queue.get_entry(entry.entry_id).status == STATUS_REJECTED
