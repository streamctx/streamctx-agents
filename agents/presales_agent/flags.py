"""Flag outreach drafts that invent pricing or legal/deal terms."""

from __future__ import annotations

import re

from agents.presales_agent.models import FLAG_LEGAL, FLAG_NONE, FLAG_PRICING

LEGAL_RE = re.compile(
    r"\b(legal|terms of service|privacy policy|gdpr|license|copyright|trademark|nda|contract)\b",
    re.I,
)
PRICING_RE = re.compile(
    r"\b(pric(?:e|ing)|paid plan|subscription|credit card|paywall|discount|"
    r"quote|deal terms?|arr\b|seat license|per-seat)\b",
    re.I,
)


def flag_draft(text: str) -> str:
    """Same idea as marketing_agent Legal/Pricing flags — human review, not auto-send."""
    blob = text or ""
    if PRICING_RE.search(blob):
        return FLAG_PRICING
    if LEGAL_RE.search(blob):
        return FLAG_LEGAL
    return FLAG_NONE
