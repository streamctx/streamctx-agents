"""SQLite store for imported Sales Navigator leads (``~/.streamctx/leads.db``)."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from agents.presales_agent.models import (
    DRAFT_NONE,
    DRAFT_STATUSES,
    FLAG_NONE,
    FLAGS,
    PIPELINE_IMPORTED,
    PIPELINE_STATUSES,
    Lead,
)
from agents.presales_agent.pending_approval import DEFAULT_AGENT_DB


class LeadStore:
    """Persists the ``leads`` table. Pending drafts live in the same file."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        self.db_path = Path(db_path or DEFAULT_AGENT_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    name TEXT,
                    title TEXT,
                    company TEXT,
                    company_size TEXT,
                    industry TEXT,
                    linkedin_url TEXT,
                    score REAL,
                    score_rationale TEXT,
                    pipeline_status TEXT,
                    draft_status TEXT,
                    draft_text TEXT,
                    draft_entry_id TEXT,
                    flag TEXT,
                    source_fingerprint TEXT,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_fingerprint
                    ON leads(source_fingerprint);
                """
            )
            self._conn.commit()

    def upsert_lead(
        self,
        *,
        name: str,
        title: str = "",
        company: str = "",
        company_size: str = "",
        industry: str = "",
        linkedin_url: Optional[str] = None,
        source_fingerprint: str,
        lead_id: Optional[str] = None,
        pipeline_status: str = PIPELINE_IMPORTED,
        created_at: Optional[str] = None,
    ) -> tuple[Lead, bool]:
        """Insert or refresh identity fields. Returns ``(lead, created)``."""
        fingerprint = (source_fingerprint or "").strip()
        if not fingerprint:
            raise ValueError("source_fingerprint is empty")
        if pipeline_status not in PIPELINE_STATUSES:
            raise ValueError(f"Invalid pipeline_status {pipeline_status!r}")
        now = created_at or datetime.now(timezone.utc).isoformat()
        existing = self.get_by_fingerprint(fingerprint)
        if existing is not None:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE leads SET
                        name = ?, title = ?, company = ?, company_size = ?,
                        industry = ?, linkedin_url = ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (
                        name,
                        title,
                        company,
                        company_size,
                        industry,
                        linkedin_url,
                        now,
                        existing.lead_id,
                    ),
                )
                self._conn.commit()
            updated = self.get_lead(existing.lead_id)
            assert updated is not None
            return updated, False

        lead_id = lead_id or str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO leads (
                    lead_id, name, title, company, company_size, industry,
                    linkedin_url, score, score_rationale, pipeline_status,
                    draft_status, draft_text, draft_entry_id, flag,
                    source_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead_id,
                    name,
                    title,
                    company,
                    company_size,
                    industry,
                    linkedin_url,
                    None,
                    "",
                    pipeline_status,
                    DRAFT_NONE,
                    "",
                    None,
                    FLAG_NONE,
                    fingerprint,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        created = self.get_lead(lead_id)
        assert created is not None
        return created, True

    def get_lead(self, lead_id: str) -> Optional[Lead]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leads WHERE lead_id = ?",
                (lead_id,),
            ).fetchone()
        return _row_to_lead(row) if row else None

    def get_by_fingerprint(self, fingerprint: str) -> Optional[Lead]:
        needle = (fingerprint or "").strip()
        if not needle:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leads WHERE source_fingerprint = ?",
                (needle,),
            ).fetchone()
        return _row_to_lead(row) if row else None

    def list_leads(
        self,
        *,
        pipeline_status: Optional[str] = None,
        order_by_score: bool = True,
    ) -> list[Lead]:
        sql = "SELECT * FROM leads"
        params: list[object] = []
        if pipeline_status is not None:
            if pipeline_status not in PIPELINE_STATUSES:
                raise ValueError(f"Invalid pipeline_status {pipeline_status!r}")
            sql += " WHERE pipeline_status = ?"
            params.append(pipeline_status)
        if order_by_score:
            sql += " ORDER BY (score IS NULL), score DESC, name ASC"
        else:
            sql += " ORDER BY updated_at DESC, name ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_lead(row) for row in rows]

    def record_score(
        self,
        lead_id: str,
        score: float,
        rationale: str,
        *,
        updated_at: Optional[str] = None,
    ) -> Optional[Lead]:
        stamp = updated_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE leads
                SET score = ?, score_rationale = ?, updated_at = ?
                WHERE lead_id = ?
                """,
                (float(score), rationale, stamp, lead_id),
            )
            self._conn.commit()
        return self.get_lead(lead_id)

    def record_draft(
        self,
        lead_id: str,
        *,
        draft_text: str,
        draft_status: str,
        draft_entry_id: Optional[str] = None,
        flag: str = FLAG_NONE,
        updated_at: Optional[str] = None,
    ) -> Optional[Lead]:
        if draft_status not in DRAFT_STATUSES:
            raise ValueError(f"Invalid draft_status {draft_status!r}")
        if flag not in FLAGS:
            raise ValueError(f"Invalid flag {flag!r}")
        stamp = updated_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE leads
                SET draft_text = ?, draft_status = ?, draft_entry_id = ?,
                    flag = ?, updated_at = ?
                WHERE lead_id = ?
                """,
                (draft_text, draft_status, draft_entry_id, flag, stamp, lead_id),
            )
            self._conn.commit()
        return self.get_lead(lead_id)

    def set_pipeline_status(
        self,
        lead_id: str,
        pipeline_status: str,
        *,
        updated_at: Optional[str] = None,
    ) -> Optional[Lead]:
        if pipeline_status not in PIPELINE_STATUSES:
            raise ValueError(f"Invalid pipeline_status {pipeline_status!r}")
        stamp = updated_at or datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE leads
                SET pipeline_status = ?, updated_at = ?
                WHERE lead_id = ?
                """,
                (pipeline_status, stamp, lead_id),
            )
            self._conn.commit()
        return self.get_lead(lead_id)

    def counts(self) -> dict[str, int]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
            pending = self._conn.execute(
                "SELECT COUNT(*) AS n FROM leads WHERE draft_status = ?",
                ("pending_approval",),
            ).fetchone()["n"]
            rows = self._conn.execute(
                """
                SELECT pipeline_status, COUNT(*) AS n
                FROM leads
                GROUP BY pipeline_status
                """
            ).fetchall()
        by_pipeline = {str(row["pipeline_status"]): int(row["n"]) for row in rows}
        return {"total": int(total), "pending_approval": int(pending), **by_pipeline}

    def leads_needing_score(self) -> list[Lead]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM leads WHERE score IS NULL ORDER BY name ASC"
            ).fetchall()
        return [_row_to_lead(row) for row in rows]

    def leads_ready_for_draft(
        self, *, min_score: float, statuses: Optional[Sequence[str]] = None
    ) -> list[Lead]:
        allowed = tuple(statuses or ("none", "rejected"))
        placeholders = ",".join("?" for _ in allowed)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM leads
                WHERE score IS NOT NULL
                  AND score >= ?
                  AND draft_status IN ({placeholders})
                ORDER BY score DESC, name ASC
                """,
                (float(min_score), *allowed),
            ).fetchall()
        return [_row_to_lead(row) for row in rows]


def _row_to_lead(row: sqlite3.Row) -> Lead:
    data = dict(row)
    score = data.get("score")
    return Lead(
        lead_id=str(data["lead_id"]),
        name=str(data.get("name") or ""),
        title=str(data.get("title") or ""),
        company=str(data.get("company") or ""),
        company_size=str(data.get("company_size") or ""),
        industry=str(data.get("industry") or ""),
        linkedin_url=data.get("linkedin_url"),
        score=float(score) if score is not None else None,
        score_rationale=str(data.get("score_rationale") or ""),
        pipeline_status=str(data.get("pipeline_status") or PIPELINE_IMPORTED),
        draft_status=str(data.get("draft_status") or DRAFT_NONE),
        draft_text=str(data.get("draft_text") or ""),
        draft_entry_id=data.get("draft_entry_id"),
        flag=str(data.get("flag") or FLAG_NONE),
        source_fingerprint=str(data.get("source_fingerprint") or ""),
        created_at=str(data["created_at"]),
        updated_at=str(data["updated_at"]),
    )
