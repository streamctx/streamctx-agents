"""Shared pytest fixtures for coding-agent tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from streamctx.storage import SessionStorage


@pytest.fixture
def sessions_db(tmp_path) -> Path:
    db_path = tmp_path / "sessions.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        CREATE TABLE calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0,
            reused_tokens INTEGER DEFAULT 0,
            waste_category TEXT,
            messages_json TEXT,
            failed INTEGER DEFAULT 0,
            healed INTEGER DEFAULT 0,
            error_message TEXT
        );
        CREATE TABLE checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            step_number INTEGER NOT NULL,
            messages_json TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def storage(sessions_db) -> SessionStorage:
    return SessionStorage(db_path=sessions_db)
