"""SQLite store for legal drafts awaiting human + counsel review.

Same style as ``agents.techsupport_agent.pending_approval``. Lives in
``compliance_findings.db``. Mode is always ``draft_only`` — this agent never
edits TERMS.md / PRIVACY.md and never publishes.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from agents.legal_compliance_agent.models import PendingApprovalEntry

DEFAULT_AGENT_DB = (
    Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    / "compliance_findings.db"
)

MODE_DRAFT_ONLY = "draft_only"
MODES = frozenset({MODE_DRAFT_ONLY})

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUSES = frozenset({STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED})

NotifierFn = Callable[["PendingApprovalEntry"], None]


class PendingApprovalStore:
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
                    finding_id TEXT,
                    title TEXT,
                    content TEXT,
                    target TEXT,
                    mode TEXT,
                    status TEXT,
                    created_at TIMESTAMP,
                    reviewed_at TIMESTAMP,
                    source_fingerprint TEXT,
                    kind TEXT
                );
                """
            )
            self._conn.commit()

    def create_entry(
        self,
        *,
        finding_id: str,
        content: str,
        title: str = "",
        target: Optional[str] = None,
        mode: str = MODE_DRAFT_ONLY,
        status: str = STATUS_PENDING,
        entry_id: Optional[str] = None,
        source_fingerprint: Optional[str] = None,
        created_at: Optional[str] = None,
        kind: str = "",
        reviewed_at: Optional[str] = None,
    ) -> PendingApprovalEntry:
        _validate_enum("mode", mode, MODES)
        _validate_enum("status", status, STATUSES)
        if not (content or "").strip():
            raise ValueError("content is empty")
        if not (finding_id or "").strip():
            raise ValueError("finding_id is empty")

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
                    entry_id, finding_id, title, content, target,
                    mode, status, created_at, reviewed_at, source_fingerprint, kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    finding_id.strip(),
                    (title or "").strip(),
                    content,
                    target_value,
                    mode,
                    status,
                    created_at,
                    reviewed_at,
                    fingerprint,
                    kind or "",
                ),
            )
            self._conn.commit()

        entry = PendingApprovalEntry(
            entry_id=entry_id,
            finding_id=finding_id.strip(),
            title=(title or "").strip(),
            content=content,
            target=target_value,
            mode=mode,
            status=status,
            created_at=created_at,
            reviewed_at=reviewed_at,
            source_fingerprint=fingerprint,
            kind=kind or "",
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
        """Human approval of copy only. The agent never writes policy files."""
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
        from agents.legal_compliance_agent.notifications import notify_pending_approval

        notify_pending_approval(entry)


def _validate_enum(name: str, value: str, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"Invalid {name} {value!r}; expected one of {sorted(allowed)}")


def _row_to_entry(row: sqlite3.Row) -> PendingApprovalEntry:
    data = dict(row)
    return PendingApprovalEntry(
        entry_id=str(data["entry_id"]),
        finding_id=str(data["finding_id"] or ""),
        title=str(data.get("title") or ""),
        content=str(data["content"]),
        target=data.get("target"),
        mode=str(data["mode"]),
        status=str(data["status"]),
        created_at=str(data["created_at"]),
        reviewed_at=data.get("reviewed_at"),
        source_fingerprint=data.get("source_fingerprint"),
        kind=str(data.get("kind") or ""),
    )
