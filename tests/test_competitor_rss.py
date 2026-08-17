"""Unit tests for Stage 3: RSS/Atom changelog polling."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.competitor_agent.models import (
    SIGNAL_TYPE_NEW_POST,
    SNAPSHOT_TYPE_CHANGELOG,
)
from agents.competitor_agent.rss import parse_feed, serialize_feed_entries, summarize_entry
from agents.competitor_agent.settings import CompetitorConfig
from agents.competitor_agent.snapshot import poll_rss, run_snapshot_poll
from agents.competitor_agent.storage import CompetitorStore

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
SHIPPED_CONFIG = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "competitor_agent"
    / "competitors.json"
)

RSS_V1 = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Blog</title>
    <item>
      <guid>https://example.test/p/one</guid>
      <title>Tracing UI refresh</title>
      <link>https://example.test/p/one</link>
      <pubDate>Fri, 01 Aug 2026 00:00:00 GMT</pubDate>
      <description>&lt;p&gt;We shipped a tracing UI refresh.&lt;/p&gt;</description>
    </item>
  </channel>
</rss>
"""

RSS_V2 = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Example Blog</title>
    <item>
      <guid>https://example.test/p/one</guid>
      <title>Tracing UI refresh</title>
      <link>https://example.test/p/one</link>
      <pubDate>Fri, 01 Aug 2026 00:00:00 GMT</pubDate>
      <description>We shipped a tracing UI refresh.</description>
    </item>
    <item>
      <guid>https://example.test/p/two</guid>
      <title>Evals in CI</title>
      <link>https://example.test/p/two</link>
      <pubDate>Sun, 16 Aug 2026 12:00:00 GMT</pubDate>
      <content:encoded>&lt;p&gt;Run dataset experiments from GitHub Actions.&lt;/p&gt;</content:encoded>
    </item>
    <item>
      <guid>https://example.test/p/three</guid>
      <title>Cheaper self-host</title>
      <link>https://example.test/p/three</link>
      <pubDate>Mon, 17 Aug 2026 08:00:00 GMT</pubDate>
      <description>ClickHouse storage costs dropped on the hobby plan.</description>
    </item>
  </channel>
