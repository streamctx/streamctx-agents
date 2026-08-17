"""Webhook notifications for marketing drafts and shared agent alerts.

Slack/Telegram-style incoming webhooks: POST ``{"text": "..." }``.
``competitor_agent`` weekly reports use this same hook rather than a
second notification path.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable, Optional

from agents.marketing_agent.models import PendingApprovalEntry

# Set to any incoming webhook URL (Slack, Discord, Zapier, Telegram bot proxy, etc.)
WEBHOOK_URL_ENV = "MARKETING_AGENT_WEBHOOK_URL"

NotifierFn = Callable[[PendingApprovalEntry], None]
TextNotifierFn = Callable[[str], None]


def format_summary(entry: PendingApprovalEntry) -> str:
    """One-line summary suitable for chat webhook notifications."""
    short_id = entry.entry_id[:8]
    target = f" target={entry.target}" if entry.target else ""
    return (
        f"[marketing-agent] {entry.status} | {entry.platform} {entry.content_type}"
        f"{target} | id={short_id}"
    )


def post_webhook(url: str, text: str) -> None:
    """POST a JSON payload with a ``text`` field to the configured webhook URL."""
    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def webhook_url() -> str:
    return os.environ.get(WEBHOOK_URL_ENV, "").strip()


def notify_text(text: str) -> None:
    """Send arbitrary text if ``MARKETING_AGENT_WEBHOOK_URL`` is configured."""
    url = webhook_url()
    if not url:
        return
    post_webhook(url, text)


def notify_pending_approval(entry: PendingApprovalEntry) -> None:
    """Send a notification if ``MARKETING_AGENT_WEBHOOK_URL`` is configured."""
    notify_text(format_summary(entry))


def get_default_notifier() -> NotifierFn:
    return notify_pending_approval
