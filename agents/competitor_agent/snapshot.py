"""Pricing, GitHub-release, and RSS/Atom snapshot/diff.

``snapshot_and_diff`` is the shared primitive: fetch, hash, compare against
the last ``competitor_snapshots`` row, persist on change, optionally emit
one ``competitor_signals`` row. Pricing uses an LLM for a 1-2 line summary;
GitHub release notes and RSS entries are stored as short human-readable
summaries (one signal per new tag or post).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import unified_diff
from typing import Callable, Optional

from agents.competitor_agent.http import (
    FetchError,
    SleepFn,
    fetch_json,
    fetch_text,
    github_headers,
)
from agents.competitor_agent.mentions import poll_all_mentions
from agents.competitor_agent.models import (
    SIGNAL_TYPE_NEW_POST,
    SIGNAL_TYPE_NEW_RELEASE,
    SIGNAL_TYPE_PRICING_CHANGE,
    SNAPSHOT_TYPE_CHANGELOG,
    SNAPSHOT_TYPE_GITHUB_RELEASE,
    SNAPSHOT_TYPE_PRICING,
    CompetitorSignal,
)
from agents.competitor_agent.rss import (
    parse_feed,
    parse_serialized_entries,
    serialize_feed_entries,
    summarize_serialized,
)
from agents.competitor_agent.settings import (
    CompetitorConfig,
    CompetitorSource,
    github_token,
)
from agents.competitor_agent.storage import CompetitorStore

FetchFn = Callable[[], str]
SummarizeFn = Callable[[str, str], tuple[str, str, Optional[str]]]
LlmFn = Callable[[str], str]
NowFn = Callable[[], datetime]

SCRIPT_RE = re.compile(r"<script[\s\S]*?</script>", re.IGNORECASE)
STYLE_RE = re.compile(r"<style[\s\S]*?</style>", re.IGNORECASE)
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

DIFF_MAX_CHARS = 6000
RELEASE_SUMMARY_MAX_CHARS = 4000
GITHUB_RELEASES_URL = "https://api.github.com/repos/{repo}/releases?per_page={per_page}"

_SIGNAL_FOR_SNAPSHOT = {
    SNAPSHOT_TYPE_PRICING: SIGNAL_TYPE_PRICING_CHANGE,
    SNAPSHOT_TYPE_GITHUB_RELEASE: SIGNAL_TYPE_NEW_RELEASE,
    SNAPSHOT_TYPE_CHANGELOG: SIGNAL_TYPE_NEW_POST,
}

RSS_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml, "
    "text/xml;q=0.9, */*;q=0.8"
)


@dataclass(frozen=True)
class SnapshotPollResult:
    signals: tuple[CompetitorSignal, ...]
    skipped: tuple[str, ...]
    errors: tuple[tuple[str, str], ...]


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalize_html(html: str) -> str:
    """Strip scripts/styles/tags so pricing hashes ignore most chrome churn."""
    text = SCRIPT_RE.sub(" ", html or "")
    text = STYLE_RE.sub(" ", text)
    text = COMMENT_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    return WS_RE.sub(" ", text).strip()


def snapshot_and_diff(
    competitor: str,
    snapshot_type: str,
    fetch_fn: FetchFn,
    *,
    store: CompetitorStore,
    summarize_fn: Optional[SummarizeFn] = None,
    source_url: Optional[str] = None,
    min_interval_seconds: int = 0,
    now_fn: Optional[NowFn] = None,
) -> Optional[CompetitorSignal]:
    """
    Fetch current content, hash it, and compare to the last snapshot.

    First capture writes a baseline snapshot and returns None. Unchanged
    hashes return None without inserting. On a hash change, a new snapshot
    is stored and one signal is written (via ``summarize_fn`` when given).
    """
    previous, current, skipped = capture_snapshot(
        competitor,
        snapshot_type,
        fetch_fn,
        store=store,
        min_interval_seconds=min_interval_seconds,
        now_fn=now_fn,
    )
    if skipped or current is None or previous is None:
        return None
    if content_hash(previous) == content_hash(current):
        return None

    if summarize_fn is not None:
        signal_type, summary, url = summarize_fn(previous, current)
        url = url or source_url
    else:
        signal_type = _SIGNAL_FOR_SNAPSHOT.get(snapshot_type, SIGNAL_TYPE_PRICING_CHANGE)
        summary = f"Detected {snapshot_type} change."
        url = source_url

    return store.insert_signal(
        competitor=competitor,
        signal_type=signal_type,
        summary=summary,
        source_url=url,
    )


def capture_snapshot(
    competitor: str,
    snapshot_type: str,
    fetch_fn: FetchFn,
    *,
    store: CompetitorStore,
    min_interval_seconds: int = 0,
    now_fn: Optional[NowFn] = None,
) -> tuple[Optional[str], Optional[str], bool]:
    """
    Return ``(previous_content, current_content, skipped)``.

    Skips the fetch when the last snapshot is still inside the min interval.
    Writes a snapshot only on first capture or when the hash changes.
    """
    now = now_fn or (lambda: datetime.now(timezone.utc))
    latest = store.latest_snapshot(competitor, snapshot_type)
    if latest is not None and _too_soon(latest.captured_at, min_interval_seconds, now()):
        return latest.raw_content, None, True

    current = fetch_fn()
    digest = content_hash(current)
    if latest is not None and latest.content_hash == digest:
        return latest.raw_content, current, False

    store.insert_snapshot(
        competitor=competitor,
        snapshot_type=snapshot_type,
        content_hash=digest,
        raw_content=current,
        captured_at=now().isoformat(),
    )
    previous = latest.raw_content if latest is not None else None
    return previous, current, False


def poll_pricing(
    competitor: CompetitorSource,
    *,
    store: CompetitorStore,
    config: CompetitorConfig,
    llm_fn: Optional[LlmFn] = None,
    fetch_fn: Optional[FetchFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    now_fn: Optional[NowFn] = None,
) -> Optional[CompetitorSignal]:
    if not competitor.pricing_url:
        return None

    def _fetch() -> str:
        html = fetch_text(
            competitor.pricing_url or "",
            headers={"User-Agent": config.user_agent, "Accept": "text/html,application/xhtml+xml"},
            max_retries=config.max_retries,
            backoff_base_seconds=config.backoff_base_seconds,
            max_backoff_seconds=config.max_backoff_seconds,
            sleep_fn=sleep_fn,
        )
        normalized = normalize_html(html)
        if not normalized:
            raise FetchError(200, "empty pricing page after normalize", competitor.pricing_url or "")
        return normalized

    def _summarize(old: str, new: str) -> tuple[str, str, Optional[str]]:
        return (
            SIGNAL_TYPE_PRICING_CHANGE,
            summarize_pricing_diff(old, new, llm_fn=llm_fn),
            competitor.pricing_url,
        )

    return snapshot_and_diff(
        competitor.name,
        SNAPSHOT_TYPE_PRICING,
        fetch_fn or _fetch,
        store=store,
        summarize_fn=_summarize,
        source_url=competitor.pricing_url,
        min_interval_seconds=config.pricing_min_interval_seconds,
        now_fn=now_fn,
    )


def poll_github_releases(
    competitor: CompetitorSource,
    *,
    store: CompetitorStore,
    config: CompetitorConfig,
    fetch_fn: Optional[FetchFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    now_fn: Optional[NowFn] = None,
    token: Optional[str] = None,
) -> list[CompetitorSignal]:
    """Poll GitHub releases and emit one ``new_release`` signal per new tag."""
    if not competitor.github_repo:
        return []

    def _fetch() -> str:
        url = GITHUB_RELEASES_URL.format(
            repo=competitor.github_repo,
            per_page=config.github_per_page,
        )
        payload = fetch_json(
            url,
            headers=github_headers(config.user_agent, token if token is not None else github_token()),
            max_retries=config.max_retries,
            backoff_base_seconds=config.backoff_base_seconds,
            max_backoff_seconds=config.max_backoff_seconds,
            sleep_fn=sleep_fn,
        )
        if not isinstance(payload, list):
            raise FetchError(200, f"expected release list, got {type(payload).__name__}", url)
        return serialize_releases(payload)

    previous, current, skipped = capture_snapshot(
        competitor.name,
        SNAPSHOT_TYPE_GITHUB_RELEASE,
        fetch_fn or _fetch,
        store=store,
        min_interval_seconds=config.github_min_interval_seconds,
        now_fn=now_fn,
    )
    if skipped or current is None or previous is None:
        return []
    if content_hash(previous) == content_hash(current):
        return []

    old_tags = {item["tag"] for item in _parse_releases(previous)}
    signals: list[CompetitorSignal] = []
    for release in _parse_releases(current):
        tag = release.get("tag") or ""
        if not tag or tag in old_tags:
            continue
        signals.append(
            store.insert_signal(
                competitor=competitor.name,
                signal_type=SIGNAL_TYPE_NEW_RELEASE,
                summary=_release_summary(release),
                source_url=release.get("html_url") or None,
            )
        )
    return signals


def poll_rss(
    competitor: CompetitorSource,
    *,
    store: CompetitorStore,
    config: CompetitorConfig,
    fetch_fn: Optional[FetchFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    now_fn: Optional[NowFn] = None,
) -> list[CompetitorSignal]:
    """Poll an RSS/Atom feed and emit one ``new_post`` signal per new entry."""
    if not competitor.rss_url:
        return []

    def _fetch() -> str:
        xml = fetch_text(
            competitor.rss_url or "",
            headers={"User-Agent": config.user_agent, "Accept": RSS_ACCEPT},
            max_retries=config.max_retries,
            backoff_base_seconds=config.backoff_base_seconds,
            max_backoff_seconds=config.max_backoff_seconds,
            sleep_fn=sleep_fn,
        )
        return serialize_feed_entries(parse_feed(xml))

    previous, current, skipped = capture_snapshot(
        competitor.name,
        SNAPSHOT_TYPE_CHANGELOG,
        fetch_fn or _fetch,
        store=store,
        min_interval_seconds=config.rss_min_interval_seconds,
        now_fn=now_fn,
    )
    if skipped or current is None or previous is None:
        return []
    if content_hash(previous) == content_hash(current):
        return []

    old_ids = {str(item.get("id") or "") for item in parse_serialized_entries(previous)}
    signals: list[CompetitorSignal] = []
    for row in parse_serialized_entries(current):
        entry_id = str(row.get("id") or "")
        if not entry_id or entry_id in old_ids:
            continue
        link = str(row.get("link") or "").strip() or None
        signals.append(
            store.insert_signal(
                competitor=competitor.name,
                signal_type=SIGNAL_TYPE_NEW_POST,
                summary=summarize_serialized(row),
                source_url=link,
            )
        )
    return signals


def run_snapshot_poll(
    store: CompetitorStore,
    *,
    config: Optional[CompetitorConfig] = None,
    llm_fn: Optional[LlmFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    now_fn: Optional[NowFn] = None,
    token: Optional[str] = None,
    http=None,
    twitter_bearer: Optional[str] = None,
    ph_token: Optional[str] = None,
) -> SnapshotPollResult:
    """Walk the config list: pricing, GitHub, RSS, then mentions."""
    spec = config or CompetitorConfig.load()
    sleeper = sleep_fn or time.sleep
    signals: list[CompetitorSignal] = []
    skipped: list[str] = []
    errors: list[tuple[str, str]] = []
    fetched = False

    def _pace() -> None:
        nonlocal fetched
        if fetched and spec.poll_delay_seconds > 0:
            sleeper(spec.poll_delay_seconds)
        fetched = True

    for item in spec.with_pricing():
        try:
            before = store.latest_snapshot(item.name, SNAPSHOT_TYPE_PRICING)
            if before is not None and _too_soon(
                before.captured_at,
                spec.pricing_min_interval_seconds,
                (now_fn or (lambda: datetime.now(timezone.utc)))(),
            ):
                skipped.append(f"{item.name}:pricing")
                continue
            _pace()
            signal = poll_pricing(
                item,
                store=store,
                config=spec,
                llm_fn=llm_fn,
                sleep_fn=sleep_fn,
                now_fn=now_fn,
            )
            if signal is not None:
                signals.append(signal)
        except Exception as exc:
            errors.append((f"{item.name}:pricing", str(exc)))

    for item in spec.with_github():
        try:
            before = store.latest_snapshot(item.name, SNAPSHOT_TYPE_GITHUB_RELEASE)
            if before is not None and _too_soon(
                before.captured_at,
                spec.github_min_interval_seconds,
                (now_fn or (lambda: datetime.now(timezone.utc)))(),
            ):
                skipped.append(f"{item.name}:github_release")
                continue
            _pace()
            signals.extend(
                poll_github_releases(
                    item,
                    store=store,
                    config=spec,
                    sleep_fn=sleep_fn,
                    now_fn=now_fn,
                    token=token,
                )
            )
        except Exception as exc:
            errors.append((f"{item.name}:github_release", str(exc)))

    for item in spec.with_rss():
        try:
            before = store.latest_snapshot(item.name, SNAPSHOT_TYPE_CHANGELOG)
            if before is not None and _too_soon(
                before.captured_at,
                spec.rss_min_interval_seconds,
                (now_fn or (lambda: datetime.now(timezone.utc)))(),
            ):
                skipped.append(f"{item.name}:changelog")
                continue
            _pace()
            signals.extend(
                poll_rss(
                    item,
                    store=store,
                    config=spec,
                    sleep_fn=sleep_fn,
                    now_fn=now_fn,
                )
            )
        except Exception as exc:
            errors.append((f"{item.name}:changelog", str(exc)))

    if spec.mentions_enabled:
        try:
            mention_signals, mention_skipped, mention_errors = poll_all_mentions(
                store,
                config=spec,
                http=http,
                twitter_bearer=twitter_bearer,
                ph_token=ph_token,
                sleep_fn=sleep_fn,
                now_fn=now_fn,
            )
            signals.extend(mention_signals)
            skipped.extend(mention_skipped)
            errors.extend(mention_errors)
        except Exception as exc:
            errors.append(("mentions", str(exc)))

    return SnapshotPollResult(
        signals=tuple(signals),
        skipped=tuple(skipped),
        errors=tuple(errors),
    )


def summarize_pricing_diff(
    old_content: str,
    new_content: str,
    *,
    llm_fn: Optional[LlmFn] = None,
) -> str:
    diff = "\n".join(
        unified_diff(
            old_content.splitlines(),
            new_content.splitlines(),
            fromfile="previous",
            tofile="current",
            lineterm="",
            n=2,
        )
    )
    if not diff.strip():
        return "Pricing page changed but the visible-text diff was empty."
    clipped = diff if len(diff) <= DIFF_MAX_CHARS else diff[:DIFF_MAX_CHARS] + "\n..."
    prompt = (
        "You summarize competitor pricing-page changes for StreamCtx, an "
        "open-source LLM agent observability SDK.\n\n"
        "Write 1-2 factual lines a human can read, e.g. "
        "'Added a new $99/mo tier.' Do not paste HTML. If the diff looks like "
        "nav/cookie/analytics churn rather than pricing, say so in one line.\n\n"
        f"Unified diff:\n{clipped}"
    )
    writer = llm_fn or _default_llm
    try:
        summary = (writer(prompt) or "").strip()
    except Exception:
        summary = ""
    if summary:
        return summary
    return "Pricing page changed; review the source URL for the new tiers or limits."


def serialize_releases(payload: list[object]) -> str:
    items: list[dict[str, str]] = []
    for row in payload:
        if not isinstance(row, dict) or row.get("draft"):
            continue
        tag = str(row.get("tag_name") or "").strip()
        if not tag:
            continue
        items.append(
            {
                "tag": tag,
                "name": str(row.get("name") or "").strip(),
                "body": str(row.get("body") or "").strip(),
                "html_url": str(row.get("html_url") or "").strip(),
                "published_at": str(row.get("published_at") or "").strip(),
            }
        )
    items.sort(key=lambda item: (item["published_at"], item["tag"]))
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _parse_releases(raw: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _release_summary(release: dict[str, str]) -> str:
    body = (release.get("body") or "").strip()
    if body:
        if len(body) > RELEASE_SUMMARY_MAX_CHARS:
            return body[:RELEASE_SUMMARY_MAX_CHARS] + "\n..."
        return body
    name = (release.get("name") or "").strip()
    tag = (release.get("tag") or "").strip()
    return name or f"Released {tag}"


def _too_soon(captured_at: str, min_interval_seconds: int, now: datetime) -> bool:
    if min_interval_seconds <= 0:
        return False
    captured = _parse_ts(captured_at)
    return (now - captured).total_seconds() < min_interval_seconds


def _parse_ts(raw: str) -> datetime:
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _default_llm(prompt: str) -> str:
    from openai import OpenAI

    from shared.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL

    if not OPENROUTER_API_KEY:
        return ""
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Poll competitor pricing, GitHub, RSS, and mention sources."
    )
    parser.add_argument("--config", help="Path to competitors.json")
    parser.add_argument(
        "--db",
        help="SQLite path (default: ~/.streamctx/competitor_agent.db)",
    )
    args = parser.parse_args(argv)
    spec = CompetitorConfig.load(args.config) if args.config else CompetitorConfig.load()
    store = CompetitorStore(db_path=args.db)
    try:
        result = run_snapshot_poll(store, config=spec)
    finally:
        store.close()
    print(
        f"[competitor-agent] signals={len(result.signals)} "
        f"skipped={len(result.skipped)} errors={len(result.errors)}"
    )
    for key, message in result.errors:
        print(f"  error {key}: {message}")
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
