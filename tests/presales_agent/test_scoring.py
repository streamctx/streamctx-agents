"""Lead scoring rank-order: buyer role, size fit, AI/dev-tools industry."""

from __future__ import annotations

from agents.presales_agent.models import PIPELINE_IMPORTED, DRAFT_NONE, Lead
from agents.presales_agent.scoring import ScoringRules, parse_headcount_range, rank_leads, score_lead


def _lead(**overrides) -> Lead:
    base = dict(
        lead_id="x",
        name="Pat",
        title="",
        company="Acme",
        company_size="51-200",
        industry="Artificial Intelligence",
        linkedin_url=None,
        score=None,
        score_rationale="",
        pipeline_status=PIPELINE_IMPORTED,
        draft_status=DRAFT_NONE,
        draft_text="",
        draft_entry_id=None,
        flag="",
        source_fingerprint="t",
        created_at="2026-08-25T00:00:00+00:00",
        updated_at="2026-08-25T00:00:00+00:00",
    )
    base.update(overrides)
    return Lead(**base)


def test_parse_sales_nav_headcount_ranges():
    assert parse_headcount_range("51-200") == (51, 200)
    assert parse_headcount_range("11 - 50 employees") == (11, 50)
    assert parse_headcount_range("10,001+") == (10001, None)
    assert parse_headcount_range("") is None


def test_role_rank_order_with_same_company_profile():
    rules = ScoringRules.load()
    ranked = rank_leads(
        [
            _lead(lead_id="ae", name="AE", title="Account Executive"),
            _lead(lead_id="swe", name="SWE", title="Software Engineer"),
            _lead(lead_id="pm", name="PM", title="Product Manager"),
            _lead(lead_id="head", name="Head", title="Head of Engineering"),
            _lead(lead_id="cto", name="CTO", title="CTO"),
        ],
        rules,
    )
    names = [lead.name for lead, _score in ranked]
    assert names == ["CTO", "Head", "PM", "SWE", "AE"]
    scores = [item[1].score for item in ranked]
    assert scores == sorted(scores, reverse=True)
    assert ranked[0][1].score > ranked[-1][1].score
    assert ranked[0][1].score > rules.min_score


def test_ai_devtools_outrank_manufacturing_same_role():
    ai = score_lead(_lead(title="Software Engineer", industry="Developer Tools"))
    factory = score_lead(
        _lead(
            title="Software Engineer",
            industry="Manufacturing",
            company_size="10001+",
        )
    )
    assert ai.score > factory.score
    assert ai.industry_score > factory.industry_score


def test_sweet_spot_company_size_beats_enterprise():
    startup = score_lead(_lead(title="CTO", company_size="51-200"))
    enterprise = score_lead(_lead(title="CTO", company_size="10001+"))
    assert startup.size_score > enterprise.size_score
    assert startup.score > enterprise.score


def test_non_buyer_at_factory_is_below_threshold():
    rules = ScoringRules.load()
    low = score_lead(
        _lead(
            title="Account Executive",
            industry="Manufacturing",
            company_size="10001+",
        ),
        rules,
    )
    assert low.score < rules.min_score
