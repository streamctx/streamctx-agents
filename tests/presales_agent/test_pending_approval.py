"""pending_approval queue for outreach drafts — copy only, never sent."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agents.presales_agent.models import DRAFT_APPROVED, DRAFT_PENDING, PIPELINE_IMPORTED
from agents.presales_agent.outreach import enqueue_draft
from agents.presales_agent.pending_approval import (
    MODE_DRAFT_ONLY,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    PendingApprovalStore,
)
from agents.presales_agent.presales_agent import approve_draft
from agents.presales_agent.storage import LeadStore


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "leads.db"


@pytest.fixture
def stores(db: Path):
    leads = LeadStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    yield leads, queue
    queue.close()
    leads.close()


def test_pending_approval_schema(db: Path, stores):
    _leads, _queue = stores
    conn = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "leads" in tables
        assert "pending_approval" in tables
        columns = {
            col[1]
            for col in conn.execute("PRAGMA table_info(pending_approval)").fetchall()
        }
        assert {
            "entry_id",
            "lead_id",
            "title",
            "content",
            "target",
            "mode",
            "status",
            "created_at",
            "reviewed_at",
            "source_fingerprint",
            "flag",
        } <= columns
    finally:
        conn.close()


def test_create_entry_is_pending_draft_only(stores):
    leads, queue = stores
    lead, _created = leads.upsert_lead(
        name="Avery Chen",
        title="CTO",
        company="Northwind AI",
        source_fingerprint="url:test",
        linkedin_url="https://www.linkedin.com/in/avery-chen-example",
    )
    entry = queue.create_entry(
        lead_id=lead.lead_id,
        content="Avery — as CTO at Northwind AI, checkpointing the transcript is the lever.",
        title="Avery Chen · CTO @ Northwind AI",
        target=lead.linkedin_url,
    )
    assert entry.status == STATUS_PENDING
    assert entry.mode == MODE_DRAFT_ONLY
    assert entry.reviewed_at is None
    fetched = queue.get_entry(entry.entry_id)
    assert fetched is not None
    assert "Northwind AI" in fetched.content


def test_approve_and_reject(stores):
    _leads, queue = stores
    entry = queue.create_entry(lead_id="lead-1", content="draft body about Byteforge")
    approved = queue.approve(entry.entry_id)
    assert approved.status == STATUS_APPROVED
    with pytest.raises(ValueError, match="cannot approve"):
        queue.approve(entry.entry_id)
    rejected = queue.reject(entry.entry_id)
    assert rejected.status == STATUS_REJECTED


def test_enqueue_sets_pending_approval_not_contacted(stores):
    leads, queue = stores
    lead, _ = leads.upsert_lead(
        name="Sam Okoye",
        title="Head of Engineering",
        company="Byteforge",
        company_size="11-50",
        industry="Developer Tools",
        source_fingerprint="url:sam",
    )
    leads.record_score(lead.lead_id, 0.9, "role high")
    lead = leads.get_lead(lead.lead_id)
    assert lead is not None
    entry_id = enqueue_draft(
        lead,
        "Sam — at Byteforge you already feel context rot between tool calls.",
        leads=leads,
        store=queue,
    )
    assert entry_id
    pending = queue.list_by_status(STATUS_PENDING)
    assert len(pending) == 1
    assert pending[0].mode == MODE_DRAFT_ONLY
    updated = leads.get_lead(lead.lead_id)
    assert updated is not None
    assert updated.draft_status == DRAFT_PENDING
    assert updated.pipeline_status == PIPELINE_IMPORTED


def test_approve_does_not_mark_contacted(stores, db: Path):
    leads, queue = stores
    lead, _ = leads.upsert_lead(
        name="Riley Nair",
        title="Product Manager",
        company="Helix Models",
        source_fingerprint="url:riley",
    )
    enqueue_draft(
        leads.get_lead(lead.lead_id),
        "Riley — as PM at Helix Models, attribution of the rotten turn is the gap.",
        leads=leads,
        store=queue,
    )
    entry = queue.list_by_status(STATUS_PENDING)[0]
    approve_draft(entry.entry_id, db_path=db)
    after = leads.get_lead(lead.lead_id)
    assert after is not None
    assert after.draft_status == DRAFT_APPROVED
    assert after.pipeline_status == PIPELINE_IMPORTED
    assert queue.get_entry(entry.entry_id).status == STATUS_APPROVED
