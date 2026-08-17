"""Product Hunt draft adapter — draft_only; no public posting API."""

from __future__ import annotations

from typing import Optional

from agents.marketing_agent.adapters.base import DraftAdapter, facts_as_lines, mention_product
from agents.marketing_agent.models import Story
from agents.marketing_agent.pending_approval import PLATFORM_PRODUCTHUNT

PRODUCTHUNT_MAX_CHARS = 800


class ProductHuntAdapter(DraftAdapter):
    platform = PLATFORM_PRODUCTHUNT

    def format(self, story: Story, target: Optional[str] = None) -> str:
        product = mention_product(story)
        tagline = story.headline.strip()
        bullets = facts_as_lines(story, limit=3, bullet="• ")
        lines = [
            tagline,
            "",
            *bullets,
            "",
            f"Proof: {story.proof_point.strip()}",
            "",
            product,
        ]
        text = "\n".join(lines).strip()
        if len(text) <= PRODUCTHUNT_MAX_CHARS:
            return text
        clipped = text[: PRODUCTHUNT_MAX_CHARS - 1].rsplit(" ", 1)[0]
        return clipped.rstrip(",;:") + "…"
