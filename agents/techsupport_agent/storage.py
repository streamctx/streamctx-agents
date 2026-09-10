"""SQLite store for raw support tickets (``~/.streamctx/support_tickets.db``)."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from agents.techsupport_agent.models import (
    STATUS_CLASSIFIED,
    STATUS_NEW,
    TICKET_STATUSES,
    Ticket,
)
from agents.techsupport_agent.pending_approval import DEFAULT_AGENT_DB
from shared.db import connect


class TicketStore:
    """Persists the ``tickets`` table. Pending drafts live in the same file."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        self.db_path = Path(db_path or DEFAULT_AGENT_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect(
            schema="support_tickets",
            db_path=self.db_path,
            check_same_thread=False,
            timeout=30,
        )
        self._init_db()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    source TEXT,
                    source_ref TEXT,
                    source_url TEXT,
                    title TEXT,
                    body TEXT,
                    author TEXT,
                    ticket_type TEXT,
                    severity TEXT,
                    classification_confidence REAL,
                    classification_rationale TEXT,
                    kb_match_path TEXT,
                    kb_match_heading TEXT,
                    kb_match_excerpt TEXT,
                    kb_match_confidence REAL,
                    status TEXT,
                    draft_text TEXT,
                    draft_entry_id TEXT,
                    source_fingerprint TEXT,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_fingerprint
                    ON tickets(source_fingerprint);
                CREATE TABLE IF NOT EXISTS support_poll_state (
                    source TEXT PRIMARY KEY,
                    last_polled_at TIMESTAMP
                );
                """
            )
            self._conn.commit()

    def insert_ticket(
        self,
        *,
        source: str,
        source_ref: str,
        source_url: str,
        title: str,
        body: str,
        author: str,
        source_fingerprint: str,
        ticket_id: Optional[str] = None,
        status: str = STATUS_NEW,
        created_at: Optional[str] = None,
    ) -> Ticket:
        fingerprint = (source_fingerprint or "").strip()
        if not fingerprint:
            raise ValueError("source_fingerprint is empty")
        if status not in TICKET_STATUSES:
            raise ValueError(f"Invalid status {status!r}")
        now = created_at or datetime.now(timezone.utc).isoformat()
        ticket_id = ticket_id or str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tickets (
                    ticket_id, source, source_ref, source_url, title, body,
                    author, ticket_type, severity, classification_confidence,
                    classification_rationale, kb_match_path, kb_match_heading,
                    kb_match_excerpt, kb_match_confidence, status, draft_text,
                    draft_entry_id, source_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    source,
                    source_ref,
                    source_url,
                    title,
                    body,
                    author,
                    None,
                    None,
                    None,
                    "",
                    "",
                    "",
                    "",
                    None,
                    status,
                    "",
                    None,
                    fingerprint,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        created = self.get_ticket(ticket_id)
        assert created is not None
        return created

    def get_ticket(self, ticket_id: str) -> Optional[Ticket]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        return _row_to_ticket(row) if row else None

    def get_by_fingerprint(self, fingerprint: str) -> Optional[Ticket]:
        needle = (fingerprint or "").strip()
        if not needle:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tickets WHERE source_fingerprint = ?",
                (needle,),
            ).fetchone()
        return _row_to_ticket(row) if row else None

    def list_tickets(
        self,
        *,
        status: Optional[str] = None,
        statuses: Optional[Sequence[str]] = None,
    ) -> list[Ticket]:
        sql = "SELECT * FROM tickets"
        params: list[object] = []
        if status is not None:
            if status not in TICKET_STATUSES:
                raise ValueError(f"Invalid status {status!r}")
            sql += " WHERE status = ?"
            params.append(status)
        elif statuses is not None:
            for item in statuses:
                if item not in TICKET_STATUSES:
                    raise ValueError(f"Invalid status {item!r}")
            placeholders = ",".join("?" for _ in statuses)
            sql += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY created_at DESC, ticket_id DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_ticket(row) for row in rows]

    def tickets_needing_process(self) -> list[Ticket]:
        return self.list_tickets(
            statuses=(STATUS_NEW, STATUS_CLASSIFIED),
        )

    def record_classification(
        self,
        ticket_id: str,
        *,
        ticket_type: str,
        severity: str,
        confidence: float,
        rationale: str,
        updated_at: Optional[str] = None,
        status: str = "classified",
    ) -> Optional[Ticket]:
        if status not in TICKET_STATUSES:
            raise ValueError(f"Invalid status {status!r}")
        stamp = updated_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE tickets
                SET ticket_type = ?, severity = ?, classification_confidence = ?,
                    classification_rationale = ?, status = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (
                    ticket_type,
                    severity,
                    float(confidence),
                    rationale,
                    status,
                    stamp,
                    ticket_id,
                ),
            )
            self._conn.commit()
        return self.get_ticket(ticket_id)

    def record_kb_match(
        self,
        ticket_id: str,
        *,
        path: str,
        heading: str,
        excerpt: str,
        confidence: Optional[float],
        updated_at: Optional[str] = None,
    ) -> Optional[Ticket]:
        stamp = updated_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE tickets
                SET kb_match_path = ?, kb_match_heading = ?, kb_match_excerpt = ?,
                    kb_match_confidence = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (path, heading, excerpt, confidence, stamp, ticket_id),
            )
            self._conn.commit()
        return self.get_ticket(ticket_id)

    def record_draft(
        self,
        ticket_id: str,
        *,
        draft_text: str,
        draft_entry_id: Optional[str],
        status: str,
        updated_at: Optional[str] = None,
    ) -> Optional[Ticket]:
        if status not in TICKET_STATUSES:
            raise ValueError(f"Invalid status {status!r}")
        stamp = updated_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE tickets
                SET draft_text = ?, draft_entry_id = ?, status = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (draft_text, draft_entry_id, status, stamp, ticket_id),
            )
            self._conn.commit()
        return self.get_ticket(ticket_id)

    def set_status(
        self,
        ticket_id: str,
        status: str,
        *,
        updated_at: Optional[str] = None,
    ) -> Optional[Ticket]:
        if status not in TICKET_STATUSES:
            raise ValueError(f"Invalid status {status!r}")
        stamp = updated_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE tickets SET status = ?, updated_at = ? WHERE ticket_id = ?
                """,
                (status, stamp, ticket_id),
            )
            self._conn.commit()
        return self.get_ticket(ticket_id)

    def last_polled_at(self, source: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_polled_at FROM support_poll_state WHERE source = ?",
                (source,),
            ).fetchone()
        if row is None:
            return None
        value = row["last_polled_at"]
        return str(value) if value else None

    def set_last_polled_at(self, source: str, stamped: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO support_poll_state (source, last_polled_at)
                VALUES (?, ?)
                ON CONFLICT(source) DO UPDATE SET last_polled_at = excluded.last_polled_at
                """,
                (source, stamped),
            )
            self._conn.commit()

    def counts(self) -> dict[str, int]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"
            ).fetchall()
        by_status = {str(row["status"]): int(row["n"]) for row in rows}
        return {"total": int(total), **by_status}


def _row_to_ticket(row: sqlite3.Row) -> Ticket:
    data = dict(row)
    conf = data.get("classification_confidence")
    kb_conf = data.get("kb_match_confidence")
    return Ticket(
        ticket_id=str(data["ticket_id"]),
        source=str(data.get("source") or ""),
        source_ref=str(data.get("source_ref") or ""),
        source_url=str(data.get("source_url") or ""),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        author=str(data.get("author") or ""),
        ticket_type=data.get("ticket_type"),
        severity=data.get("severity"),
        classification_confidence=float(conf) if conf is not None else None,
        classification_rationale=str(data.get("classification_rationale") or ""),
        kb_match_path=str(data.get("kb_match_path") or ""),
        kb_match_heading=str(data.get("kb_match_heading") or ""),
        kb_match_excerpt=str(data.get("kb_match_excerpt") or ""),
        kb_match_confidence=float(kb_conf) if kb_conf is not None else None,
        status=str(data.get("status") or STATUS_NEW),
        draft_text=str(data.get("draft_text") or ""),
        draft_entry_id=data.get("draft_entry_id"),
        source_fingerprint=str(data.get("source_fingerprint") or ""),
        created_at=str(data["created_at"]),
        updated_at=str(data["updated_at"]),
    )
