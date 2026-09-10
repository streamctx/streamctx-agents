"""Dual-backend DB access: SQLite locally, Postgres when DATABASE_URL is set.

Each former ``~/.streamctx/*.db`` file maps to a Postgres schema so the six
incompatible ``pending_approval`` tables can keep their original names.

When ``DATABASE_URL`` is unset, behaviour matches the historical SQLite files
(used by the existing pytest suite with ``tmp_path`` DBs).
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlparse, urlunparse

# Logical schema / former SQLite filename stem → Postgres schema name.
SCHEMA_BY_STEM: dict[str, str] = {
    "coding_agent": "coding",
    "marketing_agent": "marketing",
    "competitor_agent": "competitor",
    "research_agent": "research",
    "leads": "leads",
    "support_tickets": "support_tickets",
    "compliance_findings": "compliance_findings",
    "content_pipeline": "content_pipeline",
    "dashboard_state": "dashboard",
}

STEM_BY_SCHEMA: dict[str, str] = {v: k for k, v in SCHEMA_BY_STEM.items()}

_PG_LOCK = threading.Lock()
_PG_POOL: dict[str, Any] = {}


def database_url() -> str:
    return (os.environ.get("DATABASE_URL") or "").strip()


def uses_postgres() -> bool:
    return bool(database_url())


def schema_for_path(db_path: Optional[Path | str]) -> str:
    """Map a legacy SQLite path to a Postgres schema name."""
    if db_path is None:
        raise ValueError("db_path is required to resolve schema")
    stem = Path(db_path).stem
    if stem in SCHEMA_BY_STEM:
        return SCHEMA_BY_STEM[stem]
    # Allow callers that already pass the schema name as the "path".
    if stem in STEM_BY_SCHEMA:
        return stem
    raise ValueError(f"unknown agent DB path for schema mapping: {db_path!r}")


def default_sqlite_path(schema: str) -> Path:
    stem = STEM_BY_SCHEMA.get(schema, schema)
    home = Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    return home / f"{stem}.db"


def connect(
    *,
    schema: str,
    db_path: Optional[Path | str] = None,
    check_same_thread: bool = False,
    timeout: float = 30.0,
) -> "DbConnection":
    """Open a connection scoped to ``schema`` (Postgres) or ``db_path`` (SQLite)."""
    if uses_postgres():
        return PostgresConnection(database_url(), schema)
    path = Path(db_path) if db_path is not None else default_sqlite_path(schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteConnection(
        path, check_same_thread=check_same_thread, timeout=timeout
    )


def upsert_sql(
    table: str,
    columns: Sequence[str],
    *,
    conflict: str,
    postgres: bool,
) -> str:
    """INSERT OR REPLACE (SQLite) / ON CONFLICT DO UPDATE (Postgres)."""
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    if not postgres:
        return f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})"
    assignments = ", ".join(
        f"{col}=EXCLUDED.{col}" for col in columns if col != conflict
    )
    if not assignments:
        assignments = f"{conflict}=EXCLUDED.{conflict}"
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) DO UPDATE SET {assignments}"
    )


def adapt_sql(sql: str, *, postgres: bool) -> str:
    """Translate a small set of SQLite idioms when talking to Postgres."""
    text = sql
    if not postgres:
        return text
    # competitor_snapshots: SQLite implicit rowid → explicit id BIGSERIAL
    text = re.sub(
        r"CREATE TABLE IF NOT EXISTS competitor_snapshots\s*\(",
        "CREATE TABLE IF NOT EXISTS competitor_snapshots (\n"
        "                    id BIGSERIAL PRIMARY KEY,",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\browid\b", "id", text, flags=re.IGNORECASE)
    # Placeholder style: sqlite3 uses '?'; psycopg2 uses '%s'.
    text = _qmark_to_percent(text)
    text = re.sub(
        r"INSERT\s+OR\s+REPLACE\s+INTO",
        "INSERT INTO",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("AUTOINCREMENT", "")
    # SQLite boolean defaults often use 0/1.
    text = re.sub(
        r"BOOLEAN DEFAULT 0",
        "BOOLEAN DEFAULT FALSE",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"BOOLEAN DEFAULT 1",
        "BOOLEAN DEFAULT TRUE",
        text,
        flags=re.IGNORECASE,
    )
    return text


def adapt_insert_returning_id(sql: str, *, postgres: bool) -> str:
    if not postgres:
        return sql
    if re.match(r"INSERT\s+INTO\s+competitor_snapshots\b", sql.strip(), re.I):
        if "RETURNING" not in sql.upper():
            return sql.rstrip().rstrip(";") + " RETURNING id"
    return sql


_STRING_RE = re.compile(
    r"('([^']|'')*')|(\"([^\"]|\"\")*\")|(--[^\n]*)|(/\*.*?\*/)",
    re.DOTALL,
)


def _qmark_to_percent(sql: str) -> str:
    """Replace ``?`` placeholders outside string literals with ``%s``."""
    out: list[str] = []
    last = 0
    for match in _STRING_RE.finditer(sql):
        out.append(sql[last : match.start()].replace("?", "%s"))
        out.append(match.group(0))
        last = match.end()
    out.append(sql[last:].replace("?", "%s"))
    return "".join(out)


class DbRow(dict):
    """dict that also supports sqlite3.Row-style index / name access."""

    def __getitem__(self, key: Any) -> Any:  # type: ignore[override]
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

    def keys(self):  # type: ignore[override]
        return super().keys()


class DbConnection:
    """Minimal connection surface used by the agent stores."""

    backend: str
    schema: str
    db_path: Optional[Path]
    row_factory: Any = None

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        raise NotImplementedError

    def executescript(self, script: str) -> None:
        raise NotImplementedError

    def commit(self) -> None:
        raise NotImplementedError

    def rollback(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def cursor(self) -> Any:
        raise NotImplementedError

    @property
    def total_changes(self) -> int:
        return 0


class SqliteConnection(DbConnection):
    backend = "sqlite"

    def __init__(
        self,
        path: Path,
        *,
        check_same_thread: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.schema = schema_for_path(path) if path.stem in SCHEMA_BY_STEM else path.stem
        self.db_path = path
        self._conn = sqlite3.connect(
            str(path), check_same_thread=check_same_thread, timeout=timeout
        )
        self._conn.row_factory = sqlite3.Row

    @property
    def row_factory(self) -> Any:
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._conn.row_factory = value

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        return self._conn.execute(sql, params or ())

    def executescript(self, script: str) -> None:
        self._conn.executescript(script)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def cursor(self) -> Any:
        return self._conn.cursor()

    @property
    def total_changes(self) -> int:
        return int(self._conn.total_changes)


class _PgCursor:
    def __init__(self, cursor: Any, lastrowid: Optional[int] = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = cursor.rowcount

    def fetchone(self) -> Any:
        row = self._cursor.fetchone()
        return _pg_row(row, self._cursor) if row is not None else None

    def fetchall(self) -> list[Any]:
        rows = self._cursor.fetchall()
        return [_pg_row(row, self._cursor) for row in rows]

    def close(self) -> None:
        self._cursor.close()


def _pg_row(row: Any, cursor: Any) -> DbRow:
    if row is None:
        return DbRow()
    if isinstance(row, dict):
        return DbRow(row)
    cols = [d[0] for d in (cursor.description or [])]
    return DbRow(zip(cols, row))


class PostgresConnection(DbConnection):
    backend = "postgres"

    def __init__(self, url: str, schema: str) -> None:
        import psycopg2
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

        self.schema = schema
        self.db_path = None
        self._changes = 0
        dsn = _normalize_database_url(url)
        with _PG_LOCK:
            # Ensure schema exists once per process (autocommit).
            key = f"schema:{schema}"
            if key not in _PG_POOL:
                admin = psycopg2.connect(dsn)
                admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                try:
                    with admin.cursor() as cur:
                        cur.execute(
                            f'CREATE SCHEMA IF NOT EXISTS "{schema}"'
                        )
                finally:
                    admin.close()
                _PG_POOL[key] = True
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = False
        with self._conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}", public')
        self._conn.commit()

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> _PgCursor:
        text = sql.strip()
        if text.upper().startswith("PRAGMA TABLE_INFO"):
            return self._pragma_table_info(text)
        adapted = adapt_sql(text, postgres=True)
        adapted = adapt_insert_returning_id(adapted, postgres=True)
        # INSERT OR REPLACE → upsert. Match on the *original* SQL: after
        # adapt_sql strips OR REPLACE, plain INSERT would violate PK.
        if re.search(r"INSERT\s+OR\s+REPLACE", text, re.I):
            adapted = _rewrite_insert_or_replace(adapted)
        cur = self._conn.cursor()
        cur.execute(adapted, tuple(params or ()))
        self._changes += max(cur.rowcount or 0, 0)
        lastrowid = None
        if "RETURNING" in adapted.upper():
            row = cur.fetchone()
            if row is not None:
                lastrowid = int(row[0])
        return _PgCursor(cur, lastrowid=lastrowid)

    def _pragma_table_info(self, sql: str) -> _PgCursor:
        match = re.search(r"PRAGMA\s+table_info\((\w+)\)", sql, re.I)
        if not match:
            cur = self._conn.cursor()
            cur.execute("SELECT NULL WHERE FALSE")
            return _PgCursor(cur)
        table = match.group(1)
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT
                ordinal_position - 1 AS cid,
                column_name AS name,
                data_type AS type,
                CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull,
                column_default AS dflt_value,
                CASE WHEN EXISTS (
                    SELECT 1 FROM information_schema.key_column_usage k
                    WHERE k.table_schema = c.table_schema
                      AND k.table_name = c.table_name
                      AND k.column_name = c.column_name
                ) THEN 1 ELSE 0 END AS pk
            FROM information_schema.columns c
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (self.schema, table),
        )
        return _PgCursor(cur)

    def executescript(self, script: str) -> None:
        # Strip SQLite-only bits; run statements one at a time.
        cleaned = script
        for stmt in _split_sql_statements(cleaned):
            stmt = stmt.strip()
            if not stmt:
                continue
            self.execute(stmt)
        self.commit()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def cursor(self) -> Any:
        return self._conn.cursor()

    @property
    def total_changes(self) -> int:
        return self._changes


def _normalize_database_url(url: str) -> str:
    """Supabase / Heroku often provide postgres://; psycopg2 wants postgresql://."""
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def _rewrite_insert_or_replace(sql: str) -> str:
    """Best-effort: INSERT INTO t (a,b) VALUES (%s,%s) → ON CONFLICT (a) DO UPDATE."""
    match = re.match(
        r"INSERT\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)\s*;?\s*$",
        sql.strip(),
        re.I | re.DOTALL,
    )
    if not match:
        return sql
    table, cols_raw, vals = match.group(1), match.group(2), match.group(3)
    cols = [c.strip() for c in cols_raw.split(",") if c.strip()]
    if not cols:
        return sql
    pk = cols[0]
    assignments = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols[1:]) or f"{pk}=EXCLUDED.{pk}"
    return (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({vals.strip()}) "
        f"ON CONFLICT ({pk}) DO UPDATE SET {assignments}"
    )


def _split_sql_statements(script: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    in_single = False
    i = 0
    while i < len(script):
        ch = script[i]
        if ch == "'" and not in_single:
            in_single = True
            buf.append(ch)
        elif ch == "'" and in_single:
            if i + 1 < len(script) and script[i + 1] == "'":
                buf.append("''")
                i += 1
            else:
                in_single = False
                buf.append(ch)
        elif ch == ";" and not in_single:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))
    return parts


def list_agent_sqlite_files(home: Optional[Path] = None) -> dict[str, Path]:
    """Return schema → existing SQLite path under STREAMCTX_HOME."""
    base = home or Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    found: dict[str, Path] = {}
    for stem, schema in SCHEMA_BY_STEM.items():
        path = base / f"{stem}.db"
        if path.exists():
            found[schema] = path
    return found
