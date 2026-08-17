"""SQLite store for marketing drafts awaiting human review.

Same style as ``agents.coding_agent.pending_approval`` (SQLite file under
``~/.streamctx``, uuid ``entry_id``, UTC timestamps) but a different column
set: coding-agent rows are diffs/diagnoses; marketing rows are platform
content. They do not share a table.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from agents.marketing_agent.models import PendingApprovalEntry

DEFAULT_AGENT_DB = (
    Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    / "marketing_agent.db"
)

PLATFORM_TWITTER = "twitter"
PLATFORM_DEVTO = "devto"
PLATFORM_REDDIT = "reddit"
PLATFORM_LINKEDIN = "linkedin"
PLATFORM_HN = "hn"
PLATFORM_INDIEHACKERS = "indiehackers"
PLATFORM_PRODUCTHUNT = "producthunt"

PLATFORMS = frozenset(
    {
        PLATFORM_TWITTER,
        PLATFORM_DEVTO,
        PLATFORM_REDDIT,
        PLATFORM_LINKEDIN,
        PLATFORM_HN,
        PLATFORM_INDIEHACKERS,
        PLATFORM_PRODUCTHUNT,
    }
)

CONTENT_POST = "post"
CONTENT_COMMENT = "comment"
CONTENT_DM = "dm"
CONTENT_TYPES = frozenset({CONTENT_POST, CONTENT_COMMENT, CONTENT_DM})

MODE_AUTO_AFTER_APPROVAL = "auto_after_approval"
MODE_DRAFT_ONLY = "draft_only"
MODES = frozenset({MODE_AUTO_AFTER_APPROVAL, MODE_DRAFT_ONLY})

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_PUBLISHED = "published"
STATUSES = frozenset(
    {STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_PUBLISHED}
)

NotifierFn = Callable[["PendingApprovalEntry"], None]


class PendingApprovalStore:
    """Persists marketing ``pending_approval`` rows."""

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
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
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
                    platform TEXT,
                    content_type TEXT,
                    content TEXT,
                    target TEXT,
                    mode TEXT,
                    status TEXT,
                    created_at TIMESTAMP,
                    published_at TIMESTAMP,
                    source_fingerprint TEXT
                );
                """
            )
            self._ensure_column("pending_approval", "source_fingerprint", "TEXT")
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
        platform: str,
        content_type: str,
        content: str,
        target: Optional[str] = None,
        mode: str = MODE_DRAFT_ONLY,
        status: str = STATUS_PENDING,
        entry_id: Optional[str] = None,
        published_at: Optional[str] = None,
        source_fingerprint: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> PendingApprovalEntry:
        _validate_enum("platform", platform, PLATFORMS)
        _validate_enum("content_type", content_type, CONTENT_TYPES)
        _validate_enum("mode", mode, MODES)
        _validate_enum("status", status, STATUSES)
        if not (content or "").strip():
            raise ValueError("content is empty")

        entry_id = entry_id or str(uuid.uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        target_value = target.strip() if target and target.strip() else None
        fingerprint = (
            source_fingerprint.strip() if source_fingerprint and source_fingerprint.strip() else None
        )

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pending_approval (
                    entry_id, platform, content_type, content, target,
                    mode, status, created_at, published_at, source_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    platform,
                    content_type,
                    content,
                    target_value,
                    mode,
                    status,
                    created_at,
                    published_at,
                    fingerprint,
                ),
            )
            self._conn.commit()

        entry = PendingApprovalEntry(
            entry_id=entry_id,
            platform=platform,
            content_type=content_type,
            content=content,
            target=target_value,
            mode=mode,
            status=status,
            created_at=created_at,
            published_at=published_at,
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

    def list_by_platform(self, platform: str) -> list[PendingApprovalEntry]:
        _validate_enum("platform", platform, PLATFORMS)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM pending_approval
                WHERE platform = ?
                ORDER BY created_at DESC
                """,
                (platform,),
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def update_status(
        self,
        entry_id: str,
        status: str,
        *,
        published_at: Optional[str] = None,
    ) -> Optional[PendingApprovalEntry]:
        _validate_enum("status", status, STATUSES)
        with self._lock:
            if published_at is not None:
                self._conn.execute(
                    """
                    UPDATE pending_approval
                    SET status = ?, published_at = ?
                    WHERE entry_id = ?
                    """,
                    (status, published_at, entry_id),
                )
            else:
                self._conn.execute(
                    "UPDATE pending_approval SET status = ? WHERE entry_id = ?",
                    (status, entry_id),
                )
            self._conn.commit()
        return self.get_entry(entry_id)

    def list_on_utc_day(
        self,
        day: str,
        *,
        statuses: Optional[Sequence[str]] = None,
    ) -> list[PendingApprovalEntry]:
        """Rows whose UTC ``created_at`` falls on ``YYYY-MM-DD``."""
        allowed = tuple(statuses) if statuses is not None else tuple(STATUSES)
        placeholders = ",".join("?" for _ in allowed)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM pending_approval
                WHERE created_at LIKE ?
                  AND status IN ({placeholders})
                ORDER BY created_at DESC
                """,
                (f"{day}%", *allowed),
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def approve(self, entry_id: str) -> PendingApprovalEntry:
        """Human approval gate — required before any auto-tier ``publish()``."""
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
        if entry.status == STATUS_PUBLISHED:
            raise ValueError(f"cannot reject already-published entry {entry_id}")
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
        from agents.marketing_agent.notifications import notify_pending_approval

        notify_pending_approval(entry)


def _validate_enum(name: str, value: str, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"Invalid {name} {value!r}; expected one of {sorted(allowed)}")


def _row_to_entry(row: sqlite3.Row) -> PendingApprovalEntry:
    data = dict(row)
    return PendingApprovalEntry(
        entry_id=str(data["entry_id"]),
        platform=str(data["platform"]),
        content_type=str(data["content_type"]),
        content=str(data["content"]),
        target=data.get("target"),
        mode=str(data["mode"]),
        status=str(data["status"]),
        created_at=str(data["created_at"]),
        published_at=data.get("published_at"),
        source_fingerprint=data.get("source_fingerprint"),
    )
