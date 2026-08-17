"""Unit tests for Stage 2 detect + diagnose + replay-verify."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agents.coding_agent.diagnose import (
    FailureDiagnostician,
    extract_error_type,
    extract_relevant_file,
)
from agents.coding_agent.failure_detector import poll_failed_calls, poll_failed_sessions
from agents.coding_agent.fix_patterns import FixPatternStore
from agents.coding_agent.models import FailedCallRecord
from streamctx.attribution import AttributionEngine
from streamctx.replay import CounterfactualReplayer
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


def _seed_drift_failure(storage: SessionStorage) -> FailedCallRecord:
    session_id = storage.start_session()
    messages_by_step = [
        [{"role": "user", "content": "step-0"}],
        [{"role": "user", "content": "step-0"}, {"role": "assistant", "content": "reply-1"}],
        [
            {"role": "user", "content": "step-0"},
            {"role": "assistant", "content": "reply-1"},
            {"role": "user", "content": "step-2-failure"},
        ],
    ]

    call_id: int | None = None
    for step, messages in enumerate(messages_by_step):
        storage.record_call(
            session_id=session_id,
            provider="openrouter",
            model="test-model",
            input_tokens=100 + (step * 250),
            output_tokens=25,
            cost=0.0,
            reused_tokens=10,
            waste_category="drift" if step == 2 else None,
            messages=messages,
            failed=step == 2,
            error_message="RuntimeError: simulated context drift failure" if step == 2 else None,
        )
        storage.save_checkpoint(
            session_id=session_id,
            step_number=step,
            messages=messages,
        )

    rows = storage.get_calls_for_session(session_id)
    failed = next(row for row in rows if row["failed"])
    return FailedCallRecord(
        call_id=int(failed["id"]),
        session_id=session_id,
        error_message=str(failed["error_message"]),
        timestamp=str(failed["timestamp"]),
        messages=messages_by_step[-1],
    )


def _seed_compression_failure(storage: SessionStorage) -> FailedCallRecord:
    session_id = storage.start_session()
    messages = [{"role": "user", "content": "compress me"}]

    storage.record_call(
        session_id=session_id,
        provider="openrouter",
        model="test-model",
        input_tokens=500,
        output_tokens=20,
        cost=0.0,
        reused_tokens=450,
        waste_category="repeated system prompt",
        messages=messages,
        failed=False,
    )
    storage.save_checkpoint(
        session_id=session_id,
        step_number=0,
        messages=messages,
    )

    storage.record_call(
        session_id=session_id,
        provider="openrouter",
        model="test-model",
        input_tokens=520,
        output_tokens=0,
        cost=0.0,
        reused_tokens=480,
        waste_category="repeated system prompt",
        messages=messages,
        failed=True,
        error_message="CompressionError: context over-compressed",
    )
    storage.save_checkpoint(
        session_id=session_id,
        step_number=1,
        messages=messages + [{"role": "assistant", "content": "partial"}],
    )

    rows = storage.get_calls_for_session(session_id)
    failed = next(row for row in rows if row["failed"])
    return FailedCallRecord(
        call_id=int(failed["id"]),
        session_id=session_id,
        error_message=str(failed["error_message"]),
        timestamp=str(failed["timestamp"]),
        messages=json.loads(failed["messages_json"]),
    )


def test_poll_failed_calls_returns_only_failed_rows(storage, sessions_db):
    _seed_drift_failure(storage)

    failures = poll_failed_calls(db_path=sessions_db)
    assert len(failures) == 1
    assert failures[0].error_message.startswith("RuntimeError")


def test_poll_failed_sessions_returns_distinct_ids(storage, sessions_db):
    _seed_drift_failure(storage)

    session_ids = poll_failed_sessions(db_path=sessions_db)
    assert len(session_ids) == 1


def test_extract_error_type_and_relevant_file():
    message = (
        'RuntimeError: boom\n  File "agents/coding_agent/sample_target/token_utils.py", line 9'
    )
    assert extract_error_type(message) == "RuntimeError"
    assert extract_relevant_file(message).endswith("token_utils.py")


def test_diagnose_marks_unclear_without_checkpoints(storage):
    session_id = storage.start_session()
    storage.record_call(
        session_id=session_id,
        provider="openrouter",
        model="test-model",
        input_tokens=10,
        output_tokens=0,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=[{"role": "user", "content": "fail"}],
        failed=True,
        error_message="ValueError: no checkpoints",
    )
    rows = storage.get_calls_for_session(session_id)
    failure = FailedCallRecord(
        call_id=int(rows[0]["id"]),
        session_id=session_id,
        error_message="ValueError: no checkpoints",
        timestamp=str(rows[0]["timestamp"]),
        messages=[{"role": "user", "content": "fail"}],
    )

    diagnostician = FailureDiagnostician(storage=storage)
    result = diagnostician.diagnose_failure(failure)

    assert result.root_cause == "UNCLEAR"
    assert result.replay_verified is False
    assert result.skip_to_stage4 is False


def test_diagnose_verifies_drift_failure(storage):
    failure = _seed_drift_failure(storage)
    diagnostician = FailureDiagnostician(storage=storage)

    result = diagnostician.diagnose_failure(failure)

    assert result.root_cause in {"DRIFT", "COMPRESSION", "RECENCY"}
    assert result.replay_verified is True
    assert result.confidence > 0.0


def test_diagnose_verifies_compression_failure(storage):
    failure = _seed_compression_failure(storage)
    diagnostician = FailureDiagnostician(
        storage=storage,
        attribution_engine=AttributionEngine(storage=storage),
        replayer=CounterfactualReplayer(storage=storage),
    )

    result = diagnostician.diagnose_failure(failure)

    assert result.replay_verified is True
    assert result.root_cause != "UNCLEAR"


def test_pattern_match_sets_skip_to_stage4(storage, tmp_path):
    failure = _seed_drift_failure(storage)
    pattern_db = tmp_path / "patterns.db"
    store = FixPatternStore(db_path=pattern_db)

    diagnostician = FailureDiagnostician(storage=storage, pattern_store=store)
    initial = diagnostician.diagnose_failure(failure)
    assert initial.skip_to_stage4 is False

    store.upsert_pattern(
        signature_hash=initial.signature_hash,
        root_cause_type=initial.root_cause,
        fix_diff_template="diff --git a/foo.py b/foo.py\n",
        success_count=3,
        reject_count=1,
    )

    matched = diagnostician.diagnose_failure(failure)
    assert matched.skip_to_stage4 is True
    assert matched.matched_pattern is not None
    assert matched.matched_pattern.fix_diff_template.startswith("diff --git")


def test_run_processes_all_failed_calls(storage):
    _seed_drift_failure(storage)
    _seed_compression_failure(storage)

    diagnostician = FailureDiagnostician(storage=storage)
    results = diagnostician.run()

    assert len(results) == 2
    assert all(result.failed_call_id for result in results)
