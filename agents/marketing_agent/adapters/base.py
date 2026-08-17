"""Shared draft/auto adapter interface.

Draft-tier: format + submit only. Auto-tier: publish() after approval.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from datetime import datetime, timezone

from agents.marketing_agent.models import PendingApprovalEntry, Story
from agents.marketing_agent.pending_approval import (
    CONTENT_POST,
    MODE_AUTO_AFTER_APPROVAL,
    MODE_DRAFT_ONLY,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    PendingApprovalStore,
)
from agents.marketing_agent.safety import SafetyGate, story_fingerprint


class PublishError(RuntimeError):
    """Raised when auto-publish is refused or the platform API fails."""


class DraftOnlyError(PublishError):
    """Draft-tier platforms have no posting API."""


class CredentialsError(PublishError):
    """Required platform credentials are missing."""


class DraftAdapter(ABC):
    """Format a Story for one platform and write a pending_approval row."""

    platform: str
    mode: str = MODE_DRAFT_ONLY

    def __init__(
        self,
        store: PendingApprovalStore,
        *,
        content_type: str = CONTENT_POST,
        safety: Optional[SafetyGate] = None,
    ) -> None:
        self.store = store
        self.content_type = content_type
        self.safety = safety

    def _safety(self) -> SafetyGate:
        if self.safety is None:
            self.safety = SafetyGate.default()
        return self.safety

    @abstractmethod
    def format(self, story: Story, target: Optional[str] = None) -> str:
        """Platform-specific tone/length adaptation."""

    def submit(
        self,
        content: str,
        target: Optional[str] = None,
        *,
        fingerprint: Optional[str] = None,
    ) -> str:
        """Write a pending_approval row after safety checks. Never posts."""
        prepared = self._safety().prepare(
            content,
            platform=self.platform,
            store=self.store,
            fingerprint=fingerprint,
        )
        entry = self.store.create_entry(
            platform=self.platform,
            content_type=self.content_type,
            content=prepared,
            target=target,
            mode=self.mode,
            status=STATUS_PENDING,
            source_fingerprint=fingerprint,
        )
        return entry.entry_id

    def queue(self, story: Story, target: Optional[str] = None) -> str:
        """format() then submit() — still pending; never hits a platform API."""
        return self.submit(
            self.format(story, target=target),
            target=target,
            fingerprint=story_fingerprint(story),
        )

    def publish(self, entry_id: str) -> PendingApprovalEntry:
        """Draft-tier platforms cannot auto-post."""
        raise DraftOnlyError(
            f"{self.platform} is draft_only; copy the queued draft and post manually."
        )


class AutoAdapter(DraftAdapter):
    """
    API-backed adapter. ``submit()`` queues ``auto_after_approval``.

    ``publish(entry_id)`` is the only path that may call a platform API, and
    only after the row is ``approved``.
    """

    mode: str = MODE_AUTO_AFTER_APPROVAL

    def publish(self, entry_id: str) -> PendingApprovalEntry:
        entry = self._require_approved(entry_id)
        self._deliver(entry)
        published_at = datetime.now(timezone.utc).isoformat()
        updated = self.store.update_status(
            entry_id, STATUS_PUBLISHED, published_at=published_at
        )
        if updated is None:
            raise PublishError(f"entry {entry_id} vanished after publish")
        return updated

    def _require_approved(self, entry_id: str) -> PendingApprovalEntry:
        entry = self.store.get_entry(entry_id)
        if entry is None:
            raise PublishError(f"unknown entry_id {entry_id}")
        if entry.platform != self.platform:
            raise PublishError(
                f"entry {entry_id} is platform {entry.platform!r}, not {self.platform!r}"
            )
        if entry.mode != MODE_AUTO_AFTER_APPROVAL:
            raise PublishError(
                f"entry {entry_id} is {entry.mode}; auto-publish requires auto_after_approval"
            )
        if entry.status == STATUS_PUBLISHED:
            raise PublishError(f"entry {entry_id} is already published")
        if entry.status != STATUS_APPROVED:
            raise PublishError(
                f"entry {entry_id} status is {entry.status!r}; publish requires approved"
            )
        return entry

    def _deliver(self, entry: PendingApprovalEntry) -> None:
        raise NotImplementedError


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


def strip_version_prefix(headline: str) -> str:
    if ":" in headline and headline.split(":", 1)[0].strip()[:1].isdigit():
        rest = headline.split(":", 1)[1].strip()
        return rest or headline
    return headline.strip()


def clip_text(text: str, max_chars: int) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    clipped = text[: max_chars - 1].rsplit(" ", 1)[0]
    return (clipped or text[: max_chars - 1]).rstrip(",;:") + "…"
