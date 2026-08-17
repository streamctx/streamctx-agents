"""Hacker News draft adapter — draft_only, no posting API.

Reply drafts must target a real ``item?id=`` page (not Algolia) and the
page must still expose a reply link. HN locks comments after ~45 days;
the reply link disappearing is the source of truth, not a date heuristic.
"""

from __future__ import annotations

import re
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from agents.marketing_agent.adapters.base import DraftAdapter, facts_as_lines
from agents.marketing_agent.models import Story
from agents.marketing_agent.pending_approval import CONTENT_COMMENT, CONTENT_POST, PLATFORM_HN

FetchPage = Callable[[str], str]

HN_ITEM_HOSTS = frozenset({"news.ycombinator.com"})
REPLY_LINK_RE = re.compile(r"""href=["'](?:/)?reply\?id=\d+""", re.IGNORECASE)
ALGOLIA_HINT_RE = re.compile(r"algolia", re.IGNORECASE)
PROMO_LEAD_RE = re.compile(
    r"\b(launch|announcing|show hn|check out|we just)\b",
    re.IGNORECASE,
)

HN_COMMENT_MAX_CHARS = 1200
HN_POST_MAX_CHARS = 1800
FETCH_TIMEOUT_SECONDS = 15


class HNThreadError(ValueError):
    """Base error for HN thread verification failures."""


class HNInvalidTargetError(HNThreadError):
    """Target is missing, Algolia, or not an item?id= page."""


class HNThreadUnreachableError(HNThreadError):
    """The item page could not be fetched."""


class HNThreadLockedError(HNThreadError):
    """Item page loaded but no reply link — comment window is closed."""


class HackerNewsAdapter(DraftAdapter):
    platform = PLATFORM_HN

    def __init__(
        self,
        store,
        *,
        content_type: str = CONTENT_POST,
        fetch_page: Optional[FetchPage] = None,
        safety=None,
    ) -> None:
        super().__init__(store, content_type=content_type, safety=safety)
        self.fetch_page = fetch_page or fetch_hn_item_page

    def format(self, story: Story, target: Optional[str] = None) -> str:
        if self.content_type == CONTENT_COMMENT:
            verify_hn_thread(target, fetch_page=self.fetch_page)
            text = _format_hn_comment(story)
        else:
            text = _format_hn_post(story)
        return text

    def submit(self, content: str, target: Optional[str] = None, **kwargs) -> str:
        if self.content_type == CONTENT_COMMENT:
            target = verify_hn_thread(target, fetch_page=self.fetch_page)
        return super().submit(content, target=target, **kwargs)


def verify_hn_thread(
    target: Optional[str],
    *,
    fetch_page: FetchPage,
) -> str:
    """
    Fetch the canonical ``item?id=`` page and require a reply link.

    Returns the canonical URL. Never treats an Algolia search/result URL
    as a thread.
    """
    canonical = canonical_hn_item_url(target)
    try:
        html = fetch_page(canonical)
    except HNThreadError:
        raise
    except Exception as exc:
        raise HNThreadUnreachableError(
            f"Failed to fetch HN item page {canonical}: {exc}"
        ) from exc

    if not (html or "").strip():
        raise HNThreadUnreachableError(f"Empty response from {canonical}")

    if not REPLY_LINK_RE.search(html):
        raise HNThreadLockedError(
            f"No reply link on {canonical}; thread is past the comment lock window."
        )
    return canonical


def canonical_hn_item_url(target: Optional[str]) -> str:
    if not target or not str(target).strip():
        raise HNInvalidTargetError(
            "HN comment drafts require a target news.ycombinator.com/item?id= URL."
        )

    raw = str(target).strip()
    parsed = urlparse(raw)
    host = parsed.netloc.lower().removeprefix("www.")

    if ALGOLIA_HINT_RE.search(host) or ALGOLIA_HINT_RE.search(parsed.path):
        raise HNInvalidTargetError(
            "Algolia search/result URLs are not HN threads. "
            "Use https://news.ycombinator.com/item?id=<id>."
        )

    if host not in HN_ITEM_HOSTS:
        raise HNInvalidTargetError(
            f"Expected news.ycombinator.com/item?id=, got {raw!r}."
        )

    if parsed.path.rstrip("/") != "/item":
        raise HNInvalidTargetError(
            f"Expected /item?id= path, got {parsed.path!r}."
        )

    item_id = (parse_qs(parsed.query).get("id") or [None])[0]
    if not item_id or not str(item_id).isdigit():
        raise HNInvalidTargetError(f"Missing numeric id= on HN item URL: {raw!r}")

    return f"https://news.ycombinator.com/item?id={item_id}"


def fetch_hn_item_page(url: str) -> str:
    """stdlib fetch of a canonical item page. Injected in tests."""
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; streamctx-marketing-agent/0.1; "
                "draft-thread-check)"
            )
        },
    )
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise HNThreadUnreachableError(
            f"HN item page returned HTTP {exc.code} for {url}"
        ) from exc
    except URLError as exc:
        raise HNThreadUnreachableError(
            f"HN item page unreachable ({url}): {exc.reason}"
        ) from exc


def _format_hn_comment(story: Story) -> str:
    lead = _technical_lead(story)
    proof = (story.proof_point or "").strip()
    parts = [lead]
    if proof and proof.lower() not in lead.lower():
        parts.append(proof)
    return _clip(" ".join(parts), HN_COMMENT_MAX_CHARS)


def _format_hn_post(story: Story) -> str:
    title = _technical_lead(story)
    body_lines = facts_as_lines(story, limit=3, bullet="")
    body = "\n\n".join(line.strip() for line in body_lines if line.strip())
    proof = (story.proof_point or "").strip()
    chunks = [title]
    if body and body.lower() not in title.lower():
        chunks.append(body)
    if proof and proof.lower() not in "\n".join(chunks).lower():
        chunks.append(proof)
    return _clip("\n\n".join(chunks), HN_POST_MAX_CHARS)


def _technical_lead(story: Story) -> str:
    headline = (story.headline or "").strip()
    if headline and not PROMO_LEAD_RE.search(headline):
        if ":" in headline and headline.split(":", 1)[0].strip()[0:1].isdigit():
            return headline.split(":", 1)[1].strip() or headline
        return headline
    if story.key_facts:
        return story.key_facts[0]
    return story.proof_point


def _clip(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+\n", "\n", text).strip()
    if len(text) <= max_chars:
        return text
    clipped = text[: max_chars - 1].rsplit(" ", 1)[0]
    return (clipped or text[: max_chars - 1]).rstrip(",;:") + "…"
