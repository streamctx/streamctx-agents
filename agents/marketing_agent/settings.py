"""Env-driven defaults for auto-tier adapters. No extra packages."""

from __future__ import annotations

import os


def env_str(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


# Twitter/X — OAuth 2 user token OR OAuth 1.0a key set.
TWITTER_BEARER_TOKEN = env_str("TWITTER_BEARER_TOKEN")
TWITTER_API_KEY = env_str("TWITTER_API_KEY")
TWITTER_API_SECRET = env_str("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = env_str("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = env_str("TWITTER_ACCESS_TOKEN_SECRET")

# Dev.to
DEVTO_API_KEY = env_str("DEVTO_API_KEY")

# Reddit script app
REDDIT_CLIENT_ID = env_str("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = env_str("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME = env_str("REDDIT_USERNAME")
REDDIT_PASSWORD = env_str("REDDIT_PASSWORD")
REDDIT_USER_AGENT = env_str(
    "REDDIT_USER_AGENT",
    default="streamctx-marketing-agent/0.1 (draft/approval gated)",
)

# Conservative defaults: spam-filter history on Reddit.
REDDIT_MIN_LINK_KARMA = env_int("REDDIT_MIN_LINK_KARMA", 50)
REDDIT_MIN_COMMENT_KARMA = env_int("REDDIT_MIN_COMMENT_KARMA", 50)
REDDIT_SUBREDDIT_INTERVAL_SECONDS = env_int(
    "REDDIT_SUBREDDIT_INTERVAL_SECONDS", 172800
)  # 48 hours
