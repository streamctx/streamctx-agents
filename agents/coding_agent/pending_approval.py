"""SQLite store for pending fix approvals awaiting human review."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agents.coding_agent.models import DiagnosisResult, PendingApprovalEntry

DEFAULT_AGENT_DB = Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx")) / "coding_agent.db"

STATUS_NEEDS_HUMAN_REVIEW = "needs_human_review"
STATUS_READY_FOR_APPROVAL = "ready_for_approval"
STATUS_AUTO_FIX_FAILED = "auto_fix_failed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


class PendingApprovalStore:
    """Persists ``pending_approval`` rows in the coding-agent SQLite database."""

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
                CREATE TABLE IF NOT EXISTS pending_approval (
                    entry_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    root_cause TEXT,
                    confidence REAL,
                    matched_pattern_id TEXT,
                    diff TEXT,
                    regression_test TEXT,
                    test_results TEXT,
                    retries_used INTEGER,
                    status TEXT,
                    created_at TIMESTAMP
                );
                """
            )
            self._conn.commit()

    def create_needs_human_review(self, diagnosis: DiagnosisResult) -> PendingApprovalEntry:
        """Record a low-confidence or unclear diagnosis for manual review."""
        return self.create_entry(
            session_id=str(diagnosis.session_id),
            root_cause=diagnosis.root_cause,
            confidence=diagnosis.confidence,
            matched_pattern_id=(
                diagnosis.matched_pattern.signature_hash
                if diagnosis.matched_pattern
                else None
            ),
            diff=None,
            regression_test=None,
            test_results=_diagnosis_context_json(diagnosis),
            retries_used=0,
            status=STATUS_NEEDS_HUMAN_REVIEW,
        )

    def create_entry(
        self,
        *,
        session_id: str,
        root_cause: str,
        confidence: float,
        matched_pattern_id: Optional[str],
        diff: Optional[str],
        regression_test: Optional[str],
        test_results: Optional[str | dict[str, Any]],
        retries_used: int,
        status: str,
        entry_id: Optional[str] = None,
    ) -> PendingApprovalEntry:
        entry_id = entry_id or str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        test_results_json = _serialize_test_results(test_results)

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pending_approval (
                    entry_id, session_id, root_cause, confidence,
                    matched_pattern_id, diff, regression_test, test_results,
                    retries_used, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    session_id,
                    root_cause,
                    confidence,
                    matched_pattern_id,
                    diff,
                    regression_test,
                    test_results_json,
                    retries_used,
                    status,
                    created_at,
                ),
            )
            self._conn.commit()

        return PendingApprovalEntry(
            entry_id=entry_id,
            session_id=session_id,
            root_cause=root_cause,
            confidence=confidence,
            matched_pattern_id=matched_pattern_id,
            diff=diff,
            regression_test=regression_test,
            test_results=test_results_json,
            retries_used=retries_used,
            status=status,
            created_at=created_at,
        )

    def get_entry(self, entry_id: str) -> Optional[PendingApprovalEntry]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_approval WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
        return _row_to_entry(dict(row)) if row else None

    def list_by_status(self, status: str) -> list[PendingApprovalEntry]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM pending_approval
                WHERE status = ?
                ORDER BY created_at DESC
                """,
                (status,),
            ).fetchall()
        return [_row_to_entry(dict(row)) for row in rows]

    def update_status(self, entry_id: str, status: str) -> Optional[PendingApprovalEntry]:
        with self._lock:
            self._conn.execute(
                "UPDATE pending_approval SET status = ? WHERE entry_id = ?",
                (status, entry_id),
            )
            self._conn.commit()
        return self.get_entry(entry_id)


def _serialize_test_results(value: Optional[str | dict[str, Any]]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _diagnosis_context_json(diagnosis: DiagnosisResult) -> str:
    return json.dumps(
        {
            "failed_call_id": diagnosis.failed_call_id,
            "reason": diagnosis.reason,
            "error_type": diagnosis.error_type,
            "relevant_file": diagnosis.relevant_file,
            "signature_hash": diagnosis.signature_hash,
            "replay_verified": diagnosis.replay_verified,
            "signal_breakdown": diagnosis.signal_breakdown,
        },
        ensure_ascii=False,
    )


def _row_to_entry(row: dict[str, Any]) -> PendingApprovalEntry:
    return PendingApprovalEntry(
        entry_id=str(row["entry_id"]),
        session_id=str(row["session_id"]),
        root_cause=str(row["root_cause"]),
        confidence=float(row["confidence"]),
        matched_pattern_id=row.get("matched_pattern_id"),
        diff=row.get("diff"),
        regression_test=row.get("regression_test"),
        test_results=row.get("test_results"),
        retries_used=int(row.get("retries_used") or 0),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
    )
