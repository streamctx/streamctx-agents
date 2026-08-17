"""Stage 5: feature vs new-product vs not-actionable classifier.

One LLM call per scored idea that still has a null ``classification``.
Calibrated against AgentLedger / LiteAgent (strong ideas kept as separate
products, not StreamCtx core). Failures leave classification null for retry.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

from agents.research_agent.http import SleepFn
from agents.research_agent.models import (
    CLASSIFICATION_FEATURE,
    CLASSIFICATION_NEW_PRODUCT,
    CLASSIFICATION_NOT_ACTIONABLE,
    CLASSIFICATIONS,
    ClassifyResult,
    ResearchIdea,
)
from agents.research_agent.settings import ResearchConfig, default_config
from agents.research_agent.storage import ResearchStore

LlmFn = Callable[[str], str]
LABEL_RE = re.compile(
    r"\b(feature|new_product|not_actionable)\b",
    re.IGNORECASE,
)


def parse_classification(raw: str) -> str:
    """Extract a classification from JSON or a bare token."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty classification response")

    payload = _extract_json(text)
    if payload is not None:
        label = str(
            payload.get("classification") or payload.get("label") or ""
        ).strip()
        normalized = _normalize_label(label)
        if normalized:
            return normalized

    match = LABEL_RE.search(text)
    if match:
        normalized = _normalize_label(match.group(1))
        if normalized:
            return normalized
    raise ValueError(f"unrecognized classification response: {text[:200]!r}")


def build_classify_prompt(idea: ResearchIdea, config: ResearchConfig) -> str:
    parked = ", ".join(config.parked_products)
    features = "; ".join(config.streamctx_features)
    gap = (idea.gap_description or "").strip() or "(none)"
    excerpt = (idea.content_excerpt or "").strip() or "(no excerpt)"
    if config.gap_excerpt_chars > 0 and len(excerpt) > config.gap_excerpt_chars:
        excerpt = excerpt[: config.gap_excerpt_chars - 3].rstrip() + "..."
    scores = (
        f"feasibility={idea.feasibility_score} "
        f"pain_match={idea.pain_match_score} "
        f"novelty={idea.novelty_score} "
        f"composite={idea.composite_score}"
    )
    return (
        "You classify research gaps for StreamCtx, an open-source LLM agent "
        "observability SDK.\n\n"
        f"Current StreamCtx architecture/features: {features}.\n"
        f"Parked separate-product precedents (strong ideas deliberately kept "
        f"out of StreamCtx core): {parked}. Classify similar standalone "
        "surfaces as new_product, not feature.\n\n"
        "Return exactly one label:\n"
        "- feature: addable inside StreamCtx's existing SDK/architecture "
        "(checkpoint/resume, attribution, compression, poison detection, replay).\n"
        "- new_product: needs its own product surface, users, or positioning "
        f"(same bar as {parked}).\n"
        "- not_actionable: already covered, competitor-specific, or no StreamCtx "
        "fit.\n\n"
        f"title: {idea.title}\n"
        f"gap_description: {gap}\n"
        f"scores: {scores}\n"
        f"excerpt: {excerpt}\n\n"
        'Return JSON only: {"classification": "feature"} or '
        '{"classification": "new_product"} or '
        '{"classification": "not_actionable"}.'
    )


def run_classify(
    store: ResearchStore,
    *,
    config: Optional[ResearchConfig] = None,
    llm_fn: Optional[LlmFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    limit: Optional[int] = None,
) -> ClassifyResult:
    spec = config or default_config()
    classifier = llm_fn or default_classify_llm
    sleeper = sleep_fn or (lambda _seconds: None)
    classified: list[ResearchIdea] = []
    errors: list[tuple[str, str]] = []

    pending = store.list_unclassified(limit=limit)
    for index, idea in enumerate(pending):
        if index and spec.classify_delay_seconds > 0:
            sleeper(spec.classify_delay_seconds)
        try:
            label = parse_classification(classifier(build_classify_prompt(idea, spec)))
            updated = store.set_classification(idea.idea_id, label)
        except Exception as exc:
            errors.append((idea.idea_id, str(exc)))
            continue
        classified.append(updated)

    return ClassifyResult(classified=tuple(classified), errors=tuple(errors))


def summarize(result: ClassifyResult) -> str:
    counts = {label: 0 for label in sorted(CLASSIFICATIONS)}
    for idea in result.classified:
        if idea.classification in counts:
            counts[idea.classification] += 1
    parts = " ".join(f"{key}={value}" for key, value in counts.items())
    return f"classified={len(result.classified)} {parts} errors={len(result.errors)}"


def default_classify_llm(prompt: str) -> str:
    from openai import OpenAI

    from shared.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL

    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is required for classification.")
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def _normalize_label(value: str) -> Optional[str]:
    text = value.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "feature": CLASSIFICATION_FEATURE,
        "new_product": CLASSIFICATION_NEW_PRODUCT,
        "not_actionable": CLASSIFICATION_NOT_ACTIONABLE,
        "notactionable": CLASSIFICATION_NOT_ACTIONABLE,
    }
    return aliases.get(text)


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
