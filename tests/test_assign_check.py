"""Directed Roster Assign checks for competitor_agent."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agents.competitor_agent.assign_check import (
    REASON_BASELINE,
    REASON_CHANGED,
    REASON_NO_URL,
    REASON_UNCHANGED,
    REASON_UNKNOWN,
    DirectedCheckError,
    parse_directed_brief,
    run_directed_check,
)
from agents.competitor_agent.models import SNAPSHOT_TYPE_PRICING
from agents.competitor_agent.settings import CompetitorConfig
from agents.competitor_agent.snapshot import content_hash, normalize_html
from agents.competitor_agent.storage import CompetitorStore

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


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
                "pricing_min_interval_seconds": 86400,
                "github_min_interval_seconds": 86400,
                "rss_min_interval_seconds": 86400,
                "mentions_enabled": False,
                "competitors": [
                    {
                        "name": "LangSmith",
                        "pricing_url": "https://example.test/langsmith/pricing",
                        "github_repo": None,
                        "rss_url": "https://example.test/langsmith/rss.xml",
                    },
                    {
                        "name": "Langfuse",
                        "pricing_url": "https://example.test/langfuse/pricing",
                        "github_repo": "langfuse/langfuse",
                    },
                    {
                        "name": "Helicone",
                        "pricing_url": None,
                        "github_repo": "Helicone/helicone",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return CompetitorConfig.load(path)


def test_parse_defaults_to_pricing():
    spec = CompetitorConfig.load()
    parsed = parse_directed_brief("check if LangSmith changed their page", spec)
    assert parsed.competitor.name == "LangSmith"
    assert parsed.kind == "pricing"


def test_parse_kind_from_brief(config):
    assert parse_directed_brief("Langfuse github releases", config).kind == "github"
    assert parse_directed_brief("LangSmith blog / rss", config).kind == "rss"
    assert parse_directed_brief("LangSmith pricing page", config).kind == "pricing"


def test_parse_unknown_competitor(config):
    with pytest.raises(DirectedCheckError, match="unknown competitor") as exc:
        parse_directed_brief("check if CompetitorX changed pricing", config)
    assert exc.value.reason == REASON_UNKNOWN


def test_parse_no_url_in_config(config):
    with pytest.raises(DirectedCheckError, match="no URL in config") as exc:
        parse_directed_brief("check Helicone pricing", config)
    assert exc.value.reason == REASON_NO_URL


def test_directed_check_baselines_then_reports_unchanged_and_changed(store, config):
    pages = ["<p>Hobby free</p>"]

    def fetch() -> str:
        return normalize_html(pages[0])

    first = run_directed_check(
        "check LangSmith pricing",
        store=store,
        config=config,
        fetch_fn=fetch,
        now_fn=lambda: NOW,
    )
    assert first.reason == REASON_BASELINE
    assert store.latest_snapshot("LangSmith", SNAPSHOT_TYPE_PRICING) is not None
    assert store.list_signals() == []

    second = run_directed_check(
        "check LangSmith pricing",
        store=store,
        config=config,
        fetch_fn=fetch,
        now_fn=lambda: NOW + timedelta(minutes=5),
    )
    assert second.reason == REASON_UNCHANGED
    assert "no change since" in second.message
    assert store.list_signals() == []

    pages[0] = "<p>Hobby free</p><p>Team $199</p>"
    third = run_directed_check(
        "check LangSmith pricing",
        store=store,
        config=config,
        fetch_fn=fetch,
        llm_fn=lambda prompt: "Added a Team $199/mo plan",
        now_fn=lambda: NOW + timedelta(minutes=10),
    )
    assert third.reason == REASON_CHANGED
    assert "Added a Team $199/mo plan" in third.message
    signals = store.list_signals()
    assert len(signals) == 1
    assert signals[0].competitor == "LangSmith"


def test_directed_check_bypasses_min_interval(store, config):
    page = normalize_html("<p>Hobby free</p>")
    store.insert_snapshot(
        competitor="LangSmith",
        snapshot_type=SNAPSHOT_TYPE_PRICING,
        content_hash=content_hash(page),
        raw_content=page,
        captured_at=NOW.isoformat(),
    )
    fetches = {"n": 0}

    def fetch() -> str:
        fetches["n"] += 1
        return page

    result = run_directed_check(
        "LangSmith pricing",
        store=store,
        config=config,
        fetch_fn=fetch,
        now_fn=lambda: NOW + timedelta(minutes=2),
    )
    assert fetches["n"] == 1
    assert result.reason == REASON_UNCHANGED


def test_directed_check_does_not_poll_other_competitors(store, config):
    seen: list[str] = []

    def fetch() -> str:
        seen.append("langsmith")
        return normalize_html("<p>Hobby</p>")

    run_directed_check(
        "check LangSmith pricing",
        store=store,
        config=config,
        fetch_fn=fetch,
        now_fn=lambda: NOW,
    )
    assert seen == ["langsmith"]
    assert store.latest_snapshot("Langfuse", SNAPSHOT_TYPE_PRICING) is None


def test_run_directed_check_unknown_does_not_write(store, config):
    result = run_directed_check(
        "check CompetitorX pricing",
        store=store,
        config=config,
        fetch_fn=lambda: "should not run",
    )
    assert result.ok is False
    assert result.reason == REASON_UNKNOWN
    assert store.list_signals() == []
