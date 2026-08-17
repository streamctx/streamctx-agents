"""Unit tests for Stage 1 GitHub Search (topic-tagged trending approximation)."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from agents.research_agent.github import (
    fetch_github_repos,
    github_search_url,
    parse_github_search,
    repos_to_source_items,
)
from agents.research_agent.models import SOURCE_TYPE_GITHUB
from agents.research_agent.settings import ResearchConfig

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)

SEARCH_PAYLOAD = {
    "total_count": 3,
    "items": [
        {
            "full_name": "acme/silent-agent",
            "html_url": "https://github.com/acme/silent-agent",
            "description": "Detects silent agent failures",
            "stargazers_count": 420,
            "topics": ["llm-agent", "observability"],
            "fork": False,
        },
        {
            "full_name": "langfuse/langfuse",
            "html_url": "https://github.com/langfuse/langfuse",
            "description": "OSS LLM observability",
            "stargazers_count": 9000,
            "topics": ["llm-agent"],
            "fork": False,
        },
        {
            "full_name": "skip/no-url",
            "html_url": "",
            "description": "broken row",
            "topics": ["ai-agents"],
        },
    ],
}


def test_github_search_url_topics_recency_and_no_forks():
    url = github_search_url(
        ("llm-agent", "multi-agent"),
        since_date="2026-08-10",
        per_page=25,
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.path == "/search/repositories"
    q = query["q"][0]
    assert "topic:llm-agent" in q
    assert "topic:multi-agent" in q
    assert "pushed:>2026-08-10" in q
    assert "fork:false" in q
    assert query["sort"] == ["stars"]
    assert query["per_page"] == ["25"]


def test_parse_github_search_ignores_non_dicts():
    rows = parse_github_search(SEARCH_PAYLOAD)
    assert len(rows) == 3
    assert parse_github_search({"items": "nope"}) == []
    assert parse_github_search(None) == []


def test_fetch_github_repos_skips_competitor_tracked_full_names():
    urls: list[str] = []

    def fetch(url: str):
        urls.append(url)
        return SEARCH_PAYLOAD

    spec = ResearchConfig(
        github_lookback_days=7,
        github_topics=("llm-agent",),
        skip_github_repos=frozenset({"langfuse/langfuse", "helicone/helicone"}),
    )
    repos = fetch_github_repos(spec, fetch_fn=fetch, now_fn=lambda: NOW)
    assert [row["full_name"] for row in repos] == ["acme/silent-agent"]
    q = parse_qs(urlparse(urls[0]).query)["q"][0]
    assert "pushed:>2026-08-10" in q

    items = repos_to_source_items(repos, excerpt_max_chars=200)
    assert items[0].source_type == SOURCE_TYPE_GITHUB
    assert items[0].source_url == "https://github.com/acme/silent-agent"
    assert items[0].title.startswith("acme/silent-agent")
    assert "llm-agent" in items[0].content_excerpt
