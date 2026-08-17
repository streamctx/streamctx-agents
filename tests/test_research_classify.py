"""Unit tests for Stage 5 feature vs new-product classifier."""

from __future__ import annotations

import pytest

from agents.research_agent.classify import (
    build_classify_prompt,
    parse_classification,
    run_classify,
)
from agents.research_agent.models import (
    CLASSIFICATION_FEATURE,
    CLASSIFICATION_NEW_PRODUCT,
    CLASSIFICATION_NOT_ACTIONABLE,
    HYPE_TECHNICAL,
    SOURCE_TYPE_ARXIV,
    STATUS_NEW,
    STATUS_REVIEWED,
    ResearchIdea,
)
from agents.research_agent.settings import ResearchConfig
from agents.research_agent.storage import ResearchStore


@pytest.fixture
def store(tmp_path):
    db = ResearchStore(db_path=tmp_path / "research_agent.db")
    yield db
    db.close()


def _scored(store, *, url: str, title: str, gap: str, detected_at: str, status: str = STATUS_NEW):
    return store.insert_idea(
        source_url=url,
        source_type=SOURCE_TYPE_ARXIV,
        title=title,
        gap_description=gap,
        content_excerpt="method details",
        feasibility_score=4,
        pain_match_score=5,
        novelty_score=4,
        composite_score=4.4,
        hype_label=HYPE_TECHNICAL,
        status=status,
        detected_at=detected_at,
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"classification": "feature"}', CLASSIFICATION_FEATURE),
        ('{"label": "new_product"}', CLASSIFICATION_NEW_PRODUCT),
        ('```json\n{"classification": "not_actionable"}\n```', CLASSIFICATION_NOT_ACTIONABLE),
        ("I would call this a feature.", CLASSIFICATION_FEATURE),
        ("new_product", CLASSIFICATION_NEW_PRODUCT),
    ],
)
def test_parse_classification_accepts_json_and_bare_tokens(raw, expected):
    assert parse_classification(raw) == expected


def test_parse_classification_rejects_empty_and_unknown():
    with pytest.raises(ValueError, match="empty"):
        parse_classification("  ")
    with pytest.raises(ValueError, match="unrecognized"):
        parse_classification("maybe a plugin")


def test_build_classify_prompt_includes_parked_products_and_features():
    spec = ResearchConfig()
    idea = ResearchIdea(
        idea_id="x",
        source_url="https://arxiv.org/abs/1",
        source_type=SOURCE_TYPE_ARXIV,
        title="Standalone ledger for agent spend",
        gap_description="Needs its own billing surface.",
        feasibility_score=2,
        pain_match_score=4,
        novelty_score=4,
        composite_score=3.4,
        classification=None,
        status=STATUS_NEW,
        detected_at="2026-08-17T12:00:00+00:00",
        content_excerpt="A separate product for agent cost ledgers.",
        hype_label=HYPE_TECHNICAL,
    )
    prompt = build_classify_prompt(idea, spec)
    assert "AgentLedger" in prompt
    assert "LiteAgent" in prompt
    assert "checkpoint/resume" in prompt
    assert "new_product" in prompt
    assert "not_actionable" in prompt
    assert "Needs its own billing surface." in prompt


def test_run_classify_labels_scored_ideas_and_skips_the_rest(store):
    feature = _scored(
        store,
        url="https://arxiv.org/abs/feature",
        title="Struggle score on existing traces",
        gap="Reuse attribution infra to flag silent success.",
        detected_at="2026-08-17T12:00:00+00:00",
    )
    parked = _scored(
        store,
        url="https://arxiv.org/abs/ledger",
        title="Agent spend ledger with its own dashboard",
        gap="Needs a separate product surface like AgentLedger.",
        detected_at="2026-08-17T13:00:00+00:00",
        status=STATUS_REVIEWED,
    )
    store.insert_idea(
        source_url="https://arxiv.org/abs/unscored",
        source_type=SOURCE_TYPE_ARXIV,
        title="Not scored",
        hype_label=HYPE_TECHNICAL,
        detected_at="2026-08-17T14:00:00+00:00",
    )
    already = _scored(
        store,
        url="https://arxiv.org/abs/done",
        title="Already classified",
        gap="done",
        detected_at="2026-08-17T11:00:00+00:00",
    )
    store.set_classification(already.idea_id, CLASSIFICATION_FEATURE)

    def llm(prompt: str) -> str:
        if "AgentLedger" in prompt and "spend ledger" in prompt:
            return '{"classification": "new_product"}'
        return '{"classification": "feature"}'

    spec = ResearchConfig(classify_delay_seconds=0)
    result = run_classify(store, config=spec, llm_fn=llm)
    assert [idea.idea_id for idea in result.classified] == [feature.idea_id, parked.idea_id]
    assert store.get_idea(feature.idea_id).classification == CLASSIFICATION_FEATURE
    assert store.get_idea(parked.idea_id).classification == CLASSIFICATION_NEW_PRODUCT
    assert store.get_idea(already.idea_id).classification == CLASSIFICATION_FEATURE
    assert result.errors == ()
    assert store.list_unclassified() == []


def test_run_classify_retries_errors_and_honors_limit(store):
    broken = _scored(
        store,
        url="https://arxiv.org/abs/retry",
        title="Ambiguous item",
        gap="unclear",
        detected_at="2026-08-17T12:00:00+00:00",
    )
    later = _scored(
        store,
        url="https://arxiv.org/abs/ok",
        title="Clear SDK feature",
        gap="Fits checkpoint/resume.",
        detected_at="2026-08-17T13:00:00+00:00",
    )
    sleeps: list[float] = []

    def llm(prompt: str) -> str:
        if "Ambiguous" in prompt:
            return "not sure"
        return '{"classification": "feature"}'

    spec = ResearchConfig(classify_delay_seconds=0.25)
    first = run_classify(store, config=spec, llm_fn=llm, sleep_fn=sleeps.append, limit=1)
    assert first.classified == ()
    assert first.errors[0][0] == broken.idea_id
    assert store.get_idea(broken.idea_id).classification is None
    assert sleeps == []

    second = run_classify(store, config=spec, llm_fn=llm, sleep_fn=sleeps.append)
    assert [idea.idea_id for idea in second.classified] == [later.idea_id]
    assert second.errors[0][0] == broken.idea_id
    assert sleeps == [0.25]

    def llm_ok(_prompt: str) -> str:
        return '{"classification": "not_actionable"}'

    third = run_classify(store, config=spec, llm_fn=llm_ok)
    assert [idea.idea_id for idea in third.classified] == [broken.idea_id]
    assert store.get_idea(broken.idea_id).classification == CLASSIFICATION_NOT_ACTIONABLE
    assert store.get_idea(broken.idea_id).status == STATUS_NEW
