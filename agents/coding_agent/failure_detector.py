"""Poll StreamCtx sessions.db for failed LLM calls."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

from streamctx.storage import SessionStorage

from agents.coding_agent.models import FailedCallRecord


def _default_db_path() -> Path:
    base = Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    return base / "sessions.db"


def poll_failed_calls(
    storage: Optional[SessionStorage] = None,
    *,
    db_path: Optional[Path | str] = None,
    limit: Optional[int] = None,
) -> list[FailedCallRecord]:
    """
    Return every call row where ``failed = 1``, newest first.

    Historical scan is intentional: the coding agent diagnoses failed LLM
    calls across StreamCtx sessions, not only the agent's own current run.
    Callers must skip ``failed_call_id`` values that already have a
    ``pending_approval`` row so repeated ``run()`` calls stay idempotent.

    Uses the shared StreamCtx SQLite file at ``~/.streamctx/sessions.db``
    unless ``storage`` or ``db_path`` is provided.
    """
    path = _resolve_db_path(storage, db_path)
    query = """
        SELECT id, session_id, error_message, timestamp, messages_json
        FROM calls
        WHERE failed = 1
        ORDER BY timestamp DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()

    return [_row_to_failed_call(dict(row)) for row in rows]


def poll_failed_sessions(
    storage: Optional[SessionStorage] = None,
    *,
    db_path: Optional[Path | str] = None,
) -> list[int]:
    """Distinct session IDs that contain at least one failed call."""
    failures = poll_failed_calls(storage=storage, db_path=db_path)
    seen: set[int] = set()
    ordered: list[int] = []
    for record in failures:
        if record.session_id not in seen:
            seen.add(record.session_id)
            ordered.append(record.session_id)
    return ordered


def _resolve_db_path(
    storage: Optional[SessionStorage],
    db_path: Optional[Path | str],
) -> Path:
    if db_path is not None:
        return Path(db_path)
    if storage is not None:
        return Path(storage.db_path)
    return _default_db_path()


def _row_to_failed_call(row: dict[str, Any]) -> FailedCallRecord:
    raw_messages = row.get("messages_json")
    try:
        messages = json.loads(raw_messages) if raw_messages else []
    except (TypeError, ValueError):
        messages = []

    return FailedCallRecord(
        call_id=int(row["id"]),
        session_id=int(row["session_id"]),
        error_message=row.get("error_message"),
        timestamp=str(row["timestamp"]),
        messages=messages,
    )
