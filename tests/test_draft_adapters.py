"""Unit tests for draft-tier marketing adapters."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agents.marketing_agent.adapters.hn import (
    HNInvalidTargetError,
    HNThreadLockedError,
    HNThreadUnreachableError,
    HackerNewsAdapter,
    canonical_hn_item_url,
    verify_hn_thread,
)
from agents.marketing_agent.adapters.base import DraftOnlyError
from agents.marketing_agent.safety import DuplicateContentError
from agents.marketing_agent.adapters.indiehackers import IndieHackersAdapter
from agents.marketing_agent.adapters.linkedin import LinkedInAdapter
from agents.marketing_agent.adapters.producthunt import ProductHuntAdapter
from agents.marketing_agent.models import Story
from agents.marketing_agent.pending_approval import (
    CONTENT_COMMENT,
    MODE_DRAFT_ONLY,
    STATUS_PENDING,
    PendingApprovalStore,
)
from agents.marketing_agent.sources import parse_changelog
from agents.marketing_agent.story import generate_story

FIXTURE_CHANGELOG = Path(__file__).resolve().parent / "fixtures" / "marketing" / "CHANGELOG.md"

HN_ITEM = "https://news.ycombinator.com/item?id=44400001"
OPEN_HTML = """
<html><body>
<a href="item?id=44400001">parent</a>
<a href="reply?id=44400001&amp;goto=item%3Fid%3D44400001"><font size="1">reply</font></a>
</body></html>
"""
LOCKED_HTML = """
<html><body>
<a href="item?id=44400001">parent</a>
<p>This post is too old to comment on.</p>
</body></html>
"""
HYPE_RE = (
    r"game-changing|revolutionary|please upvote|follow us|show hn:|"
    r"check out our|we just launched"
)


@pytest.fixture
def store(tmp_path):
    db = PendingApprovalStore(db_path=tmp_path / "marketing_agent.db")
    yield db
    db.close()


@pytest.fixture
def story() -> Story:
    return generate_story(parse_changelog(FIXTURE_CHANGELOG)[0])


def _open_fetch(_url: str) -> str:
    return OPEN_HTML


def _locked_fetch(_url: str) -> str:
    return LOCKED_HTML


def test_linkedin_format_and_submit_are_draft_only(store, story):
    adapter = LinkedInAdapter(store)
    content = adapter.format(story)
    assert "wrap()" in content or "0.4.4" in content
    assert "#5" in content or "changelog" in content.lower()
    assert adapter.mode == MODE_DRAFT_ONLY

    entry_id = adapter.submit(content)
    row = store.get_entry(entry_id)
    assert row is not None
    assert row.platform == "linkedin"
    assert row.content_type == "post"
    assert row.mode == MODE_DRAFT_ONLY
    assert row.status == STATUS_PENDING
    assert row.published_at is None
    assert row.content == content


def test_linkedin_publish_raises_draft_only(store, story):
    adapter = LinkedInAdapter(store)
    entry_id = adapter.queue(story)
    with pytest.raises(DraftOnlyError, match="draft_only"):
        adapter.publish(entry_id)
    row = store.get_entry(entry_id)
    assert row is not None
    assert row.status == STATUS_PENDING
    assert row.published_at is None


def test_indiehackers_and_producthunt_queue(store, story):
    ih = IndieHackersAdapter(store)
    ph = ProductHuntAdapter(store)
    ih_id = ih.queue(story)
    ih_row = store.get_entry(ih_id)
    assert ih_row is not None
    assert ih_row.platform == "indiehackers"
    assert "What changed:" in ih_row.content
    formatted = ph.format(story)
    assert formatted.splitlines()[0]
    assert "• " in formatted
    with pytest.raises(DuplicateContentError, match="Stagger"):
        ph.queue(story)


def test_hn_post_does_not_fetch_or_pitch(store, story):
    fetches: list[str] = []

    def fetch(url: str) -> str:
        fetches.append(url)
        return OPEN_HTML

    adapter = HackerNewsAdapter(store, fetch_page=fetch)
    content = adapter.format(story)
    assert fetches == []
    assert "Show HN" not in content
    assert re.search(HYPE_RE, content, re.I) is None
    entry_id = adapter.submit(content)
    row = store.get_entry(entry_id)
    assert row is not None
    assert row.platform == "hn"
    assert row.content_type == "post"
    assert row.target is None


def test_hn_comment_verifies_item_page_reply_link(store, story):
    adapter = HackerNewsAdapter(
        store,
        content_type=CONTENT_COMMENT,
        fetch_page=_open_fetch,
    )
    content = adapter.format(story, target=HN_ITEM)
    assert "wrap()" in content or "per-client" in content or "#5" in content
    assert re.search(HYPE_RE, content, re.I) is None

    entry_id = adapter.submit(content, target=HN_ITEM + "&foo=1")
    row = store.get_entry(entry_id)
    assert row is not None
    assert row.target == HN_ITEM
    assert row.content_type == "comment"
    assert row.mode == MODE_DRAFT_ONLY
    assert row.status == STATUS_PENDING


def test_hn_comment_rejects_algolia_without_fetching(store, story):
    fetches: list[str] = []

    def fetch(url: str) -> str:
        fetches.append(url)
        raise AssertionError("must not fetch Algolia URLs")

    adapter = HackerNewsAdapter(
        store, content_type=CONTENT_COMMENT, fetch_page=fetch
    )
    algolia = "https://hn.algolia.com/?q=streamctx&type=story"
    with pytest.raises(HNInvalidTargetError, match="Algolia"):
        adapter.format(story, target=algolia)
    assert fetches == []


def test_hn_comment_rejects_non_item_url(store, story):
    adapter = HackerNewsAdapter(
        store, content_type=CONTENT_COMMENT, fetch_page=_open_fetch
    )
    with pytest.raises(HNInvalidTargetError):
        adapter.format(story, target="https://news.ycombinator.com/newest")


def test_hn_comment_rejects_locked_thread(store, story):
    adapter = HackerNewsAdapter(
        store, content_type=CONTENT_COMMENT, fetch_page=_locked_fetch
    )
    with pytest.raises(HNThreadLockedError, match="reply link"):
        adapter.format(story, target=HN_ITEM)
    assert store.list_by_status(STATUS_PENDING) == []


def test_hn_comment_unreachable_does_not_queue(store, story):
    def fetch(_url: str) -> str:
        raise HNThreadUnreachableError("HTTP 404")

    adapter = HackerNewsAdapter(
        store, content_type=CONTENT_COMMENT, fetch_page=fetch
    )
    with pytest.raises(HNThreadUnreachableError):
        adapter.queue(story, target=HN_ITEM)
    assert store.list_by_status(STATUS_PENDING) == []


def test_canonical_hn_item_url():
    assert canonical_hn_item_url(HN_ITEM) == HN_ITEM
    assert (
        canonical_hn_item_url("https://www.news.ycombinator.com/item?id=99&x=1")
        == "https://news.ycombinator.com/item?id=99"
    )
    with pytest.raises(HNInvalidTargetError):
        canonical_hn_item_url(None)


def test_verify_hn_thread_requires_reply_href():
    url = verify_hn_thread(HN_ITEM, fetch_page=_open_fetch)
    assert url == HN_ITEM
    with pytest.raises(HNThreadLockedError):
        verify_hn_thread(HN_ITEM, fetch_page=_locked_fetch)


def test_changelog_story_is_staggered_across_platforms(store, story):
    """Same story cannot enter the queue on two platforms the same day."""
    linkedin = LinkedInAdapter(store)
    first = linkedin.queue(story)
    row = store.get_entry(first)
    assert row is not None
    assert row.platform == "linkedin"
    assert row.status == STATUS_PENDING
    assert row.source_fingerprint

    with pytest.raises(DuplicateContentError, match="linkedin"):
        IndieHackersAdapter(store).queue(story)
    with pytest.raises(DuplicateContentError):
        HackerNewsAdapter(
            store, content_type=CONTENT_COMMENT, fetch_page=_open_fetch
        ).queue(story, target=HN_ITEM)

    pending = store.list_by_status(STATUS_PENDING)
    assert len(pending) == 1
