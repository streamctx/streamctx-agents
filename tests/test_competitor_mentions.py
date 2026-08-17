"""Unit tests for Stage 4: HN / Product Hunt / Twitter mention tracking."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import pytest

from agents.competitor_agent.mentions import (
    PRODUCTHUNT_GRAPHQL_URL,
    is_notable,
    poll_mentions,
    search_producthunt,
)
from agents.competitor_agent.models import SIGNAL_TYPE_MENTION, SNAPSHOT_TYPE_MENTIONS
from agents.competitor_agent.settings import CompetitorConfig
from agents.competitor_agent.storage import CompetitorStore
from agents.marketing_agent.models import PublicPost
from agents.marketing_agent.outreach import HN_SEARCH_URL, TWITTER_SEARCH_URL

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


class PrefixFakeHttp:
    def __init__(self, mapping: Optional[dict[tuple[str, str], Any]] = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.mapping = mapping or {}

    def post_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("POST", url))
        return self._resolve("POST", url)

    def get_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("GET", url))
        return self._resolve("GET", url)

    def _resolve(self, method: str, url: str) -> dict[str, Any]:
        for (mapped_method, prefix), value in self.mapping.items():
            if mapped_method == method and url.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return dict(value)
        raise AssertionError(f"unexpected {method} {url}; calls={self.calls}")

    def urls(self) -> list[str]:
        return [url for _method, url in self.calls]


@pytest.fixture
def store(tmp_path):
    db = CompetitorStore(db_path=tmp_path / "competitor_agent.db")
    yield db
    db.close()


@pytest.fixture
def config(tmp_path) -> CompetitorConfig:
    path = tmp_path / "competitors.json"
    path.write_text(
        json.dumps(
            {
                "poll_delay_seconds": 0,
                "mention_min_interval_seconds": 0,
                "mention_lookback_days": 7,
                "hn_min_points": 40,
                "hn_min_comments": 20,
                "twitter_min_likes": 30,
                "producthunt_min_votes": 40,
                "competitors": [{"name": "Langfuse", "search_names": ["Langfuse"]}],
            }
        ),
        encoding="utf-8",
    )
    return CompetitorConfig.load(path)


def _hn_payload(*hits: dict[str, Any]) -> dict[str, Any]:
    return {"hits": list(hits)}


def test_is_notable_launch_beats_low_points(config):
    post = PublicPost(
        platform="hn",
        post_id="1",
        url="https://news.ycombinator.com/item?id=1",
        author="a",
        title="Show HN: Langfuse 3.0",
        body="We launched a new tracing UI.",
        points=8,
    )
    notable, reason = is_notable(post, config)
    assert notable is True
    assert reason == "launch"


def test_is_notable_high_engagement_without_launch_words(config):
    post = PublicPost(
        platform="hn",
        post_id="2",
        url="https://news.ycombinator.com/item?id=2",
        author="b",
        title="Anyone using Langfuse in prod?",
        body="Curious how people like it versus Helicone.",
        points=95,
        comment_count=40,
    )
    notable, reason = is_notable(post, config)
    assert notable is True
    assert reason == "engagement"


def test_low_engagement_generic_mention_is_ignored(config):
    post = PublicPost(
        platform="hn",
        post_id="3",
        url="https://news.ycombinator.com/item?id=3",
        author="c",
        title="Observability tools",
        body="I tried Langfuse last week.",
        points=3,
        comment_count=1,
    )
    notable, reason = is_notable(post, config)
    assert notable is False


def test_poll_mentions_reuses_hn_search_and_seven_day_filter(store, config):
    http = PrefixFakeHttp(
        {
            ("GET", HN_SEARCH_URL): _hn_payload(
                {
                    "objectID": "111",
                    "title": "Show HN: Langfuse ClickHouse tracing",
                    "story_text": "We launched Langfuse on Product Hunt today.",
                    "author": "marc",
                    "created_at": "2026-08-16T00:00:00.000Z",
                    "points": 12,
                    "num_comments": 4,
                },
                {
                    "objectID": "222",
                    "title": "Quiet Langfuse mention",
                    "story_text": "Langfuse is fine.",
                    "author": "x",
                    "points": 2,
                    "num_comments": 0,
                },
            )
        }
    )
    signals = poll_mentions(
        config.by_name("Langfuse"),
        store=store,
        config=config,
        http=http,
        twitter_bearer="",
        ph_token="",
        now_fn=lambda: NOW,
    )
    assert len(signals) == 1
    assert signals[0].signal_type == SIGNAL_TYPE_MENTION
    assert "Launch" in signals[0].summary
    assert signals[0].source_url == "https://news.ycombinator.com/item?id=111"
    hn_url = next(url for url in http.urls() if url.startswith(HN_SEARCH_URL))
    params = parse_qs(urlparse(hn_url).query)
    cutoff = int((NOW - timedelta(days=7)).timestamp())
    assert params["numericFilters"][0] == f"created_at_i>{cutoff}"
    assert params["query"][0] == '"Langfuse"'


def test_poll_mentions_skips_duplicate_urls(store, config):
    http = PrefixFakeHttp(
        {
            ("GET", HN_SEARCH_URL): _hn_payload(
                {
                    "objectID": "111",
                    "title": "Langfuse raised a Series A",
                    "author": "news",
                    "points": 10,
                }
            )
        }
    )
    first = poll_mentions(
        config.by_name("Langfuse"),
        store=store,
        config=config,
        http=http,
        twitter_bearer="",
        ph_token="",
        now_fn=lambda: NOW,
    )
    assert len(first) == 1
    second = poll_mentions(
        config.by_name("Langfuse"),
        store=store,
        config=config,
        http=http,
        twitter_bearer="",
        ph_token="",
        now_fn=lambda: NOW + timedelta(hours=1),
    )
    assert second == []
    assert len(store.list_signals(competitor="Langfuse", signal_type=SIGNAL_TYPE_MENTION)) == 1


def test_twitter_skipped_without_bearer_then_notable_with_likes(store, config):
    mapping = {
        ("GET", HN_SEARCH_URL): _hn_payload(),
        ("GET", TWITTER_SEARCH_URL): {
            "data": [
                {
                    "id": "99",
                    "text": "Langfuse just shipped GA tracing",
                    "author_id": "1",
                    "public_metrics": {"like_count": 80, "reply_count": 4},
                }
            ]
        },
    }
    http = PrefixFakeHttp(mapping)
    skipped = poll_mentions(
        config.by_name("Langfuse"),
        store=store,
        config=config,
        http=http,
        twitter_bearer="",
        ph_token="",
        now_fn=lambda: NOW,
    )
    assert skipped == []
    assert all(not url.startswith(TWITTER_SEARCH_URL) for url in http.urls())


def test_producthunt_graphql_matches_competitor_name(store, config):
    http = PrefixFakeHttp(
        {
            ("GET", HN_SEARCH_URL): _hn_payload(),
            ("POST", PRODUCTHUNT_GRAPHQL_URL): {"data": {"posts": {"edges": []}}},
        }
    )
    posts = [
        PublicPost(
            platform="producthunt",
            post_id="ph-1",
            url="https://www.producthunt.com/posts/langfuse",
            author="",
            title="Langfuse",
            body="Open-source LLM observability",
            points=120,
        ),
        PublicPost(
            platform="producthunt",
            post_id="ph-2",
            url="https://www.producthunt.com/posts/other",
            author="",
            title="OtherApp",
            body="Unrelated",
            points=900,
        ),
    ]
    signals = poll_mentions(
        config.by_name("Langfuse"),
        store=store,
        config=config,
        http=http,
        twitter_bearer="",
        ph_token="ph-token",
        now_fn=lambda: NOW,
        producthunt_posts=posts,
    )
    assert len(signals) == 1
    assert signals[0].source_url.endswith("/langfuse")


def test_search_producthunt_parses_graphql_edges():
    http = PrefixFakeHttp(
        {
            ("POST", PRODUCTHUNT_GRAPHQL_URL): {
                "data": {
                    "posts": {
                        "edges": [
                            {
                                "node": {
                                    "id": "ph-1",
                                    "name": "Langfuse",
                                    "tagline": "OSS observability",
                                    "votesCount": 50,
                                    "url": "https://www.producthunt.com/posts/langfuse",
                                }
                            }
                        ]
                    }
                }
            }
        }
    )
    posts = search_producthunt(http, token="ph-token", since="2026-08-10T00:00:00Z")
    assert posts[0].platform == "producthunt"
    assert posts[0].points == 50
    assert posts[0].title == "Langfuse"
