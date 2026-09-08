"""
Tech Support Agent — poll GitHub/Discord, classify, KB-match, draft replies.

Never posts a GitHub comment. Never sends a Discord message. Every matched
reply lands in pending_approval. Unmatched tickets are flagged
needs_manual_review and skip draft generation.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from streamctx import get_tracker

from agents.techsupport_agent.classify import classify_ticket
from agents.techsupport_agent.confidence_gate import ConfidenceGate
from agents.techsupport_agent.draft import ChatFn, enqueue_draft, generate_reply_draft
from agents.techsupport_agent.kb import KnowledgeBase
from agents.techsupport_agent.models import (
    STATUS_APPROVED,
    STATUS_CLASSIFIED,
    STATUS_DRAFTED,
    STATUS_NEEDS_MANUAL_REVIEW,
    STATUS_NEW,
    STATUS_REJECTED,
    Ticket,
    RunSummary,
)
from agents.techsupport_agent.pending_approval import PendingApprovalStore
from agents.techsupport_agent.poll import run_poll, summarize as summarize_poll
from agents.techsupport_agent.settings import TechSupportConfig, default_config
from agents.techsupport_agent.storage import TicketStore
from shared.audit_log import log_action
from shared.config import AGENT_IDS, STATUS_PENDING


def process_ticket(
    ticket: Ticket,
    *,
    tickets: TicketStore,
    store: PendingApprovalStore,
    kb: KnowledgeBase,
    gate: Optional[ConfidenceGate] = None,
    chat_fn: Optional[ChatFn] = None,
    api_key: Optional[str] = None,
    preferred_model: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[str, Optional[str], str]:
    """Classify → KB → gate. Draft only on a confident match. Never posts.

    Returns ``(status, entry_id_or_none, model_used)``.
    """
    checker = gate or ConfidenceGate()
    classification = classify_ticket(ticket)
    updated = tickets.record_classification(
        ticket.ticket_id,
        ticket_type=classification.ticket_type,
        severity=classification.severity,
        confidence=classification.confidence,
        rationale=classification.rationale,
        status=STATUS_CLASSIFIED,
    )
    assert updated is not None
    match = kb.search(updated)
    if match is not None:
        tickets.record_kb_match(
            updated.ticket_id,
            path=match.path,
            heading=match.heading,
            excerpt=match.excerpt,
            confidence=match.confidence,
        )
        updated = tickets.get_ticket(updated.ticket_id) or updated
    else:
        tickets.record_kb_match(
            updated.ticket_id,
            path="",
            heading="",
            excerpt="",
            confidence=None,
        )
        updated = tickets.get_ticket(updated.ticket_id) or updated

    result = checker.evaluate(updated, match)
    if not result.proceed_to_draft:
        tickets.set_status(updated.ticket_id, STATUS_NEEDS_MANUAL_REVIEW)
        return STATUS_NEEDS_MANUAL_REVIEW, None, ""

    assert match is not None
    draft, used = generate_reply_draft(
        updated,
        match,
        chat_fn=chat_fn,
        api_key=api_key,
        preferred_model=preferred_model,
        model=model,
    )
    if not draft.strip():
        tickets.set_status(updated.ticket_id, STATUS_NEEDS_MANUAL_REVIEW)
        return STATUS_NEEDS_MANUAL_REVIEW, None, used
    entry_id = enqueue_draft(
        updated,
        draft,
        tickets=tickets,
        store=store,
        kb_citation=match.citation,
    )
    if entry_id is None:
        return STATUS_DRAFTED, None, used
    return STATUS_DRAFTED, entry_id, used


def process_open_tickets(
    tickets: TicketStore,
    store: PendingApprovalStore,
    *,
    kb: Optional[KnowledgeBase] = None,
    gate: Optional[ConfidenceGate] = None,
    chat_fn: Optional[ChatFn] = None,
    api_key: Optional[str] = None,
    preferred_model: Optional[str] = None,
    config: Optional[TechSupportConfig] = None,
    kb_root: Optional[Path | str] = None,
    limit: Optional[int] = None,
) -> RunSummary:
    spec = config or default_config()
    index = kb or KnowledgeBase(root=kb_root, config=spec)
    checker = gate or ConfidenceGate()
    pending = [
        ticket
        for ticket in tickets.list_tickets()
        if ticket.status in {STATUS_NEW, STATUS_CLASSIFIED}
    ]
    if limit is not None:
        pending = pending[: int(limit)]

    model_used = ""
    resolved_key = ""
    drafted: list[str] = []
    classified = 0
    needs_review = 0
    skipped = 0
    errors: list[str] = []

    if chat_fn is None and pending:
        try:
            from agents.techsupport_agent.draft import resolve_openrouter_model

            model_used, resolved_key = resolve_openrouter_model(
                api_key=api_key,
                preferred_model=preferred_model,
            )
        except Exception as exc:
            errors.append(f"openrouter: {exc}")

    for ticket in pending:
        try:
            status, entry_id, used = process_ticket(
                ticket,
                tickets=tickets,
                store=store,
                kb=index,
                gate=checker,
                chat_fn=chat_fn,
                api_key=resolved_key or api_key,
                preferred_model=preferred_model,
                model=model_used or None,
            )
            classified += 1
            model_used = used or model_used
            if status == STATUS_DRAFTED:
                if entry_id:
                    drafted.append(entry_id)
                else:
                    skipped += 1
            elif status == STATUS_NEEDS_MANUAL_REVIEW:
                needs_review += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append(f"{ticket.ticket_id}: {exc}")
            skipped += 1

    return RunSummary(
        ingested=0,
        classified=classified,
        drafted=len(drafted),
        needs_review=needs_review,
        skipped=skipped,
        errors=tuple(errors),
        entry_ids=tuple(drafted),
        model_used=model_used,
    )


def run(
    *,
    db_path: Optional[Path | str] = None,
    config: Optional[TechSupportConfig] = None,
    kb_root: Optional[Path | str] = None,
    chat_fn: Optional[ChatFn] = None,
    enable_notifications: bool = True,
    github: bool = True,
    discord: bool = True,
    fetch_github_fn=None,
    fetch_discord_fn=None,
    limit: Optional[int] = None,
) -> RunSummary:
    """Poll → classify → KB gate → queue drafts. Does not post."""
    spec = config or default_config()
    tickets = TicketStore(db_path=db_path)
    store = PendingApprovalStore(
        db_path=db_path,
        enable_default_notifier=enable_notifications,
    )
    try:
        poll = run_poll(
            tickets,
            config=spec,
            github=github,
            discord=discord,
            fetch_github_fn=fetch_github_fn,
            fetch_discord_fn=fetch_discord_fn,
        )
        print(f"[techsupport-agent] poll {summarize_poll(poll)}")
        for source, error in poll.errors:
            print(f"  poll error {source}: {error}")
        summary = process_open_tickets(
            tickets,
            store,
            config=spec,
            kb_root=kb_root,
            chat_fn=chat_fn,
            limit=limit,
        )
        poll_errors = [f"{source}: {error}" for source, error in poll.errors]
        summary = RunSummary(
            ingested=len(poll.inserted),
            classified=summary.classified,
            drafted=summary.drafted,
            needs_review=summary.needs_review,
            skipped=summary.skipped,
            errors=tuple(poll_errors + list(summary.errors)),
            entry_ids=summary.entry_ids,
            model_used=summary.model_used,
        )
        print(
            f"[techsupport-agent] ingested={summary.ingested} classified={summary.classified} "
            f"drafted={summary.drafted} needs_review={summary.needs_review} "
            f"model={summary.model_used or '(stub)'}"
        )
        for entry_id in summary.entry_ids:
            print(f"  pending_approval id={entry_id}")
        for error in summary.errors:
            print(f"  error: {error}")
        return summary
    finally:
        store.close()
        tickets.close()


def approve_draft(
    entry_id: str,
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    """Mark copy as founder-approved. Does not post to GitHub or Discord."""
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    tickets = TicketStore(db_path=db_path)
    try:
        entry = store.approve(entry_id)
        tickets.record_draft(
            entry.ticket_id,
            draft_text=entry.content,
            draft_entry_id=entry.entry_id,
            status=STATUS_APPROVED,
        )
        print(
            f"[techsupport-agent] approved {entry_id} (copy only — not posted to "
            "GitHub or Discord)"
        )
    finally:
        store.close()
        tickets.close()


def reject_draft(
    entry_id: str,
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    tickets = TicketStore(db_path=db_path)
    try:
        entry = store.reject(entry_id)
        tickets.record_draft(
            entry.ticket_id,
            draft_text=entry.content,
            draft_entry_id=entry.entry_id,
            status=STATUS_REJECTED,
        )
        print(f"[techsupport-agent] rejected {entry_id}")
    finally:
        store.close()
        tickets.close()


def save_edited_draft(
    entry_id: str,
    content: str,
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    tickets = TicketStore(db_path=db_path)
    try:
        entry = store.update_content(entry_id, content)
        if entry is None:
            raise KeyError(entry_id)
        ticket = tickets.get_ticket(entry.ticket_id)
        if ticket is not None:
            tickets.record_draft(
                ticket.ticket_id,
                draft_text=entry.content,
                draft_entry_id=entry.entry_id,
                status=ticket.status if ticket.status in {STATUS_DRAFTED, STATUS_APPROVED} else STATUS_DRAFTED,
            )
    finally:
        store.close()
        tickets.close()


def _cmd_run(args: argparse.Namespace) -> int:
    tracker = get_tracker(AGENT_IDS["techsupport"])
    tracker.start()
    try:
        summary = run(
            enable_notifications=not args.no_notify,
            github=not args.discord_only,
            discord=not args.github_only,
            limit=args.limit,
        )
        session_id = tracker.get_session_id()
        log_action(
            agent_name=AGENT_IDS["techsupport"],
            session_id=session_id,
            action_type="techsupport_reply_drafts",
            payload={
                "ingested": summary.ingested,
                "classified": summary.classified,
                "drafted": summary.drafted,
                "needs_review": summary.needs_review,
                "model": summary.model_used,
            },
            status=STATUS_PENDING,
        )
    finally:
        tracker.stop()
    return 1 if summary.errors and not summary.drafted else 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="StreamCtx Tech Support Agent — poll, draft, never post.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Poll GitHub/Discord and queue reply drafts")
    run_p.add_argument("--limit", type=int, default=None)
    run_p.add_argument("--no-notify", action="store_true")
    exclusive = run_p.add_mutually_exclusive_group()
    exclusive.add_argument("--github-only", action="store_true")
    exclusive.add_argument("--discord-only", action="store_true")
    run_p.set_defaults(func=_cmd_run)

    ap = sub.add_parser("approve", help="Approve copy (does not post)")
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
