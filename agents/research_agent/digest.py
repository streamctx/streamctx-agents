"""Stage 4: daily digest of top-scored ideas, via the marketing webhook.

Selects the top 3–5 ``new`` scored ideas from the last 24 hours, formats a
short digest, pushes it through ``marketing_agent.notifications.notify_text``,
then marks those rows ``reviewed``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from agents.marketing_agent.notifications import notify_text
from agents.research_agent.models import DigestResult, ResearchIdea, STATUS_REVIEWED
from agents.research_agent.settings import ResearchConfig, default_config
from agents.research_agent.storage import ResearchStore

NowFn = Callable[[], datetime]
NotifierFn = Callable[[str], None]
WS_RE = re.compile(r"\s+")
GAP_MAX_CHARS = 160


def format_digest(items: list[ResearchIdea], *, now: datetime) -> str:
    """Short digest: score + title + one-line gap per item."""
    day = now.astimezone(timezone.utc).date().isoformat()
    if not items:
        return f"[research-agent] daily digest — {day}\nNo scored ideas in the last 24 hours."
    lines = [f"[research-agent] daily digest — {day}"]
    for idea in items:
        score = f"{float(idea.composite_score):.2f}" if idea.composite_score is not None else "—"
        gap = _one_line(idea.gap_description)
        lines.append(f"{score} {idea.title} — {gap}")
    return "\n".join(lines)


def run_digest(
    store: ResearchStore,
    *,
    config: Optional[ResearchConfig] = None,
    now_fn: Optional[NowFn] = None,
    notifier: Optional[NotifierFn] = None,
    notify: bool = True,
) -> DigestResult:
    spec = config or default_config()
    now = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    since = (now - timedelta(hours=max(1, spec.digest_lookback_hours))).isoformat()
    limit = min(5, max(1, spec.digest_limit))
    items = store.list_digest_candidates(since=since, limit=limit)
    body = format_digest(items, now=now)
    if not items:
        return DigestResult(items=(), body=body, notified=False)

    for idea in items:
        store.set_status(idea.idea_id, STATUS_REVIEWED)
    reviewed = tuple(store.get_idea(idea.idea_id) or idea for idea in items)

    notified = False
    if notify:
        try:
            (notifier or notify_text)(body)
            notified = True
        except Exception:
            notified = False
    return DigestResult(items=reviewed, body=body, notified=notified)


def summarize(result: DigestResult) -> str:
    return f"items={len(result.items)} notified={str(result.notified).lower()}"


def _one_line(value: Optional[str]) -> str:
    text = WS_RE.sub(" ", (value or "").strip())
    if not text:
        return "(no gap description)"
    if len(text) > GAP_MAX_CHARS:
        return text[: GAP_MAX_CHARS - 3].rstrip() + "..."
    return text
