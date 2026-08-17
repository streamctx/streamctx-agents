"""Webhook notifications for new pending_approval entries."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable, Optional

from agents.coding_agent.models import PendingApprovalEntry

# Set to any incoming webhook URL (Slack, Discord, Zapier, Telegram bot proxy, etc.)
WEBHOOK_URL_ENV = "CODING_AGENT_WEBHOOK_URL"

NotifierFn = Callable[[PendingApprovalEntry], None]


def format_summary(entry: PendingApprovalEntry) -> str:
    """One-line summary suitable for chat webhook notifications."""
    short_id = entry.entry_id[:8]
    return (
        f"[coding-agent] {entry.status} | session={entry.session_id} "
        f"| {entry.root_cause} conf={entry.confidence:.2f} | id={short_id}"
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


def notify_pending_approval(entry: PendingApprovalEntry) -> None:
    """Send a notification if ``CODING_AGENT_WEBHOOK_URL`` is configured."""
    url = os.environ.get(WEBHOOK_URL_ENV, "").strip()
    if not url:
        return
    post_webhook(url, format_summary(entry))


def get_default_notifier() -> NotifierFn:
    return notify_pending_approval
