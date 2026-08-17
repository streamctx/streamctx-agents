"""Config-driven competitor list and poll intervals. No competitor names in logic."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_COMPETITORS_PATH = Path(__file__).resolve().parent / "competitors.json"


@dataclass(frozen=True)
class CompetitorSource:
    """One tracked competitor. Add/remove rows in competitors.json, not code."""

    name: str
    pricing_url: Optional[str] = None
    github_repo: Optional[str] = None
    rss_url: Optional[str] = None
    notes: str = ""


@dataclass(frozen=True)
class CompetitorConfig:
    competitors: tuple[CompetitorSource, ...]
    user_agent: str = "streamctx-competitor-agent/0.1"
    poll_delay_seconds: float = 2.0
    pricing_min_interval_seconds: int = 21600
    github_min_interval_seconds: int = 3600
    rss_min_interval_seconds: int = 10800
    github_per_page: int = 30
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    max_backoff_seconds: float = 30.0

    def by_name(self, name: str) -> CompetitorSource:
        key = name.strip().lower()
        for item in self.competitors:
            if item.name.lower() == key:
                return item
        raise KeyError(name)

    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.competitors)

    def with_pricing(self) -> tuple[CompetitorSource, ...]:
        return tuple(item for item in self.competitors if item.pricing_url)

    def with_github(self) -> tuple[CompetitorSource, ...]:
        return tuple(item for item in self.competitors if item.github_repo)

    def with_rss(self) -> tuple[CompetitorSource, ...]:
        return tuple(item for item in self.competitors if item.rss_url)

    @classmethod
    def load(cls, path: Optional[Path | str] = None) -> CompetitorConfig:
        config_path = Path(path) if path is not None else default_competitors_path()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        competitors = tuple(
            CompetitorSource(
                name=_optional_text(row.get("name")) or "",
                pricing_url=_optional_text(row.get("pricing_url")),
                github_repo=_optional_text(row.get("github_repo")),
                rss_url=_optional_text(row.get("rss_url")),
                notes=str(row.get("notes") or ""),
            )
            for row in (data.get("competitors") or [])
        )
        if not competitors or any(not item.name for item in competitors):
            raise ValueError(f"competitors.json at {config_path} has no named competitors")
        names = [item.name.lower() for item in competitors]
        if len(names) != len(set(names)):
            raise ValueError("duplicate competitor names in config")
        return cls(
            competitors=competitors,
            user_agent=str(data.get("user_agent") or "streamctx-competitor-agent/0.1"),
            poll_delay_seconds=float(data.get("poll_delay_seconds", 2.0)),
            pricing_min_interval_seconds=int(data.get("pricing_min_interval_seconds", 21600)),
            github_min_interval_seconds=int(data.get("github_min_interval_seconds", 3600)),
            rss_min_interval_seconds=int(data.get("rss_min_interval_seconds", 10800)),
            github_per_page=int(data.get("github_per_page", 30)),
            max_retries=int(data.get("max_retries", 3)),
            backoff_base_seconds=float(data.get("backoff_base_seconds", 1.0)),
            max_backoff_seconds=float(data.get("max_backoff_seconds", 30.0)),
        )


def default_competitors_path() -> Path:
    env = os.environ.get("COMPETITOR_CONFIG")
    if env:
        return Path(env)
    return DEFAULT_COMPETITORS_PATH


def github_token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _optional_text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
