"""Unit tests for Stage 2 hype filter."""

from __future__ import annotations

import pytest

from agents.research_agent.hype import (
    build_hype_prompt,
    parse_hype_label,
    run_hype_filter,
)
from agents.research_agent.models import (
    HYPE_MARKETING,
    HYPE_TECHNICAL,
    SOURCE_TYPE_ARXIV,
    SOURCE_TYPE_GITHUB,
    STATUS_DISMISSED,
    STATUS_NEW,
)
from agents.research_agent.settings import ResearchConfig
from agents.research_agent.storage import ResearchStore


@pytest.fixture
def store(tmp_path):
    db = ResearchStore(db_path=tmp_path / "research_agent.db")
    yield db
    db.close()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"label": "technical_substance"}', HYPE_TECHNICAL),
        ('{"label": "marketing_hype"}', HYPE_MARKETING),
        ('```json\n{"label": "technical_substance"}\n```', HYPE_TECHNICAL),
        ('Here you go: {"classification": "marketing_hype"}', HYPE_MARKETING),
        ("technical_substance", HYPE_TECHNICAL),
        ("I think this is marketing_hype.", HYPE_MARKETING),
    ],
)
def test_parse_hype_label_accepts_json_and_bare_tokens(raw, expected):
    assert parse_hype_label(raw) == expected


def test_parse_hype_label_rejects_empty_and_unknown():
    with pytest.raises(ValueError, match="empty"):
        parse_hype_label("  ")
    with pytest.raises(ValueError, match="unrecognized"):
        parse_hype_label("maybe interesting?")


def test_build_hype_prompt_clips_excerpt_and_names_competitor_boundary():
    from agents.research_agent.models import ResearchIdea

    row = ResearchIdea(
        idea_id="x",
        source_url="https://arxiv.org/abs/1",
        source_type=SOURCE_TYPE_ARXIV,
        title="Memory provenance for agents",
        gap_description=None,
        feasibility_score=None,
        pain_match_score=None,
        novelty_score=None,
        composite_score=None,
        classification=None,
        status=STATUS_NEW,
        detected_at="2026-08-17T12:00:00+00:00",
        content_excerpt="A" * 1200,
    )
    prompt = build_hype_prompt(row, excerpt_chars=80)
    assert "technical_substance" in prompt
    assert "marketing_hype" in prompt
    assert "competitor_agent" in prompt
    assert "..." in prompt
    assert "A" * 1200 not in prompt


def test_run_hype_filter_keeps_substance_and_dismisses_hype(store):
    paper = store.insert_idea(
        source_url="https://arxiv.org/abs/2401.11111",
        source_type=SOURCE_TYPE_ARXIV,
        title="Fractional blame attribution with Shapley values",
        content_excerpt="We assign failure credit across agent tools.",
        detected_at="2026-08-17T12:00:00+00:00",
    )
    launch = store.insert_idea(
        source_url="https://github.com/acme/10x-agents",
        source_type=SOURCE_TYPE_GITHUB,
        title="Introducing AgentCloud: the future of autonomous everything",
        content_excerpt="We raised $20M. Book a demo today.",
        detected_at="2026-08-17T12:01:00+00:00",
    )
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        if "Shapley" in prompt:
            return '{"label": "technical_substance"}'
        return '{"label": "marketing_hype"}'

    spec = ResearchConfig(hype_delay_seconds=0)
    result = run_hype_filter(store, config=spec, llm_fn=llm)

    assert [idea.idea_id for idea in result.kept] == [paper.idea_id]
    assert [idea.idea_id for idea in result.discarded] == [launch.idea_id]
    assert result.errors == ()
    assert store.get_idea(paper.idea_id).status == STATUS_NEW
    assert store.get_idea(paper.idea_id).hype_label == HYPE_TECHNICAL
    assert store.get_idea(launch.idea_id).status == STATUS_DISMISSED
    assert store.get_idea(launch.idea_id).hype_label == HYPE_MARKETING
    assert result.stats.kept == 1
    assert result.stats.discarded == 1
    discards = store.list_hype_discards()
    assert discards[0].title.startswith("Introducing AgentCloud")
    assert len(prompts) == 2


def test_run_hype_filter_skips_already_labeled_and_retries_errors(store):
    labeled = store.insert_idea(
        source_url="https://arxiv.org/abs/already",
        source_type=SOURCE_TYPE_ARXIV,
        title="Already labeled paper",
        content_excerpt="method details",
        detected_at="2026-08-17T11:00:00+00:00",
    )
    store.apply_hype_label(labeled.idea_id, HYPE_TECHNICAL)
    broken = store.insert_idea(
        source_url="https://arxiv.org/abs/retry",
        source_type=SOURCE_TYPE_ARXIV,
        title="Ambiguous item",
        content_excerpt="method details",
        detected_at="2026-08-17T12:00:00+00:00",
    )
    later = store.insert_idea(
        source_url="https://arxiv.org/abs/ok",
        source_type=SOURCE_TYPE_ARXIV,
        title="Clear method paper",
        content_excerpt="method details",
        detected_at="2026-08-17T13:00:00+00:00",
    )

    calls = {"n": 0}

    def llm(prompt: str) -> str:
        calls["n"] += 1
        if "Ambiguous" in prompt:
            return "not sure"
        return '{"label": "technical_substance"}'

    spec = ResearchConfig(hype_delay_seconds=0)
    first = run_hype_filter(store, config=spec, llm_fn=llm)
    assert [idea.idea_id for idea in first.kept] == [later.idea_id]
    assert first.errors[0][0] == broken.idea_id
    assert store.get_idea(broken.idea_id).hype_label is None
    assert first.stats.errors == 1
    assert calls["n"] == 2

    def llm_ok(_prompt: str) -> str:
        return '{"label": "technical_substance"}'

    second = run_hype_filter(store, config=spec, llm_fn=llm_ok)
    assert [idea.idea_id for idea in second.kept] == [broken.idea_id]
    assert store.get_idea(broken.idea_id).hype_label == HYPE_TECHNICAL
    assert second.stats.kept == 3
    assert second.stats.errors == 1


def test_run_hype_filter_honors_limit_and_delay(store):
    first = store.insert_idea(
        source_url="https://arxiv.org/abs/one",
        source_type=SOURCE_TYPE_ARXIV,
        title="Paper one",
        content_excerpt="method details",
        detected_at="2026-08-17T12:00:00+00:00",
    )
    store.insert_idea(
        source_url="https://arxiv.org/abs/two",
        source_type=SOURCE_TYPE_ARXIV,
        title="Paper two",
        content_excerpt="method details",
        detected_at="2026-08-17T13:00:00+00:00",
    )
    sleeps: list[float] = []
    spec = ResearchConfig(hype_delay_seconds=0.25)

    limited = run_hype_filter(
        store,
        config=spec,
        llm_fn=lambda _prompt: '{"label": "technical_substance"}',
        sleep_fn=sleeps.append,
        limit=1,
    )
    assert [idea.idea_id for idea in limited.kept] == [first.idea_id]
    assert sleeps == []
    assert len(store.list_unfiltered()) == 1

    store.insert_idea(
        source_url="https://arxiv.org/abs/three",
        source_type=SOURCE_TYPE_ARXIV,
        title="Paper three",
        content_excerpt="method details",
        detected_at="2026-08-17T14:00:00+00:00",
    )
    paced = run_hype_filter(
        store,
        config=spec,
        llm_fn=lambda _prompt: '{"label": "technical_substance"}',
        sleep_fn=sleeps.append,
    )
    assert len(paced.kept) == 2
    assert sleeps == [0.25]
    assert store.list_unfiltered() == []
