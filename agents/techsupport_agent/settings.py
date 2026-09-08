"""Poll intervals, GitHub repo, and Discord channel. Credentials come from env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from agents.competitor_agent.settings import github_token
from shared.config import BASE_DIR

DEFAULT_GITHUB_REPO = "streamctx/streamctx-agents"


@dataclass(frozen=True)
class TechSupportConfig:
    user_agent: str = "streamctx-techsupport-agent/0.1"
    poll_delay_seconds: float = 1.0
    github_min_interval_seconds: int = 900
    discord_min_interval_seconds: int = 900
    github_per_page: int = 50
    discord_limit: int = 50
    github_repo: str = DEFAULT_GITHUB_REPO
    discord_channel_id: str = ""
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    kb_roots: tuple[str, ...] = ("README.md", "CHANGELOG.md", "docs")
    excerpt_max_chars: int = 1200

    def interval_for(self, source: str) -> int:
        if source == "github":
            return self.github_min_interval_seconds
        if source == "discord":
            return self.discord_min_interval_seconds
        raise ValueError(f"no poll interval for source {source!r}")


def discord_token() -> str:
    for name in ("DISCORD_BOT_TOKEN", "DISCORD_TOKEN"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def github_repo() -> str:
    for name in ("TECHSUPPORT_GITHUB_REPO", "GITHUB_REPOSITORY"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip().lstrip("/")
    return DEFAULT_GITHUB_REPO


def discord_channel_id() -> str:
    return (os.environ.get("DISCORD_SUPPORT_CHANNEL_ID") or "").strip()


def default_config(
    *,
    github_repo_name: Optional[str] = None,
    discord_channel: Optional[str] = None,
    kb_roots: Optional[tuple[str, ...]] = None,
) -> TechSupportConfig:
    return TechSupportConfig(
        github_repo=github_repo_name if github_repo_name is not None else github_repo(),
        discord_channel_id=(
            discord_channel if discord_channel is not None else discord_channel_id()
        ),
        kb_roots=kb_roots if kb_roots is not None else ("README.md", "CHANGELOG.md", "docs"),
    )


def repo_root() -> str:
    return BASE_DIR


__all__ = [
    "DEFAULT_GITHUB_REPO",
    "TechSupportConfig",
    "default_config",
    "discord_channel_id",
    "discord_token",
    "github_repo",
    "github_token",
    "repo_root",
]
