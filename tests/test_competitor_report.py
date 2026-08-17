"""Unit tests for Stage 5 weekly report + marketing webhook hook."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agents.coding_agent.pending_approval import PendingApprovalStore as CodingStore
from agents.competitor_agent.health import collect_health
from agents.competitor_agent.models import (
    SIGNAL_TYPE_MENTION,
    SIGNAL_TYPE_NEW_RELEASE,
    SIGNAL_TYPE_PRICING_CHANGE,
)
from agents.competitor_agent.report import (
    GAP_FALLBACK,
    NO_CHANGES,
    generate_weekly_report,
    render_competitor_moves,
    resolve_week_window,
)
from agents.competitor_agent.settings import CompetitorConfig
from agents.competitor_agent.storage import CompetitorStore
from streamctx.storage import SessionStorage

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
                "roadmap": [
                    "Task Verification Auto-Checker",
                    "Runaway Cost Circuit Breaker",
                    "Multi-Agent Handoff Attribution",
                ],
                "competitors": [
                    {"name": "LangSmith", "pricing_url": "https://example.test/ls"},
                    {"name": "Braintrust"},
                    {"name": "Langfuse"},
                    {"name": "Acme"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return CompetitorConfig.load(path)


def test_shipped_config_includes_paid_roadmap():
    spec = CompetitorConfig.load()
    assert spec.roadmap[0] == "Task Verification Auto-Checker"
    assert "Runaway Cost Circuit Breaker" in spec.roadmap
    assert "Multi-Agent Handoff Attribution" in spec.roadmap


def test_week_window_covers_past_seven_days():
    week, since, until = resolve_week_window(NOW)
    assert week == "2026-08-10"
    assert since.startswith("2026-08-10T00:00:00")
    assert until.startswith("2026-08-17T12:00:00")


def test_competitor_moves_follow_config_names_not_hardcoded(config):
    markdown = render_competitor_moves(config.names(), {})
    assert "**LangSmith** — No changes detected" in markdown
    assert "**Acme** — No changes detected" in markdown
    assert "Helicone" not in markdown


def test_generate_weekly_report_renders_template_and_notifies(store, config):
    store.insert_signal(
        competitor="LangSmith",
        signal_type=SIGNAL_TYPE_PRICING_CHANGE,
        summary="Added a new $99/mo tier",
        source_url="https://example.test/ls",
        detected_at="2026-08-12T09:00:00+00:00",
    )
    store.insert_signal(
        competitor="Langfuse",
        signal_type=SIGNAL_TYPE_NEW_RELEASE,
        summary="Released v3.1 with better traces",
        detected_at="2026-08-14T11:00:00+00:00",
    )
    store.insert_signal(
        competitor="LangSmith",
        signal_type=SIGNAL_TYPE_MENTION,
        summary="Show HN: LangSmith evals",
        source_url="https://news.ycombinator.com/item?id=1",
        detected_at="2026-08-15T08:00:00+00:00",
    )
    store.insert_signal(
        competitor="LangSmith",
        signal_type=SIGNAL_TYPE_PRICING_CHANGE,
        summary="Old pricing change outside the window",
        detected_at="2026-08-01T00:00:00+00:00",
    )

    posted: list[str] = []
    llm_prompts: list[str] = []

    def fake_llm(prompt: str) -> str:
        llm_prompts.append(prompt)
        return "LangSmith pricing move is adjacent to cost controls; keep Circuit Breaker second."

    def fake_health(since: str, until: str):
        from agents.competitor_agent.health import HealthSnapshot

        assert since.startswith("2026-08-10")
        assert until.startswith("2026-08-17")
        return HealthSnapshot(
            poison_summary="pass — 2 sessions scanned, 0 poisoned (avg health 91)",
            attribution_summary="3 diagnoses, mean confidence 0.81 (2/3 ≥ 0.60)",
        )

    report = generate_weekly_report(
        store,
        config=config,
        now_fn=lambda: NOW,
        llm_fn=fake_llm,
        health_fn=fake_health,
        notifier=posted.append,
    )

    text = report.content_markdown
    assert text.startswith("## Week of 2026-08-10")
    assert "### StreamCtx Health" in text
    assert "- Poison Detector: pass — 2 sessions scanned, 0 poisoned (avg health 91)" in text
    assert "- Attribution Engine: 3 diagnoses, mean confidence 0.81 (2/3 ≥ 0.60)" in text
    assert "### Competitor Moves" in text
    assert "**LangSmith** —" in text
    assert "Added a new $99/mo tier" in text
    assert "Show HN: LangSmith evals" in text
    assert "Old pricing change outside the window" not in text
    assert "**Braintrust** — No changes detected" in text
    assert "**Langfuse** — Released v3.1 with better traces" in text
    assert "**Acme** — No changes detected" in text
    assert "### Gap Analysis" in text
    assert "keep Circuit Breaker second" in text
    assert len(llm_prompts) == 1
    assert "Task Verification Auto-Checker" in llm_prompts[0]
    assert posted == [text]
    assert store.get_report_for_week("2026-08-10").report_id == report.report_id


def test_generate_weekly_report_skips_existing_week_without_notify(store, config):
    first = generate_weekly_report(
        store,
        config=config,
        now_fn=lambda: NOW,
        llm_fn=lambda _prompt: "first",
        health_fn=lambda _since, _until: _health("a", "b"),
        notifier=lambda _text: (_ for _ in ()).throw(AssertionError("should not notify")),
        notify=False,
    )
    posted: list[str] = []
    second = generate_weekly_report(
        store,
        config=config,
        now_fn=lambda: NOW,
        llm_fn=lambda _prompt: "second",
        health_fn=lambda _since, _until: _health("c", "d"),
        notifier=posted.append,
    )
    assert second.report_id == first.report_id
    assert posted == []
    assert "first" in second.content_markdown


def test_generate_weekly_report_force_rewrites_and_notifies(store, config):
    generate_weekly_report(
        store,
        config=config,
        now_fn=lambda: NOW,
        llm_fn=lambda _prompt: "first",
        health_fn=lambda _since, _until: _health("a", "b"),
        notify=False,
    )
    posted: list[str] = []
    second = generate_weekly_report(
        store,
        config=config,
        now_fn=lambda: NOW,
        llm_fn=lambda _prompt: "reprioritize handoff attribution",
        health_fn=lambda _since, _until: _health("pass", "n/a"),
        notifier=posted.append,
        force=True,
    )
    assert "reprioritize handoff attribution" in second.content_markdown
    assert posted == [second.content_markdown]
    assert store.get_report_for_week("2026-08-10").report_id == second.report_id


def test_gap_analysis_falls_back_when_llm_empty(store, config):
    report = generate_weekly_report(
        store,
        config=config,
        now_fn=lambda: NOW,
        llm_fn=lambda _prompt: "",
        health_fn=lambda _since, _until: _health("No runs this week", "No diagnoses this week"),
        notify=False,
    )
    assert GAP_FALLBACK in report.content_markdown
    assert NO_CHANGES in report.content_markdown


@patch("agents.competitor_agent.report.notify_text")
def test_default_notifier_is_marketing_webhook(mock_notify, store, config):
    report = generate_weekly_report(
        store,
        config=config,
        now_fn=lambda: NOW,
        llm_fn=lambda _prompt: "ok",
        health_fn=lambda _since, _until: _health("n/a", "n/a"),
    )
    mock_notify.assert_called_once_with(report.content_markdown)


def test_collect_health_from_streamctx_and_coding_agent(tmp_path, storage: SessionStorage):
    clean_id = storage.start_session()
    storage.record_call(
        session_id=clean_id,
        provider="openrouter",
        model="test",
        input_tokens=10,
        output_tokens=5,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=[
            {"role": "user", "content": "summarize the notes"},
            {"role": "assistant", "content": "Here is a short summary."},
        ],
    )
    storage.save_checkpoint(
        session_id=clean_id,
        step_number=0,
        messages=[
            {"role": "user", "content": "summarize the notes"},
            {"role": "assistant", "content": "Here is a short summary."},
        ],
    )

    poisoned_id = storage.start_session()
    conn = sqlite3.connect(str(storage.db_path))
    try:
        conn.execute(
            "UPDATE sessions SET started_at = ?",
            ("2026-08-16T10:00:00+00:00",),
        )
        conn.commit()
    finally:
        conn.close()

    poisoned_messages = [
        {"role": "user", "content": "try again"},
        {"role": "assistant", "content": "error failed exception invalid success"},
        {"role": "assistant", "content": "error failed exception invalid"},
        {"role": "assistant", "content": "error failed exception invalid"},
        {"role": "assistant", "content": "error failed exception invalid"},
        {"role": "assistant", "content": "error failed exception invalid"},
    ]
    storage.record_call(
        session_id=poisoned_id,
        provider="openrouter",
        model="test",
        input_tokens=10,
        output_tokens=5,
        cost=0.0,
        reused_tokens=0,
        waste_category=None,
        messages=poisoned_messages,
    )
    storage.save_checkpoint(
        session_id=poisoned_id,
        step_number=0,
        messages=poisoned_messages,
    )

    coding = CodingStore(
        db_path=tmp_path / "coding_agent.db",
        enable_default_notifier=False,
    )
    try:
        first = coding.create_entry(
            session_id="1",
            root_cause="DRIFT",
            confidence=0.82,
            matched_pattern_id=None,
            diff=None,
            regression_test=None,
            test_results=None,
            retries_used=0,
            status="ready_for_approval",
        )
        second = coding.create_entry(
            session_id="2",
            root_cause="UNCLEAR",
            confidence=0.41,
            matched_pattern_id=None,
            diff=None,
            regression_test=None,
            test_results=None,
            retries_used=0,
            status="needs_human_review",
        )
        third = coding.create_entry(
            session_id="3",
            root_cause="DRIFT",
            confidence=0.90,
            matched_pattern_id=None,
            diff=None,
            regression_test=None,
            test_results=None,
            retries_used=0,
            status="ready_for_approval",
        )
        coding_conn = sqlite3.connect(str(tmp_path / "coding_agent.db"))
        try:
            coding_conn.execute(
                "UPDATE pending_approval SET created_at = ? WHERE entry_id = ?",
                ("2026-08-12T10:00:00+00:00", first.entry_id),
            )
            coding_conn.execute(
                "UPDATE pending_approval SET created_at = ? WHERE entry_id = ?",
                ("2026-08-14T10:00:00+00:00", second.entry_id),
            )
            coding_conn.execute(
                "UPDATE pending_approval SET created_at = ? WHERE entry_id = ?",
                ("2026-07-01T10:00:00+00:00", third.entry_id),
            )
            coding_conn.commit()
        finally:
            coding_conn.close()
        health = collect_health(
            since="2026-08-10T00:00:00+00:00",
            until="2026-08-17T12:00:00+00:00",
            session_storage=storage,
            coding_db_path=tmp_path / "coding_agent.db",
        )
    finally:
        coding.close()

    assert health.poison_summary.startswith("fail —")
    assert "2 sessions scanned" in health.poison_summary
    assert "1 poisoned" in health.poison_summary
    assert "2 diagnoses" in health.attribution_summary
    assert "mean confidence 0.6" in health.attribution_summary
    assert "1/2 ≥ 0.60" in health.attribution_summary


def test_collect_health_empty_window(tmp_path, storage: SessionStorage):
    health = collect_health(
        since="2026-08-10T00:00:00+00:00",
        until="2026-08-17T12:00:00+00:00",
        session_storage=storage,
        coding_db_path=tmp_path / "missing_coding.db",
    )
    assert health.poison_summary == "No runs this week"
    assert health.attribution_summary == "No diagnoses this week"


def _health(poison: str, attribution: str):
    from agents.competitor_agent.health import HealthSnapshot

    return HealthSnapshot(poison_summary=poison, attribution_summary=attribution)
