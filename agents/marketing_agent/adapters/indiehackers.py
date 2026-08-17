"""IndieHackers draft adapter — draft_only; no public posting API."""

from __future__ import annotations

from typing import Optional

from agents.marketing_agent.adapters.base import DraftAdapter, facts_as_lines, mention_product
from agents.marketing_agent.models import Story
from agents.marketing_agent.pending_approval import PLATFORM_INDIEHACKERS

INDIEHACKERS_MAX_CHARS = 1500


class IndieHackersAdapter(DraftAdapter):
    platform = PLATFORM_INDIEHACKERS

    def format(self, story: Story, target: Optional[str] = None) -> str:
        product = mention_product(story)
        lines = [
            f"Shipped: {story.headline.strip()}",
            "",
            "What changed:",
            *facts_as_lines(story, limit=4, bullet="- "),
            "",
            f"Why it matters: {story.proof_point.strip()}",
            "",
            f"Logged in the {product} changelog.",
        ]
        text = "\n".join(lines).strip()
        if len(text) <= INDIEHACKERS_MAX_CHARS:
            return text
        clipped = text[: INDIEHACKERS_MAX_CHARS - 1].rsplit(" ", 1)[0]
        return clipped.rstrip(",;:") + "…"
