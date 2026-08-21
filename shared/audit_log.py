"""
StreamCtx Agents — Shared Audit Log
Every agent action (proposed or executed) gets one JSON-line entry here.
This is the single source of truth for what agents suggested and what
Sneh approved/rejected.
"""

import json
import os
import uuid
from datetime import datetime, timezone

from shared.config import AUDIT_LOG_PATH, LOGS_DIR, STATUS_PENDING


def _ensure_logs_dir():
    os.makedirs(LOGS_DIR, exist_ok=True)


def log_action(agent_name, session_id, action_type, payload, status=STATUS_PENDING):
    """
    Append one audit entry. Returns the entry dict (including its id)
    so the caller can reference/update it later.
    """
    _ensure_logs_dir()
    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent_name,
        "session_id": session_id,
        "action_type": action_type,   # e.g. "code_patch", "community_post", "competitor_summary"
        "payload": payload,           # dict: patch/diff, draft text, summary, etc.
        "status": status,             # pending_approval / approved / rejected / edited_then_approved
        "approved_by": None,
        "approved_at": None,
    }
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def update_status(entry_id, new_status, approved_by=None):
    """
    Rewrites the audit log with one entry's status updated.
    (Simple approach for now — fine at this volume; can move to SQLite later.)
    """
    _ensure_logs_dir()
    if not os.path.exists(AUDIT_LOG_PATH):
        raise FileNotFoundError("No audit log found yet.")

    lines = []
    with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            if entry["id"] == entry_id:
                entry["status"] = new_status
                entry["approved_by"] = approved_by
                entry["approved_at"] = datetime.now(timezone.utc).isoformat()
            lines.append(entry)

    with open(AUDIT_LOG_PATH, "w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_pending_actions():
    """Returns all entries still awaiting human approval."""
    if not os.path.exists(AUDIT_LOG_PATH):
        return []
    pending = []
    with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            if entry["status"] == "pending_approval":
                pending.append(entry)
    return pending

