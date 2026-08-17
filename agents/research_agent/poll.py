"""Stage 1 orchestrator: poll arXiv + GitHub and persist new research_ideas rows."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

from agents.research_agent.arxiv import fetch_arxiv_papers, papers_to_source_items
from agents.research_agent.github import fetch_github_repos, repos_to_source_items
from agents.research_agent.http import SleepFn
from agents.research_agent.models import (
    SOURCE_TYPE_ARXIV,
    SOURCE_TYPE_GITHUB,
    STATUS_NEW,
    PollResult,
    ResearchIdea,
    SourceItem,
)
from agents.research_agent.settings import ResearchConfig, default_config
from agents.research_agent.storage import ResearchStore

NowFn = Callable[[], datetime]


def too_soon(last_polled_at: Optional[str], min_interval_seconds: int, now: datetime) -> bool:
    if not last_polled_at or min_interval_seconds <= 0:
        return False
    previous = _parse_ts(last_polled_at)
    elapsed = (now - previous).total_seconds()
    return elapsed < min_interval_seconds


def persist_items(
    store: ResearchStore,
    items: list[SourceItem],
    *,
    detected_at: Optional[str] = None,
) -> tuple[tuple[ResearchIdea, ...], tuple[str, ...]]:
    """Insert new URLs as ``status=new`` ideas. Scores stay null until Stage 3."""
    inserted: list[ResearchIdea] = []
    skipped: list[str] = []
    stamp = detected_at or now_iso()
    for item in items:
        existing = store.get_by_source_url(item.source_url)
        if existing is not None:
            skipped.append(item.source_url)
            continue
        try:
            idea = store.insert_idea(
                source_url=item.source_url,
                source_type=item.source_type,
                title=item.title,
                status=STATUS_NEW,
                detected_at=stamp,
                content_excerpt=item.content_excerpt,
            )
        except sqlite3.IntegrityError:
            skipped.append(item.source_url)
            continue
        inserted.append(idea)
    return tuple(inserted), tuple(skipped)


def poll_arxiv(
    store: ResearchStore,
    *,
    config: Optional[ResearchConfig] = None,
    fetch_fn=None,
    sleep_fn: Optional[SleepFn] = None,
    now_fn: Optional[NowFn] = None,
) -> PollResult:
    spec = config or default_config()
    now = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    last = store.last_polled_at(SOURCE_TYPE_ARXIV)
    if too_soon(last, spec.arxiv_min_interval_seconds, now):
        return PollResult((), (), (SOURCE_TYPE_ARXIV,), ())
    try:
        papers = fetch_arxiv_papers(spec, fetch_fn=fetch_fn, sleep_fn=sleep_fn)
        items = papers_to_source_items(papers, excerpt_max_chars=spec.excerpt_max_chars)
        inserted, skipped = persist_items(store, items, detected_at=now.isoformat())
        store.set_last_polled_at(SOURCE_TYPE_ARXIV, now.isoformat())
        return PollResult(inserted, skipped, (), ())
    except Exception as exc:
        return PollResult((), (), (), ((SOURCE_TYPE_ARXIV, str(exc)),))


def poll_github(
    store: ResearchStore,
    *,
    config: Optional[ResearchConfig] = None,
    fetch_fn=None,
    sleep_fn: Optional[SleepFn] = None,
    now_fn: Optional[NowFn] = None,
    token: Optional[str] = None,
) -> PollResult:
    spec = config or default_config()
    now = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    last = store.last_polled_at(SOURCE_TYPE_GITHUB)
    if too_soon(last, spec.github_min_interval_seconds, now):
        return PollResult((), (), (SOURCE_TYPE_GITHUB,), ())
    try:
        repos = fetch_github_repos(
            spec,
            fetch_fn=fetch_fn,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
            token=token,
        )
        items = repos_to_source_items(repos, excerpt_max_chars=spec.excerpt_max_chars)
        inserted, skipped = persist_items(store, items, detected_at=now.isoformat())
        store.set_last_polled_at(SOURCE_TYPE_GITHUB, now.isoformat())
        return PollResult(inserted, skipped, (), ())
    except Exception as exc:
        return PollResult((), (), (), ((SOURCE_TYPE_GITHUB, str(exc)),))


def run_poll(
    store: ResearchStore,
    *,
    config: Optional[ResearchConfig] = None,
    arxiv: bool = True,
    github: bool = True,
    fetch_arxiv_fn=None,
    fetch_github_fn=None,
    sleep_fn: Optional[SleepFn] = None,
    now_fn: Optional[NowFn] = None,
    token: Optional[str] = None,
) -> PollResult:
    spec = config or default_config()
    sleeper = sleep_fn or (lambda _seconds: None)
    inserted: list[ResearchIdea] = []
    skipped_duplicate: list[str] = []
    skipped_interval: list[str] = []
    errors: list[tuple[str, str]] = []
    fetched = False

    if arxiv:
        result = poll_arxiv(
            store,
            config=spec,
            fetch_fn=fetch_arxiv_fn,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
        )
        inserted.extend(result.inserted)
        skipped_duplicate.extend(result.skipped_duplicate)
        skipped_interval.extend(result.skipped_interval)
        errors.extend(result.errors)
        fetched = SOURCE_TYPE_ARXIV not in result.skipped_interval

    if github:
        if fetched and spec.poll_delay_seconds > 0:
            sleeper(spec.poll_delay_seconds)
        result = poll_github(
            store,
            config=spec,
            fetch_fn=fetch_github_fn,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
            token=token,
        )
        inserted.extend(result.inserted)
        skipped_duplicate.extend(result.skipped_duplicate)
        skipped_interval.extend(result.skipped_interval)
        errors.extend(result.errors)

    return PollResult(
        tuple(inserted),
        tuple(skipped_duplicate),
        tuple(skipped_interval),
        tuple(errors),
    )


def summarize(result: PollResult) -> str:
    return (
        f"inserted={len(result.inserted)} duplicates={len(result.skipped_duplicate)} "
        f"skipped_interval={len(result.skipped_interval)} errors={len(result.errors)}"
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(raw: str) -> datetime:
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
