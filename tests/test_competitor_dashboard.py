"""Dashboard competitor pending_approval inbox wiring."""

from __future__ import annotations

from pathlib import Path

from agents.competitor_agent.models import SIGNAL_TYPE_MENTION
from agents.competitor_agent.pending_approval import (
    STATUS_PENDING,
    PendingApprovalStore,
    queue_signals,
)
from agents.competitor_agent.storage import CompetitorStore
from dashboard import (
    PendingItem,
    RosterDbPaths,
    approve_entry,
    load_pending_approvals,
    reject_entry,
)


def test_load_pending_approvals_includes_competitor_signals(
    tmp_path: Path, monkeypatch
):
    db = tmp_path / "competitor_agent.db"
    store = CompetitorStore(db_path=db)
    try:
        signal = store.insert_signal(
            competitor="Langfuse",
            signal_type=SIGNAL_TYPE_MENTION,
            summary="HN thread comparing Langfuse tracing to StreamCtx.",
            source_url="https://news.ycombinator.com/item?id=1",
        )
        queue_signals([signal], db_path=db)
    finally:
        store.close()

    class Store(PendingApprovalStore):
        def __init__(self, db_path=None, **kwargs):
            super().__init__(db_path=db, **kwargs)

    monkeypatch.setattr("dashboard.CompetitorPendingStore", Store)
    items = load_pending_approvals()
    competitor = [item for item in items if item.store == "competitor"]
    assert competitor
    assert all(item.status == STATUS_PENDING for item in competitor)
    assert "Langfuse" in competitor[0].summary
    assert competitor[0].body
    assert competitor[0].agent_key == "competitor"


def test_approve_entry_routes_to_competitor_approve(tmp_path: Path):
    db = tmp_path / "competitor_agent.db"
    store = CompetitorStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        signal = store.insert_signal(
            competitor="LangSmith",
            signal_type=SIGNAL_TYPE_MENTION,
            summary="LangSmith posted a tracing cookbook.",
        )
        entry_id = queue_signals([signal], db_path=db)[0]
        entry = queue.get_entry(entry_id)
        assert entry is not None
        item = PendingItem(
            agent_key="competitor",
            agent_name="Competitor Agent",
            entry_id=entry.entry_id,
            status=entry.status,
            created_at=entry.created_at,
            title=entry.title,
            preview=entry.content,
            store="competitor",
            summary=f"Competitor: {entry.title}",
            body=entry.content,
            kind="competitor_signal",
        )
        approve_entry(item, competitor_db=db)
        updated = queue.get_entry(entry.entry_id)
        assert updated is not None
        assert updated.status == "approved"
        assert store.list_signals()
    finally:
        queue.close()
        store.close()


def test_reject_entry_routes_to_competitor_reject(tmp_path: Path):
    db = tmp_path / "competitor_agent.db"
    store = CompetitorStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        signal = store.insert_signal(
            competitor="Helicone",
            signal_type=SIGNAL_TYPE_MENTION,
            summary="Helicone tweeted about gateways.",
        )
        entry_id = queue_signals([signal], db_path=db)[0]
        entry = queue.get_entry(entry_id)
        item = PendingItem(
            agent_key="competitor",
            agent_name="Competitor Agent",
            entry_id=entry.entry_id,
            status=entry.status,
            created_at=entry.created_at,
            title=entry.title,
            preview=entry.content,
            store="competitor",
        )
        reject_entry(item, competitor_db=db)
        assert queue.get_entry(entry.entry_id).status == "rejected"
    finally:
        queue.close()
        store.close()


def test_roster_paths_include_competitor_agent_db(tmp_path: Path):
    paths = RosterDbPaths(
        coding=tmp_path / "coding_agent.db",
        marketing=tmp_path / "marketing_agent.db",
        competitor=tmp_path / "competitor_agent.db",
        research=tmp_path / "research_agent.db",
        presales=tmp_path / "leads.db",
        techsupport=tmp_path / "support_tickets.db",
        legal=tmp_path / "compliance_findings.db",
    )
    assert paths.competitor.name == "competitor_agent.db"
