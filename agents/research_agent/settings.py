"""Poll intervals, arXiv keywords, and GitHub topics. No extra API credentials."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_ARXIV_CATEGORIES = ("cs.AI", "cs.MA")
DEFAULT_ARXIV_KEYWORDS = (
    "agent reliability",
    "multi-agent failure",
    "multi-agent",
    "context management",
    "memory management",
    "context compression",
    "failure attribution",
    "self-healing",
    "checkpoint",
    "poison detection",
)
DEFAULT_GITHUB_TOPICS = (
    "llm-agent",
    "llm-agents",
    "ai-agents",
    "ai-agent",
    "multi-agent",
    "multi-agent-systems",
    "autonomous-agents",
    "agentic",
)


@dataclass(frozen=True)
class ResearchConfig:
    """Conservative defaults: arXiv asks for ≥3s between calls; GitHub Search is 10 req/min."""

    user_agent: str = "streamctx-research-agent/0.1"
    poll_delay_seconds: float = 3.0
    arxiv_min_interval_seconds: int = 21600
    github_min_interval_seconds: int = 21600
    arxiv_max_results: int = 25
    github_per_page: int = 25
    github_lookback_days: int = 7
    excerpt_max_chars: int = 2000
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    arxiv_categories: tuple[str, ...] = DEFAULT_ARXIV_CATEGORIES
    arxiv_keywords: tuple[str, ...] = DEFAULT_ARXIV_KEYWORDS
    github_topics: tuple[str, ...] = DEFAULT_GITHUB_TOPICS
    skip_github_repos: frozenset[str] = field(default_factory=frozenset)

    def interval_for(self, source_type: str) -> int:
        if source_type == "arxiv":
            return self.arxiv_min_interval_seconds
        if source_type == "github":
            return self.github_min_interval_seconds
        raise ValueError(f"no poll interval for source_type {source_type!r}")


def github_token() -> str:
    """Reuse the same optional token competitor_agent already reads. Not required."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def competitor_github_repos() -> frozenset[str]:
    """Repos competitor_agent already tracks — skip them here to avoid duplicate coverage."""
    try:
        from agents.competitor_agent.settings import CompetitorConfig

        return frozenset(
            item.github_repo.strip().lower()
            for item in CompetitorConfig.load().competitors
            if item.github_repo and item.github_repo.strip()
        )
    except (OSError, ValueError, KeyError, TypeError, ImportError):
        return frozenset()


def default_config(*, skip_github_repos: Optional[frozenset[str]] = None) -> ResearchConfig:
    repos = skip_github_repos if skip_github_repos is not None else competitor_github_repos()
    return ResearchConfig(skip_github_repos=repos)
