"""Personalized outreach drafts via OpenRouter. Never sends a message."""

from __future__ import annotations

import os
from typing import Callable, Mapping, Optional, Sequence

from agents.presales_agent.flags import flag_draft
from agents.presales_agent.models import (
    DRAFT_DRAFTED,
    DRAFT_PENDING,
    FLAG_NONE,
    Lead,
    RunSummary,
)
from agents.presales_agent.pending_approval import (
    MODE_DRAFT_ONLY,
    STATUS_PENDING,
    PendingApprovalStore,
)
from agents.presales_agent.scoring import ScoringRules, score_lead
from agents.presales_agent.storage import LeadStore

ChatFn = Callable[[str, str, Sequence[Mapping[str, str]]], str]
ResolveModelFn = Callable[[], tuple[str, str]]

POSITIONING = """
StreamCtx is a Context Nervous System for AI agents: a lightweight Python SDK
that sits between an app and any LLM API and watches messages for poisoned
context, silent drift, runaway loops, and failures that trace back to earlier
steps. Positioning: most tools answer "how many tokens?"; StreamCtx answers
"why is my agent broken, what caused it, and how do I fix it?"

Pricing (do not invent other prices or plans; do not quote numbers):
- Core SDK (all features + local SQLite) is free forever, MIT-licensed.
- A managed offering is planned; do not claim it is shipped.
""".strip()

DRAFT_INSTRUCTIONS = """
Write a short LinkedIn connection note / InMail the founder can copy-paste.
Requirements:
- Reference this person's actual role and company in a specific way.
- Connect that context to a concrete agent-context failure mode StreamCtx addresses.
- No generic templates ("hope this finds you well", "I'd love to connect",
  "just wanted to reach out", "synergy", "circle back").
- Do not invent metrics, customers, or shipped features.
- Do not include pricing, discounts, contracts, or deal terms.
- Do not offer to send a calendar invite automatically.
- 80–160 words. Return ONLY the message body.
""".strip()


def draft_fingerprint(lead_id: str) -> str:
    return f"presales-outreach|{lead_id}"


def build_messages(lead: Lead) -> list[dict[str, str]]:
    score_line = (
        f"{lead.score:.2f} — {lead.score_rationale}"
        if lead.score is not None
        else lead.score_rationale or "(unscored)"
    )
    user = f"""
Lead:
- Name: {lead.name or '(unknown)'}
- Title: {lead.title or '(unknown)'}
- Company: {lead.company or '(unknown)'}
- Company size: {lead.company_size or '(unknown)'}
- Industry: {lead.industry or '(unknown)'}
- Score: {score_line}

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


def generate_outreach_draft(
    lead: Lead,
    *,
    chat_fn: Optional[ChatFn] = None,
    api_key: Optional[str] = None,
    preferred_model: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[str, str]:
    """Return ``(draft_text, model_id)``. Does not queue or send."""
    messages = build_messages(lead)
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


def entry_title(lead: Lead) -> str:
    role = lead.title.strip() if lead.title else "lead"
    company = lead.company.strip() if lead.company else "unknown company"
    return f"{lead.name} · {role} @ {company}"


def enqueue_draft(
    lead: Lead,
    draft: str,
    *,
    leads: LeadStore,
    store: PendingApprovalStore,
    flag: str = FLAG_NONE,
) -> Optional[str]:
    fingerprint = draft_fingerprint(lead.lead_id)
    existing = store.get_by_source_fingerprint(fingerprint)
    if existing is not None:
        return None
    leads.record_draft(
        lead.lead_id,
        draft_text=draft,
        draft_status=DRAFT_DRAFTED,
        flag=flag,
    )
    entry = store.create_entry(
        lead_id=lead.lead_id,
        content=draft,
        title=entry_title(lead),
        target=lead.linkedin_url,
        mode=MODE_DRAFT_ONLY,
        status=STATUS_PENDING,
        source_fingerprint=fingerprint,
        flag=flag,
    )
    leads.record_draft(
        lead.lead_id,
        draft_text=draft,
        draft_status=DRAFT_PENDING,
        draft_entry_id=entry.entry_id,
        flag=flag,
    )
    return entry.entry_id


def score_all_leads(
    leads: LeadStore,
    *,
    rules: Optional[ScoringRules] = None,
    only_unscored: bool = False,
) -> int:
    spec = rules or ScoringRules.load()
    rows = leads.leads_needing_score() if only_unscored else leads.list_leads()
    for lead in rows:
        result = score_lead(lead, spec)
        leads.record_score(lead.lead_id, result.score, result.rationale)
    return len(rows)


def generate_and_queue(
    leads: LeadStore,
    store: PendingApprovalStore,
    *,
    rules: Optional[ScoringRules] = None,
    min_score: Optional[float] = None,
    chat_fn: Optional[ChatFn] = None,
    api_key: Optional[str] = None,
    preferred_model: Optional[str] = None,
    resolve_model_fn: Optional[ResolveModelFn] = None,
    limit: Optional[int] = None,
) -> RunSummary:
    """Score → draft (OpenRouter) → pending_approval. Never sends."""
    spec = rules or ScoringRules.load()
    threshold = spec.min_score if min_score is None else min_score
    score_all_leads(leads, rules=spec, only_unscored=False)
    candidates = leads.leads_ready_for_draft(min_score=threshold)
    if limit is not None:
        candidates = candidates[: int(limit)]

    model_used = ""
    resolved_key = ""
    if chat_fn is None:
        if resolve_model_fn is not None:
            model_used, resolved_key = resolve_model_fn()
        else:
            model_used, resolved_key = resolve_openrouter_model(
                api_key=api_key,
                preferred_model=preferred_model,
            )

    queued: list[str] = []
    skipped = 0
    flagged = 0
    errors: list[str] = []
    for lead in candidates:
        try:
            draft, used = generate_outreach_draft(
                lead,
                chat_fn=chat_fn,
                api_key=resolved_key or api_key,
                preferred_model=preferred_model,
                model=model_used or None,
            )
            model_used = used or model_used
        except Exception as exc:
            errors.append(f"{lead.name}: {exc}")
            skipped += 1
            continue
        if not draft.strip():
            skipped += 1
            errors.append(f"{lead.name}: empty draft")
            continue
        flag = flag_draft(draft)
        if flag != FLAG_NONE:
            flagged += 1
        entry_id = enqueue_draft(lead, draft, leads=leads, store=store, flag=flag)
        if entry_id:
            queued.append(entry_id)
        else:
            skipped += 1

    return RunSummary(
        scored=len(leads.list_leads()),
        queued=len(queued),
        skipped=skipped,
        flagged=flagged,
        errors=tuple(errors),
        entry_ids=tuple(queued),
        model_used=model_used,
    )
