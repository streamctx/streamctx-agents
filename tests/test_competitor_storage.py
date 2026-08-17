"""Unit tests for competitor_agent storage schema and CRUD."""

from __future__ import annotations

import sqlite3

import pytest

from agents.competitor_agent.models import (
    SIGNAL_TYPE_MENTION,
    SIGNAL_TYPE_NEW_POST,
    SIGNAL_TYPE_PRICING_CHANGE,
    SNAPSHOT_TYPE_GITHUB_RELEASE,
    SNAPSHOT_TYPE_PRICING,
)
from agents.competitor_agent.storage import CompetitorStore


@pytest.fixture
def store(tmp_path):
    db = CompetitorStore(db_path=tmp_path / "competitor_agent.db")
    yield db
    db.close()


def test_tables_and_columns_match_schema(store, tmp_path):
    conn = sqlite3.connect(tmp_path / "competitor_agent.db")
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "competitor_snapshots",
            "competitor_signals",
            "weekly_reports",
        }.issubset(tables)

        snapshot_cols = {
            col[1]
            for col in conn.execute("PRAGMA table_info(competitor_snapshots)").fetchall()
        }
        assert snapshot_cols == {
            "competitor",
            "snapshot_type",
            "content_hash",
            "raw_content",
            "captured_at",
        }

        signal_cols = {
            col[1]
            for col in conn.execute("PRAGMA table_info(competitor_signals)").fetchall()
        }
        assert signal_cols == {
            "signal_id",
            "competitor",
            "signal_type",
            "summary",
            "source_url",
            "detected_at",
        }

        report_cols = {
            col[1]
            for col in conn.execute("PRAGMA table_info(weekly_reports)").fetchall()
        }
        assert report_cols == {
            "report_id",
            "week_start",
            "content_markdown",
            "created_at",
        }

        signal_pk = [
            col[1]
            for col in conn.execute("PRAGMA table_info(competitor_signals)").fetchall()
            if col[5]
        ]
        report_pk = [
            col[1]
            for col in conn.execute("PRAGMA table_info(weekly_reports)").fetchall()
            if col[5]
        ]
        assert signal_pk == ["signal_id"]
        assert report_pk == ["report_id"]
    finally:
        conn.close()


def test_insert_snapshot_and_latest_by_competitor_type(store):
    store.insert_snapshot(
        competitor="LangSmith",
        snapshot_type=SNAPSHOT_TYPE_PRICING,
        content_hash="aaa",
        raw_content="<html>old</html>",
        captured_at="2026-08-01T00:00:00+00:00",
    )
    newer = store.insert_snapshot(
        competitor="LangSmith",
        snapshot_type=SNAPSHOT_TYPE_PRICING,
        content_hash="bbb",
        raw_content="<html>new</html>",
        captured_at="2026-08-10T00:00:00+00:00",
    )
    store.insert_snapshot(
        competitor="LangSmith",
        snapshot_type=SNAPSHOT_TYPE_GITHUB_RELEASE,
        content_hash="rel",
        raw_content="v1.0.0 notes",
        captured_at="2026-08-11T00:00:00+00:00",
    )
    store.insert_snapshot(
        competitor="Langfuse",
        snapshot_type=SNAPSHOT_TYPE_PRICING,
        content_hash="other",
        raw_content="<html>langfuse</html>",
        captured_at="2026-08-12T00:00:00+00:00",
    )

    latest = store.latest_snapshot("LangSmith", SNAPSHOT_TYPE_PRICING)
    assert latest is not None
    assert latest.content_hash == "bbb"
    assert latest.raw_content == "<html>new</html>"
    assert latest.rowid == newer.rowid
    assert store.latest_snapshot("Helicone", SNAPSHOT_TYPE_PRICING) is None


def test_insert_snapshot_rejects_unknown_type_and_empty_competitor(store):
    with pytest.raises(ValueError, match="snapshot_type"):
        store.insert_snapshot(
            competitor="LangSmith",
            snapshot_type="homepage",
            content_hash="x",
            raw_content="y",
        )
    with pytest.raises(ValueError, match="competitor"):
        store.insert_snapshot(
            competitor="  ",
            snapshot_type=SNAPSHOT_TYPE_PRICING,
            content_hash="x",
            raw_content="y",
        )


def test_insert_signal_round_trip_and_list_since(store):
    first = store.insert_signal(
        competitor="Braintrust",
        signal_type=SIGNAL_TYPE_PRICING_CHANGE,
        summary="Added a new $99/mo tier",
        source_url="https://www.braintrust.dev/pricing",
        detected_at="2026-08-10T12:00:00+00:00",
    )
    store.insert_signal(
        competitor="Langfuse",
        signal_type=SIGNAL_TYPE_NEW_POST,
        summary="Changelog: tracing UI refresh",
        detected_at="2026-08-01T00:00:00+00:00",
    )
    mention = store.insert_signal(
        competitor="Helicone",
        signal_type=SIGNAL_TYPE_MENTION,
        summary="HN thread on OSS observability",
        source_url="https://news.ycombinator.com/item?id=1",
        detected_at="2026-08-12T08:00:00+00:00",
    )

    fetched = store.get_signal(first.signal_id)
    assert fetched is not None
    assert fetched.summary == "Added a new $99/mo tier"
    assert fetched.source_url == "https://www.braintrust.dev/pricing"

    recent = store.list_signals(since="2026-08-10T00:00:00+00:00")
    assert [row.signal_id for row in recent] == [mention.signal_id, first.signal_id]

    grouped = store.list_signals_grouped_by_competitor(
        since="2026-08-10T00:00:00+00:00"
    )
    assert set(grouped) == {"Braintrust", "Helicone"}
    assert grouped["Braintrust"][0].signal_type == SIGNAL_TYPE_PRICING_CHANGE


def test_insert_signal_rejects_unknown_type(store):
    with pytest.raises(ValueError, match="signal_type"):
        store.insert_signal(
            competitor="Latitude",
            signal_type="rumor",
            summary="something",
        )


def test_weekly_report_round_trip(store):
    report = store.insert_weekly_report(
        week_start="2026-08-11",
        content_markdown="## Week of 2026-08-11\n\nNo changes detected",
    )
    fetched = store.get_weekly_report(report.report_id)
    assert fetched is not None
    assert fetched.week_start == "2026-08-11"
    assert "No changes detected" in fetched.content_markdown
    assert store.get_report_for_week("2026-08-11").report_id == report.report_id
    assert store.get_report_for_week("2026-08-04") is None
    assert len(store.list_weekly_reports()) == 1


def test_weekly_report_rejects_bad_week_start(store):
    with pytest.raises(ValueError):
        store.insert_weekly_report(week_start="08-11-2026", content_markdown="x")
    with pytest.raises(ValueError, match="content_markdown"):
        store.insert_weekly_report(week_start="2026-08-11", content_markdown="  ")
