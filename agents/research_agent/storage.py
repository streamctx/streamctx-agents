"""SQLite store for research ideas and per-source poll timestamps.

Same style as ``agents.competitor_agent.storage`` (SQLite file under
``~/.streamctx``, uuid ids, UTC ISO timestamps).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.research_agent.models import (
    CLASSIFICATIONS,
    SOURCE_TYPES,
    STATUSES,
    STATUS_NEW,
    ResearchIdea,
)

DEFAULT_AGENT_DB = (
    Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    / "research_agent.db"
)


class ResearchStore:
    """Persists ``research_ideas`` plus last-poll times used for rate limits."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        self.db_path = Path(db_path or DEFAULT_AGENT_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_ideas (
                    idea_id TEXT PRIMARY KEY,
                    source_url TEXT,
                    source_type TEXT,
                    title TEXT,
                    gap_description TEXT,
                    feasibility_score INTEGER,
                    pain_match_score INTEGER,
                    novelty_score INTEGER,
                    composite_score REAL,
                    classification TEXT,
                    status TEXT,
                    detected_at TIMESTAMP,
                    content_excerpt TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_research_ideas_source_url
                    ON research_ideas (source_url);

                CREATE INDEX IF NOT EXISTS idx_research_ideas_detected_at
                    ON research_ideas (detected_at);

                CREATE INDEX IF NOT EXISTS idx_research_ideas_status_detected
                    ON research_ideas (status, detected_at);

                CREATE TABLE IF NOT EXISTS research_poll_state (
                    source_type TEXT PRIMARY KEY,
                    last_polled_at TIMESTAMP
                );
                """
            )
            self._conn.commit()

    def insert_idea(
        self,
        *,
        source_url: str,
        source_type: str,
        title: str,
        gap_description: Optional[str] = None,
        feasibility_score: Optional[int] = None,
        pain_match_score: Optional[int] = None,
        novelty_score: Optional[int] = None,
        composite_score: Optional[float] = None,
        classification: Optional[str] = None,
        status: str = STATUS_NEW,
        detected_at: Optional[str] = None,
        idea_id: Optional[str] = None,
        content_excerpt: Optional[str] = None,
    ) -> ResearchIdea:
        url = _require_text("source_url", source_url)
        _validate_enum("source_type", source_type, SOURCE_TYPES)
        title_text = _require_text("title", title)
        _validate_enum("status", status, STATUSES)
        if classification is not None:
            _validate_enum("classification", classification, CLASSIFICATIONS)
        idea_id = idea_id or str(uuid.uuid4())
        detected_at = detected_at or _utc_now()
        excerpt = (content_excerpt or "").strip() or None
        gap = (gap_description or "").strip() or None

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO research_ideas (
                    idea_id, source_url, source_type, title, gap_description,
                    feasibility_score, pain_match_score, novelty_score,
                    composite_score, classification, status, detected_at,
                    content_excerpt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idea_id,
                    url,
                    source_type,
                    title_text,
                    gap,
                    feasibility_score,
                    pain_match_score,
                    novelty_score,
                    composite_score,
                    classification,
                    status,
                    detected_at,
                    excerpt,
                ),
            )
            self._conn.commit()

        return ResearchIdea(
            idea_id=idea_id,
            source_url=url,
            source_type=source_type,
            title=title_text,
            gap_description=gap,
            feasibility_score=feasibility_score,
            pain_match_score=pain_match_score,
            novelty_score=novelty_score,
            composite_score=composite_score,
            classification=classification,
            status=status,
            detected_at=detected_at,
            content_excerpt=excerpt,
        )

    def get_idea(self, idea_id: str) -> Optional[ResearchIdea]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM research_ideas WHERE idea_id = ?",
                (idea_id,),
            ).fetchone()
        return _row_to_idea(row) if row else None

    def get_by_source_url(self, source_url: str) -> Optional[ResearchIdea]:
        url = _require_text("source_url", source_url)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM research_ideas WHERE source_url = ?",
                (url,),
            ).fetchone()
        return _row_to_idea(row) if row else None

    def list_ideas(
        self,
        *,
        source_type: Optional[str] = None,
        status: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[ResearchIdea]:
        clauses: list[str] = []
        params: list[object] = []
        if source_type is not None:
            _validate_enum("source_type", source_type, SOURCE_TYPES)
            clauses.append("source_type = ?")
            params.append(source_type)
        if status is not None:
            _validate_enum("status", status, STATUSES)
            clauses.append("status = ?")
            params.append(status)
        if since is not None:
            clauses.append("detected_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("detected_at <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM research_ideas "
            f"{where} ORDER BY detected_at DESC, idea_id DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_idea(row) for row in rows]

    def last_polled_at(self, source_type: str) -> Optional[str]:
        _validate_enum("source_type", source_type, SOURCE_TYPES)
        with self._lock:
            row = self._conn.execute(
                "SELECT last_polled_at FROM research_poll_state WHERE source_type = ?",
                (source_type,),
            ).fetchone()
        if row is None:
            return None
        return str(row["last_polled_at"])

    def set_last_polled_at(self, source_type: str, polled_at: Optional[str] = None) -> str:
        _validate_enum("source_type", source_type, SOURCE_TYPES)
        stamp = polled_at or _utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO research_poll_state (source_type, last_polled_at)
                VALUES (?, ?)
                ON CONFLICT(source_type) DO UPDATE SET last_polled_at = excluded.last_polled_at
                """,
                (source_type, stamp),
            )
            self._conn.commit()
        return stamp


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_text(name: str, value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} is empty")
    return text


def _validate_enum(name: str, value: str, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"Invalid {name} {value!r}; expected one of {sorted(allowed)}")


def _row_to_idea(row: sqlite3.Row) -> ResearchIdea:
    data = dict(row)
    excerpt = data.get("content_excerpt")
    gap = data.get("gap_description")
    classification = data.get("classification")
    return ResearchIdea(
        idea_id=str(data["idea_id"]),
        source_url=str(data["source_url"]),
        source_type=str(data["source_type"]),
        title=str(data["title"]),
        gap_description=str(gap) if gap else None,
        feasibility_score=_optional_int(data.get("feasibility_score")),
        pain_match_score=_optional_int(data.get("pain_match_score")),
        novelty_score=_optional_int(data.get("novelty_score")),
        composite_score=_optional_float(data.get("composite_score")),
        classification=str(classification) if classification else None,
        status=str(data["status"]),
        detected_at=str(data["detected_at"]),
        content_excerpt=str(excerpt) if excerpt else None,
    )


def _optional_int(value: object) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _optional_float(value: object) -> Optional[float]:
    if value is None:
        return None
    return float(value)
