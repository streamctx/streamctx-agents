"""Poll GitHub Issues + Discord into ``tickets``. Same interval/dedupe style as research_agent.poll."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

from agents.techsupport_agent.discord import fetch_channel_messages
from agents.techsupport_agent.github_issues import fetch_open_issues
from agents.techsupport_agent.models import (
    SOURCE_DISCORD,
    SOURCE_GITHUB,
    IncomingItem,
    PollResult,
    Ticket,
)
from agents.techsupport_agent.settings import TechSupportConfig, default_config
from agents.techsupport_agent.storage import TicketStore

NowFn = Callable[[], datetime]


def too_soon(last_polled_at: Optional[str], min_interval_seconds: int, now: datetime) -> bool:
    if not last_polled_at or min_interval_seconds <= 0:
        return False
    previous = _parse_ts(last_polled_at)
    elapsed = (now - previous).total_seconds()
    return elapsed < min_interval_seconds


def persist_items(
    store: TicketStore,
    items: list[IncomingItem],
    *,
    detected_at: Optional[str] = None,
) -> tuple[tuple[Ticket, ...], tuple[str, ...]]:
    inserted: list[Ticket] = []
    skipped: list[str] = []
    stamp = detected_at or now_iso()
    for item in items:
        existing = store.get_by_fingerprint(item.source_fingerprint)
        if existing is not None:
            skipped.append(item.source_fingerprint)
            continue
        body = item.body
        if item.labels:
            body = f"{body}\n\nLabels: {', '.join(item.labels)}".strip()
        try:
            ticket = store.insert_ticket(
                source=item.source,
                source_ref=item.source_ref,
                source_url=item.source_url,
                title=item.title,
                body=body,
                author=item.author,
                source_fingerprint=item.source_fingerprint,
                created_at=stamp,
            )
        except sqlite3.IntegrityError:
            skipped.append(item.source_fingerprint)
            continue
        inserted.append(ticket)
    return tuple(inserted), tuple(skipped)


def poll_github(
    store: TicketStore,
    *,
    config: Optional[TechSupportConfig] = None,
    fetch_fn=None,
    now_fn: Optional[NowFn] = None,
    token: Optional[str] = None,
) -> PollResult:
    spec = config or default_config()
    now = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    last = store.last_polled_at(SOURCE_GITHUB)
    if too_soon(last, spec.github_min_interval_seconds, now):
        return PollResult((), (), (SOURCE_GITHUB,), ())
    try:
        items = fetch_open_issues(spec, fetch_fn=fetch_fn, token=token)
        inserted, skipped = persist_items(store, items, detected_at=now.isoformat())
        store.set_last_polled_at(SOURCE_GITHUB, now.isoformat())
        return PollResult(inserted, skipped, (), ())
    except Exception as exc:
        return PollResult((), (), (), ((SOURCE_GITHUB, str(exc)),))


def poll_discord(
    store: TicketStore,
    *,
    config: Optional[TechSupportConfig] = None,
    fetch_fn=None,
    now_fn: Optional[NowFn] = None,
    token: Optional[str] = None,
) -> PollResult:
    spec = config or default_config()
    now = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    if not (spec.discord_channel_id or "").strip():
        return PollResult((), (), (SOURCE_DISCORD,), ())
    last = store.last_polled_at(SOURCE_DISCORD)
    if too_soon(last, spec.discord_min_interval_seconds, now):
        return PollResult((), (), (SOURCE_DISCORD,), ())
    try:
        items = fetch_channel_messages(spec, fetch_fn=fetch_fn, token=token)
        inserted, skipped = persist_items(store, items, detected_at=now.isoformat())
        store.set_last_polled_at(SOURCE_DISCORD, now.isoformat())
        return PollResult(inserted, skipped, (), ())
    except Exception as exc:
        return PollResult((), (), (), ((SOURCE_DISCORD, str(exc)),))


def run_poll(
    store: TicketStore,
    *,
    config: Optional[TechSupportConfig] = None,
    github: bool = True,
    discord: bool = True,
    fetch_github_fn=None,
    fetch_discord_fn=None,
    now_fn: Optional[NowFn] = None,
    github_token_value: Optional[str] = None,
    discord_token_value: Optional[str] = None,
) -> PollResult:
    spec = config or default_config()
    inserted: list[Ticket] = []
    skipped_duplicate: list[str] = []
    skipped_interval: list[str] = []
    errors: list[tuple[str, str]] = []

    if github:
        result = poll_github(
            store,
            config=spec,
            fetch_fn=fetch_github_fn,
            now_fn=now_fn,
            token=github_token_value,
        )
        inserted.extend(result.inserted)
        skipped_duplicate.extend(result.skipped_duplicate)
        skipped_interval.extend(result.skipped_interval)
        errors.extend(result.errors)

    if discord:
        result = poll_discord(
            store,
            config=spec,
            fetch_fn=fetch_discord_fn,
            now_fn=now_fn,
            token=discord_token_value,
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
