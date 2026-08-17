"""Unit tests for research_agent storage schema and CRUD."""

from __future__ import annotations

import sqlite3

import pytest

from agents.research_agent.models import (
    SOURCE_TYPE_ARXIV,
    SOURCE_TYPE_GITHUB,
    STATUS_NEW,
)
from agents.research_agent.storage import ResearchStore


@pytest.fixture
def store(tmp_path):
    db = ResearchStore(db_path=tmp_path / "research_agent.db")
    yield db
    db.close()


def test_tables_and_columns_match_schema(store, tmp_path):
    conn = sqlite3.connect(tmp_path / "research_agent.db")
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "research_ideas" in tables
        assert "research_poll_state" in tables
        assert "research_hype_discards" in tables
        assert "research_hype_stats" in tables

        idea_cols = {
            col[1]
            for col in conn.execute("PRAGMA table_info(research_ideas)").fetchall()
        }
        assert idea_cols == {
            "idea_id",
            "source_url",
            "source_type",
            "title",
            "gap_description",
            "feasibility_score",
            "pain_match_score",
            "novelty_score",
            "composite_score",
            "classification",
            "status",
            "detected_at",
            "content_excerpt",
            "hype_label",
        }

        pk = [
            col[1]
            for col in conn.execute("PRAGMA table_info(research_ideas)").fetchall()
            if col[5]
        ]
        assert pk == ["idea_id"]
    finally:
        conn.close()


def test_insert_idea_round_trip_with_null_scores(store):
    idea = store.insert_idea(
        source_url="https://arxiv.org/abs/2401.12345",
        source_type=SOURCE_TYPE_ARXIV,
        title="Agent reliability under context overflow",
        content_excerpt="We study silent failures in multi-agent memory.",
        detected_at="2026-08-17T12:00:00+00:00",
    )
    fetched = store.get_idea(idea.idea_id)
    assert fetched is not None
    assert fetched.status == STATUS_NEW
    assert fetched.feasibility_score is None
    assert fetched.gap_description is None
    assert fetched.classification is None
    assert fetched.content_excerpt.startswith("We study silent")
    by_url = store.get_by_source_url("https://arxiv.org/abs/2401.12345")
    assert by_url is not None
    assert by_url.idea_id == idea.idea_id


def test_duplicate_source_url_is_rejected(store):
    store.insert_idea(
        source_url="https://github.com/example/agent-kit",
        source_type=SOURCE_TYPE_GITHUB,
        title="example/agent-kit",
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_idea(
            source_url="https://github.com/example/agent-kit",
            source_type=SOURCE_TYPE_GITHUB,
            title="example/agent-kit again",
        )


def test_list_ideas_filters_by_source_and_since(store):
    store.insert_idea(
        source_url="https://arxiv.org/abs/1",
        source_type=SOURCE_TYPE_ARXIV,
        title="old paper",
        detected_at="2026-08-01T00:00:00+00:00",
    )
    newer = store.insert_idea(
        source_url="https://github.com/acme/llm-agent",
        source_type=SOURCE_TYPE_GITHUB,
        title="acme/llm-agent",
        detected_at="2026-08-17T00:00:00+00:00",
    )
    recent = store.list_ideas(since="2026-08-10T00:00:00+00:00")
    assert [row.idea_id for row in recent] == [newer.idea_id]
    github = store.list_ideas(source_type=SOURCE_TYPE_GITHUB)
    assert len(github) == 1


def test_insert_rejects_unknown_source_type_and_empty_title(store):
    with pytest.raises(ValueError, match="source_type"):
        store.insert_idea(
            source_url="https://example.test/x",
            source_type="blog",
            title="x",
        )
    with pytest.raises(ValueError, match="title"):
        store.insert_idea(
            source_url="https://example.test/x",
            source_type=SOURCE_TYPE_ARXIV,
            title="  ",
        )


def test_poll_state_round_trip(store):
    assert store.last_polled_at(SOURCE_TYPE_ARXIV) is None
    store.set_last_polled_at(SOURCE_TYPE_ARXIV, "2026-08-17T12:00:00+00:00")
    assert store.last_polled_at(SOURCE_TYPE_ARXIV) == "2026-08-17T12:00:00+00:00"
    store.set_last_polled_at(SOURCE_TYPE_ARXIV, "2026-08-17T18:00:00+00:00")
    assert store.last_polled_at(SOURCE_TYPE_ARXIV) == "2026-08-17T18:00:00+00:00"


def test_apply_hype_label_dismisses_marketing_and_counts(store):
    from agents.research_agent.models import HYPE_MARKETING, HYPE_TECHNICAL, STATUS_DISMISSED

    kept = store.insert_idea(
        source_url="https://arxiv.org/abs/keep",
        source_type=SOURCE_TYPE_ARXIV,
        title="Causal attribution in multi-agent traces",
    )
    hype = store.insert_idea(
        source_url="https://github.com/acme/launch",
        source_type=SOURCE_TYPE_GITHUB,
        title="We raised $20M to 10x your agents",
    )
    store.apply_hype_label(kept.idea_id, HYPE_TECHNICAL)
    store.apply_hype_label(hype.idea_id, HYPE_MARKETING, discarded_at="2026-08-17T12:00:00+00:00")

    assert store.get_idea(kept.idea_id).hype_label == HYPE_TECHNICAL
    assert store.get_idea(kept.idea_id).status == STATUS_NEW
    dismissed = store.get_idea(hype.idea_id)
    assert dismissed.hype_label == HYPE_MARKETING
    assert dismissed.status == STATUS_DISMISSED
    assert store.list_unfiltered() == []
    stats = store.hype_stats()
    assert (stats.kept, stats.discarded, stats.errors) == (1, 1, 0)
    discards = store.list_hype_discards()
    assert len(discards) == 1
    assert discards[0].title == "We raised $20M to 10x your agents"


def test_apply_gap_mapping_writes_scores_and_keeps_status_new(store):
    from agents.research_agent.models import HYPE_TECHNICAL

    idea = store.insert_idea(
        source_url="https://arxiv.org/abs/gap",
        source_type=SOURCE_TYPE_ARXIV,
        title="Memory provenance for agent beliefs",
        hype_label=HYPE_TECHNICAL,
    )
    assert store.list_unscored()[0].idea_id == idea.idea_id
    updated = store.apply_gap_mapping(
        idea.idea_id,
        gap_description="No lineage of which memory write caused a bad tool call.",
        feasibility_score=4,
        pain_match_score=5,
        novelty_score=5,
        composite_score=4.7,
    )
    assert updated.status == STATUS_NEW
    assert updated.gap_description.startswith("No lineage")
    assert updated.composite_score == 4.7
    assert store.list_unscored() == []
    with pytest.raises(ValueError, match="feasibility_score"):
        store.apply_gap_mapping(
            idea.idea_id,
            gap_description="x",
            feasibility_score=9,
            pain_match_score=1,
            novelty_score=1,
            composite_score=1.0,
        )
