"""Unit tests for Stage 3 feature-gap mapping and scoring."""

from __future__ import annotations

import pytest

from agents.research_agent.gap import (
    ALREADY_COVERED_GAP,
    build_gap_prompt,
    compute_composite,
    parse_gap_mapping,
    run_gap_map,
)
from agents.research_agent.models import (
    HYPE_MARKETING,
    HYPE_TECHNICAL,
    SOURCE_TYPE_ARXIV,
    SOURCE_TYPE_GITHUB,
    STATUS_NEW,
)
from agents.research_agent.settings import ResearchConfig
from agents.research_agent.storage import ResearchStore


@pytest.fixture
def store(tmp_path):
    db = ResearchStore(db_path=tmp_path / "research_agent.db")
    yield db
    db.close()


def _substance(store, *, url: str, title: str, excerpt: str, detected_at: str):
    return store.insert_idea(
        source_url=url,
        source_type=SOURCE_TYPE_ARXIV if "arxiv" in url else SOURCE_TYPE_GITHUB,
        title=title,
        content_excerpt=excerpt,
        hype_label=HYPE_TECHNICAL,
        detected_at=detected_at,
    )


def test_compute_composite_weights_pain_highest():
    high_pain = compute_composite(1, 5, 1)
    high_novelty = compute_composite(1, 1, 5)
    high_feasibility = compute_composite(5, 1, 1)
    assert high_pain == 3.0
    assert high_novelty == 2.2
    assert high_feasibility == 1.8
    assert high_pain > high_novelty > high_feasibility
    assert compute_composite(5, 5, 5) == 5.0


def test_parse_gap_mapping_reads_json_and_clamps_scores():
    mapping = parse_gap_mapping(
        """```json
        {
          "already_covered": false,
          "gap_description": "No Shapley-style credit across tools.",
          "feasibility_score": 4,
          "pain_match_score": 9,
          "novelty_score": 0
        }
        ```"""
    )
    assert mapping.already_covered is False
    assert "Shapley" in mapping.gap_description
    assert mapping.feasibility_score == 4
    assert mapping.pain_match_score == 5
    assert mapping.novelty_score == 1
    assert mapping.composite_score == compute_composite(4, 5, 1)


def test_parse_gap_mapping_already_covered_fills_default_gap():
    mapping = parse_gap_mapping(
        '{"already_covered": true, "feasibility_score": 5, '
        '"pain_match_score": 1, "novelty_score": 1}'
    )
    assert mapping.already_covered is True
    assert mapping.gap_description == ALREADY_COVERED_GAP


def test_parse_gap_mapping_rejects_missing_gap_when_not_covered():
    with pytest.raises(ValueError, match="gap_description"):
        parse_gap_mapping(
            '{"already_covered": false, "feasibility_score": 3, '
            '"pain_match_score": 3, "novelty_score": 3}'
        )
    with pytest.raises(ValueError, match="unrecognized"):
        parse_gap_mapping("not json")


def test_build_gap_prompt_includes_features_and_unsolved_bar():
    spec = ResearchConfig()
    idea = store_idea_stub()
    prompt = build_gap_prompt(idea, spec)
    assert "checkpoint/resume" in prompt
    assert "Silent Success Detector" in prompt
    assert "Fractional Blame Attribution" in prompt
    assert "Memory Provenance" in prompt
    assert "MAST" in prompt
    assert "competitor_agent" in prompt


def store_idea_stub():
    from agents.research_agent.models import ResearchIdea

    return ResearchIdea(
        idea_id="x",
        source_url="https://arxiv.org/abs/1",
        source_type=SOURCE_TYPE_ARXIV,
        title="Belief lineage tracking",
        gap_description=None,
        feasibility_score=None,
        pain_match_score=None,
        novelty_score=None,
        composite_score=None,
        classification=None,
        status=STATUS_NEW,
        detected_at="2026-08-17T12:00:00+00:00",
        content_excerpt="Track which memory write a later action depended on.",
        hype_label=HYPE_TECHNICAL,
    )


