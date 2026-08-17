"""Unit tests for Stage 6 revert CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents.coding_agent.pending_approval import (
    STATUS_READY_FOR_APPROVAL,
    PendingApprovalStore,
)
from agents.coding_agent.revert_cli import revert


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    file_path = repo / "example.txt"
    file_path.write_text("version-1\n", encoding="utf-8")
    subprocess.run(["git", "add", "example.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    file_path.write_text("version-2\n", encoding="utf-8")
    subprocess.run(["git", "add", "example.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "agent fix"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, sha


def test_revert_runs_git_revert_for_recorded_commit(tmp_path, git_repo):
    repo, commit_sha = git_repo
    db_path = tmp_path / "agent.db"
    store = PendingApprovalStore(
        db_path=db_path,
        notifier=lambda _entry: None,
        enable_default_notifier=False,
    )
    try:
        entry = store.create_entry(
            session_id="1",
            root_cause="DRIFT",
            confidence=0.9,
            matched_pattern_id=None,
            diff="diff",
            regression_test="test",
            test_results="{}",
            retries_used=0,
            status=STATUS_READY_FOR_APPROVAL,
        )
        store.record_applied_commit(entry.entry_id, commit_sha)

        reverted_sha = revert(entry.entry_id, repo_root=repo, approval_store=store)
        assert reverted_sha == commit_sha
        assert (repo / "example.txt").read_text(encoding="utf-8") == "version-1\n"
    finally:
        store.close()


def test_revert_requires_applied_commit(tmp_path, git_repo):
    repo, _ = git_repo
    store = PendingApprovalStore(
        db_path=tmp_path / "agent.db",
        notifier=lambda _entry: None,
        enable_default_notifier=False,
    )
    try:
        entry = store.create_entry(
            session_id="1",
            root_cause="DRIFT",
            confidence=0.9,
            matched_pattern_id=None,
            diff="diff",
            regression_test="test",
            test_results="{}",
            retries_used=0,
            status=STATUS_READY_FOR_APPROVAL,
        )
        with pytest.raises(ValueError, match="no applied_commit"):
            revert(entry.entry_id, repo_root=repo, approval_store=store)
    finally:
        store.close()


def test_record_applied_commit_persists(tmp_path):
    store = PendingApprovalStore(
        db_path=tmp_path / "agent.db",
        notifier=lambda _entry: None,
        enable_default_notifier=False,
    )
    try:
        entry = store.create_entry(
            session_id="1",
            root_cause="DRIFT",
            confidence=0.9,
            matched_pattern_id=None,
            diff="diff",
            regression_test="test",
            test_results="{}",
            retries_used=0,
            status=STATUS_READY_FOR_APPROVAL,
        )
        updated = store.record_applied_commit(entry.entry_id, "abc123def")
        assert updated is not None
        assert updated.applied_commit == "abc123def"
    finally:
        store.close()