</rss>
"""

ATOM = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <id>tag:example.test,2026:four</id>
    <title>Launch week recap</title>
    <link rel="alternate" href="https://example.test/p/four"/>
    <updated>2026-08-10T00:00:00Z</updated>
    <summary>Five drops aimed at production evals.</summary>
  </entry>
</feed>
"""


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
                "poll_delay_seconds": 0.25,
                "pricing_min_interval_seconds": 0,
                "github_min_interval_seconds": 0,
                "rss_min_interval_seconds": 0,
                "mentions_enabled": False,
                "competitors": [
                    {
                        "name": "LangSmith",
                        "rss_url": "https://example.test/langsmith/rss.xml",
                    },
                    {
                        "name": "Braintrust",
                        "pricing_url": "https://example.test/braintrust/pricing",
                    },
                    {
                        "name": "Helicone",
                        "rss_url": "https://example.test/helicone/feed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return CompetitorConfig.load(path)


def test_parse_rss_strips_html_and_normalizes_pubdate():
    entries = parse_feed(RSS_V1)
    assert len(entries) == 1
    assert entries[0].entry_id == "https://example.test/p/one"
    assert entries[0].title == "Tracing UI refresh"
    assert entries[0].summary == "We shipped a tracing UI refresh."
    assert entries[0].published.startswith("2026-08-01")


def test_parse_atom_uses_id_and_alternate_link():
    entries = parse_feed(ATOM)
    assert len(entries) == 1
    assert entries[0].entry_id == "tag:example.test,2026:four"
    assert entries[0].link == "https://example.test/p/four"
    assert "production evals" in summarize_entry(entries[0])


def test_parse_feed_rejects_html():
    with pytest.raises(ValueError, match="unsupported feed root|invalid RSS/Atom"):
        parse_feed("<html><body>not a feed</body></html>")


def test_poll_rss_baselines_then_emits_one_signal_per_new_post(store, config):
    pages = [RSS_V1]
    langsmith = config.by_name("LangSmith")

    def fetch() -> str:
        return serialize_feed_entries(parse_feed(pages[0]))

    assert (
        poll_rss(
            langsmith,
            store=store,
            config=config,
            fetch_fn=fetch,
            now_fn=lambda: NOW,
        )
        == []
    )
    assert store.latest_snapshot("LangSmith", SNAPSHOT_TYPE_CHANGELOG) is not None
    assert store.list_signals() == []

    pages[0] = RSS_V2
    signals = poll_rss(
        langsmith,
        store=store,
        config=config,
        fetch_fn=fetch,
        now_fn=lambda: NOW + timedelta(hours=4),
    )
    assert [item.signal_type for item in signals] == [
        SIGNAL_TYPE_NEW_POST,
        SIGNAL_TYPE_NEW_POST,
    ]
    summaries = [item.summary for item in signals]
    assert any("Evals in CI" in text for text in summaries)
    assert any("Cheaper self-host" in text for text in summaries)
    assert all("Tracing UI refresh" not in text for text in summaries)
    assert signals[0].source_url == "https://example.test/p/two"


def test_poll_rss_skips_when_inside_min_interval(store, tmp_path):
    path = tmp_path / "competitors.json"
    path.write_text(
        json.dumps(
            {
                "rss_min_interval_seconds": 10800,
                "mentions_enabled": False,
                "competitors": [
                    {"name": "LangSmith", "rss_url": "https://example.test/rss.xml"}
                ],
            }
        ),
        encoding="utf-8",
    )
    spec = CompetitorConfig.load(path)
    fetches = {"n": 0}

    def fetch() -> str:
        fetches["n"] += 1
        return serialize_feed_entries(parse_feed(RSS_V1))

    poll_rss(
        spec.by_name("LangSmith"),
        store=store,
        config=spec,
        fetch_fn=fetch,
        now_fn=lambda: NOW,
    )
    again = poll_rss(
        spec.by_name("LangSmith"),
        store=store,
        config=spec,
        fetch_fn=fetch,
        now_fn=lambda: NOW + timedelta(minutes=10),
    )
    assert again == []
    assert fetches["n"] == 1


def test_run_snapshot_poll_only_hits_configured_rss_feeds(store, config, monkeypatch):
    fetches: list[str] = []

    def fake_rss(item, **kwargs):
        fetches.append(item.name)
        return poll_rss(
            item,
            store=kwargs["store"],
            config=kwargs["config"],
            fetch_fn=lambda: serialize_feed_entries(parse_feed(RSS_V1)),
            now_fn=kwargs["now_fn"],
        )

    monkeypatch.setattr("agents.competitor_agent.snapshot.poll_rss", fake_rss)
    monkeypatch.setattr(
        "agents.competitor_agent.snapshot.poll_pricing",
        lambda *args, **kwargs: None,
    )
    result = run_snapshot_poll(
        store, config=config, sleep_fn=lambda _: None, now_fn=lambda: NOW
    )
    assert fetches == ["LangSmith", "Helicone"]
    assert result.errors == ()
    assert "Braintrust" not in fetches


def test_rss_url_on_seventh_competitor_needs_no_code_change(tmp_path):
    shipped = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    shipped["competitors"].append(
        {
            "name": "Phoenix",
            "rss_url": "https://example.test/phoenix/rss.xml",
        }
    )
    path = tmp_path / "competitors.json"
    path.write_text(json.dumps(shipped), encoding="utf-8")
    spec = CompetitorConfig.load(path)
    assert spec.by_name("Phoenix").rss_url == "https://example.test/phoenix/rss.xml"
    assert "Phoenix" in [item.name for item in spec.with_rss()]
