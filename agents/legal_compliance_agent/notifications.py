"""Webhook notifications for legal/compliance drafts awaiting founder + counsel."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable

from agents.legal_compliance_agent.models import PendingApprovalEntry

WEBHOOK_URL_ENV = "LEGAL_COMPLIANCE_AGENT_WEBHOOK_URL"
NotifierFn = Callable[[PendingApprovalEntry], None]


def format_summary(entry: PendingApprovalEntry) -> str:
    short_id = entry.entry_id[:8]
    kind = f" kind={entry.kind}" if entry.kind else ""
    return (
        f"[legal-compliance-agent] {entry.status} | {entry.title or entry.finding_id}"
        f"{kind} | id={short_id} — draft only, not legal advice, not published"
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
