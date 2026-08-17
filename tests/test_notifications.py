"""Unit tests for Stage 6 webhook notifications."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agents.coding_agent.models import PendingApprovalEntry
from agents.coding_agent.notifications import (
    WEBHOOK_URL_ENV,
    format_summary,
    notify_pending_approval,
    post_webhook,
)
from agents.coding_agent.pending_approval import (
    STATUS_READY_FOR_APPROVAL,
    PendingApprovalStore,
)


def _entry() -> PendingApprovalEntry:
    return PendingApprovalEntry(
        entry_id="abcd1234-5678-90ab-cdef-1234567890ab",
        session_id="42",
        root_cause="DRIFT",
        confidence=0.85,
        matched_pattern_id=None,
        diff="diff",
        regression_test="test",
        test_results="{}",
        retries_used=0,
        status=STATUS_READY_FOR_APPROVAL,
        created_at="2026-08-17T10:00:00+00:00",
    )


def test_format_summary_is_one_line():
    summary = format_summary(_entry())
    assert "\n" not in summary
    assert "session=42" in summary
    assert "DRIFT" in summary
    assert "abcd1234" in summary


@patch.dict("os.environ", {}, clear=True)
def test_notify_skips_when_webhook_url_unset():
    with patch("agents.coding_agent.notifications.post_webhook") as mock_post:
        notify_pending_approval(_entry())
        mock_post.assert_not_called()


@patch.dict("os.environ", {WEBHOOK_URL_ENV: "https://hooks.example.test/notify"})
@patch("agents.coding_agent.notifications.post_webhook")
def test_notify_posts_when_webhook_url_set(mock_post):
    entry = _entry()
    notify_pending_approval(entry)
    mock_post.assert_called_once_with(
        "https://hooks.example.test/notify",
        format_summary(entry),
    )


@patch("urllib.request.urlopen")
def test_post_webhook_sends_json_text_payload(mock_urlopen):
    post_webhook("https://hooks.example.test/notify", "hello")
    request = mock_urlopen.call_args[0][0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://hooks.example.test/notify"
    assert b'"text": "hello"' in request.data


def test_create_entry_dispatches_notifier(tmp_path):
    received: list[PendingApprovalEntry] = []

    def capture(entry: PendingApprovalEntry) -> None:
        received.append(entry)

    store = PendingApprovalStore(
        db_path=tmp_path / "agent.db",
        notifier=capture,
        enable_default_notifier=False,
    )
    try:
        created = store.create_entry(
            session_id="7",
            root_cause="COMPRESSION",
            confidence=0.7,
            matched_pattern_id=None,
            diff=None,
            regression_test=None,
            test_results=None,
            retries_used=0,
            status=STATUS_READY_FOR_APPROVAL,
        )
        assert len(received) == 1
        assert received[0].entry_id == created.entry_id
    finally:
        store.close()
