"""Stage 2: cheap technical_substance vs marketing_hype filter.

One LLM call per unlabeled idea. Substance stays ``status=new`` for Stage 3.
Hype is dismissed and logged so the discard rate can be sanity-checked.
Parse/LLM failures leave the row unlabeled (retry later) rather than dropping it.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

from agents.research_agent.http import SleepFn
from agents.research_agent.models import (
    HYPE_LABELS,
    HYPE_MARKETING,
    HypeFilterResult,
    ResearchIdea,
)
from agents.research_agent.settings import ResearchConfig, default_config
from agents.research_agent.storage import ResearchStore

LlmFn = Callable[[str], str]
LABEL_RE = re.compile(
    r"\b(technical_substance|marketing_hype)\b",
    re.IGNORECASE,
)


def parse_hype_label(raw: str) -> str:
    """Extract a hype label from JSON or a bare token. Raises ValueError if unclear."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty hype-filter response")

    payload = _extract_json(text)
    if payload is not None:
        label = str(payload.get("label") or payload.get("classification") or "").strip()
        normalized = _normalize_label(label)
        if normalized:
            return normalized

    match = LABEL_RE.search(text)
    if match:
        normalized = _normalize_label(match.group(1))
        if normalized:
            return normalized
    raise ValueError(f"unrecognized hype-filter response: {text[:200]!r}")


def build_hype_prompt(idea: ResearchIdea, *, excerpt_chars: int = 800) -> str:
    excerpt = (idea.content_excerpt or "").strip()
    if excerpt_chars > 0 and len(excerpt) > excerpt_chars:
        excerpt = excerpt[: excerpt_chars - 3].rstrip() + "..."
    excerpt = excerpt or "(no excerpt)"
    return (
        "You classify inbound research signals for StreamCtx, an open-source LLM "
        "agent observability SDK (checkpoint/resume, context compression, "
        "self-healing, poison detection, causal failure attribution, "
        "counterfactual replay).\n\n"
        "Return exactly one label:\n"
        "- technical_substance: a paper, method, algorithm, architecture, or "
        "implementation with enough technical content to evaluate as a possible "
        "feature or research gap.\n"
        "- marketing_hype: launch/funding announcements, landing-page copy, "
        "thought-leadership with no method, or a named competitor's product/"
        "pricing move (those belong in competitor_agent, not here).\n\n"
        f"source_type: {idea.source_type}\n"
        f"title: {idea.title}\n"
        f"excerpt: {excerpt}\n\n"
        'Return JSON only: {"label": "technical_substance"} or '
        '{"label": "marketing_hype"}.'
    )


def run_hype_filter(
    store: ResearchStore,
    *,
    config: Optional[ResearchConfig] = None,
    llm_fn: Optional[LlmFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    limit: Optional[int] = None,
) -> HypeFilterResult:
    spec = config or default_config()
    classifier = llm_fn or default_hype_llm
    sleeper = sleep_fn or (lambda _seconds: None)
    kept: list[ResearchIdea] = []
    discarded: list[ResearchIdea] = []
    errors: list[tuple[str, str]] = []

    pending = store.list_unfiltered(limit=limit)
    for index, idea in enumerate(pending):
        if index and spec.hype_delay_seconds > 0:
            sleeper(spec.hype_delay_seconds)
        try:
            label = parse_hype_label(
                classifier(build_hype_prompt(idea, excerpt_chars=spec.hype_excerpt_chars))
            )
            updated = store.apply_hype_label(idea.idea_id, label)
        except Exception as exc:
            store.record_hype_error()
            errors.append((idea.idea_id, str(exc)))
            continue
        if updated.hype_label == HYPE_MARKETING:
            discarded.append(updated)
        else:
            kept.append(updated)

    return HypeFilterResult(
        kept=tuple(kept),
        discarded=tuple(discarded),
        errors=tuple(errors),
        stats=store.hype_stats(),
    )


def summarize(result: HypeFilterResult) -> str:
    return (
        f"kept={len(result.kept)} discarded={len(result.discarded)} "
        f"errors={len(result.errors)} "
        f"totals kept={result.stats.kept} discarded={result.stats.discarded} "
        f"errors={result.stats.errors}"
    )


def default_hype_llm(prompt: str) -> str:
    from openai import OpenAI

    from shared.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL

    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is required for the hype filter.")
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def _normalize_label(value: str) -> Optional[str]:
    text = value.strip().lower().replace(" ", "_").replace("-", "_")
    if text in HYPE_LABELS:
        return text
    return None


def _extract_json(raw: str) -> Optional[dict]:
    text = raw.strip()
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
