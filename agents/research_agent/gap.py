"""Stage 3: feature-gap mapping and 1-5 scoring for hype-filtered ideas.

One LLM call per ``technical_substance`` row that still has a null composite.
Writes ``gap_description`` + scores and keeps ``status=new``. Failures leave
scores null so the row can be retried.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

from agents.research_agent.http import SleepFn
from agents.research_agent.models import GapMapping, GapMapResult, ResearchIdea
from agents.research_agent.settings import ResearchConfig, default_config
from agents.research_agent.storage import ResearchStore

LlmFn = Callable[[str], str]
ALREADY_COVERED_GAP = "Already covered by StreamCtx; no new gap."


def compute_composite(
    feasibility_score: int,
    pain_match_score: int,
    novelty_score: int,
    *,
    pain_weight: float = 0.5,
    novelty_weight: float = 0.3,
    feasibility_weight: float = 0.2,
) -> float:
    """Weighted average with ``pain_match_score`` heaviest."""
    total = pain_weight + novelty_weight + feasibility_weight
    if total <= 0:
        raise ValueError("score weights must sum to a positive number")
    raw = (
        pain_weight * pain_match_score
        + novelty_weight * novelty_score
        + feasibility_weight * feasibility_score
    ) / total
    return round(float(raw), 2)


def clamp_score(value: object, name: str) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not a number") from exc
    if score < 1:
        return 1
    if score > 5:
        return 5
    return score


def parse_gap_mapping(
    raw: str,
    *,
    pain_weight: float = 0.5,
    novelty_weight: float = 0.3,
    feasibility_weight: float = 0.2,
) -> GapMapping:
    payload = _extract_json(raw or "")
    if payload is None:
        raise ValueError(f"unrecognized gap-mapping response: {(raw or '')[:200]!r}")

    already = _as_bool(payload.get("already_covered"))
    gap = str(payload.get("gap_description") or "").strip()
    if not gap:
        if already:
            gap = ALREADY_COVERED_GAP
        else:
            raise ValueError("gap_description is empty")

    feasibility = clamp_score(payload.get("feasibility_score"), "feasibility_score")
    pain = clamp_score(payload.get("pain_match_score"), "pain_match_score")
    novelty = clamp_score(payload.get("novelty_score"), "novelty_score")
    return GapMapping(
        already_covered=already,
        gap_description=gap,
        feasibility_score=feasibility,
        pain_match_score=pain,
        novelty_score=novelty,
        composite_score=compute_composite(
            feasibility,
            pain,
            novelty,
            pain_weight=pain_weight,
            novelty_weight=novelty_weight,
            feasibility_weight=feasibility_weight,
        ),
    )


def build_gap_prompt(idea: ResearchIdea, config: ResearchConfig) -> str:
    excerpt = (idea.content_excerpt or "").strip() or "(no excerpt)"
    if config.gap_excerpt_chars > 0 and len(excerpt) > config.gap_excerpt_chars:
        excerpt = excerpt[: config.gap_excerpt_chars - 3].rstrip() + "..."
    features = "; ".join(config.streamctx_features)
    unsolved = "; ".join(config.known_unsolved)
    return (
        "You map inbound research to gaps in StreamCtx, an open-source LLM agent "
        "observability SDK.\n\n"
        f"Current StreamCtx features: {features}.\n"
        "Known research-backed unsolved problems (calibrate novelty_score=5 to this "
        f"bar, not incremental wrapping): {unsolved}.\n"
        "pain_match_score should reflect MAST Taxonomy-style failure modes "
        "(specification issues, misalignment, verification failures, coordination/"
        "handoff failures, tool-use errors, context/memory loss) — not merely "
        "that the idea sounds interesting.\n"
        "feasibility_score=5 means it can reuse existing StreamCtx infra "
        "(checkpoint/resume, attribution, compression, poison detection, replay); "
        "1 means entirely new infrastructure.\n\n"
        "If StreamCtx already covers the item, set already_covered=true, explain "
        "which feature covers it in gap_description, and keep novelty_score and "
        "pain_match_score at 1-2.\n"
        "Named competitor product/pricing moves are out of scope (competitor_agent).\n\n"
        f"source_type: {idea.source_type}\n"
        f"title: {idea.title}\n"
        f"excerpt: {excerpt}\n\n"
        "Return JSON only with keys: already_covered (boolean), gap_description "
        "(string), feasibility_score, pain_match_score, novelty_score "
        "(each an integer 1-5)."
    )


def run_gap_map(
    store: ResearchStore,
    *,
    config: Optional[ResearchConfig] = None,
    llm_fn: Optional[LlmFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    limit: Optional[int] = None,
) -> GapMapResult:
    spec = config or default_config()
    mapper = llm_fn or default_gap_llm
    sleeper = sleep_fn or (lambda _seconds: None)
    mapped: list[ResearchIdea] = []
    errors: list[tuple[str, str]] = []

    pending = store.list_unscored(limit=limit)
    for index, idea in enumerate(pending):
        if index and spec.gap_delay_seconds > 0:
            sleeper(spec.gap_delay_seconds)
        try:
            mapping = parse_gap_mapping(
                mapper(build_gap_prompt(idea, spec)),
                pain_weight=spec.pain_weight,
                novelty_weight=spec.novelty_weight,
                feasibility_weight=spec.feasibility_weight,
            )
            updated = store.apply_gap_mapping(
                idea.idea_id,
                gap_description=mapping.gap_description,
                feasibility_score=mapping.feasibility_score,
                pain_match_score=mapping.pain_match_score,
                novelty_score=mapping.novelty_score,
                composite_score=mapping.composite_score,
            )
        except Exception as exc:
            errors.append((idea.idea_id, str(exc)))
            continue
        mapped.append(updated)

    return GapMapResult(mapped=tuple(mapped), errors=tuple(errors))


def summarize(result: GapMapResult) -> str:
    return f"mapped={len(result.mapped)} errors={len(result.errors)}"


def default_gap_llm(prompt: str) -> str:
    from openai import OpenAI

    from shared.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL

    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is required for gap mapping.")
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _extract_json(raw: str) -> Optional[dict]:
    text = raw.strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
