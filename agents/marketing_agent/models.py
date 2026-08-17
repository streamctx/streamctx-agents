"""Dataclasses for the marketing-agent content core."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


CHANGELOG_KIND = "changelog"
COMMIT_KIND = "commit"


@dataclass(frozen=True)
class Story:
    """Platform-agnostic story extracted from a single source item."""

    headline: str
    key_facts: list[str]
    proof_point: str
    tone_tags: list[str]


@dataclass(frozen=True)
class PendingApprovalEntry:
    """One row in the marketing ``pending_approval`` queue."""

    entry_id: str
    platform: str
    content_type: str
    content: str
    target: Optional[str]
    mode: str
    status: str
    created_at: str
    published_at: Optional[str] = None
    source_fingerprint: Optional[str] = None


@dataclass(frozen=True)
class PublicPost:
    """One public HN / Reddit / Twitter post or comment."""

    platform: str
    post_id: str
    url: str
    author: str
    title: str
    body: str
    created_at: Optional[str] = None


@dataclass(frozen=True)
class ScoredLead:
    """A public post scored against StreamCtx features."""

    post: PublicPost
    score: float
    matched_features: tuple[str, ...]
    matched_terms: tuple[str, ...]
    reason: str
    primary_feature: str = ""


@dataclass(frozen=True)
class SourceData:
    """
    One changelog entry (version section) or one git commit.

    ``generate_story`` turns a single ``SourceData`` into one ``Story``.
    """

    kind: str
    title: str
    body: str
    items: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    category: Optional[str] = None
    version: Optional[str] = None
    date: Optional[str] = None
    identifier: str = ""
    author: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SourceData:
        """Build from a dict so callers can pass unstructured source_data."""
        kind = str(data.get("kind") or CHANGELOG_KIND)
        title = str(data.get("title") or data.get("headline") or "")
        body = str(data.get("body") or data.get("text") or "")
        items = list(data.get("items") or [])
        if not items and body:
            items = [line.strip() for line in body.splitlines() if line.strip()]
        categories = list(data.get("categories") or [])
        category = data.get("category")
        if category and category not in categories:
            categories = [str(category), *categories]
        return cls(
            kind=kind,
            title=title,
            body=body,
            items=items,
            categories=[str(c) for c in categories],
            category=str(category) if category else None,
            version=data.get("version"),
            date=data.get("date"),
            identifier=str(data.get("identifier") or data.get("version") or title),
            author=data.get("author"),
        )
