"""Directed one-shot competitor check from a Roster Assign brief.

This is the existing snapshot/diff poll for a single config competitor,
forced now (interval bypass). It does not walk ``run_snapshot_poll``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agents.competitor_agent.models import (
    SNAPSHOT_TYPE_CHANGELOG,
    SNAPSHOT_TYPE_GITHUB_RELEASE,
    SNAPSHOT_TYPE_PRICING,
)
from agents.competitor_agent.settings import CompetitorConfig, CompetitorSource
from agents.competitor_agent.pending_approval import queue_signals
from agents.competitor_agent.snapshot import (
    FetchFn,
    LlmFn,
    NowFn,
    poll_github_releases,
    poll_pricing,
    poll_rss,
)
from agents.competitor_agent.storage import CompetitorStore

KIND_PRICING = "pricing"
KIND_GITHUB = "github"
KIND_RSS = "rss"
KINDS = (KIND_PRICING, KIND_GITHUB, KIND_RSS)

REASON_UNKNOWN = "unknown_competitor"
REASON_NO_URL = "no_url"
REASON_CHANGED = "changed"
REASON_UNCHANGED = "unchanged"
REASON_BASELINE = "baseline"

_SNAPSHOT_TYPE = {
    KIND_PRICING: SNAPSHOT_TYPE_PRICING,
    KIND_GITHUB: SNAPSHOT_TYPE_GITHUB_RELEASE,
    KIND_RSS: SNAPSHOT_TYPE_CHANGELOG,
}
_GITHUB_RE = re.compile(r"\b(github|release|releases)\b", re.IGNORECASE)
_RSS_RE = re.compile(r"\b(rss|atom|blog|changelog|feed)\b", re.IGNORECASE)
_PRICING_RE = re.compile(r"\b(pricing|price|prices)\b", re.IGNORECASE)


class DirectedCheckError(ValueError):
    """Parse or config failure before any fetch."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ParsedCheck:
    competitor: CompetitorSource
    kind: str


@dataclass(frozen=True)
class DirectedCheckResult:
    ok: bool
    reason: str
    message: str
    competitor: Optional[str] = None
    kind: Optional[str] = None


def parse_directed_brief(
    text: str,
    config: Optional[CompetitorConfig] = None,
) -> ParsedCheck:
    """Extract a config competitor and snapshot kind from free-form Assign text."""
    body = (text or "").strip()
    if not body:
        raise DirectedCheckError(REASON_UNKNOWN, "unknown competitor / not in config")
    spec = config or CompetitorConfig.load()
    source = _match_competitor(body, spec)
    if source is None:
        raise DirectedCheckError(REASON_UNKNOWN, "unknown competitor / not in config")
    kind = _match_kind(body)
    if not _has_source_url(source, kind):
        raise DirectedCheckError(
            REASON_NO_URL,
            f"no URL in config for {source.name} {kind}",
        )
    return ParsedCheck(competitor=source, kind=kind)


def run_directed_check(
    text: str,
    *,
    store: CompetitorStore,
    config: Optional[CompetitorConfig] = None,
    fetch_fn: Optional[FetchFn] = None,
    llm_fn: Optional[LlmFn] = None,
    now_fn: Optional[NowFn] = None,
    db_path: Optional[Path | str] = None,
) -> DirectedCheckResult:
    """Force one snapshot check. ``db_path`` is unused when ``store`` is passed."""
    del db_path
    spec = config or CompetitorConfig.load()
    try:
        parsed = parse_directed_brief(text, spec)
    except DirectedCheckError as exc:
        return DirectedCheckResult(ok=False, reason=exc.reason, message=exc.message)

    source = parsed.competitor
    kind = parsed.kind
    snapshot_type = _SNAPSHOT_TYPE[kind]
    before = store.latest_snapshot(source.name, snapshot_type)

    if kind == KIND_PRICING:
        signal = poll_pricing(
            source,
            store=store,
            config=spec,
            fetch_fn=fetch_fn,
            llm_fn=llm_fn,
            now_fn=now_fn,
            min_interval_seconds=0,
        )
        signals = (signal,) if signal is not None else ()
    elif kind == KIND_GITHUB:
        signals = tuple(
            poll_github_releases(
                source,
                store=store,
                config=spec,
                fetch_fn=fetch_fn,
                now_fn=now_fn,
                min_interval_seconds=0,
            )
        )
    else:
        signals = tuple(
            poll_rss(
                source,
                store=store,
                config=spec,
                fetch_fn=fetch_fn,
                now_fn=now_fn,
                min_interval_seconds=0,
            )
        )

    if signals:
        queue_signals(signals, db_path=store.db_path, enable_notifications=False)
        summary = signals[0].summary
        extra = f" (+{len(signals) - 1} more)" if len(signals) > 1 else ""
        return DirectedCheckResult(
            ok=True,
            reason=REASON_CHANGED,
            message=f"changed: {summary}{extra}",
            competitor=source.name,
            kind=kind,
        )

    after = store.latest_snapshot(source.name, snapshot_type)
    if before is None and after is not None:
        return DirectedCheckResult(
            ok=True,
            reason=REASON_BASELINE,
            message=f"baseline captured for {source.name} {kind}; no prior snapshot to compare",
            competitor=source.name,
            kind=kind,
        )
    since = (before.captured_at if before is not None else None) or "unknown"
    return DirectedCheckResult(
        ok=True,
        reason=REASON_UNCHANGED,
        message=f"no change since {since}",
        competitor=source.name,
        kind=kind,
    )


def _match_competitor(text: str, config: CompetitorConfig) -> Optional[CompetitorSource]:
    haystack = text.lower()
    for item in sorted(config.competitors, key=lambda row: len(row.name), reverse=True):
        pattern = re.compile(rf"\b{re.escape(item.name)}\b", re.IGNORECASE)
        if pattern.search(text):
            return item
        if item.name.lower() in haystack:
            return item
    return None


def _match_kind(text: str) -> str:
    if _GITHUB_RE.search(text):
        return KIND_GITHUB
    if _RSS_RE.search(text):
        return KIND_RSS
    if _PRICING_RE.search(text):
        return KIND_PRICING
    return KIND_PRICING


def _has_source_url(source: CompetitorSource, kind: str) -> bool:
    if kind == KIND_PRICING:
        return bool(source.pricing_url)
    if kind == KIND_GITHUB:
        return bool(source.github_repo)
    return bool(source.rss_url)
