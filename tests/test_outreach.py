"""Unit tests for Stage 5 outreach (discover → score → draft_only DM)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from agents.marketing_agent.adapters.devto import DEVTO_ARTICLES_URL
from agents.marketing_agent.adapters.reddit import REDDIT_COMMENT_URL, REDDIT_SUBMIT_URL
from agents.marketing_agent.adapters.twitter import TWITTER_TWEETS_URL
from agents.marketing_agent.http import JsonHttpError
from agents.marketing_agent.models import PublicPost
from agents.marketing_agent.outreach import (
    BOILERPLATE,
    HN_SEARCH_URL,
    REDDIT_SEARCH_URL,
    TWITTER_SEARCH_URL,
    Outreach,
    OutreachRules,
    generate_outreach_draft,
    score_lead,
)
from agents.marketing_agent.pending_approval import (
    CONTENT_DM,
    MODE_DRAFT_ONLY,
    STATUS_PENDING,
    PendingApprovalStore,
)
from agents.marketing_agent.safety import DuplicateContentError, SafetyGate, SafetyRules

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "marketing"
HN_FIXTURE = json.loads((FIXTURES / "hn_search.json").read_text(encoding="utf-8"))
REDDIT_FIXTURE = json.loads((FIXTURES / "reddit_search.json").read_text(encoding="utf-8"))
SHIPPED_SAFETY = (
    Path(__file__).resolve().parents[1] / "agents" / "marketing_agent" / "safety_rules.json"
)
PUBLISH_URLS = {TWITTER_TWEETS_URL, DEVTO_ARTICLES_URL, REDDIT_SUBMIT_URL, REDDIT_COMMENT_URL}


class PrefixFakeHttp:
    """Match GET/POST by URL prefix so query strings can vary."""

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

    def methods(self) -> list[str]:
        return [method for method, _url in self.calls]

    def urls(self) -> list[str]:
        return [url for _method, url in self.calls]


@pytest.fixture
def store(tmp_path):
    db = PendingApprovalStore(db_path=tmp_path / "marketing_agent.db")
    yield db
    db.close()


@pytest.fixture
def rules() -> OutreachRules:
    return OutreachRules.load()


def _context_post() -> PublicPost:
    hit = HN_FIXTURE["hits"][0]
    return PublicPost(
        platform="hn",
        post_id=str(hit["objectID"]),
        url=f"https://news.ycombinator.com/item?id={hit['objectID']}",
        author=str(hit["author"]),
        title=str(hit["title"]),
        body=str(hit["comment_text"]),
    )


def _search_http(extra: Optional[dict[tuple[str, str], Any]] = None) -> PrefixFakeHttp:
    mapping: dict[tuple[str, str], Any] = {
        ("GET", HN_SEARCH_URL): HN_FIXTURE,
        ("GET", REDDIT_SEARCH_URL): REDDIT_FIXTURE,
    }
    if extra:
        mapping.update(extra)
    return PrefixFakeHttp(mapping)


def test_hn_and_reddit_fixtures_parse_into_posts(store, rules):
    http = _search_http()
    outreach = Outreach(store, http=http, rules=rules, twitter_bearer="")
    posts = outreach.discover()
    urls = {post.url for post in posts}
    assert "https://news.ycombinator.com/item?id=42424242" in urls
    assert "https://www.reddit.com/r/LocalLLaMA/comments/abc123/lost_context_mid_run/" in urls
    hn = next(p for p in posts if p.platform == "hn")
    assert "third tool call" in hn.body
    reddit = next(p for p in posts if p.platform == "reddit")
    assert "context got compressed" in reddit.body


def test_context_loss_scores_as_compression(rules):
    lead = score_lead(_context_post(), rules)
    assert lead.score >= rules.min_score
    assert lead.primary_feature == "compression"
    assert "context window" in lead.matched_terms or "dropped the earlier messages" in lead.matched_terms


def test_unrelated_post_scores_zero(rules):
    post = PublicPost(
        platform="reddit",
        post_id="kb",
        url="https://www.reddit.com/r/MechanicalKeyboards/comments/kb/",
        author="sam",
        title="Best clicky switch for office use?",
        body="I like tactile keyboards and want something quieter than blues.",
    )
    lead = score_lead(post, rules)
    assert lead.score == 0.0
    assert lead.matched_features == ()


def test_specific_feature_outscores_vague_multi_hit(rules):
    specific = score_lead(_context_post(), rules)
    vague = score_lead(
        PublicPost(
            platform="hn",
            post_id="1",
            url="https://news.ycombinator.com/item?id=1",
            author="x",
            title="Agents are messy",
            body=(
                "There is context loss, no observability, an agent failure, "
                "and I can't resume after a crash."
            ),
        ),
        rules,
    )
    assert specific.score > vague.score


def test_draft_quotes_unique_snippet_not_boilerplate(rules):
    lead = score_lead(_context_post(), rules)
    draft = generate_outreach_draft(lead)
    assert "third tool call" in draft
    assert "https://news.ycombinator.com/item?id=42424242" in draft
    lowered = draft.lower()
    for phrase in BOILERPLATE:
        assert phrase not in lowered


def test_run_queues_dm_draft_only_and_never_publishes(store, rules):
    http = _search_http()
    outreach = Outreach(store, http=http, rules=rules, twitter_bearer="")
    entry_ids = outreach.run()
    assert entry_ids
    rows = [store.get_entry(eid) for eid in entry_ids]
    assert all(row is not None for row in rows)
    assert all(row.content_type == CONTENT_DM for row in rows)
    assert all(row.mode == MODE_DRAFT_ONLY for row in rows)
    assert all(row.status == STATUS_PENDING for row in rows)
    assert all("third tool call" in row.content or "context got compressed" in row.content for row in rows)
    assert "POST" not in http.methods()
    for url in http.urls():
        assert not any(url.startswith(pub) for pub in PUBLISH_URLS)


def test_second_enqueue_same_target_is_skipped(store, rules):
    http = _search_http()
    outreach = Outreach(store, http=http, rules=rules, twitter_bearer="")
    lead = score_lead(_context_post(), rules)
    first = outreach.enqueue_draft(lead)
    second = outreach.enqueue_draft(lead)
    assert first
    assert second is None
    dms = [row for row in store.list_by_status("pending") if row.content_type == CONTENT_DM]
    assert len(dms) == 1


def test_duplicate_dm_prepare_raises(store):
    gate = SafetyGate(rules=SafetyRules.load(SHIPPED_SAFETY))
    store.create_entry(
        platform="hn",
        content_type=CONTENT_DM,
        content="draft",
        target="https://news.ycombinator.com/item?id=42424242",
        mode=MODE_DRAFT_ONLY,
    )
    with pytest.raises(DuplicateContentError, match="already queued"):
        gate.prepare(
            "another draft mentioning StreamCtx",
            platform="hn",
            store=store,
            content_type=CONTENT_DM,
            target="https://news.ycombinator.com/item?id=42424242",
        )


def test_twitter_skipped_without_bearer(store, rules):
    http = _search_http()
    outreach = Outreach(store, http=http, rules=rules, twitter_bearer="")
    outreach.discover()
    assert all(TWITTER_SEARCH_URL not in url for url in http.urls())


def test_twitter_search_used_when_bearer_present(store, rules):
    tweet = {
        "data": [
            {
                "id": "999",
                "text": "Lost context again — the context window ate my system prompt.",
                "author_id": "42",
            }
        ]
    }
    http = _search_http({("GET", TWITTER_SEARCH_URL): tweet})
    outreach = Outreach(store, http=http, rules=rules, twitter_bearer="test-bearer")
    posts = outreach.discover()
    assert any(p.platform == "twitter" and p.post_id == "999" for p in posts)
    assert any(url.startswith(TWITTER_SEARCH_URL) for url in http.urls())


def test_hn_dm_may_mention_streamctx(store, rules):
    """Public-post self-promo filter does not apply to human-reviewed DMs."""
    http = _search_http()
    outreach = Outreach(store, http=http, rules=rules, twitter_bearer="")
    lead = score_lead(_context_post(), rules)
    entry_id = outreach.enqueue_draft(lead)
    row = store.get_entry(entry_id)
    assert row is not None
    assert "StreamCtx" in row.content
    assert row.platform == "hn"
    assert row.content_type == CONTENT_DM


def test_source_http_error_does_not_abort_other_sources(store, rules):
    http = _search_http(
        {("GET", HN_SEARCH_URL): JsonHttpError(500, "down", HN_SEARCH_URL)}
    )
    outreach = Outreach(store, http=http, rules=rules, twitter_bearer="")
    posts = outreach.discover()
    assert any(p.platform == "reddit" for p in posts)
    assert not any(p.platform == "hn" for p in posts)
