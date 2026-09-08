"""pending_approval for competitor signals and research summaries."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agents.competitor_agent.competitor_agent import approve_draft, reject_draft
from agents.competitor_agent.models import SIGNAL_TYPE_NEW_RELEASE
from agents.competitor_agent.pending_approval import (
    MODE_DRAFT_ONLY,
    STATUS_PENDING,
    PendingApprovalStore,
    enqueue_research_summary,
    enqueue_signal,
    queue_signals,
)
from agents.competitor_agent.storage import CompetitorStore
from shared.audit_log import get_pending_actions, log_action
from shared.config import AGENT_IDS, STATUS_PENDING as AUDIT_PENDING


def test_pending_approval_schema(tmp_path: Path):
    db = tmp_path / "competitor_agent.db"
    signals = CompetitorStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        conn = sqlite3.connect(db)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "competitor_signals" in tables
        assert "pending_approval" in tables
        columns = {
            col[1]
            for col in conn.execute("PRAGMA table_info(pending_approval)").fetchall()
        }
        assert {
            "entry_id",
            "title",
            "content",
            "target",
            "mode",
            "status",
            "created_at",
            "reviewed_at",
            "source_fingerprint",
            "competitor_name",
        } <= columns
        conn.close()
    finally:
        queue.close()
        signals.close()


def test_enqueue_signal_is_idempotent(tmp_path: Path):
    db = tmp_path / "competitor_agent.db"
    store = CompetitorStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        signal = store.insert_signal(
            competitor="Langfuse",
            signal_type=SIGNAL_TYPE_NEW_RELEASE,
            summary="v1.2 shipped with a new playground.",
            source_url="https://github.com/langfuse/langfuse/releases/tag/v1.2",
        )
        first = enqueue_signal(signal, store=queue)
        second = enqueue_signal(signal, store=queue)
        assert first is not None
        assert second is not None
        assert first.entry_id == second.entry_id
        assert first.status == STATUS_PENDING
        assert first.competitor_name == "Langfuse"
        assert first.mode == MODE_DRAFT_ONLY
        assert queue.list_by_status(STATUS_PENDING) == [first]
    finally:
        queue.close()
        store.close()


def test_approve_and_reject_do_not_drop_signals(tmp_path: Path):
    db = tmp_path / "competitor_agent.db"
    store = CompetitorStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        signal = store.insert_signal(
            competitor="LangSmith",
            signal_type=SIGNAL_TYPE_NEW_RELEASE,
            summary="Pricing page added a team tier.",
        )
        queued = queue_signals([signal], db_path=db)
        entry_id = queued[0]
        approve_draft(entry_id, db_path=db)
        assert queue.get_entry(entry_id).status == "approved"
        assert store.list_signals(competitor="LangSmith")

        other = store.insert_signal(
            competitor="Helicone",
            signal_type=SIGNAL_TYPE_NEW_RELEASE,
            summary="New gateway release.",
        )
        other_id = queue_signals([other], db_path=db)[0]
        reject_draft(other_id, db_path=db)
        assert queue.get_entry(other_id).status == "rejected"
        assert store.list_signals(competitor="Helicone")
    finally:
        queue.close()
        store.close()


def test_research_summary_keeps_audit_log(tmp_path: Path, monkeypatch):
    db = tmp_path / "competitor_agent.db"
    monkeypatch.setenv("STREAMCTX_HOME", str(tmp_path))
    audit_path = tmp_path / "audit_log.jsonl"
    monkeypatch.setattr("shared.audit_log.AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setattr("shared.config.AUDIT_LOG_PATH", str(audit_path))

    log_action(
        agent_name=AGENT_IDS["competitor"],
        session_id="test",
        action_type="competitor_summary",
        payload={"competitor": "Langfuse", "summary": "No pricing change."},
        status=AUDIT_PENDING,
    )
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        entry = enqueue_research_summary(
            competitor_name="Langfuse",
            question="any pricing changes?",
            summary="No public pricing change this month.",
            store=queue,
        )
        assert entry.status == STATUS_PENDING
        assert "pricing changes" in entry.title.lower() or "Langfuse" in entry.title
        pending = get_pending_actions()
        assert any(
            row.get("action_type") == "competitor_summary" for row in pending
        )
    finally:
        queue.close()
