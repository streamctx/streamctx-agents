"""SQLite store for competitor snapshots, signals, and weekly reports.

Same style as ``agents.coding_agent.pending_approval`` (SQLite file under
``~/.streamctx``, uuid ids, UTC ISO timestamps). Competitor names are
free-form TEXT so the tracked list stays config-driven in later stages.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.competitor_agent.models import (
    SIGNAL_TYPES,
    SNAPSHOT_TYPES,
    CompetitorSignal,
    CompetitorSnapshot,
    WeeklyReport,
)

DEFAULT_AGENT_DB = (
    Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    / "competitor_agent.db"
)


class CompetitorStore:
    """Persists competitor tracking rows used by later poll/report stages."""

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
                CREATE TABLE IF NOT EXISTS competitor_snapshots (
                    competitor TEXT,
                    snapshot_type TEXT,
                    content_hash TEXT,
                    raw_content TEXT,
                    captured_at TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS competitor_signals (
                    signal_id TEXT PRIMARY KEY,
                    competitor TEXT,
                    signal_type TEXT,
                    summary TEXT,
                    source_url TEXT,
                    detected_at TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS weekly_reports (
                    report_id TEXT PRIMARY KEY,
                    week_start DATE,
                    content_markdown TEXT,
                    created_at TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_competitor_type_captured
                    ON competitor_snapshots (competitor, snapshot_type, captured_at);

                CREATE INDEX IF NOT EXISTS idx_signals_detected_at
                    ON competitor_signals (detected_at);

                CREATE INDEX IF NOT EXISTS idx_signals_competitor_detected
                    ON competitor_signals (competitor, detected_at);

                CREATE INDEX IF NOT EXISTS idx_weekly_reports_week_start
                    ON weekly_reports (week_start);
                """
            )
            self._conn.commit()

    # --- snapshots ---------------------------------------------------------

    def insert_snapshot(
        self,
        *,
        competitor: str,
        snapshot_type: str,
        content_hash: str,
        raw_content: str,
        captured_at: Optional[str] = None,
    ) -> CompetitorSnapshot:
        competitor_name = _require_text("competitor", competitor)
        _validate_enum("snapshot_type", snapshot_type, SNAPSHOT_TYPES)
        digest = _require_text("content_hash", content_hash)
        captured_at = captured_at or _utc_now()

        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO competitor_snapshots (
                    competitor, snapshot_type, content_hash, raw_content, captured_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (competitor_name, snapshot_type, digest, raw_content, captured_at),
            )
            self._conn.commit()
            rowid = int(cursor.lastrowid)

        return CompetitorSnapshot(
            competitor=competitor_name,
            snapshot_type=snapshot_type,
            content_hash=digest,
            raw_content=raw_content,
            captured_at=captured_at,
            rowid=rowid,
        )

    def latest_snapshot(
        self,
        competitor: str,
        snapshot_type: str,
    ) -> Optional[CompetitorSnapshot]:
        """Most recent snapshot for ``competitor`` + ``snapshot_type``."""
        competitor_name = _require_text("competitor", competitor)
        _validate_enum("snapshot_type", snapshot_type, SNAPSHOT_TYPES)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT rowid, competitor, snapshot_type, content_hash,
                       raw_content, captured_at
                FROM competitor_snapshots
                WHERE competitor = ? AND snapshot_type = ?
                ORDER BY captured_at DESC, rowid DESC
                LIMIT 1
                """,
                (competitor_name, snapshot_type),
            ).fetchone()
        return _row_to_snapshot(row) if row else None

    def list_snapshots(
        self,
        *,
        competitor: Optional[str] = None,
        snapshot_type: Optional[str] = None,
        since: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[CompetitorSnapshot]:
        clauses: list[str] = []
        params: list[object] = []
        if competitor is not None:
            clauses.append("competitor = ?")
            params.append(_require_text("competitor", competitor))
        if snapshot_type is not None:
            _validate_enum("snapshot_type", snapshot_type, SNAPSHOT_TYPES)
            clauses.append("snapshot_type = ?")
            params.append(snapshot_type)
        if since is not None:
            clauses.append("captured_at >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT rowid, competitor, snapshot_type, content_hash, "
            "raw_content, captured_at FROM competitor_snapshots "
            f"{where} ORDER BY captured_at DESC, rowid DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_snapshot(row) for row in rows]

    # --- signals -----------------------------------------------------------

    def insert_signal(
        self,
        *,
        competitor: str,
        signal_type: str,
        summary: str,
        source_url: Optional[str] = None,
        signal_id: Optional[str] = None,
        detected_at: Optional[str] = None,
    ) -> CompetitorSignal:
        competitor_name = _require_text("competitor", competitor)
        _validate_enum("signal_type", signal_type, SIGNAL_TYPES)
        summary_text = _require_text("summary", summary)
        signal_id = signal_id or str(uuid.uuid4())
        detected_at = detected_at or _utc_now()
        url = source_url.strip() if source_url and source_url.strip() else None

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO competitor_signals (
                    signal_id, competitor, signal_type, summary, source_url, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    competitor_name,
                    signal_type,
                    summary_text,
                    url,
                    detected_at,
                ),
            )
            self._conn.commit()

        return CompetitorSignal(
            signal_id=signal_id,
            competitor=competitor_name,
            signal_type=signal_type,
            summary=summary_text,
            source_url=url,
            detected_at=detected_at,
        )

    def get_signal(self, signal_id: str) -> Optional[CompetitorSignal]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM competitor_signals WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        return _row_to_signal(row) if row else None

    def list_signals(
        self,
        *,
        competitor: Optional[str] = None,
        signal_type: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> list[CompetitorSignal]:
        """Signals newest-first. ``since``/``until`` are inclusive ISO timestamps."""
        clauses: list[str] = []
        params: list[object] = []
        if competitor is not None:
            clauses.append("competitor = ?")
            params.append(_require_text("competitor", competitor))
        if signal_type is not None:
            _validate_enum("signal_type", signal_type, SIGNAL_TYPES)
            clauses.append("signal_type = ?")
            params.append(signal_type)
        if since is not None:
            clauses.append("detected_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("detected_at <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM competitor_signals
                {where}
                ORDER BY detected_at DESC, signal_id DESC
                """,
                params,
            ).fetchall()
        return [_row_to_signal(row) for row in rows]

    def list_signals_grouped_by_competitor(
        self,
        *,
        since: str,
        until: Optional[str] = None,
    ) -> dict[str, list[CompetitorSignal]]:
        """Past-week helper for the weekly report: signals grouped by competitor."""
        grouped: dict[str, list[CompetitorSignal]] = {}
        for signal in self.list_signals(since=since, until=until):
            grouped.setdefault(signal.competitor, []).append(signal)
        return grouped

    # --- weekly reports ----------------------------------------------------

    def insert_weekly_report(
        self,
        *,
        week_start: str,
        content_markdown: str,
        report_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> WeeklyReport:
        week = _require_week_start(week_start)
        markdown = _require_text("content_markdown", content_markdown)
        report_id = report_id or str(uuid.uuid4())
        created_at = created_at or _utc_now()

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO weekly_reports (
                    report_id, week_start, content_markdown, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (report_id, week, markdown, created_at),
            )
            self._conn.commit()

        return WeeklyReport(
            report_id=report_id,
            week_start=week,
            content_markdown=markdown,
            created_at=created_at,
        )

    def get_weekly_report(self, report_id: str) -> Optional[WeeklyReport]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM weekly_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        return _row_to_report(row) if row else None

    def get_report_for_week(self, week_start: str) -> Optional[WeeklyReport]:
        """Newest report whose ``week_start`` matches ``YYYY-MM-DD``."""
        week = _require_week_start(week_start)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM weekly_reports
                WHERE week_start = ?
                ORDER BY created_at DESC, report_id DESC
                LIMIT 1
                """,
                (week,),
            ).fetchone()
        return _row_to_report(row) if row else None

    def list_weekly_reports(self, *, limit: Optional[int] = None) -> list[WeeklyReport]:
        sql = (
            "SELECT * FROM weekly_reports "
            "ORDER BY week_start DESC, created_at DESC"
        )
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_report(row) for row in rows]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_text(name: str, value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} is empty")
    return text


def _require_week_start(value: str) -> str:
    week = _require_text("week_start", value)
    datetime.strptime(week, "%Y-%m-%d")
    return week


def _validate_enum(name: str, value: str, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"Invalid {name} {value!r}; expected one of {sorted(allowed)}")


def _row_to_snapshot(row: sqlite3.Row) -> CompetitorSnapshot:
    data = dict(row)
    return CompetitorSnapshot(
        competitor=str(data["competitor"]),
        snapshot_type=str(data["snapshot_type"]),
        content_hash=str(data["content_hash"]),
        raw_content=str(data["raw_content"]),
        captured_at=str(data["captured_at"]),
        rowid=int(data["rowid"]) if data.get("rowid") is not None else None,
    )


def _row_to_signal(row: sqlite3.Row) -> CompetitorSignal:
    data = dict(row)
    source_url = data.get("source_url")
    return CompetitorSignal(
        signal_id=str(data["signal_id"]),
        competitor=str(data["competitor"]),
        signal_type=str(data["signal_type"]),
        summary=str(data["summary"]),
        source_url=str(source_url) if source_url else None,
        detected_at=str(data["detected_at"]),
    )


def _row_to_report(row: sqlite3.Row) -> WeeklyReport:
    data = dict(row)
    return WeeklyReport(
        report_id=str(data["report_id"]),
        week_start=str(data["week_start"]),
        content_markdown=str(data["content_markdown"]),
        created_at=str(data["created_at"]),
    )
