"""Unit tests for Stage 1 poll orchestrator: persist, dedupe, min-interval."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agents.research_agent.models import SOURCE_TYPE_ARXIV, SOURCE_TYPE_GITHUB, STATUS_NEW
from agents.research_agent.poll import persist_items, poll_arxiv, poll_github, run_poll
from agents.research_agent.settings import ResearchConfig
from agents.research_agent.storage import ResearchStore

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)

ARXIV_ATOM = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.11111v1</id>
    <title>Agent reliability with checkpoint/resume</title>
    <published>2026-08-16T00:00:00Z</published>
    <summary>Multi-agent failure attribution after a poisoned tool result.</summary>
    <link href="http://arxiv.org/abs/2401.11111v1" rel="alternate" type="text/html"/>
  </entry>
</feed>
"""

GITHUB_PAYLOAD = {
    "items": [
        {
            "full_name": "acme/silent-agent",
            "html_url": "https://github.com/acme/silent-agent",
            "description": "Detects silent agent failures",
            "topics": ["llm-agent"],
        }
    ]
}


@pytest.fixture
def store(tmp_path):
    db = ResearchStore(db_path=tmp_path / "research_agent.db")
    yield db
    db.close()


@pytest.fixture
def config() -> ResearchConfig:
    return ResearchConfig(
        poll_delay_seconds=0,
        arxiv_min_interval_seconds=21600,
        github_min_interval_seconds=21600,
        arxiv_categories=("cs.AI",),
        arxiv_keywords=("agent reliability", "multi-agent"),
        github_topics=("llm-agent",),
        github_lookback_days=7,
        skip_github_repos=frozenset(),
    )


def test_persist_items_inserts_new_then_skips_duplicates(store):
    from agents.research_agent.models import SourceItem

    items = [
        SourceItem(
            source_url="https://arxiv.org/abs/2401.11111",
            source_type=SOURCE_TYPE_ARXIV,
            title="Agent reliability with checkpoint/resume",
            content_excerpt="abstract",
        )
    ]
    inserted, skipped = persist_items(store, items, detected_at=NOW.isoformat())
    assert len(inserted) == 1
    assert skipped == ()
    assert inserted[0].status == STATUS_NEW
    assert inserted[0].feasibility_score is None
    again, skipped_again = persist_items(store, items, detected_at=NOW.isoformat())
    assert again == ()
    assert skipped_again == ("https://arxiv.org/abs/2401.11111",)
    assert len(store.list_ideas()) == 1


def test_poll_arxiv_writes_ideas_and_honors_min_interval(store, config):
    fetches = {"n": 0}

    def fetch(_url: str) -> str:
        fetches["n"] += 1
        return ARXIV_ATOM

    first = poll_arxiv(
        store,
        config=config,
        fetch_fn=fetch,
        now_fn=lambda: NOW,
    )
    assert len(first.inserted) == 1
    assert first.inserted[0].source_url == "https://arxiv.org/abs/2401.11111"
    assert first.skipped_interval == ()
    assert fetches["n"] == 1

    second = poll_arxiv(
        store,
        config=config,
        fetch_fn=fetch,
        now_fn=lambda: NOW + timedelta(hours=1),
    )
    assert second.inserted == ()
    assert second.skipped_interval == (SOURCE_TYPE_ARXIV,)
    assert fetches["n"] == 1

    third = poll_arxiv(
        store,
        config=config,
        fetch_fn=fetch,
        now_fn=lambda: NOW + timedelta(hours=7),
    )
    assert third.skipped_interval == ()
    assert third.skipped_duplicate == ("https://arxiv.org/abs/2401.11111",)
    assert fetches["n"] == 2


def test_poll_github_skips_interval_and_records_errors(store, config):
    def fetch(_url: str):
        return GITHUB_PAYLOAD

    first = poll_github(store, config=config, fetch_fn=fetch, now_fn=lambda: NOW)
    assert len(first.inserted) == 1
    assert first.inserted[0].source_type == SOURCE_TYPE_GITHUB

    def boom(_url: str):
        raise RuntimeError("rate limited")

    # still inside the 6h window — must not call fetch
    skipped = poll_github(store, config=config, fetch_fn=boom, now_fn=lambda: NOW + timedelta(minutes=10))
    assert skipped.skipped_interval == (SOURCE_TYPE_GITHUB,)
    assert skipped.errors == ()


def test_run_poll_hits_both_sources_and_paces_between_them(store, config):
    sleeps: list[float] = []
    spec = ResearchConfig(
        poll_delay_seconds=3.0,
        arxiv_min_interval_seconds=0,
        github_min_interval_seconds=0,
        arxiv_categories=("cs.AI",),
        arxiv_keywords=("agent reliability",),
        github_topics=("llm-agent",),
        skip_github_repos=frozenset(),
    )
    result = run_poll(
        store,
        config=spec,
        fetch_arxiv_fn=lambda _url: ARXIV_ATOM,
        fetch_github_fn=lambda _url: GITHUB_PAYLOAD,
        sleep_fn=sleeps.append,
        now_fn=lambda: NOW,
    )
    assert {idea.source_type for idea in result.inserted} == {
        SOURCE_TYPE_ARXIV,
        SOURCE_TYPE_GITHUB,
    }
    assert sleeps == [3.0]
    assert result.errors == ()


def test_run_poll_arxiv_only_does_not_touch_github(store, config):
    github_calls = {"n": 0}

    def github_fetch(_url: str):
        github_calls["n"] += 1
        return GITHUB_PAYLOAD

    result = run_poll(
        store,
        config=config,
        arxiv=True,
        github=False,
        fetch_arxiv_fn=lambda _url: ARXIV_ATOM,
        fetch_github_fn=github_fetch,
        now_fn=lambda: NOW,
    )
    assert github_calls["n"] == 0
    assert all(idea.source_type == SOURCE_TYPE_ARXIV for idea in result.inserted)
