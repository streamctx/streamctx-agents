"""Dataclasses and allowed enums for research ideas and poll results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SOURCE_TYPE_ARXIV = "arxiv"
SOURCE_TYPE_GITHUB = "github"
SOURCE_TYPE_HN = "hn"
SOURCE_TYPE_REDDIT = "reddit"
SOURCE_TYPE_NEWSLETTER = "newsletter"
SOURCE_TYPE_TWITTER = "twitter"
SOURCE_TYPES = frozenset(
    {
        SOURCE_TYPE_ARXIV,
        SOURCE_TYPE_GITHUB,
        SOURCE_TYPE_HN,
        SOURCE_TYPE_REDDIT,
        SOURCE_TYPE_NEWSLETTER,
        SOURCE_TYPE_TWITTER,
    }
)

STATUS_NEW = "new"
STATUS_REVIEWED = "reviewed"
STATUS_IN_BACKLOG = "in_backlog"
STATUS_PROTOTYPED = "prototyped"
STATUS_DISMISSED = "dismissed"
STATUSES = frozenset(
    {
        STATUS_NEW,
        STATUS_REVIEWED,
        STATUS_IN_BACKLOG,
        STATUS_PROTOTYPED,
        STATUS_DISMISSED,
    }
)

CLASSIFICATION_FEATURE = "feature"
CLASSIFICATION_NEW_PRODUCT = "new_product"
CLASSIFICATION_NOT_ACTIONABLE = "not_actionable"
CLASSIFICATIONS = frozenset(
    {
        CLASSIFICATION_FEATURE,
        CLASSIFICATION_NEW_PRODUCT,
        CLASSIFICATION_NOT_ACTIONABLE,
    }
)

HYPE_TECHNICAL = "technical_substance"
HYPE_MARKETING = "marketing_hype"
HYPE_LABELS = frozenset({HYPE_TECHNICAL, HYPE_MARKETING})


@dataclass(frozen=True)
class ResearchIdea:
    """One row in ``research_ideas``. Scores stay null until gap-mapping."""

    idea_id: str
    source_url: str
    source_type: str
    title: str
    gap_description: Optional[str]
    feasibility_score: Optional[int]
    pain_match_score: Optional[int]
    novelty_score: Optional[int]
    composite_score: Optional[float]
    classification: Optional[str]
    status: str
    detected_at: str
    content_excerpt: Optional[str] = None
    hype_label: Optional[str] = None


@dataclass(frozen=True)
class HypeStats:
    """Running totals so the hype filter can be sanity-checked."""

    kept: int
    discarded: int
    errors: int


@dataclass(frozen=True)
class HypeDiscard:
    idea_id: str
    source_url: str
    title: str
    discarded_at: str


@dataclass(frozen=True)
class HypeFilterResult:
    """Outcome of one Stage 2 pass. Independently inspectable in tests."""

    kept: tuple[ResearchIdea, ...]
    discarded: tuple[ResearchIdea, ...]
    errors: tuple[tuple[str, str], ...]
    stats: HypeStats


@dataclass(frozen=True)
class GapMapping:
    """Parsed Stage 3 LLM result plus computed composite."""

    already_covered: bool
    gap_description: str
    feasibility_score: int
    pain_match_score: int
    novelty_score: int
    composite_score: float


@dataclass(frozen=True)
class GapMapResult:
    """Outcome of one Stage 3 pass. Independently inspectable in tests."""

    mapped: tuple[ResearchIdea, ...]
    errors: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DigestResult:
    """Outcome of one Stage 4 daily digest. Independently inspectable in tests."""

    items: tuple[ResearchIdea, ...]
    body: str
    notified: bool


@dataclass(frozen=True)
class ClassifyResult:
    """Outcome of one Stage 5 pass. Independently inspectable in tests."""

    classified: tuple[ResearchIdea, ...]
    errors: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class HandoffResult:
    """Outcome of one Stage 6 coding-agent handoff. Independently inspectable."""

    handed_off: tuple[ResearchIdea, ...]
    errors: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SourceItem:
    """A raw paper or repo from a poll, before persistence."""

    source_url: str
    source_type: str
    title: str
    content_excerpt: str


@dataclass(frozen=True)
class PollResult:
    """Outcome of one ``run_poll`` call. Independently inspectable in tests."""

    inserted: tuple[ResearchIdea, ...]
    skipped_duplicate: tuple[str, ...]
    skipped_interval: tuple[str, ...]
    errors: tuple[tuple[str, str], ...]
