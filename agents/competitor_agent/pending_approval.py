"""SQLite store for competitor findings awaiting human review.

Lives in ``competitor_agent.db`` alongside snapshots/signals. Mode is always
``draft_only`` — approve/reject only changes the review queue, never publishes
and never treats output as a strategy decision.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from agents.competitor_agent.models import CompetitorSignal, PendingApprovalEntry
from shared.db import connect

DEFAULT_AGENT_DB = (
    Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    / "competitor_agent.db"
)

MODE_DRAFT_ONLY = "draft_only"
MODES = frozenset({MODE_DRAFT_ONLY})

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUSES = frozenset({STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED})

SIGNAL_FINGERPRINT_PREFIX = "competitor-signal:"
SUMMARY_FINGERPRINT_PREFIX = "competitor-summary:"

NotifierFn = Callable[["PendingApprovalEntry"], None]


class PendingApprovalStore:
    """Persists competitor ``pending_approval`` rows in ``competitor_agent.db``."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        *,
        notifier: Optional[NotifierFn] = None,
        enable_default_notifier: bool = True,
    ) -> None:
        self.db_path = Path(db_path or DEFAULT_AGENT_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect(
            schema="competitor",
            db_path=self.db_path,
            check_same_thread=False,
            timeout=30,
        )
        self._notifier = notifier
        self._enable_default_notifier = enable_default_notifier
        self._init_db()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pending_approval (
                    entry_id TEXT PRIMARY KEY,
                    title TEXT,
                    content TEXT,
                    target TEXT,
                    mode TEXT,
                    status TEXT,
                    created_at TIMESTAMP,
                    reviewed_at TIMESTAMP,
                    source_fingerprint TEXT,
                    competitor_name TEXT
                );
                """
            )
            self._conn.commit()

    def create_entry(
        self,
        *,
        content: str,
        title: str = "",
        target: Optional[str] = None,
        competitor_name: str = "",
        mode: str = MODE_DRAFT_ONLY,
        status: str = STATUS_PENDING,
        entry_id: Optional[str] = None,
        source_fingerprint: Optional[str] = None,
        created_at: Optional[str] = None,
        reviewed_at: Optional[str] = None,
    ) -> PendingApprovalEntry:
        _validate_enum("mode", mode, MODES)
        _validate_enum("status", status, STATUSES)
        if not (content or "").strip():
            raise ValueError("content is empty")

        entry_id = entry_id or str(uuid.uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        target_value = target.strip() if target and target.strip() else None
        fingerprint = (
            source_fingerprint.strip()
            if source_fingerprint and source_fingerprint.strip()
            else None
        )

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pending_approval (
                    entry_id, title, content, target, mode, status,
                    created_at, reviewed_at, source_fingerprint, competitor_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    (title or "").strip(),
                    content,
                    target_value,
                    mode,
                    status,
                    created_at,
                    reviewed_at,
                    fingerprint,
                    (competitor_name or "").strip(),
                ),
            )
            self._conn.commit()

        entry = PendingApprovalEntry(
            entry_id=entry_id,
            title=(title or "").strip(),
            content=content,
            target=target_value,
            mode=mode,
            status=status,
            created_at=created_at,
            competitor_name=(competitor_name or "").strip(),
            reviewed_at=reviewed_at,
            source_fingerprint=fingerprint,
        )
        self._dispatch_notification(entry)
        return entry

    def get_entry(self, entry_id: str) -> Optional[PendingApprovalEntry]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_approval WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
        return _row_to_entry(row) if row else None

    def get_by_source_fingerprint(
        self, fingerprint: str
    ) -> Optional[PendingApprovalEntry]:
        needle = (fingerprint or "").strip()
        if not needle:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM pending_approval
                WHERE source_fingerprint = ?
                ORDER BY created_at ASC, entry_id ASC
                LIMIT 1
                """,
                (needle,),
            ).fetchone()
        return _row_to_entry(row) if row else None

    def list_by_status(self, status: str) -> list[PendingApprovalEntry]:
        _validate_enum("status", status, STATUSES)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM pending_approval
                WHERE status = ?
                ORDER BY created_at DESC
                """,
                (status,),
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def list_entries(
        self, *, statuses: Optional[Sequence[str]] = None
    ) -> list[PendingApprovalEntry]:
        allowed = tuple(statuses) if statuses is not None else tuple(STATUSES)
        for status in allowed:
            _validate_enum("status", status, STATUSES)
        placeholders = ",".join("?" for _ in allowed)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM pending_approval
                WHERE status IN ({placeholders})
                ORDER BY created_at DESC
                """,
                allowed,
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def update_status(
        self,
        entry_id: str,
        status: str,
        *,
        reviewed_at: Optional[str] = None,
    ) -> Optional[PendingApprovalEntry]:
        _validate_enum("status", status, STATUSES)
        stamp = reviewed_at
        if status in {STATUS_APPROVED, STATUS_REJECTED} and stamp is None:
            stamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if stamp is not None:
                self._conn.execute(
                    """
                    UPDATE pending_approval
                    SET status = ?, reviewed_at = ?
                    WHERE entry_id = ?
                    """,
                    (status, stamp, entry_id),
                )
            else:
                self._conn.execute(
                    "UPDATE pending_approval SET status = ? WHERE entry_id = ?",
                    (status, entry_id),
                )
            self._conn.commit()
        return self.get_entry(entry_id)

    def approve(self, entry_id: str) -> PendingApprovalEntry:
        """Human review of a finding. Does not publish or change strategy."""
        entry = self.get_entry(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        if entry.status != STATUS_PENDING:
            raise ValueError(
                f"cannot approve entry {entry_id} in status {entry.status!r}"
            )
        updated = self.update_status(entry_id, STATUS_APPROVED)
        if updated is None:
            raise KeyError(entry_id)
        return updated

    def reject(self, entry_id: str) -> PendingApprovalEntry:
        entry = self.get_entry(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        if entry.status == STATUS_REJECTED:
            return entry
        updated = self.update_status(entry_id, STATUS_REJECTED)
        if updated is None:
            raise KeyError(entry_id)
        return updated

    def _dispatch_notification(self, entry: PendingApprovalEntry) -> None:
        if self._notifier is not None:
            self._notifier(entry)
            return
        if not self._enable_default_notifier:
            return
        from agents.competitor_agent.notifications import notify_pending_approval

        notify_pending_approval(entry)


def enqueue_signal(
    signal: CompetitorSignal,
    *,
    store: PendingApprovalStore,
) -> Optional[PendingApprovalEntry]:
    """Queue a detected signal for review. Idempotent on ``signal_id``."""
    fingerprint = f"{SIGNAL_FINGERPRINT_PREFIX}{signal.signal_id}"
    existing = store.get_by_source_fingerprint(fingerprint)
    if existing is not None:
        return existing
    label = (signal.signal_type or "signal").replace("_", " ")
    return store.create_entry(
        title=f"{signal.competitor}: {label}",
        content=signal.summary,
        target=signal.source_url,
        competitor_name=signal.competitor,
        source_fingerprint=fingerprint,
        created_at=signal.detected_at,
    )


def enqueue_research_summary(
    *,
    competitor_name: str,
    question: str,
    summary: str,
    store: PendingApprovalStore,
    fingerprint: Optional[str] = None,
) -> PendingApprovalEntry:
    """Queue a research-cycle summary. Audit-log write stays with the caller."""
    body = (summary or "").strip()
    if not body:
        raise ValueError("summary is empty")
    name = (competitor_name or "").strip() or "competitor"
    asked = (question or "").strip()
    title = f"{name}: research summary"
    if asked:
        title = f"{name}: research summary — {asked[:72]}"
    content = body if not asked else f"Question: {asked}\n\n{body}"
    return store.create_entry(
        title=title,
        content=content,
        target=asked or None,
        competitor_name=name,
        source_fingerprint=fingerprint or f"{SUMMARY_FINGERPRINT_PREFIX}{uuid.uuid4()}",
    )


def queue_signals(
    signals: Sequence[CompetitorSignal],
    *,
    db_path: Optional[Path | str] = None,
    enable_notifications: bool = False,
) -> list[str]:
    """Write pending_approval rows for newly detected signals."""
    store = PendingApprovalStore(
        db_path=db_path,
        enable_default_notifier=enable_notifications,
    )
    try:
        entry_ids: list[str] = []
        for signal in signals:
            entry = enqueue_signal(signal, store=store)
            if entry is not None:
                entry_ids.append(entry.entry_id)
        return entry_ids
    finally:
        store.close()


def _validate_enum(name: str, value: str, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"Invalid {name} {value!r}; expected one of {sorted(allowed)}")


def _row_to_entry(row: sqlite3.Row) -> PendingApprovalEntry:
    data = dict(row)
    return PendingApprovalEntry(
        entry_id=str(data["entry_id"]),
        title=str(data.get("title") or ""),
        content=str(data["content"]),
        target=data.get("target"),
        mode=str(data["mode"]),
        status=str(data["status"]),
        created_at=str(data["created_at"]),
        competitor_name=str(data.get("competitor_name") or ""),
        reviewed_at=data.get("reviewed_at"),
        source_fingerprint=data.get("source_fingerprint"),
    )
