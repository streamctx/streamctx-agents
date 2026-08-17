"""Unit tests for marketing_agent Slack/Telegram webhook notifications."""

from __future__ import annotations

from unittest.mock import patch

from agents.marketing_agent.models import PendingApprovalEntry
from agents.marketing_agent.notifications import (
    WEBHOOK_URL_ENV,
    format_summary,
    notify_pending_approval,
    notify_text,
    post_webhook,
)
from agents.marketing_agent.pending_approval import (
    PLATFORM_TWITTER,
    STATUS_PENDING,
    PendingApprovalStore,
)


def _entry() -> PendingApprovalEntry:
    return PendingApprovalEntry(
        entry_id="abcd1234-5678-90ab-cdef-1234567890ab",
        platform=PLATFORM_TWITTER,
        content_type="post",
        content="hello",
        target=None,
        mode="draft_only",
        status=STATUS_PENDING,
        created_at="2026-08-17T10:00:00+00:00",
    )


def test_format_summary_is_one_line():
    summary = format_summary(_entry())
    assert "\n" not in summary
    assert "twitter" in summary
    assert "abcd1234" in summary
    assert "[marketing-agent]" in summary


@patch.dict("os.environ", {}, clear=True)
def test_notify_skips_when_webhook_url_unset():
    with patch("agents.marketing_agent.notifications.post_webhook") as mock_post:
        notify_pending_approval(_entry())
        notify_text("weekly report")
        mock_post.assert_not_called()


@patch.dict("os.environ", {WEBHOOK_URL_ENV: "https://hooks.example.test/notify"})
@patch("agents.marketing_agent.notifications.post_webhook")
def test_notify_posts_when_webhook_url_set(mock_post):
    entry = _entry()
    notify_pending_approval(entry)
    mock_post.assert_called_once_with(
        "https://hooks.example.test/notify",
        format_summary(entry),
    )


@patch.dict("os.environ", {WEBHOOK_URL_ENV: "https://hooks.example.test/notify"})
@patch("agents.marketing_agent.notifications.post_webhook")
def test_notify_text_posts_arbitrary_body(mock_post):
    notify_text("## Week of 2026-08-10")
    mock_post.assert_called_once_with(
        "https://hooks.example.test/notify",
        "## Week of 2026-08-10",
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
        db_path=tmp_path / "marketing_agent.db",
        notifier=capture,
        enable_default_notifier=False,
    )
    try:
        created = store.create_entry(
            platform="hn",
            content_type="comment",
            content="draft comment",
        )
        assert len(received) == 1
        assert received[0].entry_id == created.entry_id
    finally:
        store.close()
