"""
Pre-Sales / Outreach Agent — CSV in, scored drafts out, human send only.

Never scrapes LinkedIn. Never sends email or InMail. Every outreach body
lands in pending_approval for the founder to view / edit / approve / reject.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from streamctx import get_tracker

from agents.presales_agent.csv_import import import_csv
from agents.presales_agent.models import (
    DRAFT_APPROVED,
    DRAFT_PENDING,
    DRAFT_REJECTED,
    PIPELINE_IMPORTED,
    RunSummary,
)
from agents.presales_agent.outreach import ChatFn, generate_and_queue, score_all_leads
from agents.presales_agent.pending_approval import PendingApprovalStore
from agents.presales_agent.scoring import ScoringRules
from agents.presales_agent.storage import LeadStore
from shared.audit_log import log_action
from shared.config import AGENT_IDS, STATUS_PENDING


def run(
    *,
    db_path: Optional[Path | str] = None,
    csv_path: Optional[Path | str] = None,
    min_score: Optional[float] = None,
    limit: Optional[int] = None,
    chat_fn: Optional[ChatFn] = None,
    enable_notifications: bool = True,
) -> RunSummary:
    """Import (optional) → score → queue personalized drafts. Does not send."""
    leads = LeadStore(db_path=db_path)
    store = PendingApprovalStore(
        db_path=db_path,
        enable_default_notifier=enable_notifications,
    )
    try:
        if csv_path:
            imported = import_csv(csv_path, store=leads)
            print(
                f"[presales-agent] import imported={imported.imported} "
                f"updated={imported.updated} skipped={imported.skipped}"
            )
        summary = generate_and_queue(
            leads,
            store,
            min_score=min_score,
            chat_fn=chat_fn,
            limit=limit,
        )
        print(
            f"[presales-agent] scored={summary.scored} queued={summary.queued} "
            f"skipped={summary.skipped} flagged={summary.flagged} "
            f"model={summary.model_used or '(stub)'}"
        )
        for entry_id in summary.entry_ids:
            print(f"  pending_approval id={entry_id}")
        for error in summary.errors:
            print(f"  error: {error}")
        return summary
    finally:
        store.close()
        leads.close()


def assign_presales_task(
    text: str,
    *,
    db_path: Optional[Path | str] = None,
) -> str:
    """Roster Assign: import a Sales Navigator CSV. Does not send outreach."""
    body = (text or "").strip().strip('"').strip("'")
    if not body:
        raise ValueError("task is empty")
    path = Path(body)
    if path.suffix.lower() != ".csv":
        raise ValueError("Provide a path to a Sales Navigator CSV export")
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    leads = LeadStore(db_path=db_path)
    try:
        result = import_csv(path, store=leads)
        return (
            f"imported={result.imported} updated={result.updated} "
            f"skipped={result.skipped}"
        )
    finally:
        leads.close()


def approve_draft(
    entry_id: str,
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    """Mark copy as founder-approved. Does not send and does not mark Contacted."""
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    leads = LeadStore(db_path=db_path)
    try:
        entry = store.approve(entry_id)
        lead = leads.get_lead(entry.lead_id)
        if lead is not None:
            leads.record_draft(
                lead.lead_id,
                draft_text=entry.content,
                draft_status=DRAFT_APPROVED,
                draft_entry_id=entry.entry_id,
                flag=entry.flag,
            )
            if lead.pipeline_status != PIPELINE_IMPORTED:
                pass
        print(
            f"[presales-agent] approved {entry_id} (copy only — not sent; "
            "advance pipeline to Contacted after you send it yourself)"
        )
    finally:
        store.close()
        leads.close()


def reject_draft(
    entry_id: str,
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    leads = LeadStore(db_path=db_path)
    try:
        entry = store.reject(entry_id)
        lead = leads.get_lead(entry.lead_id)
        if lead is not None:
            leads.record_draft(
                lead.lead_id,
                draft_text=entry.content,
                draft_status=DRAFT_REJECTED,
                draft_entry_id=entry.entry_id,
                flag=entry.flag,
            )
        print(f"[presales-agent] rejected {entry_id}")
    finally:
        store.close()
        leads.close()


def save_edited_draft(
    entry_id: str,
    content: str,
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    from agents.presales_agent.flags import flag_draft

    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    leads = LeadStore(db_path=db_path)
    try:
        flag = flag_draft(content)
        entry = store.update_content(entry_id, content, flag=flag)
        if entry is None:
            raise KeyError(entry_id)
        lead = leads.get_lead(entry.lead_id)
        if lead is not None:
            leads.record_draft(
                lead.lead_id,
                draft_text=entry.content,
                draft_status=lead.draft_status or DRAFT_PENDING,
                draft_entry_id=entry.entry_id,
                flag=entry.flag,
            )
    finally:
        store.close()
        leads.close()


def _cmd_run(args: argparse.Namespace) -> int:
    tracker = get_tracker(AGENT_IDS["presales"])
    tracker.start()
    try:
        summary = run(
            csv_path=args.csv,
            min_score=args.min_score,
            limit=args.limit,
            enable_notifications=not args.no_notify,
        )
        session_id = tracker.get_session_id()
        log_action(
            agent_name=AGENT_IDS["presales"],
            session_id=session_id,
            action_type="presales_outreach_drafts",
            payload={
                "queued": summary.queued,
                "scored": summary.scored,
                "skipped": summary.skipped,
                "flagged": summary.flagged,
                "model": summary.model_used,
                "csv": args.csv,
            },
            status=STATUS_PENDING,
        )
    finally:
        tracker.stop()
    return 1 if summary.errors and not summary.queued else 0


def _cmd_import(args: argparse.Namespace) -> int:
    leads = LeadStore()
    try:
        result = import_csv(args.csv, store=leads)
        print(
            f"[presales-agent] imported={result.imported} updated={result.updated} "
            f"skipped={result.skipped}"
        )
        for error in result.errors:
            print(f"  {error}")
        return 0
    finally:
        leads.close()


def _cmd_score(args: argparse.Namespace) -> int:
    del args
    leads = LeadStore()
    try:
        n = score_all_leads(leads, rules=ScoringRules.load())
        print(f"[presales-agent] scored {n} lead(s)")
        return 0
    finally:
        leads.close()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="StreamCtx Pre-Sales Agent — CSV leads, scored drafts, no send.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Score leads and queue outreach drafts")
    run_p.add_argument("--csv", default=None, help="Optional Sales Navigator CSV to import first")
    run_p.add_argument("--min-score", type=float, default=None)
    run_p.add_argument("--limit", type=int, default=None)
    run_p.add_argument("--no-notify", action="store_true")
    run_p.set_defaults(func=_cmd_run)

    imp = sub.add_parser("import", help="Import a Sales Navigator CSV (no LinkedIn calls)")
    imp.add_argument("csv")
    imp.set_defaults(func=_cmd_import)

    sc = sub.add_parser("score", help="Re-score stored leads")
    sc.set_defaults(func=_cmd_score)

    ap = sub.add_parser("approve", help="Approve copy (does not send)")
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
