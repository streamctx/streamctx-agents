"""Unit tests for auto-tier adapters and the Reddit rate limiter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from agents.marketing_agent.adapters.base import PublishError
from agents.marketing_agent.adapters.devto import DEVTO_ARTICLES_URL, DevToAdapter
from agents.marketing_agent.adapters.rate_limit import (
    RedditKarma,
    RedditKarmaGateError,
    RedditRateLimitError,
    RedditRateLimiter,
)
from agents.marketing_agent.adapters.reddit import (
    REDDIT_COMMENT_URL,
    REDDIT_ME_URL,
    REDDIT_SUBMIT_URL,
    RedditAdapter,
    RedditCredentials,
    parse_reddit_target,
)
from agents.marketing_agent.adapters.twitter import (
    TWITTER_TWEETS_URL,
    TwitterAdapter,
    TwitterCredentials,
)
from agents.marketing_agent.http import JsonHttpError
from agents.marketing_agent.models import Story
from agents.marketing_agent.pending_approval import (
    CONTENT_COMMENT,
    MODE_AUTO_AFTER_APPROVAL,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    PendingApprovalStore,
)
from agents.marketing_agent.sources import parse_changelog
from agents.marketing_agent.story import generate_story

FIXTURE_CHANGELOG = Path(__file__).resolve().parent / "fixtures" / "marketing" / "CHANGELOG.md"


class FakeHttp:
    def __init__(self, mapping: Optional[dict[tuple[str, str], Any]] = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.mapping = mapping or {}

    def post_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        body = kwargs.get("json_body")
        if body is None:
            body = kwargs.get("form")
        self.calls.append(("POST", url, body))
        return self._resolve("POST", url)

    def get_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("GET", url, None))
        return self._resolve("GET", url)

    def _resolve(self, method: str, url: str) -> dict[str, Any]:
        value = self.mapping.get((method, url))
        if value is None:
            raise AssertionError(f"unexpected {method} {url}; calls={self.calls}")
        if isinstance(value, Exception):
            raise value
        return dict(value)

    def posted_urls(self) -> list[str]:
        return [url for method, url, _ in self.calls if method == "POST"]


@pytest.fixture
def store(tmp_path):
    db = PendingApprovalStore(db_path=tmp_path / "marketing_agent.db")
    yield db
    db.close()


@pytest.fixture
def story() -> Story:
    return generate_story(parse_changelog(FIXTURE_CHANGELOG)[0])


def _twitter(store, http: FakeHttp) -> TwitterAdapter:
    return TwitterAdapter(
        store,
        http=http,
        credentials=TwitterCredentials(bearer_token="test-bearer"),
    )


def _devto(store, http: FakeHttp) -> DevToAdapter:
    return DevToAdapter(store, http=http, api_key="test-devto")


def _reddit(store, http: FakeHttp, limiter: RedditRateLimiter) -> RedditAdapter:
    return RedditAdapter(
        store,
        http=http,
        credentials=RedditCredentials(
            client_id="id",
            client_secret="secret",
            username="user",
            password="pass",
        ),
        limiter=limiter,
        access_token="test-reddit-token",
    )


def test_twitter_submit_is_auto_after_approval_pending(store, story):
    http = FakeHttp()
    adapter = _twitter(store, http)
    entry_id = adapter.queue(story)
    row = store.get_entry(entry_id)
    assert row is not None
    assert row.platform == "twitter"
    assert row.mode == MODE_AUTO_AFTER_APPROVAL
    assert row.status == STATUS_PENDING
    assert len(adapter.format(story)) <= 280
    assert http.calls == []


def test_twitter_publish_refuses_until_approved(store, story):
    http = FakeHttp({("POST", TWITTER_TWEETS_URL): {"data": {"id": "1"}}})
    adapter = _twitter(store, http)
    entry_id = adapter.queue(story)

    with pytest.raises(PublishError, match="requires approved"):
        adapter.publish(entry_id)
    assert http.calls == []
    assert store.get_entry(entry_id).status == STATUS_PENDING

    store.reject(entry_id)
    with pytest.raises(PublishError, match="requires approved"):
        adapter.publish(entry_id)
    assert http.calls == []


def test_twitter_publish_after_approval_marks_published(store, story):
    http = FakeHttp({("POST", TWITTER_TWEETS_URL): {"data": {"id": "12345"}}})
    adapter = _twitter(store, http)
    entry_id = adapter.queue(story)
    store.approve(entry_id)

    published = adapter.publish(entry_id)
    assert published.status == STATUS_PUBLISHED
    assert published.published_at is not None
    assert http.posted_urls() == [TWITTER_TWEETS_URL]
    assert http.calls[0][2]["text"] == store.get_entry(entry_id).content

    with pytest.raises(PublishError, match="already published"):
        adapter.publish(entry_id)
    assert http.posted_urls() == [TWITTER_TWEETS_URL]


def test_twitter_api_failure_does_not_mark_published(store, story):
    http = FakeHttp(
        {("POST", TWITTER_TWEETS_URL): JsonHttpError(403, "forbidden", TWITTER_TWEETS_URL)}
    )
    adapter = _twitter(store, http)
    entry_id = adapter.queue(story)
    store.approve(entry_id)
    with pytest.raises(PublishError, match="Twitter API failed"):
        adapter.publish(entry_id)
    row = store.get_entry(entry_id)
    assert row.status == STATUS_APPROVED
    assert row.published_at is None


def test_devto_publish_after_approval(store, story):
    http = FakeHttp(
        {("POST", DEVTO_ARTICLES_URL): {"id": 99, "url": "https://dev.to/x/y"}}
    )
    adapter = _devto(store, http)
    entry_id = adapter.queue(story)
    assert store.get_entry(entry_id).mode == MODE_AUTO_AFTER_APPROVAL
    content = adapter.format(story)
    assert content.startswith("# ")
    assert "What changed" in content

    with pytest.raises(PublishError, match="requires approved"):
        adapter.publish(entry_id)

    store.approve(entry_id)
    published = adapter.publish(entry_id)
    assert published.status == STATUS_PUBLISHED
    body = http.calls[0][2]
    assert body["article"]["published"] is True
    assert body["article"]["body_markdown"] == store.get_entry(entry_id).content


def test_reddit_rate_limiter_interval_and_karma(tmp_path):
    clock = {"now": datetime(2026, 8, 17, tzinfo=timezone.utc)}
    limiter = RedditRateLimiter(
        tmp_path / "limits.db",
        interval_seconds=3600,
        min_link_karma=50,
        min_comment_karma=50,
        now_fn=lambda: clock["now"],
    )
    ok = RedditKarma(link_karma=80, comment_karma=90)
    limiter.assert_can_post("python", ok)
    limiter.record_post("r/python")

    with pytest.raises(RedditRateLimitError, match="r/python"):
        limiter.assert_can_post("Python", ok)
    limiter.assert_can_post("MachineLearning", ok)

    clock["now"] = clock["now"] + timedelta(hours=1)
    limiter.assert_can_post("python", ok)

    with pytest.raises(RedditKarmaGateError, match="link_karma"):
        limiter.assert_can_post("python", RedditKarma(link_karma=1, comment_karma=90))
    limiter.close()


def test_parse_reddit_target():
    assert parse_reddit_target("r/python", "post") == ("python", None)
    sub, thing = parse_reddit_target(
        "https://www.reddit.com/r/python/comments/abc123/title/", "comment"
    )
    assert sub == "python"
    assert thing == "t3_abc123"
    with pytest.raises(PublishError):
        parse_reddit_target(None, "post")


def test_reddit_publish_respects_approval_karma_and_interval(store, story):
    http = FakeHttp(
        {
            ("GET", REDDIT_ME_URL): {"link_karma": 120, "comment_karma": 200},
            ("POST", REDDIT_SUBMIT_URL): {"json": {"errors": [], "data": {"id": "abc"}}},
        }
    )
    clock = {"now": datetime(2026, 8, 17, tzinfo=timezone.utc)}
    limiter = RedditRateLimiter(
        store.db_path,
        interval_seconds=86_400,
        min_link_karma=50,
        min_comment_karma=50,
        now_fn=lambda: clock["now"],
    )
    adapter = _reddit(store, http, limiter)

    first = adapter.queue(story, target="r/python")
    assert store.get_entry(first).mode == MODE_AUTO_AFTER_APPROVAL
    with pytest.raises(PublishError, match="requires approved"):
        adapter.publish(first)
    assert REDDIT_SUBMIT_URL not in http.posted_urls()

    store.approve(first)
    published = adapter.publish(first)
    assert published.status == STATUS_PUBLISHED
    assert REDDIT_SUBMIT_URL in http.posted_urls()

    with pytest.raises(RedditRateLimitError):
        adapter.queue(story, target="python")

    other = adapter.queue(story, target="r/MachineLearning")
    store.approve(other)
    adapter.publish(other)
    assert http.posted_urls().count(REDDIT_SUBMIT_URL) == 2
    limiter.close()


def test_reddit_karma_gate_blocks_even_when_approved(store, story):
    http = FakeHttp(
        {
            ("GET", REDDIT_ME_URL): {"link_karma": 2, "comment_karma": 2},
            ("POST", REDDIT_SUBMIT_URL): {"json": {"errors": [], "data": {"id": "nope"}}},
        }
    )
    limiter = RedditRateLimiter(
        store.db_path, interval_seconds=60, min_link_karma=50, min_comment_karma=50
    )
    adapter = _reddit(store, http, limiter)
    entry_id = adapter.queue(story, target="python")
    store.approve(entry_id)
    with pytest.raises(RedditKarmaGateError):
        adapter.publish(entry_id)
    assert REDDIT_SUBMIT_URL not in http.posted_urls()
    assert store.get_entry(entry_id).status == STATUS_APPROVED
    limiter.close()


def test_reddit_comment_uses_permalink_thing_id(store, story):
    http = FakeHttp(
        {
            ("GET", REDDIT_ME_URL): {"link_karma": 80, "comment_karma": 80},
            ("POST", REDDIT_COMMENT_URL): {"json": {"errors": []}},
        }
    )
    limiter = RedditRateLimiter(store.db_path, interval_seconds=60)
    adapter = RedditAdapter(
        store,
        content_type=CONTENT_COMMENT,
        http=http,
        limiter=limiter,
        access_token="tok",
        credentials=RedditCredentials(
            client_id="id", client_secret="s", username="u", password="p"
        ),
    )
    target = "https://www.reddit.com/r/python/comments/xyz789/context/"
    entry_id = adapter.queue(story, target=target)
    store.approve(entry_id)
    adapter.publish(entry_id)
    form = http.calls[-1][2]
    assert form["thing_id"] == "t3_xyz789"
    limiter.close()
