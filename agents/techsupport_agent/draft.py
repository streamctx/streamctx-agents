"""Reply drafts via OpenRouter. Never posts to GitHub or Discord."""

from __future__ import annotations

import os
from typing import Callable, Mapping, Optional, Sequence

from agents.techsupport_agent.models import KbMatch, Ticket
from agents.techsupport_agent.pending_approval import (
    MODE_DRAFT_ONLY,
    STATUS_PENDING,
    PendingApprovalStore,
)
from agents.techsupport_agent.storage import TicketStore

ChatFn = Callable[[str, str, Sequence[Mapping[str, str]]], str]
ResolveModelFn = Callable[[], tuple[str, str]]

POSITIONING = """
StreamCtx is a Context Nervous System for AI agents: a lightweight Python SDK
that sits between an app and any LLM API and watches messages for poisoned
context, silent drift, runaway loops, and failures that trace back to earlier
steps. You are drafting a support reply the founder will review before sending.
""".strip()

DRAFT_INSTRUCTIONS = """
Write a support reply the founder can copy-paste to GitHub or Discord.
Requirements:
- Answer from the cited knowledge-base section only. Do not invent APIs, flags, or prices.
- Explicitly name the source as `path § heading` (the citation given below).
- If the ticket is a bug, acknowledge it and point at the documented behavior or workaround.
- If the ticket is a question, give concrete steps from the docs.
- If the ticket is a feature request, say it is noted and point at the closest existing capability.
- Calm, specific, no hype. Return ONLY the reply body.
""".strip()


def draft_fingerprint(ticket_id: str) -> str:
    return f"techsupport-reply|{ticket_id}"


def build_messages(ticket: Ticket, match: KbMatch) -> list[dict[str, str]]:
    user = f"""
Ticket:
- Source: {ticket.source} ({ticket.source_url or ticket.source_ref or 'n/a'})
- Type: {ticket.ticket_type or 'unclassified'}
- Severity: {ticket.severity or 'unscored'}
- Title: {ticket.title}
- Body:
{ticket.body or '(empty)'}

Knowledge-base match (citation: {match.citation}):
- Path: {match.path}
- Heading: {match.heading}
- Excerpt:
{match.excerpt}

{DRAFT_INSTRUCTIONS}
""".strip()
    return [
        {"role": "system", "content": POSITIONING},
        {"role": "user", "content": user},
    ]


def resolve_openrouter_model(
    *,
    api_key: Optional[str] = None,
    preferred_model: Optional[str] = None,
) -> tuple[str, str]:
    """Reuse weekly_content_draft's live free-tier picker. Returns (model_id, key)."""
    from scripts.weekly_content_draft import (
        DEFAULT_PREFERRED_MODEL,
        list_free_models,
        resolve_model,
    )

    key = (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is missing")
    preferred = (
        preferred_model
        if preferred_model is not None
        else (os.environ.get("OPENROUTER_MODEL") or DEFAULT_PREFERRED_MODEL)
    ).strip()
    free_ids = list_free_models(key)
    model, _fallback = resolve_model(preferred, free_ids)
    return model, key


def generate_reply_draft(
    ticket: Ticket,
    match: KbMatch,
    *,
    chat_fn: Optional[ChatFn] = None,
    api_key: Optional[str] = None,
    preferred_model: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[str, str]:
    """Return ``(draft_text, model_id)``. Does not queue or post."""
    messages = build_messages(ticket, match)
    if chat_fn is not None:
        used = model or preferred_model or "stub"
        text = chat_fn(used, "", messages)
        return text.strip(), used

    from scripts.weekly_content_draft import chat_completion

    used, key = (
        (model, (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip())
        if model
        else resolve_openrouter_model(api_key=api_key, preferred_model=preferred_model)
    )
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is missing")
    text = chat_completion(key, used, messages)
    return text.strip(), used


def entry_title(ticket: Ticket) -> str:
    source = ticket.source or "ticket"
    ref = ticket.source_ref or ticket.ticket_id[:8]
    title = (ticket.title or "support ticket").strip()
    return f"{source} #{ref} · {title}"[:160]


def enqueue_draft(
    ticket: Ticket,
    draft: str,
    *,
    tickets: TicketStore,
    store: PendingApprovalStore,
    kb_citation: str,
) -> Optional[str]:
    fingerprint = draft_fingerprint(ticket.ticket_id)
    existing = store.get_by_source_fingerprint(fingerprint)
    if existing is not None:
        return None
    entry = store.create_entry(
        ticket_id=ticket.ticket_id,
        content=draft,
        title=entry_title(ticket),
        target=ticket.source_url or None,
        mode=MODE_DRAFT_ONLY,
        status=STATUS_PENDING,
        source_fingerprint=fingerprint,
        kb_citation=kb_citation,
    )
    tickets.record_draft(
        ticket.ticket_id,
        draft_text=draft,
        draft_entry_id=entry.entry_id,
        status="drafted",
    )
    return entry.entry_id
