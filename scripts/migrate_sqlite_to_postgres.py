"""One-time SQLite → Postgres migration for StreamCtx agent stores.

Reads every ``~/.streamctx/*.db`` file (except StreamCtx ``sessions.db``, which
is owned by the ``streamctx`` package) and copies rows into the matching
Postgres schema on ``DATABASE_URL``.

Usage::

    set DATABASE_URL=postgresql://...
    python scripts/migrate_sqlite_to_postgres.py
    python scripts/migrate_sqlite_to_postgres.py --execute

Default is dry-run (counts only).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.db import (  # noqa: E402
    SCHEMA_BY_STEM,
    connect,
    database_url,
    list_agent_sqlite_files,
    uses_postgres,
)

# Ensure destination tables exist by constructing each store once.
STORE_BOOTSTRAP = {
    "coding": (
        ("agents.coding_agent.pending_approval", "PendingApprovalStore"),
        ("agents.coding_agent.fix_patterns", "FixPatternStore"),
    ),
    "marketing": (
        ("agents.marketing_agent.pending_approval", "PendingApprovalStore"),
    ),
    "competitor": (
        ("agents.competitor_agent.storage", "CompetitorStore"),
        ("agents.competitor_agent.pending_approval", "PendingApprovalStore"),
    ),
    "research": (("agents.research_agent.storage", "ResearchStore"),),
    "leads": (
        ("agents.presales_agent.storage", "LeadStore"),
        ("agents.presales_agent.pending_approval", "PendingApprovalStore"),
    ),
    "support_tickets": (
        ("agents.techsupport_agent.storage", "TicketStore"),
        ("agents.techsupport_agent.pending_approval", "PendingApprovalStore"),
    ),
    "compliance_findings": (
        ("agents.legal_compliance_agent.storage", "FindingStore"),
        ("agents.legal_compliance_agent.pending_approval", "PendingApprovalStore"),
    ),
    "content_pipeline": (("content_pipeline", "ContentPipelineStore"),),
    "dashboard": (),
}


def _import_store(module_path: str, class_name: str):
    mod = __import__(module_path, fromlist=[class_name])
    return getattr(mod, class_name)


def bootstrap_schema(schema: str, sqlite_path: Path) -> None:
    if schema == "dashboard":
        conn = connect(schema="dashboard", db_path=sqlite_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()
        return
    for module_path, class_name in STORE_BOOTSTRAP.get(schema, ()):
        cls = _import_store(module_path, class_name)
        # Force Postgres path: stores respect DATABASE_URL.
        store = cls(db_path=sqlite_path)
        store.close()


def _pg_boolean_columns(dest, schema: str, table: str) -> set[str]:
    rows = dest.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = ? AND table_name = ?
          AND data_type = 'boolean'
        """,
        (schema, table),
    ).fetchall()
    return {str(row["column_name"] if hasattr(row, "keys") else row[0]) for row in rows}


def _coerce_row(values: list, cols: list[str], bool_cols: set[str]) -> list:
    out = []
    for col, value in zip(cols, values):
        if col in bool_cols and value is not None:
            if isinstance(value, (int, float)) and value in (0, 1):
                out.append(bool(value))
            elif isinstance(value, bytes):
                out.append(value != b"\x00")
            else:
                out.append(value)
        else:
            out.append(value)
    return out


def copy_table(schema: str, sqlite_path: Path, table: str) -> tuple[int, int]:
    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.Error:
        src.close()
        return 0, 0
    if not rows:
        src.close()
        return 0, 0
    cols = [d[0] for d in src.execute(f"SELECT * FROM {table} LIMIT 0").description]
    src.close()

    # Don't push SQLite's implicit rowid into Postgres unless the table has it.
    if "rowid" in cols and table == "competitor_snapshots":
        # Prefer explicit id on Postgres; map sqlite rowid → id if present as column name
        pass

    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

    dest = connect(schema=schema, db_path=sqlite_path)
    inserted = 0
    try:
        bool_cols = _pg_boolean_columns(dest, schema, table)
        for row in rows:
            values = _coerce_row([row[c] for c in cols], cols, bool_cols)
            try:
                dest.execute(insert_sql, values)
                inserted += 1
            except Exception as exc:
                # Skip duplicates on re-run.
                msg = str(exc).lower()
                if "unique" in msg or "duplicate" in msg:
                    dest.rollback()
                    continue
                raise
        dest.commit()
    finally:
        dest.close()
    return len(rows), inserted


def list_tables(sqlite_path: Path) -> list[str]:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx")),
    )
    args = parser.parse_args(argv)

    if not database_url():
        print("DATABASE_URL is not set. Export your Supabase connection string first.")
        return 2
    if not uses_postgres():
        print("uses_postgres() is False — unexpected.")
        return 2

    files = list_agent_sqlite_files(args.home)
    # Always include dashboard_state if present.
    dash = args.home / "dashboard_state.db"
    if dash.exists():
        files["dashboard"] = dash

    print(f"DATABASE_URL host={database_url().split('@')[-1]}")
    print(f"STREAMCTX_HOME={args.home}")
    print(f"mode={'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(
        "NOTE: sessions.db is owned by streamctx and is NOT migrated. "
        "audit_log.jsonl remains file-based unless you move it separately."
    )

    if not files:
        print("No agent SQLite files found.")
        return 0

    total_src = 0
    total_ins = 0
    for schema, path in sorted(files.items()):
        tables = list_tables(path)
        print(f"\n=== {schema} ← {path.name} tables={tables} ===")
        if not args.execute:
            for table in tables:
                conn = sqlite3.connect(str(path))
                n = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                conn.close()
                print(f"  {table}: {n} rows")
                total_src += n
            continue
        bootstrap_schema(schema, path)
        for table in tables:
            src_n, ins_n = copy_table(schema, path, table)
            print(f"  {table}: source={src_n} inserted={ins_n}")
            total_src += src_n
            total_ins += ins_n

    print(f"\nsummary source_rows={total_src} inserted={total_ins}")
    if not args.execute:
        print("Re-run with --execute to copy into Postgres.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
