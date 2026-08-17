"""Dev.to auto-tier adapter. Posts only via publish() after approval."""

from __future__ import annotations

import re
from typing import Optional

from agents.marketing_agent.adapters.base import (
    AutoAdapter,
    CredentialsError,
    PublishError,
    clip_text,
    facts_as_lines,
    mention_product,
    strip_version_prefix,
)
from agents.marketing_agent.http import JsonHttpClient, JsonHttpError
from agents.marketing_agent.models import PendingApprovalEntry, Story
from agents.marketing_agent.pending_approval import PLATFORM_DEVTO
from agents.marketing_agent.settings import DEVTO_API_KEY

DEVTO_ARTICLES_URL = "https://dev.to/api/articles"
DEVTO_TITLE_MAX = 128
DEFAULT_TAGS = ("python", "opensource", "ai")


class DevToAdapter(AutoAdapter):
    platform = PLATFORM_DEVTO

    def __init__(
        self,
        store,
        *,
        content_type: str = "post",
        http: Optional[JsonHttpClient] = None,
        api_key: Optional[str] = None,
        safety=None,
    ) -> None:
        super().__init__(store, content_type=content_type, safety=safety)
        self.http = http or JsonHttpClient()
        self.api_key = api_key if api_key is not None else DEVTO_API_KEY

    def format(self, story: Story, target: Optional[str] = None) -> str:
        title = clip_text(strip_version_prefix(story.headline), DEVTO_TITLE_MAX)
        product = mention_product(story)
        bullets = "\n".join(facts_as_lines(story, limit=5, bullet="- "))
        proof = story.proof_point.strip()
        return (
            f"# {title}\n\n"
            f"{product} shipped a concrete change worth writing down.\n\n"
            f"## What changed\n\n"
            f"{bullets}\n\n"
            f"## Proof\n\n"
            f"{proof}\n"
        )

    def _deliver(self, entry: PendingApprovalEntry) -> None:
        if not self.api_key:
            raise CredentialsError(
                "Dev.to credentials missing. Set DEVTO_API_KEY "
                "(https://dev.to/settings/extensions)."
            )
        title = _title_from_markdown(entry.content)
        try:
            response = self.http.post_json(
                DEVTO_ARTICLES_URL,
                json_body={
                    "article": {
                        "title": title,
                        "published": True,
                        "body_markdown": entry.content,
                        "tags": list(DEFAULT_TAGS),
                    }
                },
                headers={"api-key": self.api_key},
            )
        except JsonHttpError as exc:
            raise PublishError(f"Dev.to API failed: {exc}") from exc
        if not response.get("id") and not response.get("url"):
            raise PublishError(f"Dev.to API returned no article id: {response!r}")


def _title_from_markdown(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return clip_text(stripped[2:].strip(), DEVTO_TITLE_MAX)
    first = next((line.strip() for line in content.splitlines() if line.strip()), "Update")
    return clip_text(re.sub(r"^#+\s*", "", first), DEVTO_TITLE_MAX)
