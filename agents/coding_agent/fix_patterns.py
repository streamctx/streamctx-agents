"""Fix-pattern memory for recurring bug signatures."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from agents.coding_agent.models import FixPatternMatch, RootCauseType

DEFAULT_AGENT_DB = Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx")) / "coding_agent.db"

# Pattern must have more successes than rejections and at least one success.
MIN_PATTERN_SUCCESS_COUNT = 1


class FixPatternStore:
    """SQLite-backed store for prior fix templates keyed by bug signature."""

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
                CREATE TABLE IF NOT EXISTS fix_patterns (
                    signature_hash TEXT PRIMARY KEY,
                    root_cause_type TEXT,
                    fix_diff_template TEXT,
                    success_count INTEGER DEFAULT 0,
                    reject_count INTEGER DEFAULT 0
                );
                """
            )
            self._conn.commit()

    @staticmethod
    def compute_signature(
        error_type: str,
        root_cause: RootCauseType,
        relevant_file: str,
    ) -> str:
        payload = f"{error_type}|{root_cause}|{relevant_file}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def find_match(self, signature_hash: str) -> Optional[FixPatternMatch]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT signature_hash, root_cause_type, fix_diff_template,
                       success_count, reject_count
                FROM fix_patterns
                WHERE signature_hash = ?
                """,
                (signature_hash,),
            ).fetchone()

        if row is None:
            return None

        match = FixPatternMatch(
            signature_hash=str(row["signature_hash"]),
            root_cause_type=str(row["root_cause_type"]),
            fix_diff_template=str(row["fix_diff_template"]),
            success_count=int(row["success_count"] or 0),
            reject_count=int(row["reject_count"] or 0),
        )
        if not self._has_good_success_history(match):
            return None
        return match

    def upsert_pattern(
        self,
        signature_hash: str,
        root_cause_type: str,
        fix_diff_template: str,
        *,
        success_count: int = 0,
        reject_count: int = 0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO fix_patterns (
                    signature_hash, root_cause_type, fix_diff_template,
                    success_count, reject_count
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(signature_hash) DO UPDATE SET
                    root_cause_type = excluded.root_cause_type,
                    fix_diff_template = excluded.fix_diff_template,
                    success_count = excluded.success_count,
                    reject_count = excluded.reject_count
                """,
                (
                    signature_hash,
                    root_cause_type,
                    fix_diff_template,
                    success_count,
                    reject_count,
                ),
            )
            self._conn.commit()

    @staticmethod
    def _has_good_success_history(match: FixPatternMatch) -> bool:
        return (
            match.success_count >= MIN_PATTERN_SUCCESS_COUNT
            and match.success_count > match.reject_count
        )
