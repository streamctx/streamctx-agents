"""Roster helpers: status, activity, weekly stats, and existing task queues."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.coding_agent.pending_approval import (
    ROOT_CAUSE_INTAKE,
    STATUS_AUTO_FIX_FAILED,
    STATUS_NEEDS_HUMAN_REVIEW,
    PendingApprovalStore as CodingStore,
)
from agents.competitor_agent.storage import CompetitorStore
from agents.marketing_agent.pending_approval import (
    CONTENT_POST,
    MODE_DRAFT_ONLY,
    PLATFORM_LINKEDIN,
    STATUS_PENDING,
    PendingApprovalStore as MarketingStore,
)
from agents.research_agent.storage import ResearchStore
from dashboard import (
    ROSTER_ERROR,
    ROSTER_IDLE,
    ROSTER_WAITING,
    ROSTER_WORKING,
    RosterDbPaths,
    RuntimeState,
    assign_coding_task,
    assign_marketing_task,
    build_morning_briefing,
    derive_roster_status,
    load_coding_domain,
    load_competitor_domain,
    load_marketing_domain,
    load_research_domain,
    load_roster_cards,
    relative_time,
    week_start_utc,
)


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def paths(tmp_path: Path) -> RosterDbPaths:
    return RosterDbPaths(
        coding=tmp_path / "coding_agent.db",
        marketing=tmp_path / "marketing_agent.db",
        competitor=tmp_path / "competitor_agent.db",
        research=tmp_path / "research_agent.db",
    )


def test_relative_time_buckets():
    assert relative_time(NOW - timedelta(seconds=10), NOW) == "just now"
    assert relative_time(NOW - timedelta(minutes=2), NOW) == "2m ago"
    assert relative_time(NOW - timedelta(hours=2), NOW) == "2h ago"
    assert relative_time(NOW - timedelta(days=3), NOW) == "3d ago"
    assert relative_time(None, NOW) == "never"


def test_week_start_is_monday_utc():
    # Friday 21 Aug 2026 → week starts Monday 17 Aug
    assert week_start_utc(NOW) == datetime(2026, 8, 17, tzinfo=timezone.utc)


def test_derive_status_priority():
    kwargs = dict(
        runtime_error=None,
        pending=0,
        durable_error=False,
        open_session=False,
        session_started_at=None,
        now=NOW,
    )
    assert derive_roster_status(runtime_status="running", **kwargs) == ROSTER_WORKING
    assert (
        derive_roster_status(
            runtime_status="idle",
            **{**kwargs, "open_session": True, "session_started_at": NOW.isoformat()},
        )
        == ROSTER_WORKING
    )
    assert (
        derive_roster_status(
            runtime_status="idle",
            **{
                **kwargs,
                "open_session": True,
                "session_started_at": (NOW - timedelta(days=1)).isoformat(),
            },
        )
        == ROSTER_ERROR
    )
    assert derive_roster_status(runtime_status="failed", **kwargs) == ROSTER_ERROR
    assert (
        derive_roster_status(runtime_status="idle", **{**kwargs, "durable_error": True})
        == ROSTER_ERROR
    )
    assert (
        derive_roster_status(runtime_status="idle", **{**kwargs, "pending": 3})
        == ROSTER_WAITING
    )
    assert derive_roster_status(runtime_status="idle", **kwargs) == ROSTER_IDLE


def test_assign_coding_task_uses_intake_queue(paths):
    entry_id = assign_coding_task(
        "Fix the flaky token test\nInclude regression coverage.",
        db_path=paths.coding,
    )
    store = CodingStore(db_path=paths.coding, enable_default_notifier=False)
    try:
        entry = store.get_entry(entry_id)
        assert entry is not None
        assert entry.status == STATUS_NEEDS_HUMAN_REVIEW
        assert entry.root_cause == ROOT_CAUSE_INTAKE
        assert entry.diff is None
        assert entry.session_id.startswith("dashboard:")
        payload = json.loads(entry.test_results)
        assert payload["kind"] == "intake"
        assert payload["summary"] == "Fix the flaky token test"
        assert "regression coverage" in payload["request"]
        assert payload["source"] == "dashboard_roster"
    finally:
        store.close()


def test_assign_coding_task_rejects_empty(paths):
    with pytest.raises(ValueError, match="empty"):
        assign_coding_task("  ", db_path=paths.coding)


def test_assign_marketing_task_queues_linkedin_draft_only(paths):
    entry_id = assign_marketing_task(
        "Draft a post about the new roster view.",
        db_path=paths.marketing,
    )
    store = MarketingStore(db_path=paths.marketing, enable_default_notifier=False)
    try:
        entry = store.get_entry(entry_id)
        assert entry is not None
        assert entry.platform == PLATFORM_LINKEDIN
        assert entry.content_type == CONTENT_POST
        assert entry.mode == MODE_DRAFT_ONLY
        assert entry.status == STATUS_PENDING
        assert entry.content == "Draft a post about the new roster view."
    finally:
        store.close()


def test_coding_domain_activity_and_week_stats(paths):
    store = CodingStore(db_path=paths.coding, enable_default_notifier=False)
    try:
        store.create_entry(
            session_id="1",
            root_cause="UNCLEAR",
            confidence=0.2,
            matched_pattern_id=None,
            diff=None,
            regression_test=None,
            test_results=None,
            retries_used=0,
            status=STATUS_NEEDS_HUMAN_REVIEW,
        )
        failed = store.create_entry(
            session_id="2",
            root_cause="DRIFT",
            confidence=0.9,
            matched_pattern_id=None,
            diff="--- a\n+++ b\n",
            regression_test=None,
            test_results=None,
            retries_used=2,
            status=STATUS_AUTO_FIX_FAILED,
        )
        store.update_status(failed.entry_id, STATUS_AUTO_FIX_FAILED)
        ready = store.create_entry(
            session_id="3",
            root_cause="DRIFT",
            confidence=0.9,
            matched_pattern_id=None,
            diff="--- a\n+++ b\n",
            regression_test="assert True",
            test_results=None,
            retries_used=1,
            status="ready_for_approval",
        )
        store.update_status(ready.entry_id, "approved")
    finally:
        store.close()

    snap = load_coding_domain(
        paths.coding,
        now=NOW,
        since_iso=(NOW - timedelta(days=1)).isoformat(),
        week_iso=week_start_utc(NOW).isoformat(),
        today_iso=datetime(2026, 8, 21, tzinfo=timezone.utc).isoformat(),
    )
    assert snap.last_text == "approved a fix"
    assert snap.completed_week == 1
    assert snap.errors_week == 1
    assert snap.durable_error is True
    assert "approved 1 fix" in snap.briefing_line.lower()


def test_coding_stale_auto_fix_before_today(paths):
    store = CodingStore(db_path=paths.coding, enable_default_notifier=False)
    try:
        entry = store.create_entry(
            session_id="9",
            root_cause="DRIFT",
            confidence=0.4,
            matched_pattern_id=None,
            diff=None,
            regression_test=None,
            test_results=None,
            retries_used=3,
            status=STATUS_AUTO_FIX_FAILED,
        )
    finally:
        store.close()

    import sqlite3

    yesterday = (NOW - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(str(paths.coding))
    conn.execute(
        "UPDATE pending_approval SET created_at = ? WHERE entry_id = ?",
        (yesterday, entry.entry_id),
    )
    conn.commit()
    conn.close()

    snap = load_coding_domain(
        paths.coding,
        now=NOW,
        since_iso=NOW.isoformat(),
        week_iso=week_start_utc(NOW).isoformat(),
        today_iso=datetime(2026, 8, 21, tzinfo=timezone.utc).isoformat(),
    )
    assert snap.stale_error is True
    assert snap.durable_error is True


def test_competitor_last_activity_prefers_latest_signal(paths):
    store = CompetitorStore(db_path=paths.competitor)
    try:
        store.insert_snapshot(
            competitor="LangSmith",
            snapshot_type="changelog",
            content_hash="abc",
            raw_content="old",
            captured_at=(NOW - timedelta(hours=3)).isoformat(),
        )
        store.insert_signal(
            competitor="Langfuse",
            signal_type="new_release",
            summary="v1.2 shipped",
            detected_at=(NOW - timedelta(hours=1)).isoformat(),
        )
    finally:
        store.close()

    snap = load_competitor_domain(
        paths.competitor,
        now=NOW,
        since_iso=(NOW - timedelta(days=1)).isoformat(),
        week_iso=week_start_utc(NOW).isoformat(),
        today_iso=datetime(2026, 8, 21, tzinfo=timezone.utc).isoformat(),
    )
    assert snap.last_text == "detected new_release for Langfuse"
    assert snap.completed_week == 1
    assert "1 signal" in snap.briefing_line


def test_marketing_and_research_domain_snapshots(paths):
    marketing = MarketingStore(db_path=paths.marketing, enable_default_notifier=False)
    try:
        marketing.create_entry(
            platform=PLATFORM_LINKEDIN,
            content_type=CONTENT_POST,
            content="hello",
            created_at=(NOW - timedelta(hours=4)).isoformat(),
        )
    finally:
        marketing.close()

    research = ResearchStore(db_path=paths.research)
    try:
        research.insert_idea(
            source_url="https://example.com/a",
            source_type="arxiv",
            title="A useful paper",
            status="reviewed",
            detected_at=(NOW - timedelta(hours=5)).isoformat(),
        )
        research.set_last_polled_at("arxiv", (NOW - timedelta(hours=6)).isoformat())
    finally:
        research.close()

    m = load_marketing_domain(
        paths.marketing,
        now=NOW,
        since_iso=(NOW - timedelta(days=1)).isoformat(),
        week_iso=week_start_utc(NOW).isoformat(),
        today_iso=datetime(2026, 8, 21, tzinfo=timezone.utc).isoformat(),
    )
    assert m.last_text == "queued a linkedin post"
    assert "queued 1 draft" in m.briefing_line.lower()

    r = load_research_domain(
        paths.research,
        now=NOW,
        since_iso=(NOW - timedelta(days=1)).isoformat(),
        week_iso=week_start_utc(NOW).isoformat(),
        today_iso=datetime(2026, 8, 21, tzinfo=timezone.utc).isoformat(),
    )
    assert 'logged "A useful paper"' in r.last_text
    assert r.completed_week == 1


def test_load_roster_cards_waiting_and_briefing(paths):
    store = CodingStore(db_path=paths.coding, enable_default_notifier=False)
    try:
        store.create_entry(
            session_id="10",
            root_cause="UNCLEAR",
            confidence=0.1,
            matched_pattern_id=None,
            diff=None,
            regression_test=None,
            test_results=None,
            retries_used=0,
            status=STATUS_NEEDS_HUMAN_REVIEW,
        )
    finally:
        store.close()

    cards = load_roster_cards(
        now=NOW,
        paths=paths,
        pending={"coding": 4, "marketing": 2, "competitor": 0, "research": 0},
        sessions={key: None for key in ("coding", "marketing", "competitor", "research")},
        runtimes={
            "coding": RuntimeState(status="idle"),
            "marketing": RuntimeState(status="idle"),
            "competitor": RuntimeState(status="idle"),
            "research": RuntimeState(status="idle"),
        },
    )
    by_key = {card.key: card for card in cards}
    assert by_key["coding"].status == ROSTER_WAITING
    assert by_key["coding"].pending == 4
    assert by_key["coding"].role == "fixes failing tests"
    assert by_key["coding"].assign_mode == "coding"
    assert by_key["competitor"].assign_mode == "none"
    assert by_key["research"].assign_mode == "none"
    assert by_key["marketing"].assign_mode == "marketing"
    assert "2h ago" in by_key["coding"].last_activity or "flagged UNCLEAR" in by_key["coding"].last_activity
    assert "flagged UNCLEAR for review" in by_key["coding"].last_activity

    briefing = build_morning_briefing(cards)
    assert briefing.pending_total == 6
    assert briefing.stuck == ()
    assert any(line.startswith("Coding Agent:") for line in briefing.lines)


def test_briefing_flags_stale_open_session(paths):
    cards = load_roster_cards(
        now=NOW,
        paths=paths,
        pending={"coding": 0, "marketing": 0, "competitor": 0, "research": 0},
        sessions={
            "coding": {
                "id": 1,
                "started_at": (NOW - timedelta(days=2)).isoformat(),
                "ended_at": None,
            },
            "marketing": None,
            "competitor": None,
            "research": None,
        },
        runtimes={key: RuntimeState(status="idle") for key in ("coding", "marketing", "competitor", "research")},
    )
    by_key = {card.key: card for card in cards}
    assert by_key["coding"].status == ROSTER_ERROR
    assert by_key["coding"].stale_error is True
    briefing = build_morning_briefing(cards)
    assert "Coding Agent" in briefing.stuck
