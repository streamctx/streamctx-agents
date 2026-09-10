"""One-time bulk-close of the 2026-08-20/21 coding pending_approval backlog.

Marks every currently-pending coding SQLite row and leftover coding JSONL
audit entries as ``rejected`` via the same paths the dashboard Reject button
uses (``dashboard.reject_entry`` → ``CodingStore.update_status`` /
``shared.audit_log.update_status``). Rows are never deleted.

Usage::

    python scripts/bulk_close_coding_backlog.py
    python scripts/bulk_close_coding_backlog.py --execute
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.coding_agent.pending_approval import (  # noqa: E402
    STATUS_AUTO_FIX_FAILED,
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    STATUS_REJECTED,
    PendingApprovalStore,
    DEFAULT_AGENT_DB,
)
from dashboard import PendingItem, reject_entry  # noqa: E402
from shared.audit_log import get_pending_actions  # noqa: E402
from shared.config import AGENT_IDS  # noqa: E402

NOTE = (
    "bulk-closed historical backlog from 2026-08-20/21 WAL burst, 2026-09-09"
)
PENDING_STATUSES = (
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    STATUS_AUTO_FIX_FAILED,
)


def _annotate_test_results(raw: str | None) -> str:
    """Merge the bulk-close note into existing diagnosis JSON when present."""
    payload: dict
    try:
        parsed = json.loads(raw or "")
        payload = parsed if isinstance(parsed, dict) else {"prior": parsed}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {"prior_test_results": raw} if raw else {}
    payload["bulk_close_note"] = NOTE
    return json.dumps(payload, ensure_ascii=False)


def _coding_pending_items(db_path: Path) -> list[PendingItem]:
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    try:
        items: list[PendingItem] = []
        for status in PENDING_STATUSES:
            for entry in store.list_by_status(status):
                items.append(
                    PendingItem(
                        agent_key="coding",
                        agent_name="Coding Agent",
                        entry_id=entry.entry_id,
                        status=entry.status,
                        created_at=entry.created_at,
                        title=entry.root_cause or "coding",
                        preview="",
                        store="coding",
                    )
                )
        return items
    finally:
        store.close()


def _coding_audit_pending() -> list[PendingItem]:
    coding_id = AGENT_IDS["coding"]
    items: list[PendingItem] = []
    for entry in get_pending_actions():
        if str(entry.get("agent") or "") != coding_id:
            continue
        items.append(
            PendingItem(
                agent_key="coding",
                agent_name="Coding Agent",
                entry_id=str(entry["id"]),
                status=str(entry.get("status") or "pending_approval"),
                created_at=str(entry.get("timestamp") or ""),
                title=str(entry.get("action_type") or "audit"),
                preview="",
                store="audit",
            )
        )
    return items


def _annotate_then_reject_coding(item: PendingItem, db_path: Path) -> None:
    """Annotate test_results (no dedicated note column), then reject_entry."""
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    try:
        entry = store.get_entry(item.entry_id)
        if entry is None:
            return
        with store._lock:
            store._conn.execute(
                "UPDATE pending_approval SET test_results = ? WHERE entry_id = ?",
                (_annotate_test_results(entry.test_results), item.entry_id),
            )
            store._conn.commit()
    finally:
        store.close()
    reject_entry(item, coding_db=db_path)


def _pending_counts(db_path: Path) -> dict[str, int]:
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    try:
        by_status = {
            status: len(store.list_by_status(status)) for status in PENDING_STATUSES
        }
        rejected = len(store.list_by_status(STATUS_REJECTED))
    finally:
        store.close()
    audit = len(_coding_audit_pending())
    return {
        "sqlite_pending": sum(by_status.values()),
        **{f"sqlite_{k}": v for k, v in by_status.items()},
        "sqlite_rejected": rejected,
        "audit_pending": audit,
        "dashboard_coding_pending": sum(by_status.values()) + audit,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-reject historical coding pending_approval backlog."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply rejects. Default is dry-run.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_AGENT_DB,
        help=f"coding_agent.db path (default: {DEFAULT_AGENT_DB})",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    before = _pending_counts(db_path)
    coding_items = _coding_pending_items(db_path)
    audit_items = _coding_audit_pending()

    print("=== bulk close coding backlog ===")
    print(f"  db={db_path}")
    print(f"  note={NOTE!r}")
    print(f"  before={before}")
    print(f"  sqlite_to_reject={len(coding_items)}")
    print(f"  audit_to_reject={len(audit_items)}")
    print(f"  mode={'EXECUTE' if args.execute else 'DRY-RUN'}")

    if not args.execute:
        print("Re-run with --execute to reject via dashboard.reject_entry.")
        return 0

    for item in coding_items:
        _annotate_then_reject_coding(item, db_path)
    for item in audit_items:
        # Same path as the dashboard Reject button (approved_by="dashboard").
        # Audit JSONL has no free-text note column; reason is in this script +
        # coding rows' test_results.bulk_close_note.
        reject_entry(item, coding_db=db_path)

    after = _pending_counts(db_path)
    print(f"  after={after}")
    print(
        f"  rejected_sqlite={len(coding_items)} rejected_audit={len(audit_items)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