def test_run_gap_map_scores_substance_and_skips_hype_and_already_scored(store):
    target = _substance(
        store,
        url="https://arxiv.org/abs/2401.11111",
        title="Fractional blame attribution with Shapley values",
        excerpt="Assign failure credit across agent tools.",
        detected_at="2026-08-17T12:00:00+00:00",
    )
    store.insert_idea(
        source_url="https://github.com/acme/launch",
        source_type=SOURCE_TYPE_GITHUB,
        title="We raised $20M",
        hype_label=HYPE_MARKETING,
        detected_at="2026-08-17T12:01:00+00:00",
    )
    scored = _substance(
        store,
        url="https://arxiv.org/abs/2401.22222",
        title="Already scored paper",
        excerpt="done",
        detected_at="2026-08-17T12:02:00+00:00",
    )
    store.apply_gap_mapping(
        scored.idea_id,
        gap_description="existing",
        feasibility_score=2,
        pain_match_score=2,
        novelty_score=2,
        composite_score=2.0,
    )

    def llm(prompt: str) -> str:
        assert "Shapley" in prompt
        return (
            '{"already_covered": false, '
            '"gap_description": "No Shapley-style credit across tools.", '
            '"feasibility_score": 4, "pain_match_score": 5, "novelty_score": 5}'
        )

    spec = ResearchConfig(gap_delay_seconds=0)
    result = run_gap_map(store, config=spec, llm_fn=llm)
    assert [idea.idea_id for idea in result.mapped] == [target.idea_id]
    assert result.errors == ()
    updated = store.get_idea(target.idea_id)
    assert updated.status == STATUS_NEW
    assert updated.feasibility_score == 4
    assert updated.pain_match_score == 5
    assert updated.novelty_score == 5
    assert updated.composite_score == compute_composite(4, 5, 5)
    assert store.get_idea(scored.idea_id).composite_score == 2.0
    assert store.list_unscored() == []


def test_run_gap_map_retries_parse_errors_and_honors_limit(store):
    broken = _substance(
        store,
        url="https://arxiv.org/abs/retry",
        title="Ambiguous method paper",
        excerpt="unclear",
        detected_at="2026-08-17T12:00:00+00:00",
    )
    later = _substance(
        store,
        url="https://arxiv.org/abs/ok",
        title="Clear method paper",
        excerpt="clear",
        detected_at="2026-08-17T13:00:00+00:00",
    )
    sleeps: list[float] = []

    def llm(prompt: str) -> str:
        if "Ambiguous" in prompt:
            return "not json"
        return (
            '{"already_covered": false, "gap_description": "A real gap.", '
            '"feasibility_score": 3, "pain_match_score": 4, "novelty_score": 2}'
        )

    spec = ResearchConfig(gap_delay_seconds=0.25)
    first = run_gap_map(store, config=spec, llm_fn=llm, sleep_fn=sleeps.append, limit=1)
    assert first.mapped == ()
    assert first.errors[0][0] == broken.idea_id
    assert store.get_idea(broken.idea_id).composite_score is None
    assert sleeps == []

    second = run_gap_map(store, config=spec, llm_fn=llm, sleep_fn=sleeps.append)
    assert [idea.idea_id for idea in second.mapped] == [later.idea_id]
    assert second.errors[0][0] == broken.idea_id
    assert sleeps == [0.25]

    def llm_ok(_prompt: str) -> str:
        return (
            '{"already_covered": true, "gap_description": '
            '"Covered by causal failure attribution.", '
            '"feasibility_score": 5, "pain_match_score": 1, "novelty_score": 1}'
        )

    third = run_gap_map(store, config=spec, llm_fn=llm_ok)
    assert [idea.idea_id for idea in third.mapped] == [broken.idea_id]
    assert store.get_idea(broken.idea_id).status == STATUS_NEW
    assert "causal failure attribution" in store.get_idea(broken.idea_id).gap_description
