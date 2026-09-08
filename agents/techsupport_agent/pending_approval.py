"""SQLite store for tech-support reply drafts awaiting human review.

Same style as ``agents.presales_agent.pending_approval`` (SQLite under
``~/.streamctx``, uuid ``entry_id``, UTC timestamps). Lives in
``support_tickets.db`` alongside the tickets table. Mode is always
``draft_only`` — there is no GitHub comment write and no Discord send.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from agents.techsupport_agent.models import PendingApprovalEntry

DEFAULT_AGENT_DB = (
    Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    / "support_tickets.db"
)

MODE_DRAFT_ONLY = "draft_only"
MODES = frozenset({MODE_DRAFT_ONLY})

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUSES = frozenset({STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED})

NotifierFn = Callable[["PendingApprovalEntry"], None]


class PendingApprovalStore:
    """Persists tech-support ``pending_approval`` rows in ``support_tickets.db``."""

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
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
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
                    ticket_id TEXT,
                    title TEXT,
                    content TEXT,
                    target TEXT,
                    mode TEXT,
                    status TEXT,
                    created_at TIMESTAMP,
                    reviewed_at TIMESTAMP,
                    source_fingerprint TEXT,
                    kb_citation TEXT
                );
                """
            )
            self._ensure_column("pending_approval", "kb_citation", "TEXT")
            self._ensure_column("pending_approval", "title", "TEXT")
            self._ensure_column("pending_approval", "reviewed_at", "TIMESTAMP")
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, col_type: str) -> None:
        columns = {
            row[1]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")

    def create_entry(
        self,
        *,
        ticket_id: str,
        content: str,
        title: str = "",
        target: Optional[str] = None,
        mode: str = MODE_DRAFT_ONLY,
        status: str = STATUS_PENDING,
        entry_id: Optional[str] = None,
        source_fingerprint: Optional[str] = None,
        created_at: Optional[str] = None,
        kb_citation: str = "",
        reviewed_at: Optional[str] = None,
    ) -> PendingApprovalEntry:
        _validate_enum("mode", mode, MODES)
        _validate_enum("status", status, STATUSES)
        if not (content or "").strip():
            raise ValueError("content is empty")
        if not (ticket_id or "").strip():
            raise ValueError("ticket_id is empty")

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
                    entry_id, ticket_id, title, content, target,
                    mode, status, created_at, reviewed_at, source_fingerprint,
                    kb_citation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    ticket_id.strip(),
                    (title or "").strip(),
                    content,
                    target_value,
                    mode,
                    status,
                    created_at,
                    reviewed_at,
                    fingerprint,
                    kb_citation or "",
                ),
            )
            self._conn.commit()

        entry = PendingApprovalEntry(
            entry_id=entry_id,
            ticket_id=ticket_id.strip(),
            title=(title or "").strip(),
            content=content,
            target=target_value,
            mode=mode,
            status=status,
            created_at=created_at,
            reviewed_at=reviewed_at,
            source_fingerprint=fingerprint,
            kb_citation=kb_citation or "",
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

    def get_by_ticket_id(self, ticket_id: str) -> Optional[PendingApprovalEntry]:
        needle = (ticket_id or "").strip()
        if not needle:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM pending_approval
                WHERE ticket_id = ?
                ORDER BY created_at DESC, entry_id DESC
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

    def update_content(
        self, entry_id: str, content: str
    ) -> Optional[PendingApprovalEntry]:
        body = (content or "").strip()
        if not body:
            raise ValueError("content is empty")
        with self._lock:
            self._conn.execute(
                "UPDATE pending_approval SET content = ? WHERE entry_id = ?",
                (body, entry_id),
            )
            self._conn.commit()
        return self.get_entry(entry_id)

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
        """Human approval of copy only. The agent never posts the reply."""
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
        from agents.techsupport_agent.notifications import notify_pending_approval

        notify_pending_approval(entry)


def _validate_enum(name: str, value: str, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"Invalid {name} {value!r}; expected one of {sorted(allowed)}")


def _row_to_entry(row: sqlite3.Row) -> PendingApprovalEntry:
    data = dict(row)
    return PendingApprovalEntry(
        entry_id=str(data["entry_id"]),
        ticket_id=str(data["ticket_id"] or ""),
        title=str(data.get("title") or ""),
        content=str(data["content"]),
        target=data.get("target"),
        mode=str(data["mode"]),
        status=str(data["status"]),
        created_at=str(data["created_at"]),
        reviewed_at=data.get("reviewed_at"),
        source_fingerprint=data.get("source_fingerprint"),
        kb_citation=str(data.get("kb_citation") or ""),
    )
