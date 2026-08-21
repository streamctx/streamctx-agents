"""One-time cleanup of duplicate pending_approval rows.

``pending_approval`` is stored in the per-agent SQLite files under
``~/.streamctx`` (``coding_agent.db``, ``marketing_agent.db``). StreamCtx's
``sessions.db`` has sessions/calls/checkpoints only — this script still
checks it and reports if a ``pending_approval`` table is ever present.

Default is dry-run. Pass ``--execute`` to delete extras after the summary.

Usage::

    python cleanup_duplicate_approvals.py
    python cleanup_duplicate_approvals.py --execute
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional


def _home() -> Path:
    return Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _failed_call_id(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("failed_call_id") is None:
        return None
    try:
        return int(payload["failed_call_id"])
    except (TypeError, ValueError):
        return None


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _group_keep_earliest(
    rows: list[sqlite3.Row],
    key_fn,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    grouped: dict[Any, list[sqlite3.Row]] = defaultdict(list)
    unique: list[sqlite3.Row] = []
    for row in rows:
        key = key_fn(row)
        if key is None:
            unique.append(row)
            continue
        grouped[key].append(row)
    keep: list[sqlite3.Row] = list(unique)
    delete: list[sqlite3.Row] = []
    for _key, items in grouped.items():
        items.sort(key=lambda r: (str(r["created_at"]), str(r["entry_id"])))
        keep.append(items[0])
        delete.extend(items[1:])
    return keep, delete


def inspect_sessions_db(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "pending_approval": False, "rows": 0}
    conn = _connect(path)
    try:
        tables = _tables(conn)
        has_pending = "pending_approval" in tables
        rows = 0
        if has_pending:
            rows = int(conn.execute("SELECT COUNT(*) FROM pending_approval").fetchone()[0])
        return {
            "path": str(path),
            "exists": True,
            "tables": sorted(tables),
            "pending_approval": has_pending,
            "rows": rows,
        }
    finally:
        conn.close()


def coding_duplicates(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "total": 0, "delete": []}
    conn = _connect(path)
    try:
        if "pending_approval" not in _tables(conn):
            return {"path": str(path), "exists": True, "total": 0, "delete": []}
        rows = conn.execute(
            "SELECT * FROM pending_approval ORDER BY created_at ASC"
        ).fetchall()
        _keep, delete = _group_keep_earliest(
            list(rows),
            lambda row: _failed_call_id(row["test_results"]),
        )
        return {
            "path": str(path),
            "exists": True,
            "total": len(rows),
            "delete": delete,
        }
    finally:
        conn.close()


def marketing_duplicates(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "total": 0, "delete": []}
    conn = _connect(path)
    try:
        if "pending_approval" not in _tables(conn):
            return {"path": str(path), "exists": True, "total": 0, "delete": []}
        rows = conn.execute(
            "SELECT * FROM pending_approval ORDER BY created_at ASC"
        ).fetchall()

        def key(row: sqlite3.Row) -> Optional[tuple]:
            fp = (row["source_fingerprint"] or "").strip()
            if fp:
                return ("fp", fp)
            target = (row["target"] or "").strip()
            if target:
                return ("target", row["platform"], row["content_type"], target)
            return None

        _keep, delete = _group_keep_earliest(list(rows), key)
        return {
            "path": str(path),
            "exists": True,
            "total": len(rows),
            "delete": delete,
        }
    finally:
        conn.close()


def _delete_ids(path: Path, entry_ids: list[str]) -> int:
    if not entry_ids:
        return 0
    conn = _connect(path)
    try:
        conn.executemany(
            "DELETE FROM pending_approval WHERE entry_id = ?",
            [(entry_id,) for entry_id in entry_ids],
        )
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


def print_report(
    *,
    sessions: dict[str, Any],
    coding: dict[str, Any],
    marketing: dict[str, Any],
    execute: bool,
) -> None:
    print("=== sessions.db ===")
    print(f"  path={sessions.get('path')}")
    print(f"  exists={sessions.get('exists')} pending_approval={sessions.get('pending_approval')}")
    if sessions.get("tables"):
        print(f"  tables={sessions['tables']}")
    if sessions.get("pending_approval"):
        print(f"  pending_approval rows={sessions.get('rows')}")
    else:
        print("  no pending_approval table (expected)")

    print("=== coding_agent.db ===")
    print(f"  path={coding.get('path')} exists={coding.get('exists')}")
    print(f"  total rows={coding.get('total', 0)}")
    coding_delete = coding.get("delete") or []
    print(f"  duplicate extras={len(coding_delete)}")
    for row in coding_delete[:12]:
        call_id = _failed_call_id(row["test_results"])
        print(
            f"    would drop {row['entry_id']} failed_call_id={call_id} "
            f"status={row['status']} created_at={row['created_at']}"
        )
    if len(coding_delete) > 12:
        print(f"    … {len(coding_delete) - 12} more")

    print("=== marketing_agent.db ===")
    print(f"  path={marketing.get('path')} exists={marketing.get('exists')}")
    print(f"  total rows={marketing.get('total', 0)}")
    marketing_delete = marketing.get("delete") or []
    print(f"  duplicate extras={len(marketing_delete)}")
    for row in marketing_delete[:12]:
        print(
            f"    would drop {row['entry_id']} fp={row['source_fingerprint']!r} "
            f"status={row['status']} created_at={row['created_at']}"
        )
    if len(marketing_delete) > 12:
        print(f"    … {len(marketing_delete) - 12} more")

    total_delete = len(coding_delete) + len(marketing_delete)
    print("=== summary ===")
    print(f"  coding extras to delete: {len(coding_delete)}")
    print(f"  marketing extras to delete: {len(marketing_delete)}")
    print(f"  total extras to delete: {total_delete}")
    print(f"  mode: {'EXECUTE' if execute else 'DRY-RUN (no deletes)'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove duplicate pending_approval rows (keep earliest per record)."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete extras. Default is dry-run.",
    )
    args = parser.parse_args(argv)
    home = _home()
    sessions = inspect_sessions_db(home / "sessions.db")
    coding = coding_duplicates(home / "coding_agent.db")
    marketing = marketing_duplicates(home / "marketing_agent.db")
    print_report(
        sessions=sessions,
        coding=coding,
        marketing=marketing,
        execute=args.execute,
    )
    if not args.execute:
        print("Re-run with --execute to delete the extras listed above.")
        return 0
    coding_n = _delete_ids(
        Path(coding["path"]),
        [row["entry_id"] for row in (coding.get("delete") or [])],
    )
    marketing_n = _delete_ids(
        Path(marketing["path"]),
        [row["entry_id"] for row in (marketing.get("delete") or [])],
    )
    print(f"deleted coding={coding_n} marketing={marketing_n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
