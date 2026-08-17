"""Mention/launch tracking via HN, Product Hunt, and Twitter.

Reuses ``marketing_agent.outreach`` search helpers (same Algolia HN and
Twitter recent-search endpoints) instead of a second HTTP stack.
Product Hunt uses the official GraphQL API when a token is present, and
falls back to HN ``site:producthunt.com`` otherwise.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from agents.competitor_agent.http import SleepFn
from agents.competitor_agent.models import (
    SIGNAL_TYPE_MENTION,
    SNAPSHOT_TYPE_MENTIONS,
    CompetitorSignal,
)
from agents.competitor_agent.settings import (
    CompetitorConfig,
    CompetitorSource,
    producthunt_token,
)
from agents.competitor_agent.storage import CompetitorStore
from agents.marketing_agent.http import JsonHttpClient, JsonHttpError
from agents.marketing_agent.models import PublicPost
from agents.marketing_agent.outreach import search_hn, search_twitter
from agents.marketing_agent.settings import TWITTER_BEARER_TOKEN

NowFn = Callable[[], datetime]

PRODUCTHUNT_GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"
PRODUCTHUNT_POSTS_QUERY = """
query RecentPosts($since: DateTime!, $first: Int!) {
  posts(postedAfter: $since, first: $first, order: NEWEST) {
    edges {
      node {
        id
        name
        tagline
        votesCount
        url
        createdAt
      }
    }
  }
}
""".strip()

LAUNCH_RE = re.compile(
    r"\b(show\s*hn|product\s*hunt|launched|launching|launch week|"
    r"generally available|\bga\b|open[- ]sourced)\b",
    re.IGNORECASE,
)
FUNDING_RE = re.compile(
    r"\b(raised|funding|series [a-d]\b|seed round|acquired|acquisition|"
    r"unicorn|valu(?:ed|ation))\b",
    re.IGNORECASE,
)


def poll_mentions(
    competitor: CompetitorSource,
    *,
    store: CompetitorStore,
    config: CompetitorConfig,
    http: Optional[JsonHttpClient] = None,
    twitter_bearer: Optional[str] = None,
    ph_token: Optional[str] = None,
    sleep_fn: Optional[SleepFn] = None,
    now_fn: Optional[NowFn] = None,
    producthunt_posts: Optional[list[PublicPost]] = None,
) -> list[CompetitorSignal]:
    """Search last-N-days mentions and insert notable ``mention`` signals."""
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    latest = store.latest_snapshot(competitor.name, SNAPSHOT_TYPE_MENTIONS)
    if latest is not None and _too_soon(
        latest.captured_at, config.mention_min_interval_seconds, now
    ):
        return []

    client = http or JsonHttpClient(user_agent=config.user_agent)
    sleeper = sleep_fn or time.sleep
    since_unix = int((now - timedelta(days=config.mention_lookback_days)).timestamp())
    start_time = (now - timedelta(days=config.mention_lookback_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    bearer = twitter_bearer if twitter_bearer is not None else TWITTER_BEARER_TOKEN
    token = ph_token if ph_token is not None else producthunt_token()

    seen = _seen_urls(latest.raw_content if latest else None)
    existing = {
        row.source_url
        for row in store.list_signals(
            competitor=competitor.name, signal_type=SIGNAL_TYPE_MENTION
        )
        if row.source_url
    }
    fetched: list[PublicPost] = []
    first = True

    def _pace() -> None:
        nonlocal first
        if not first and config.poll_delay_seconds > 0:
            sleeper(config.poll_delay_seconds)
        first = False

    for name in competitor.query_names():
        query = f'"{name}"'
        _pace()
        try:
            fetched.extend(
                search_hn(
                    client,
                    query,
                    limit=config.mention_search_limit,
                    since_unix=since_unix,
                )
            )
        except JsonHttpError:
            pass
        if bearer:
            _pace()
            try:
                fetched.extend(
                    search_twitter(
                        client,
                        query,
                        bearer=bearer,
                        limit=config.mention_search_limit,
                        start_time=start_time,
                    )
                )
            except JsonHttpError:
                pass
        if not token:
            _pace()
            try:
                fetched.extend(
                    search_hn(
                        client,
                        f"{query} site:producthunt.com",
                        limit=config.mention_search_limit,
                        since_unix=since_unix,
                    )
                )
            except JsonHttpError:
                pass

    if producthunt_posts is None and token:
        _pace()
        try:
            producthunt_posts = search_producthunt(
                client,
                token=token,
                since=start_time,
                limit=max(10, config.mention_search_limit),
            )
        except JsonHttpError:
            producthunt_posts = []
    fetched.extend(producthunt_posts or [])

    names = competitor.query_names()
    signals: list[CompetitorSignal] = []
    new_urls: set[str] = set()
    for post in _dedupe_posts(fetched):
        new_urls.add(post.url)
        if post.url in seen or post.url in existing:
            continue
        if not _mentions_name(post, names):
            continue
        notable, reason = is_notable(post, config)
        if not notable:
            continue
        signals.append(
            store.insert_signal(
                competitor=competitor.name,
                signal_type=SIGNAL_TYPE_MENTION,
                summary=_mention_summary(post, reason),
                source_url=post.url,
            )
        )

    store.insert_snapshot(
        competitor=competitor.name,
        snapshot_type=SNAPSHOT_TYPE_MENTIONS,
        content_hash=str(len(seen | new_urls)),
        raw_content=json.dumps(
            {"seen_urls": sorted(seen | new_urls)},
            ensure_ascii=False,
        ),
        captured_at=now.isoformat(),
    )
    return signals


def poll_all_mentions(
    store: CompetitorStore,
    *,
    config: CompetitorConfig,
    http: Optional[JsonHttpClient] = None,
    twitter_bearer: Optional[str] = None,
    ph_token: Optional[str] = None,
    sleep_fn: Optional[SleepFn] = None,
    now_fn: Optional[NowFn] = None,
) -> tuple[list[CompetitorSignal], list[str], list[tuple[str, str]]]:
    """One Product Hunt fetch, then per-competitor HN/Twitter."""
    client = http or JsonHttpClient(user_agent=config.user_agent)
    token = ph_token if ph_token is not None else producthunt_token()
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    start_time = (now - timedelta(days=config.mention_lookback_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    ph_posts: list[PublicPost] = []
    if token:
        try:
            ph_posts = search_producthunt(
                client,
                token=token,
                since=start_time,
                limit=max(20, config.mention_search_limit),
            )
        except JsonHttpError:
            ph_posts = []

    signals: list[CompetitorSignal] = []
    skipped: list[str] = []
    errors: list[tuple[str, str]] = []
    for item in config.competitors:
        latest = store.latest_snapshot(item.name, SNAPSHOT_TYPE_MENTIONS)
        if latest is not None and _too_soon(
            latest.captured_at, config.mention_min_interval_seconds, now
        ):
            skipped.append(f"{item.name}:mentions")
            continue
        try:
            signals.extend(
                poll_mentions(
                    item,
                    store=store,
                    config=config,
                    http=client,
                    twitter_bearer=twitter_bearer,
                    ph_token=token,
                    sleep_fn=sleep_fn,
                    now_fn=now_fn,
                    producthunt_posts=ph_posts,
                )
            )
        except Exception as exc:
            errors.append((f"{item.name}:mentions", str(exc)))
    return signals, skipped, errors


def search_producthunt(
    http: JsonHttpClient,
    *,
    token: str,
    since: str,
    limit: int = 20,
) -> list[PublicPost]:
    payload = http.post_json(
        PRODUCTHUNT_GRAPHQL_URL,
        json_body={
            "query": PRODUCTHUNT_POSTS_QUERY,
            "variables": {"since": since, "first": int(limit)},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    edges = (((payload.get("data") or {}).get("posts") or {}).get("edges")) or []
    posts: list[PublicPost] = []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        post = _post_from_producthunt(node)
        if post:
            posts.append(post)
    return posts


def is_notable(post: PublicPost, config: CompetitorConfig) -> tuple[bool, str]:
    blob = f"{post.title}\n{post.body}"
    if LAUNCH_RE.search(blob):
        return True, "launch"
    if FUNDING_RE.search(blob):
        return True, "funding"
    points = post.points or 0
    comments = post.comment_count or 0
    likes = post.like_count or 0
    if post.platform == "hn" and (
        points >= config.hn_min_points or comments >= config.hn_min_comments
    ):
        return True, "engagement"
    if post.platform == "twitter" and likes >= config.twitter_min_likes:
        return True, "engagement"
    if post.platform == "producthunt" and points >= config.producthunt_min_votes:
        return True, "engagement"
    return False, ""


def _mentions_name(post: PublicPost, names: tuple[str, ...]) -> bool:
    blob = f"{post.title}\n{post.body}".lower()
    return any(re.search(rf"\b{re.escape(name.lower())}\b", blob) for name in names)


def _mention_summary(post: PublicPost, reason: str) -> str:
    title = (post.title or "").strip() or "(untitled)"
    if reason == "launch":
        prefix = "Launch"
    elif reason == "funding":
        prefix = "Funding"
    else:
        prefix = "High engagement"
    extras: list[str] = [post.platform]
    if post.points:
        extras.append(f"{post.points} pts")
    if post.like_count:
        extras.append(f"{post.like_count} likes")
    if post.comment_count:
        extras.append(f"{post.comment_count} comments")
    meta = ", ".join(extras)
    return f"{prefix} ({meta}): {title}"


def _post_from_producthunt(node: dict) -> Optional[PublicPost]:
    post_id = str(node.get("id") or "").strip()
    name = str(node.get("name") or "").strip()
    url = str(node.get("url") or "").strip()
    if not post_id or not name:
        return None
    tagline = str(node.get("tagline") or "").strip()
    return PublicPost(
        platform="producthunt",
        post_id=post_id,
        url=url or f"https://www.producthunt.com/posts/{post_id}",
        author="",
        title=name,
        body=tagline,
        created_at=str(node.get("createdAt") or "") or None,
        points=_as_int(node.get("votesCount")),
    )


def _dedupe_posts(posts: list[PublicPost]) -> list[PublicPost]:
    seen: set[str] = set()
    unique: list[PublicPost] = []
    for post in posts:
        if post.url in seen:
            continue
        seen.add(post.url)
        unique.append(post)
    return unique


def _seen_urls(raw: Optional[str]) -> set[str]:
    if not raw:
        return set()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    urls = payload.get("seen_urls") if isinstance(payload, dict) else None
    if not isinstance(urls, list):
        return set()
    return {str(url) for url in urls if url}


def _too_soon(captured_at: str, min_interval_seconds: int, now: datetime) -> bool:
    if min_interval_seconds <= 0:
        return False
    text = captured_at[:-1] + "+00:00" if captured_at.endswith("Z") else captured_at
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() < min_interval_seconds


def _as_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
