"""Unit tests for Stage 2: config-driven snapshot/diff (pricing + GitHub)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from agents.competitor_agent.http import FetchError, fetch_text
from agents.competitor_agent.models import (
    SIGNAL_TYPE_NEW_RELEASE,
    SIGNAL_TYPE_PRICING_CHANGE,
    SNAPSHOT_TYPE_GITHUB_RELEASE,
    SNAPSHOT_TYPE_PRICING,
)
from agents.competitor_agent.settings import CompetitorConfig
from agents.competitor_agent.snapshot import (
    capture_snapshot,
    content_hash,
    normalize_html,
    poll_github_releases,
    poll_pricing,
    run_snapshot_poll,
    serialize_releases,
    snapshot_and_diff,
)
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
                "poll_delay_seconds": 0.5,
                "pricing_min_interval_seconds": 0,
                "github_min_interval_seconds": 0,
                "max_retries": 3,
                "backoff_base_seconds": 1.0,
                "competitors": [
                    {
                        "name": "LangSmith",
                        "pricing_url": "https://example.test/langsmith/pricing",
                        "github_repo": None,
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


def test_shipped_config_lists_six_competitors_without_code_names():
    spec = CompetitorConfig.load()
    assert spec.names() == (
        "LangSmith",
        "Braintrust",
        "Langfuse",
        "Laminar",
        "Latitude",
        "Helicone",
    )
    assert spec.with_github() == (
        spec.by_name("Langfuse"),
        spec.by_name("Helicone"),
    )
    assert spec.with_rss() == (
        spec.by_name("LangSmith"),
        spec.by_name("Helicone"),
    )
    assert all(item.pricing_url for item in spec.with_pricing())


def test_seventh_competitor_is_a_config_change(tmp_path):
    shipped = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "agents"
            / "competitor_agent"
            / "competitors.json"
        ).read_text(encoding="utf-8")
    )
    shipped["competitors"].append(
        {
            "name": "Phoenix",
            "pricing_url": "https://example.test/phoenix/pricing",
            "github_repo": "Arize-ai/phoenix",
        }
    )
    path = tmp_path / "competitors.json"
    path.write_text(json.dumps(shipped), encoding="utf-8")
    spec = CompetitorConfig.load(path)
    assert "Phoenix" in spec.names()
    assert spec.by_name("Phoenix").github_repo == "Arize-ai/phoenix"


def test_snapshot_and_diff_first_fetch_is_baseline_only(store):
    pages = ["<html>hobby $0</html>"]

    signal = snapshot_and_diff(
        "LangSmith",
        SNAPSHOT_TYPE_PRICING,
        lambda: pages[0],
        store=store,
        summarize_fn=lambda old, new: (SIGNAL_TYPE_PRICING_CHANGE, "should not run", None),
        now_fn=lambda: NOW,
    )
    assert signal is None
    latest = store.latest_snapshot("LangSmith", SNAPSHOT_TYPE_PRICING)
    assert latest is not None
    assert latest.content_hash == content_hash(pages[0])
    assert store.list_signals() == []


def test_snapshot_and_diff_same_hash_does_not_write_signal_or_row(store):
    snapshot_and_diff(
        "LangSmith",
        SNAPSHOT_TYPE_PRICING,
        lambda: "tier-a",
        store=store,
        now_fn=lambda: NOW,
    )
    signal = snapshot_and_diff(
        "LangSmith",
        SNAPSHOT_TYPE_PRICING,
        lambda: "tier-a",
        store=store,
        summarize_fn=lambda old, new: (SIGNAL_TYPE_PRICING_CHANGE, "nope", None),
        now_fn=lambda: NOW + timedelta(hours=1),
    )
    assert signal is None
    assert len(store.list_snapshots(competitor="LangSmith")) == 1
    assert store.list_signals() == []


def test_snapshot_and_diff_emits_llm_summary_on_pricing_hash_change(store):
    snapshot_and_diff(
        "Braintrust",
        SNAPSHOT_TYPE_PRICING,
        lambda: "Starter $0",
        store=store,
        now_fn=lambda: NOW,
    )
    calls: list[tuple[str, str]] = []

    def summarize(old: str, new: str) -> tuple[str, str, str]:
        calls.append((old, new))
        return SIGNAL_TYPE_PRICING_CHANGE, "Added a new $99/mo tier", "https://example.test/pricing"

    signal = snapshot_and_diff(
        "Braintrust",
        SNAPSHOT_TYPE_PRICING,
        lambda: "Starter $0\nPro $99/mo",
        store=store,
        summarize_fn=summarize,
        now_fn=lambda: NOW + timedelta(days=1),
    )
    assert signal is not None
    assert signal.signal_type == SIGNAL_TYPE_PRICING_CHANGE
    assert signal.summary == "Added a new $99/mo tier"
    assert signal.source_url == "https://example.test/pricing"
    assert calls == [("Starter $0", "Starter $0\nPro $99/mo")]
    assert len(store.list_snapshots(competitor="Braintrust")) == 2


def test_min_interval_skips_fetch(store):
    fetches = {"n": 0}

    def fetch() -> str:
        fetches["n"] += 1
        return "page"

    capture_snapshot(
        "Laminar",
        SNAPSHOT_TYPE_PRICING,
        fetch,
        store=store,
        now_fn=lambda: NOW,
    )
    previous, current, skipped = capture_snapshot(
        "Laminar",
        SNAPSHOT_TYPE_PRICING,
        fetch,
        store=store,
        min_interval_seconds=3600,
        now_fn=lambda: NOW + timedelta(minutes=10),
    )
    assert skipped is True
    assert current is None
    assert previous == "page"
    assert fetches["n"] == 1


def test_poll_pricing_normalizes_html_and_uses_llm(store, config):
    html_v1 = "<html><script>track()</script><p>Hobby free</p></html>"
    html_v2 = "<html><script>track()</script><p>Hobby free</p><p>Team $199</p></html>"
    pages = [html_v1]
    llm_prompts: list[str] = []

    def fetch() -> str:
        return normalize_html(pages[0])

    first = poll_pricing(
        config.by_name("LangSmith"),
        store=store,
        config=config,
        fetch_fn=fetch,
        llm_fn=lambda prompt: llm_prompts.append(prompt) or "unused",
        now_fn=lambda: NOW,
    )
    assert first is None
    pages[0] = html_v2
    signal = poll_pricing(
        config.by_name("LangSmith"),
        store=store,
        config=config,
        fetch_fn=fetch,
        llm_fn=lambda prompt: llm_prompts.append(prompt) or "Added a Team $199/mo plan",
        now_fn=lambda: NOW + timedelta(hours=1),
    )
    assert signal is not None
    assert signal.summary == "Added a Team $199/mo plan"
    assert "Team $199" in llm_prompts[-1]
    latest = store.latest_snapshot("LangSmith", SNAPSHOT_TYPE_PRICING)
    assert latest is not None
    assert "track()" not in latest.raw_content


def test_poll_github_releases_baselines_then_emits_notes_without_llm(store, config):
    releases = [
        {
            "tag_name": "v1.0.0",
            "name": "v1.0.0",
            "body": "Initial release",
            "html_url": "https://github.com/langfuse/langfuse/releases/tag/v1.0.0",
            "published_at": "2026-07-01T00:00:00Z",
            "draft": False,
        }
    ]

    def fetch() -> str:
        return serialize_releases(releases)

    assert (
        poll_github_releases(
            config.by_name("Langfuse"),
            store=store,
            config=config,
            fetch_fn=fetch,
            now_fn=lambda: NOW,
        )
        == []
    )

    releases.append(
        {
            "tag_name": "v1.1.0",
            "name": "v1.1.0",
            "body": "Adds dataset experiments and cheaper self-host.",
            "html_url": "https://github.com/langfuse/langfuse/releases/tag/v1.1.0",
            "published_at": "2026-08-16T00:00:00Z",
            "draft": False,
        }
    )
    releases.append(
        {
            "tag_name": "v1.1.1",
            "name": "v1.1.1",
            "body": "Patch: fix ClickHouse migration.",
            "html_url": "https://github.com/langfuse/langfuse/releases/tag/v1.1.1",
            "published_at": "2026-08-17T00:00:00Z",
            "draft": False,
        }
    )

    signals = poll_github_releases(
        config.by_name("Langfuse"),
        store=store,
        config=config,
        fetch_fn=fetch,
        now_fn=lambda: NOW + timedelta(hours=2),
    )
    assert [item.summary for item in signals] == [
        "Adds dataset experiments and cheaper self-host.",
        "Patch: fix ClickHouse migration.",
    ]
    assert all(item.signal_type == SIGNAL_TYPE_NEW_RELEASE for item in signals)
    assert signals[0].source_url.endswith("/v1.1.0")


def test_poll_github_skips_drafts_and_unchanged_tag_sets(store, config):
    payload = [
        {
            "tag_name": "v2.0.0",
            "body": "real",
            "html_url": "https://github.com/Helicone/helicone/releases/tag/v2.0.0",
            "published_at": "2026-08-01T00:00:00Z",
            "draft": False,
        },
        {
            "tag_name": "v2.0.1-rc",
            "body": "should be ignored",
            "html_url": "https://github.com/Helicone/helicone/releases/tag/v2.0.1-rc",
            "published_at": "2026-08-02T00:00:00Z",
            "draft": True,
        },
    ]
    poll_github_releases(
        config.by_name("Helicone"),
        store=store,
        config=config,
        fetch_fn=lambda: serialize_releases(payload),
        now_fn=lambda: NOW,
    )
    again = poll_github_releases(
        config.by_name("Helicone"),
        store=store,
        config=config,
        fetch_fn=lambda: serialize_releases(payload),
        now_fn=lambda: NOW + timedelta(hours=3),
    )
    assert again == []
    latest = store.latest_snapshot("Helicone", SNAPSHOT_TYPE_GITHUB_RELEASE)
    assert latest is not None
    assert "v2.0.0" in latest.raw_content
    assert "v2.0.1-rc" not in latest.raw_content


def test_run_snapshot_poll_paces_fetches_and_collects_errors(store, config, monkeypatch):
    sleeps: list[float] = []
    fetches: list[str] = []

    def fake_pricing(item, **kwargs):
        fetches.append(f"pricing:{item.name}")
        if item.name == "Langfuse":
            raise FetchError(500, "boom", item.pricing_url or "")
        return poll_pricing(
            item,
            store=kwargs["store"],
            config=kwargs["config"],
            fetch_fn=lambda: f"pricing-body-{item.name}",
            llm_fn=lambda prompt: "unused",
            now_fn=kwargs["now_fn"],
        )

    def fake_github(item, **kwargs):
        fetches.append(f"github:{item.name}")
        return poll_github_releases(
            item,
            store=kwargs["store"],
            config=kwargs["config"],
            fetch_fn=lambda: serialize_releases([]),
            now_fn=kwargs["now_fn"],
        )

    monkeypatch.setattr("agents.competitor_agent.snapshot.poll_pricing", fake_pricing)
    monkeypatch.setattr("agents.competitor_agent.snapshot.poll_github_releases", fake_github)
    result = run_snapshot_poll(
        store,
        config=config,
        sleep_fn=sleeps.append,
        now_fn=lambda: NOW,
    )
    assert fetches == [
        "pricing:LangSmith",
        "pricing:Langfuse",
        "github:Langfuse",
        "github:Helicone",
    ]
    assert sleeps == [0.5, 0.5, 0.5]
    assert result.errors[0][0] == "Langfuse:pricing"
    assert result.signals == ()


def test_fetch_text_retries_on_429_then_succeeds(monkeypatch):
    attempts = {"n": 0}
    sleeps: list[float] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args: Any):
            return False

        def read(self) -> bytes:
            return b"ok"

    def fake_urlopen(request, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise HTTPError(
                "https://example.test/pricing",
                429,
                "rate limited",
                {"Retry-After": "2"},
                BytesIO(b"slow down"),
            )
        return _Resp()

    monkeypatch.setattr("agents.competitor_agent.http.urlopen", fake_urlopen)
    body = fetch_text(
        "https://example.test/pricing",
        max_retries=3,
        backoff_base_seconds=1.0,
        sleep_fn=sleeps.append,
    )
    assert body == "ok"
    assert attempts["n"] == 3
    assert sleeps == [2.0, 2.0]


def test_normalize_html_strips_scripts_for_stable_hashes():
    a = normalize_html("<p>Pro $39</p><script>nonce=1</script>")
    b = normalize_html("<p>Pro $39</p><script>nonce=2</script>")
    assert a == b == "Pro $39"
    assert content_hash(a) == content_hash(b)
