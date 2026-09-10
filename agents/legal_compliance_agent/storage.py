"""SQLite store for compliance findings and DPDP checklist rows."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.legal_compliance_agent.models import (
    CHECKLIST_STATUSES,
    FINDING_KINDS,
    FINDING_STATUSES,
    STATUS_NEW,
    ChecklistItem,
    Finding,
)
from agents.legal_compliance_agent.pending_approval import DEFAULT_AGENT_DB
from shared.db import connect


class FindingStore:
    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        self.db_path = Path(db_path or DEFAULT_AGENT_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect(
            schema="compliance_findings",
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
                CREATE TABLE IF NOT EXISTS findings (
                    finding_id TEXT PRIMARY KEY,
                    kind TEXT,
                    severity TEXT,
                    title TEXT,
                    evidence TEXT,
                    suggested_language TEXT,
                    source_path TEXT,
                    status TEXT,
                    draft_text TEXT,
                    draft_entry_id TEXT,
                    source_fingerprint TEXT,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_fingerprint
                    ON findings(source_fingerprint);
                CREATE TABLE IF NOT EXISTS dpdp_checklist (
                    item_id TEXT PRIMARY KEY,
                    area TEXT,
                    requirement TEXT,
                    current_behavior TEXT,
                    gap TEXT,
                    status TEXT,
                    finding_id TEXT,
                    updated_at TIMESTAMP
                );
                """
            )
            self._conn.commit()

    def upsert_finding(
        self,
        *,
        kind: str,
        severity: str,
        title: str,
        evidence: str,
        suggested_language: str,
        source_path: str,
        source_fingerprint: str,
        finding_id: Optional[str] = None,
        status: str = STATUS_NEW,
        created_at: Optional[str] = None,
    ) -> tuple[Finding, bool]:
        if kind not in FINDING_KINDS:
            raise ValueError(f"Invalid kind {kind!r}")
        if status not in FINDING_STATUSES:
            raise ValueError(f"Invalid status {status!r}")
        fingerprint = (source_fingerprint or "").strip()
        if not fingerprint:
            raise ValueError("source_fingerprint is empty")
        existing = self.get_by_fingerprint(fingerprint)
        now = created_at or datetime.now(timezone.utc).isoformat()
        if existing is not None:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE findings SET
                        kind = ?, severity = ?, title = ?, evidence = ?,
                        suggested_language = ?, source_path = ?, updated_at = ?
                    WHERE finding_id = ?
                    """,
                    (
                        kind,
                        severity,
                        title,
                        evidence,
                        suggested_language,
                        source_path,
                        now,
                        existing.finding_id,
                    ),
                )
                self._conn.commit()
            updated = self.get_finding(existing.finding_id)
            assert updated is not None
            return updated, False

        finding_id = finding_id or str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO findings (
                    finding_id, kind, severity, title, evidence,
                    suggested_language, source_path, status, draft_text,
                    draft_entry_id, source_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding_id,
                    kind,
                    severity,
                    title,
                    evidence,
                    suggested_language,
                    source_path,
                    status,
                    "",
                    None,
                    fingerprint,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        created = self.get_finding(finding_id)
        assert created is not None
        return created, True

    def get_finding(self, finding_id: str) -> Optional[Finding]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
        return _row_to_finding(row) if row else None

    def get_by_fingerprint(self, fingerprint: str) -> Optional[Finding]:
        needle = (fingerprint or "").strip()
        if not needle:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM findings WHERE source_fingerprint = ?",
                (needle,),
            ).fetchone()
        return _row_to_finding(row) if row else None

    def list_findings(self, *, status: Optional[str] = None) -> list[Finding]:
        sql = "SELECT * FROM findings"
        params: list[object] = []
        if status is not None:
            if status not in FINDING_STATUSES:
                raise ValueError(f"Invalid status {status!r}")
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, finding_id DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_finding(row) for row in rows]

    def record_draft(
        self,
        finding_id: str,
        *,
        draft_text: str,
        draft_entry_id: Optional[str],
        status: str,
        updated_at: Optional[str] = None,
    ) -> Optional[Finding]:
        if status not in FINDING_STATUSES:
            raise ValueError(f"Invalid status {status!r}")
        stamp = updated_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE findings
                SET draft_text = ?, draft_entry_id = ?, status = ?, updated_at = ?
                WHERE finding_id = ?
                """,
                (draft_text, draft_entry_id, status, stamp, finding_id),
            )
            self._conn.commit()
        return self.get_finding(finding_id)

    def set_status(self, finding_id: str, status: str) -> Optional[Finding]:
        if status not in FINDING_STATUSES:
            raise ValueError(f"Invalid status {status!r}")
        stamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE findings SET status = ?, updated_at = ? WHERE finding_id = ?",
                (status, stamp, finding_id),
            )
            self._conn.commit()
        return self.get_finding(finding_id)

    def upsert_checklist(
        self,
        *,
        item_id: str,
        area: str,
        requirement: str,
        current_behavior: str,
        gap: str,
        status: str,
        finding_id: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> ChecklistItem:
        if status not in CHECKLIST_STATUSES:
            raise ValueError(f"Invalid checklist status {status!r}")
        stamp = updated_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO dpdp_checklist (
                    item_id, area, requirement, current_behavior, gap,
                    status, finding_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    area = excluded.area,
                    requirement = excluded.requirement,
                    current_behavior = excluded.current_behavior,
                    gap = excluded.gap,
                    status = excluded.status,
                    finding_id = excluded.finding_id,
                    updated_at = excluded.updated_at
                """,
                (
                    item_id,
                    area,
                    requirement,
                    current_behavior,
                    gap,
                    status,
                    finding_id,
                    stamp,
                ),
            )
            self._conn.commit()
        item = self.get_checklist_item(item_id)
        assert item is not None
        return item

    def get_checklist_item(self, item_id: str) -> Optional[ChecklistItem]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM dpdp_checklist WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        return _row_to_checklist(row) if row else None

    def list_checklist(self) -> list[ChecklistItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dpdp_checklist ORDER BY area ASC, item_id ASC"
            ).fetchall()
        return [_row_to_checklist(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS n FROM findings").fetchone()["n"]
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM findings GROUP BY status"
            ).fetchall()
            gaps = self._conn.execute(
                "SELECT COUNT(*) AS n FROM dpdp_checklist WHERE status = 'gap'"
            ).fetchone()["n"]
        by_status = {str(row["status"]): int(row["n"]) for row in rows}
        return {"total": int(total), "dpdp_gaps": int(gaps), **by_status}


def _row_to_finding(row: sqlite3.Row) -> Finding:
    data = dict(row)
    return Finding(
        finding_id=str(data["finding_id"]),
        kind=str(data.get("kind") or ""),
        severity=str(data.get("severity") or ""),
        title=str(data.get("title") or ""),
        evidence=str(data.get("evidence") or ""),
        suggested_language=str(data.get("suggested_language") or ""),
        source_path=str(data.get("source_path") or ""),
        status=str(data.get("status") or STATUS_NEW),
        draft_text=str(data.get("draft_text") or ""),
        draft_entry_id=data.get("draft_entry_id"),
        source_fingerprint=str(data.get("source_fingerprint") or ""),
        created_at=str(data["created_at"]),
        updated_at=str(data["updated_at"]),
    )


def _row_to_checklist(row: sqlite3.Row) -> ChecklistItem:
    data = dict(row)
    return ChecklistItem(
        item_id=str(data["item_id"]),
        area=str(data.get("area") or ""),
        requirement=str(data.get("requirement") or ""),
        current_behavior=str(data.get("current_behavior") or ""),
        gap=str(data.get("gap") or ""),
        status=str(data.get("status") or ""),
        finding_id=data.get("finding_id"),
        updated_at=str(data["updated_at"]),
    )
