"""KB match + ConfidenceGate: unmatched tickets skip draft generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.techsupport_agent.confidence_gate import (
    MIN_CONFIDENCE_FOR_DRAFT,
    ConfidenceGate,
)
from agents.techsupport_agent.kb import KnowledgeBase
from agents.techsupport_agent.models import (
    STATUS_DRAFTED,
    STATUS_NEEDS_MANUAL_REVIEW,
    STATUS_NEW,
)
from agents.techsupport_agent.pending_approval import STATUS_PENDING, PendingApprovalStore
from agents.techsupport_agent.storage import TicketStore
from agents.techsupport_agent.techsupport_agent import process_open_tickets, process_ticket
from agents.coding_agent.confidence_gate import MIN_CONFIDENCE_FOR_AUTO_FIX

KB_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "techsupport" / "kb"


def _chat(_model: str, _key: str, messages) -> str:
    user = messages[-1]["content"]
    cite = "README.md"
    for line in user.splitlines():
        if line.startswith("- Path:"):
            cite = line.split(":", 1)[1].strip()
        if line.startswith("- Heading:"):
            heading = line.split(":", 1)[1].strip()
            return (
                f"Based on `{cite} § {heading}`: run "
                "`python -m agents.coding_agent.coding_agent run` and review "
                "pending_approval before approving."
            )
    return f"Based on `{cite}`: see the docs."


@pytest.fixture
def stores(tmp_path: Path):
    db = tmp_path / "support_tickets.db"
    tickets = TicketStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    yield tickets, queue
    queue.close()
    tickets.close()


@pytest.fixture
def kb() -> KnowledgeBase:
    return KnowledgeBase(root=KB_ROOT)


def _insert(tickets: TicketStore, *, title: str, body: str, fingerprint: str):
    return tickets.insert_ticket(
        source="github",
        source_ref="1",
        source_url="https://github.com/streamctx/streamctx-agents/issues/1",
        title=title,
        body=body,
        author="alex",
        source_fingerprint=fingerprint,
        status=STATUS_NEW,
    )


def test_gate_reuses_coding_agent_threshold():
    assert MIN_CONFIDENCE_FOR_DRAFT == MIN_CONFIDENCE_FOR_AUTO_FIX == 0.6


def test_how_to_matches_readme_and_drafts(stores, kb):
    tickets, queue = stores
    ticket = _insert(
        tickets,
        title="How do I run the coding agent pipeline?",
        body="How do I run the coding agent pipeline and review pending_approval?",
        fingerprint="t-howto",
    )
    status, entry_id, used = process_ticket(
        ticket,
        tickets=tickets,
        store=queue,
        kb=kb,
        chat_fn=_chat,
    )
    assert status == STATUS_DRAFTED
    assert entry_id
    assert used == "stub"
    entry = queue.get_entry(entry_id)
    assert entry is not None
    assert entry.status == STATUS_PENDING
    assert "README.md" in entry.content or "pending_approval" in entry.content
    assert entry.kb_citation
    updated = tickets.get_ticket(ticket.ticket_id)
    assert updated is not None
    assert updated.status == STATUS_DRAFTED
    assert updated.kb_match_confidence is not None
    assert updated.kb_match_confidence >= MIN_CONFIDENCE_FOR_DRAFT


def test_unrelated_issue_skips_draft_and_flags_manual_review(stores, kb):
    tickets, queue = stores
    ticket = _insert(
        tickets,
        title="Istio sidecar drops HTTP/2 when an eBPF kprobe is attached",
        body=(
            "Our GKE cluster's Istio sidecar drops HTTP/2 streams when we attach "
            "a custom eBPF kprobe to the CNI. Completely unrelated to StreamCtx "
            "but the cluster is on fire."
        ),
        fingerprint="t-k8s",
    )
    status, entry_id, _used = process_ticket(
        ticket,
        tickets=tickets,
        store=queue,
        kb=kb,
        chat_fn=_chat,
    )
    assert status == STATUS_NEEDS_MANUAL_REVIEW
    assert entry_id is None
    assert queue.list_by_status(STATUS_PENDING) == []
    updated = tickets.get_ticket(ticket.ticket_id)
    assert updated is not None
    assert updated.status == STATUS_NEEDS_MANUAL_REVIEW
    assert not updated.draft_text
    assert updated.draft_entry_id is None
    gate = ConfidenceGate().evaluate(
        updated,
        kb.search(updated),
    )
    assert gate.proceed_to_draft is False
    assert gate.reason in {"no_kb_match", "low_classification_confidence"}


def test_process_open_tickets_separates_drafts_from_manual_review(stores, kb):
    tickets, queue = stores
    _insert(
        tickets,
        title="How do I run the coding agent pipeline?",
        body="How do I run the coding agent pipeline and review pending_approval?",
        fingerprint="t-howto",
    )
    _insert(
        tickets,
        title="Istio sidecar drops HTTP/2 when an eBPF kprobe is attached",
        body="GKE Istio eBPF kprobe CNI HTTP/2 fire, unrelated to StreamCtx.",
        fingerprint="t-k8s",
    )
    summary = process_open_tickets(
        tickets,
        queue,
        kb=kb,
        chat_fn=_chat,
    )
    assert summary.drafted == 1
    assert summary.needs_review == 1
    assert len(summary.entry_ids) == 1
    pending = queue.list_by_status(STATUS_PENDING)
    assert len(pending) == 1
    drafted = [t for t in tickets.list_tickets() if t.status == STATUS_DRAFTED]
    review = [t for t in tickets.list_tickets() if t.status == STATUS_NEEDS_MANUAL_REVIEW]
    assert len(drafted) == 1
    assert len(review) == 1
    assert drafted[0].ticket_id != review[0].ticket_id
    assert pending[0].ticket_id == drafted[0].ticket_id
    assert pending[0].ticket_id != review[0].ticket_id
