"""Hacker News draft adapter — draft_only, no posting API.

Reply drafts must target a real ``item?id=`` page (not Algolia) and the
page must still expose a reply link. HN locks comments after ~45 days;
the reply link disappearing is the source of truth, not a date heuristic.
"""

from __future__ import annotations

import re
from typing import Optional

from agents.marketing_agent.adapters.base import DraftAdapter, facts_as_lines
from agents.marketing_agent.models import Story
from agents.marketing_agent.pending_approval import CONTENT_COMMENT, CONTENT_POST, PLATFORM_HN
from agents.marketing_agent.thread_age import (
    FetchPage,
    HNInvalidTargetError,
    HNThreadError,
    HNThreadLockedError,
    HNThreadUnreachableError,
    canonical_hn_item_url,
    fetch_hn_item_page,
    verify_hn_thread,
)

PROMO_LEAD_RE = re.compile(
    r"\b(launch|announcing|show hn|check out|we just)\b",
    re.IGNORECASE,
)

HN_COMMENT_MAX_CHARS = 1200
HN_POST_MAX_CHARS = 1800

__all__ = [
    "FetchPage",
    "HNInvalidTargetError",
    "HNThreadError",
    "HNThreadLockedError",
    "HNThreadUnreachableError",
    "HackerNewsAdapter",
    "canonical_hn_item_url",
    "fetch_hn_item_page",
    "verify_hn_thread",
]


class HackerNewsAdapter(DraftAdapter):
    platform = PLATFORM_HN

    def __init__(
        self,
        store,
        *,
        content_type: str = CONTENT_POST,
        fetch_page: Optional[FetchPage] = None,
        safety=None,
    ) -> None:
        super().__init__(store, content_type=content_type, safety=safety)
        self.fetch_page = fetch_page or fetch_hn_item_page

    def _safety(self):
        gate = super()._safety()
        if gate.fetch_page is None:
            gate.fetch_page = self.fetch_page
        return gate

    def format(self, story: Story, target: Optional[str] = None) -> str:
        if self.content_type == CONTENT_COMMENT:
            self._verify_comment_target(target)
            text = _format_hn_comment(story)
        else:
            text = _format_hn_post(story)
        return text

    def submit(self, content: str, target: Optional[str] = None, **kwargs) -> str:
        if self.content_type == CONTENT_COMMENT:
            target = self._verify_comment_target(target)
        return super().submit(content, target=target, **kwargs)

    def _verify_comment_target(self, target: Optional[str]) -> str:
        rules = self._safety().rules.thread_age
        if not rules.enabled:
            return canonical_hn_item_url(target, rules=rules)
        return verify_hn_thread(target, fetch_page=self.fetch_page, rules=rules)


def _format_hn_comment(story: Story) -> str:
    lead = _technical_lead(story)
    proof = (story.proof_point or "").strip()
    parts = [lead]
    if proof and proof.lower() not in lead.lower():
        parts.append(proof)
    return _clip(" ".join(parts), HN_COMMENT_MAX_CHARS)


def _format_hn_post(story: Story) -> str:
    title = _technical_lead(story)
    body_lines = facts_as_lines(story, limit=3, bullet="")
    body = "\n\n".join(line.strip() for line in body_lines if line.strip())
    proof = (story.proof_point or "").strip()
    chunks = [title]
    if body and body.lower() not in title.lower():
        chunks.append(body)
    if proof and proof.lower() not in "\n".join(chunks).lower():
        chunks.append(proof)
    return _clip("\n\n".join(chunks), HN_POST_MAX_CHARS)


def _technical_lead(story: Story) -> str:
    headline = (story.headline or "").strip()
    if headline and not PROMO_LEAD_RE.search(headline):
        if ":" in headline and headline.split(":", 1)[0].strip()[0:1].isdigit():
            return headline.split(":", 1)[1].strip() or headline
        return headline
    if story.key_facts:
        return story.key_facts[0]
    return story.proof_point


def _clip(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+\n", "\n", text).strip()
    if len(text) <= max_chars:
        return text
    clipped = text[: max_chars - 1].rsplit(" ", 1)[0]
    return (clipped or text[: max_chars - 1]).rstrip(",;:") + "…"
