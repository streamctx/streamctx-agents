"""SQLite store for pending fix approvals awaiting human review."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agents.coding_agent.models import DiagnosisResult, PendingApprovalEntry
from shared.db import connect

DEFAULT_AGENT_DB = Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx")) / "coding_agent.db"

STATUS_NEEDS_HUMAN_REVIEW = "needs_human_review"
STATUS_READY_FOR_APPROVAL = "ready_for_approval"
STATUS_AUTO_FIX_FAILED = "auto_fix_failed"
STATUS_AUTO_APPLIED = "auto_applied"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

VERIFICATION_PENDING = "pending"
VERIFICATION_DRY_RUN_PASSED = "dry_run_passed"
VERIFICATION_DRY_RUN_FAILED = "dry_run_failed"

ROOT_CAUSE_INTAKE = "INTAKE"
INTAKE_KIND = "intake"
INTAKE_FIX_KIND = "intake_fix"
INTAKE_CHILD_SESSION_PREFIX = "intake-fix:"
FIX_STATUS_CANNOT_PARSE = "cannot_parse_tests"
FIX_STATUS_GENERATING = "generating"
FIX_STATUS_READY = "ready_for_approval"
FIX_STATUS_FAILED = "auto_fix_failed"

NotifierFn = Callable[[PendingApprovalEntry], None]


class PendingApprovalStore:
    """Persists ``pending_approval`` rows in the coding-agent SQLite database."""

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
            schema="coding",
            db_path=self.db_path,
            check_same_thread=False,
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
                    session_id TEXT,
                    root_cause TEXT,
                    confidence REAL,
                    matched_pattern_id TEXT,
                    diff TEXT,
                    regression_test TEXT,
                    test_results TEXT,
                    retries_used INTEGER,
                    status TEXT,
                    created_at TIMESTAMP,
                    applied_commit TEXT,
                    generation_mode TEXT DEFAULT 'api',
                    auto_applied BOOLEAN DEFAULT 0,
                    verification_mode TEXT DEFAULT 'pending'
                );
                """
            )
            self._ensure_column("pending_approval", "applied_commit", "TEXT")
            self._ensure_column(
                "pending_approval",
                "generation_mode",
                "TEXT DEFAULT 'api'",
            )
            self._ensure_column(
                "pending_approval",
                "auto_applied",
                "BOOLEAN DEFAULT 0",
            )
            self._ensure_column(
                "pending_approval",
                "verification_mode",
                "TEXT DEFAULT 'pending'",
            )
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, col_type: str) -> None:
        columns = {
            row[1]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
            )

    def create_intake_task(
        self,
        *,
        task_ref: str,
        summary: str,
        request: str,
        payload: Optional[dict[str, Any]] = None,
        confidence: float = 0.0,
        root_cause: str = ROOT_CAUSE_INTAKE,
    ) -> PendingApprovalEntry:
        """Queue a human-gated task. No diff is stored, so nothing auto-commits.

        Reuses an existing row with the same ``task_ref`` (stored as
        ``session_id``) so retries do not duplicate intake.
        """
        ref = _require_text("task_ref", task_ref)
        existing = self.get_latest_by_session_id(ref)
        if existing is not None:
            return existing
        body: dict[str, Any] = {
            "kind": INTAKE_KIND,
            "summary": _require_text("summary", summary),
            "request": _require_text("request", request),
        }
        if payload:
            for key, value in payload.items():
                if key not in body:
                    body[key] = value
        return self.create_entry(
            session_id=ref,
            root_cause=root_cause or ROOT_CAUSE_INTAKE,
            confidence=float(confidence),
            matched_pattern_id=None,
            diff=None,
            regression_test=None,
            test_results=body,
            retries_used=0,
            status=STATUS_NEEDS_HUMAN_REVIEW,
        )

    def merge_test_results(
        self,
        entry_id: str,
        patch: dict[str, Any],
    ) -> Optional[PendingApprovalEntry]:
        """Shallow-merge ``patch`` into the JSON ``test_results`` payload."""
        with self._lock:
            row = self._conn.execute(
                "SELECT test_results FROM pending_approval WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            if row is None:
                return None
            payload = _parse_test_results_dict(row["test_results"])
            payload.update(patch)
            self._conn.execute(
                "UPDATE pending_approval SET test_results = ? WHERE entry_id = ?",
                (json.dumps(payload, ensure_ascii=False), entry_id),
            )
            self._conn.commit()
        return self.get_entry(entry_id)

    def get_latest_by_session_id(self, session_id: str) -> Optional[PendingApprovalEntry]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM pending_approval
                WHERE session_id = ?
                ORDER BY created_at DESC, entry_id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return _row_to_entry(dict(row)) if row else None

    def get_by_failed_call_id(self, failed_call_id: int) -> Optional[PendingApprovalEntry]:
        """Return the earliest row for this failed call, any status."""
        target = int(failed_call_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM pending_approval
                ORDER BY created_at ASC, entry_id ASC
                """
            ).fetchall()
        for row in rows:
            if _failed_call_id_from_test_results(row["test_results"]) == target:
                return _row_to_entry(dict(row))
        return None

    def known_failed_call_ids(self) -> set[int]:
        """Every ``failed_call_id`` already recorded, pending or resolved."""
        ids: set[int] = set()
        with self._lock:
            rows = self._conn.execute(
                "SELECT test_results FROM pending_approval"
            ).fetchall()
        for row in rows:
            call_id = _failed_call_id_from_test_results(row["test_results"])
            if call_id is not None:
                ids.add(call_id)
        return ids

    def count_entries(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM pending_approval"
            ).fetchone()
        return int(row[0])

    def create_needs_human_review(self, diagnosis: DiagnosisResult) -> PendingApprovalEntry:
        """Record a low-confidence or unclear diagnosis for manual review.

        Idempotent: a later run for the same ``failed_call_id`` returns the
        existing row (any status) instead of inserting a duplicate.
        """
        existing = self.get_by_failed_call_id(diagnosis.failed_call_id)
        if existing is not None:
            return existing
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
        applied_commit: Optional[str] = None,
        generation_mode: str = "api",
        auto_applied: bool = False,
        verification_mode: str = VERIFICATION_PENDING,
    ) -> PendingApprovalEntry:
        entry_id = entry_id or str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        test_results_json = _serialize_test_results(test_results)
        mode = generation_mode or "api"
        applied = bool(auto_applied)
        verify_mode = verification_mode or VERIFICATION_PENDING

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pending_approval (
                    entry_id, session_id, root_cause, confidence,
                    matched_pattern_id, diff, regression_test, test_results,
                    retries_used, status, created_at, applied_commit,
                    generation_mode, auto_applied, verification_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    applied_commit,
                    mode,
                    int(applied),
                    verify_mode,
                ),
            )
            self._conn.commit()

        entry = PendingApprovalEntry(
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
            applied_commit=applied_commit,
            generation_mode=mode,
            auto_applied=applied,
            verification_mode=verify_mode,
        )
        self._dispatch_notification(entry)
        return entry

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

    def record_applied_commit(
        self,
        entry_id: str,
        commit_sha: str,
    ) -> Optional[PendingApprovalEntry]:
        with self._lock:
            self._conn.execute(
                "UPDATE pending_approval SET applied_commit = ? WHERE entry_id = ?",
                (commit_sha, entry_id),
            )
            self._conn.commit()
        return self.get_entry(entry_id)

    def _dispatch_notification(self, entry: PendingApprovalEntry) -> None:
        if self._notifier is not None:
            self._notifier(entry)
            return
        if not self._enable_default_notifier:
            return
        from agents.coding_agent.notifications import notify_pending_approval

        notify_pending_approval(entry)


def _require_text(name: str, value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} is empty")
    return text


def _serialize_test_results(value: Optional[str | dict[str, Any]]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def intake_child_session_id(parent_entry_id: str) -> str:
    return f"{INTAKE_CHILD_SESSION_PREFIX}{parent_entry_id}"


def parse_test_results(entry: PendingApprovalEntry) -> dict[str, Any]:
    return _parse_test_results_dict(entry.test_results)


def is_intake_task(entry: PendingApprovalEntry) -> bool:
    """True for a human-assigned intake ticket that has no generated diff yet."""
    if str(entry.root_cause) != ROOT_CAUSE_INTAKE:
        return False
    if (entry.diff or "").strip():
        return False
    return parse_test_results(entry).get("kind") == INTAKE_KIND


def _parse_test_results_dict(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _failed_call_id_from_test_results(raw: Optional[str]) -> Optional[int]:
    payload = _parse_test_results_dict(raw)
    if payload.get("failed_call_id") is None:
        return None
    try:
        return int(payload["failed_call_id"])
    except (TypeError, ValueError):
        return None


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
        applied_commit=row.get("applied_commit"),
        generation_mode=str(row.get("generation_mode") or "api"),
        auto_applied=bool(row.get("auto_applied") or False),
        verification_mode=str(row.get("verification_mode") or VERIFICATION_PENDING),
    )
