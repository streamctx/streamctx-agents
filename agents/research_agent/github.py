"""GitHub topic-tagged repo polling via the Search API.

GitHub has no official trending-JSON endpoint. This approximates "trending
repos tagged with agent/LLM topics" as recently-pushed, non-fork repos that
carry those topics, sorted by stars. One Search request per poll (GitHub
Search is 10 req/min). Named competitor repos already tracked by
``competitor_agent`` are skipped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlencode

from agents.research_agent.http import SleepFn, fetch_json, github_headers
from agents.research_agent.models import SOURCE_TYPE_GITHUB, SourceItem
from agents.research_agent.settings import ResearchConfig, github_token

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"

FetchJsonFn = Callable[[str], Any]
NowFn = Callable[[], datetime]


def github_search_url(
    topics: tuple[str, ...],
    *,
    since_date: str,
    per_page: int,
) -> str:
    topic_clause = " OR ".join(f"topic:{topic}" for topic in topics if topic.strip())
    query = f"({topic_clause}) pushed:>{since_date} fork:false"
    params = urlencode(
        {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": str(per_page),
        }
    )
    return f"{GITHUB_SEARCH_URL}?{params}"


def parse_github_search(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [row for row in items if isinstance(row, dict)]


def fetch_github_repos(
    config: ResearchConfig,
    *,
    fetch_fn: Optional[FetchJsonFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    now_fn: Optional[NowFn] = None,
    token: Optional[str] = None,
) -> list[dict[str, Any]]:
    """One Search API call; filter out competitor-tracked full_names."""
    del sleep_fn  # single request; pacing lives in the orchestrator
    now = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    since = (now - timedelta(days=max(1, config.github_lookback_days))).date().isoformat()
    url = github_search_url(
        config.github_topics,
        since_date=since,
        per_page=config.github_per_page,
    )
    payload = _fetch_search(url, config, fetch_fn, token)
    skip = {repo.lower() for repo in config.skip_github_repos}
    repos: list[dict[str, Any]] = []
    for row in parse_github_search(payload):
        full_name = str(row.get("full_name") or "").strip()
        html_url = str(row.get("html_url") or "").strip()
        if not full_name or not html_url:
            continue
        if full_name.lower() in skip:
            continue
        repos.append(row)
    return repos


def repos_to_source_items(
    repos: list[dict[str, Any]],
    *,
    excerpt_max_chars: int,
) -> list[SourceItem]:
    items: list[SourceItem] = []
    for row in repos:
        full_name = str(row.get("full_name") or "").strip()
        html_url = str(row.get("html_url") or "").strip()
        description = str(row.get("description") or "").strip()
        topics = row.get("topics") if isinstance(row.get("topics"), list) else []
        topic_line = ", ".join(str(topic) for topic in topics if str(topic).strip())
        excerpt_parts = [part for part in (description, topic_line) if part]
        excerpt = " | ".join(excerpt_parts)
        if excerpt_max_chars > 0 and len(excerpt) > excerpt_max_chars:
            excerpt = excerpt[: excerpt_max_chars - 3].rstrip() + "..."
        title = f"{full_name}: {description}" if description else full_name
        items.append(
            SourceItem(
                source_url=html_url,
                source_type=SOURCE_TYPE_GITHUB,
                title=title,
                content_excerpt=excerpt,
            )
        )
    return items


def _fetch_search(
    url: str,
    config: ResearchConfig,
    fetch_fn: Optional[FetchJsonFn],
    token: Optional[str],
) -> Any:
    if fetch_fn is not None:
        return fetch_fn(url)
    auth = token if token is not None else github_token()
    return fetch_json(
        url,
        headers=github_headers(config.user_agent, auth),
        max_retries=config.max_retries,
        backoff_base_seconds=config.backoff_base_seconds,
        max_backoff_seconds=config.max_backoff_seconds,
    )
