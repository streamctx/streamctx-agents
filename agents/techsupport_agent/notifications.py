"""Webhook notifications for tech-support drafts awaiting founder review."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable

from agents.techsupport_agent.models import PendingApprovalEntry

WEBHOOK_URL_ENV = "TECHSUPPORT_AGENT_WEBHOOK_URL"
NotifierFn = Callable[[PendingApprovalEntry], None]


def format_summary(entry: PendingApprovalEntry) -> str:
    short_id = entry.entry_id[:8]
    cite = f" cite={entry.kb_citation}" if entry.kb_citation else ""
    return (
        f"[techsupport-agent] {entry.status} | {entry.title or entry.ticket_id}"
        f"{cite} | id={short_id} — draft only, not posted"
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
