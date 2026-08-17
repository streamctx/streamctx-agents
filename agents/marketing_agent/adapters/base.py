"""Shared draft-adapter interface. No platform APIs — queue only."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from agents.marketing_agent.models import Story
from agents.marketing_agent.pending_approval import (
    CONTENT_POST,
    MODE_DRAFT_ONLY,
    STATUS_PENDING,
    PendingApprovalStore,
)


class DraftAdapter(ABC):
    """
    Format a Story for one platform and write a ``draft_only`` queue row.

    ``publish()`` is intentionally absent here — draft-tier platforms have
    no safe posting API. Auto-tier adapters add it in Stage 3.
    """

    platform: str
    mode: str = MODE_DRAFT_ONLY

    def __init__(
        self,
        store: PendingApprovalStore,
        *,
        content_type: str = CONTENT_POST,
    ) -> None:
        self.store = store
        self.content_type = content_type

    @abstractmethod
    def format(self, story: Story, target: Optional[str] = None) -> str:
        """Platform-specific tone/length adaptation."""

    def submit(self, content: str, target: Optional[str] = None) -> str:
        """Write a pending_approval row and return its entry_id."""
        entry = self.store.create_entry(
            platform=self.platform,
            content_type=self.content_type,
            content=content,
            target=target,
            mode=self.mode,
            status=STATUS_PENDING,
        )
        return entry.entry_id

    def queue(self, story: Story, target: Optional[str] = None) -> str:
        """format() then submit() — still draft_only / pending."""
        return self.submit(self.format(story, target=target), target=target)


def facts_as_lines(story: Story, *, limit: int = 4, bullet: str = "- ") -> list[str]:
    lines: list[str] = []
    for fact in story.key_facts[:limit]:
        cleaned = fact.strip()
        if cleaned:
            lines.append(f"{bullet}{cleaned}")
    return lines


def mention_product(story: Story) -> str:
    """One factual product/version mention, not a pitch."""
    version = None
    if story.headline and story.headline[0].isdigit():
        version = story.headline.split(":", 1)[0].strip()
    if version:
        return f"StreamCtx {version}"
    return "StreamCtx"
