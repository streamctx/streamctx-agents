"""Webhook notifications for pre-sales drafts awaiting founder review."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable

from agents.presales_agent.models import PendingApprovalEntry

WEBHOOK_URL_ENV = "PRESALES_AGENT_WEBHOOK_URL"
NotifierFn = Callable[[PendingApprovalEntry], None]


def format_summary(entry: PendingApprovalEntry) -> str:
    short_id = entry.entry_id[:8]
    flag = f" flag={entry.flag}" if entry.flag else ""
    return (
        f"[presales-agent] {entry.status} | {entry.title or entry.lead_id}"
        f"{flag} | id={short_id} — copy only, not sent"
    )


def post_webhook(url: str, text: str) -> None:
    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def notify_text(text: str) -> None:
    url = os.environ.get(WEBHOOK_URL_ENV, "").strip()
    if not url:
        return
    post_webhook(url, text)


def notify_pending_approval(entry: PendingApprovalEntry) -> None:
    notify_text(format_summary(entry))
