"""Internal StreamCtx health for the weekly competitor report.

Poison Detector scans session checkpoints from ``sessions.db``.
Attribution Engine confidence comes from coding-agent diagnoses in
``coding_agent.db`` (the same pipeline that records AttributionEngine
output). Both windows are injectable so tests never need a live SDK.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agents.coding_agent.confidence_gate import MIN_CONFIDENCE_FOR_AUTO_FIX
from agents.coding_agent.pending_approval import DEFAULT_AGENT_DB as DEFAULT_CODING_DB


@dataclass(frozen=True)
class HealthSnapshot:
    poison_summary: str
    attribution_summary: str


def collect_health(
    *,
    since: str,
    until: Optional[str] = None,
    session_storage: Optional[Any] = None,
    coding_db_path: Optional[Path | str] = None,
) -> HealthSnapshot:
    """Summarize Poison Detector + Attribution Engine results in ``[since, until]``."""
    return HealthSnapshot(
        poison_summary=collect_poison_summary(
            since=since,
            until=until,
            session_storage=session_storage,
        ),
        attribution_summary=collect_attribution_summary(
            since=since,
            until=until,
            coding_db_path=coding_db_path,
        ),
    )


def collect_poison_summary(
    *,
    since: str,
    until: Optional[str] = None,
    session_storage: Optional[Any] = None,
) -> str:
    storage = session_storage
    if storage is None:
        storage = _default_session_storage()
    if storage is None:
        return "No runs this week"

    session_ids = _session_ids_in_window(storage, since=since, until=until)
    if not session_ids:
        return "No runs this week"

    try:
        from streamctx.poison_detector import PoisonDetector
    except ImportError:
        return "No runs this week"

    detector = PoisonDetector()
    scanned = 0
    poisoned = 0
    scores: list[int] = []
    for session_id in session_ids:
        messages = _session_messages(storage, session_id)
        if not messages:
            continue
        result = detector.scan(messages)
        scanned += 1
        scores.append(int(result.get("health_score") or 0))
        if result.get("is_poisoned"):
            poisoned += 1

    if scanned == 0:
        return "No runs this week"

    avg = round(sum(scores) / scanned)
    status = "fail" if poisoned else "pass"
    return (
        f"{status} — {scanned} session{'s' if scanned != 1 else ''} scanned, "
        f"{poisoned} poisoned (avg health {avg})"
    )


def collect_attribution_summary(
    *,
    since: str,
    until: Optional[str] = None,
    coding_db_path: Optional[Path | str] = None,
) -> str:
    path = Path(coding_db_path) if coding_db_path is not None else DEFAULT_CODING_DB
    if not path.exists():
        return "No diagnoses this week"

    clauses = ["created_at >= ?"]
    params: list[object] = [since]
    if until is not None:
        clauses.append("created_at <= ?")
        params.append(until)
    where = " AND ".join(clauses)

    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"""
                SELECT confidence, root_cause FROM pending_approval
                WHERE {where}
                """,
                params,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return "No diagnoses this week"

    if not rows:
        return "No diagnoses this week"

    confidences = [float(row["confidence"]) for row in rows]
    mean = sum(confidences) / len(confidences)
    gated = sum(1 for value in confidences if value >= MIN_CONFIDENCE_FOR_AUTO_FIX)
    label = "diagnosis" if len(confidences) == 1 else "diagnoses"
    return (
        f"{len(confidences)} {label}, "
        f"mean confidence {mean:.2f} "
        f"({gated}/{len(confidences)} ≥ {MIN_CONFIDENCE_FOR_AUTO_FIX:.2f})"
    )


def _default_session_storage() -> Optional[Any]:
    try:
        from streamctx.storage import get_storage

        return get_storage()
    except Exception:
        return None


def _session_ids_in_window(
    storage: Any,
    *,
    since: str,
    until: Optional[str],
) -> list[int]:
    db_path = getattr(storage, "db_path", None)
    if db_path is None or not Path(db_path).exists():
        return []
    clauses = ["started_at >= ?"]
    params: list[object] = [since]
    if until is not None:
        clauses.append("started_at <= ?")
        params.append(until)
    where = " AND ".join(clauses)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                f"SELECT id FROM sessions WHERE {where} ORDER BY id ASC",
                params,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [int(row[0]) for row in rows]


def _session_messages(storage: Any, session_id: int) -> list[dict[str, Any]]:
    checkpoint = None
    getter = getattr(storage, "get_latest_checkpoint", None)
    if callable(getter):
        try:
            checkpoint = getter(session_id)
        except Exception:
            checkpoint = None
    if checkpoint and checkpoint.get("messages"):
        messages = checkpoint["messages"]
        if isinstance(messages, list):
            return messages

    calls_fn = getattr(storage, "get_calls_for_session", None)
    if not callable(calls_fn):
        return []
    try:
        rows = calls_fn(session_id) or []
    except Exception:
        return []
    if not rows:
        return []
    raw = rows[-1].get("messages_json") if isinstance(rows[-1], dict) else None
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []
