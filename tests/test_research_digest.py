"""Unit tests for Stage 4 daily digest + marketing webhook."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agents.research_agent.digest import format_digest, run_digest
from agents.research_agent.models import (
    HYPE_TECHNICAL,
    SOURCE_TYPE_ARXIV,
    SOURCE_TYPE_GITHUB,
    STATUS_NEW,
    STATUS_REVIEWED,
)
from agents.research_agent.settings import ResearchConfig
from agents.research_agent.storage import ResearchStore

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    db = ResearchStore(db_path=tmp_path / "research_agent.db")
    yield db
    db.close()


def _scored(
    store,
    *,
    url: str,
    title: str,
    gap: str,
    composite: float,
    detected_at: str,
    status: str = STATUS_NEW,
):
    return store.insert_idea(
        source_url=url,
        source_type=SOURCE_TYPE_ARXIV if "arxiv" in url else SOURCE_TYPE_GITHUB,
        title=title,
        gap_description=gap,
        feasibility_score=3,
        pain_match_score=3,
        novelty_score=3,
        composite_score=composite,
        hype_label=HYPE_TECHNICAL,
        status=status,
        detected_at=detected_at,
    )


def test_format_digest_is_score_title_and_one_line_gap():
    high = _idea_stub(title="Fractional blame attribution", gap="No Shapley-style credit.\nAcross tools.", score=4.8)
    body = format_digest([high], now=NOW)
    assert body.startswith("[research-agent] daily digest — 2026-08-17")
    assert "4.80 Fractional blame attribution — No Shapley-style credit. Across tools." in body
    assert "\n\n" not in body


def _idea_stub(*, title: str, gap: str, score: float):
    from agents.research_agent.models import ResearchIdea

    return ResearchIdea(
        idea_id="x",
        source_url="https://arxiv.org/abs/1",
        source_type=SOURCE_TYPE_ARXIV,
        title=title,
        gap_description=gap,
        feasibility_score=4,
        pain_match_score=5,
        novelty_score=5,
        composite_score=score,
        classification=None,
        status=STATUS_NEW,
        detected_at="2026-08-17T11:00:00+00:00",
        hype_label=HYPE_TECHNICAL,
    )


def test_run_digest_selects_top_scores_marks_reviewed_and_notifies(store):
    top = _scored(
        store,
        url="https://arxiv.org/abs/high",
        title="Fractional blame attribution",
        gap="No Shapley-style credit across tools.",
        composite=4.8,
        detected_at="2026-08-17T11:00:00+00:00",
    )
    _scored(
        store,
        url="https://arxiv.org/abs/mid",
        title="Struggle score detector",
        gap="Silent successes are not flagged.",
        composite=4.1,
        detected_at="2026-08-17T10:00:00+00:00",
    )
    _scored(
        store,
        url="https://arxiv.org/abs/old",
        title="Too old",
        gap="Should be ignored.",
        composite=5.0,
        detected_at="2026-08-16T11:00:00+00:00",
    )
    _scored(
        store,
        url="https://arxiv.org/abs/reviewed",
        title="Already reviewed",
        gap="Skip me.",
        composite=4.9,
        detected_at="2026-08-17T11:30:00+00:00",
        status=STATUS_REVIEWED,
    )
    store.insert_idea(
        source_url="https://arxiv.org/abs/unscored",
        source_type=SOURCE_TYPE_ARXIV,
        title="Not scored yet",
        hype_label=HYPE_TECHNICAL,
        detected_at="2026-08-17T11:45:00+00:00",
    )
    posted: list[str] = []
    spec = ResearchConfig(digest_limit=5, digest_lookback_hours=24)
    result = run_digest(
        store,
        config=spec,
        now_fn=lambda: NOW,
        notifier=posted.append,
    )
    assert [idea.title for idea in result.items] == [
        "Fractional blame attribution",
        "Struggle score detector",
    ]
    assert all(idea.status == STATUS_REVIEWED for idea in result.items)
    assert store.get_idea(top.idea_id).status == STATUS_REVIEWED
    assert result.notified is True
    assert posted == [result.body]
    assert "4.80 Fractional blame attribution — No Shapley-style credit across tools." in result.body
    assert "Too old" not in result.body
    assert "Already reviewed" not in result.body
    assert "Not scored yet" not in result.body


def test_run_digest_caps_at_five(store):
    for index in range(6):
        _scored(
            store,
            url=f"https://arxiv.org/abs/{index}",
            title=f"Idea {index}",
            gap=f"Gap {index}",
            composite=1.0 + index * 0.1,
            detected_at="2026-08-17T11:00:00+00:00",
        )
    posted: list[str] = []
    spec = ResearchConfig(digest_limit=5)
    result = run_digest(
        store,
        config=spec,
        now_fn=lambda: NOW,
        notifier=posted.append,
    )
    assert len(result.items) == 5
    assert result.items[0].title == "Idea 5"
    assert posted == [result.body]


def test_run_digest_skips_notify_when_empty(store):
    posted: list[str] = []
    empty = run_digest(
        store,
        now_fn=lambda: NOW,
        notifier=posted.append,
    )
    assert empty.items == ()
    assert empty.notified is False
    assert posted == []
    assert "No scored ideas" in empty.body


def test_run_digest_no_notify_still_marks_reviewed(store):
    _scored(
        store,
        url="https://arxiv.org/abs/keep",
        title="Memory provenance",
        gap="No belief lineage.",
        composite=4.2,
        detected_at="2026-08-17T11:00:00+00:00",
    )
    posted: list[str] = []
    result = run_digest(
        store,
        now_fn=lambda: NOW,
        notifier=posted.append,
        notify=False,
    )
    assert result.notified is False
    assert posted == []
    assert result.items[0].status == STATUS_REVIEWED


@patch("agents.research_agent.digest.notify_text")
def test_default_notifier_is_marketing_webhook(mock_notify, store):
    _scored(
        store,
        url="https://arxiv.org/abs/hook",
        title="Hook check",
        gap="Uses shared marketing notify_text.",
        composite=3.5,
        detected_at="2026-08-17T11:00:00+00:00",
    )
    result = run_digest(store, now_fn=lambda: NOW)
    mock_notify.assert_called_once_with(result.body)
    assert result.notified is True
