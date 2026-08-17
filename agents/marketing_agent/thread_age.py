"""HN thread-age check: real ``item?id=`` page must still expose a reply link.

HN locks comments after ~45 days. The missing reply link is the source of
truth — not a date heuristic, and never an Algolia search URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

FetchPage = Callable[[str], str]

REPLY_LINK_RE = re.compile(r"""href=["'](?:/)?reply\?id=\d+""", re.IGNORECASE)
ALGOLIA_HINT_RE = re.compile(r"algolia", re.IGNORECASE)
FETCH_TIMEOUT_SECONDS = 15
DEFAULT_ITEM_HOSTS = ("news.ycombinator.com",)


class HNThreadError(ValueError):
    """Base error for HN thread verification failures."""


class HNInvalidTargetError(HNThreadError):
    """Target is missing, Algolia, or not an item?id= page."""


class HNThreadUnreachableError(HNThreadError):
    """The item page could not be fetched."""


class HNThreadLockedError(HNThreadError):
    """Item page loaded but no reply link — comment window is closed."""


@dataclass(frozen=True)
class ThreadAgeRules:
    enabled: bool = True
    platforms: tuple[str, ...] = ("hn",)
    content_types: tuple[str, ...] = ("comment",)
    require_reply_link: bool = True
    reject_algolia: bool = True
    item_hosts: tuple[str, ...] = DEFAULT_ITEM_HOSTS

    def applies(self, platform: str, content_type: Optional[str]) -> bool:
        if not self.enabled:
            return False
        if platform not in self.platforms:
            return False
        if content_type is None:
            return False
        return content_type in self.content_types


def default_thread_age_rules() -> ThreadAgeRules:
    return ThreadAgeRules()


def verify_hn_thread(
    target: Optional[str],
    *,
    fetch_page: FetchPage,
    rules: Optional[ThreadAgeRules] = None,
) -> str:
    """
    Fetch the canonical ``item?id=`` page and require a reply link.

    Returns the canonical URL. Never treats an Algolia search/result URL
    as a thread when ``reject_algolia`` is set.
    """
    spec = rules or default_thread_age_rules()
    canonical = canonical_hn_item_url(target, rules=spec)
    if not spec.require_reply_link:
        return canonical
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


def canonical_hn_item_url(
    target: Optional[str],
    *,
    rules: Optional[ThreadAgeRules] = None,
) -> str:
    spec = rules or default_thread_age_rules()
    if not target or not str(target).strip():
        raise HNInvalidTargetError(
            "HN comment drafts require a target news.ycombinator.com/item?id= URL."
        )

    raw = str(target).strip()
    parsed = urlparse(raw)
    host = parsed.netloc.lower().removeprefix("www.")

    if spec.reject_algolia and (
        ALGOLIA_HINT_RE.search(host) or ALGOLIA_HINT_RE.search(parsed.path)
    ):
        raise HNInvalidTargetError(
            "Algolia search/result URLs are not HN threads. "
            "Use https://news.ycombinator.com/item?id=<id>."
        )

    hosts = frozenset(h.lower().removeprefix("www.") for h in spec.item_hosts)
    if host not in hosts:
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
