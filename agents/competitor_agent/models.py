"""Dataclasses for competitor snapshots, signals, and weekly reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


SNAPSHOT_TYPE_PRICING = "pricing"
SNAPSHOT_TYPE_CHANGELOG = "changelog"
SNAPSHOT_TYPE_GITHUB_RELEASE = "github_release"
SNAPSHOT_TYPE_MENTIONS = "mentions"
SNAPSHOT_TYPES = frozenset(
    {
        SNAPSHOT_TYPE_PRICING,
        SNAPSHOT_TYPE_CHANGELOG,
        SNAPSHOT_TYPE_GITHUB_RELEASE,
        SNAPSHOT_TYPE_MENTIONS,
    }
)

SIGNAL_TYPE_PRICING_CHANGE = "pricing_change"
SIGNAL_TYPE_NEW_POST = "new_post"
SIGNAL_TYPE_NEW_RELEASE = "new_release"
SIGNAL_TYPE_MENTION = "mention"
SIGNAL_TYPES = frozenset(
    {
        SIGNAL_TYPE_PRICING_CHANGE,
        SIGNAL_TYPE_NEW_POST,
        SIGNAL_TYPE_NEW_RELEASE,
        SIGNAL_TYPE_MENTION,
    }
)


@dataclass(frozen=True)
class CompetitorSnapshot:
    """One captured page/release blob for a competitor source."""

    competitor: str
    snapshot_type: str
    content_hash: str
    raw_content: str
    captured_at: str
    rowid: Optional[int] = None


@dataclass(frozen=True)
class CompetitorSignal:
    """A detected change or mention derived from snapshots / feeds / search."""

    signal_id: str
    competitor: str
    signal_type: str
    summary: str
    source_url: Optional[str]
    detected_at: str


@dataclass(frozen=True)
class WeeklyReport:
    """One rendered weekly competitive summary."""

    report_id: str
    week_start: str
    content_markdown: str
    created_at: str


@dataclass(frozen=True)
class PendingApprovalEntry:
    """A detected signal or research summary awaiting founder review."""

    entry_id: str
    title: str
    content: str
    target: Optional[str]
    mode: str
    status: str
    created_at: str
    competitor_name: str
    reviewed_at: Optional[str] = None
    source_fingerprint: Optional[str] = None
