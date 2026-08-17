"""LinkedIn draft adapter — draft_only; no posting API, manual publish."""

from __future__ import annotations

from typing import Optional

from agents.marketing_agent.adapters.base import DraftAdapter, facts_as_lines, mention_product
from agents.marketing_agent.models import Story
from agents.marketing_agent.pending_approval import PLATFORM_LINKEDIN

LINKEDIN_MAX_CHARS = 1300


class LinkedInAdapter(DraftAdapter):
    platform = PLATFORM_LINKEDIN

    def format(self, story: Story, target: Optional[str] = None) -> str:
        product = mention_product(story)
        headline = story.headline.strip()
        facts = facts_as_lines(story, limit=3, bullet="")
        fact_sentences = [line.strip().rstrip(".") + "." for line in facts if line.strip()]
        body = " ".join(fact_sentences[:2])
        proof = story.proof_point.strip().rstrip(".") + "."

        paragraphs = [
            headline if not headline.endswith(".") else headline,
            body,
            f"{proof} Details are in the {product} changelog.",
        ]
        text = "\n\n".join(p for p in paragraphs if p.strip())
        if len(text) <= LINKEDIN_MAX_CHARS:
            return text
        clipped = text[: LINKEDIN_MAX_CHARS - 1].rsplit(" ", 1)[0]
        return clipped.rstrip(",;:") + "…"
