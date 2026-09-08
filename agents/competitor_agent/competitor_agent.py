"""
agents/competitor_agent/competitor_agent.py
Competitor & Market Strategy Agent — summarizes competitor news/updates
into a digest, and drafts positioning notes for human review.
Uses StreamCtx's Poison Detection + Causal Attribution: research input
(pasted search results, article text) can be noisy/malformed, and if
the summary goes wrong, StreamCtx should help point back to which
input chunk caused it.
Never publishes anything, never treats its output as strategy decision
— only ever "suggestion, pending human review."
"""

import argparse

from openai import OpenAI
from streamctx import get_tracker
from agents.competitor_agent.pending_approval import (
    PendingApprovalStore,
    enqueue_research_summary,
    queue_signals,
)
from agents.competitor_agent.settings import CompetitorConfig
from agents.competitor_agent.snapshot import run_snapshot_poll
from agents.competitor_agent.storage import CompetitorStore
from shared.audit_log import log_action
from shared.config import (
    AGENT_IDS,
    STATUS_PENDING,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
)


def run(*, db=None, config_path=None):
    """Poll competitor pricing, GitHub, RSS, and mentions.

    ``run_research_cycle`` remains the interactive summarizer that needs
    pasted research text; this is the parameter-free dashboard entry point.
    """
    spec = (
        CompetitorConfig.load(config_path)
        if config_path
        else CompetitorConfig.load()
    )
    store = CompetitorStore(db_path=db)
    try:
        result = run_snapshot_poll(store, config=spec)
        queue_signals(
            result.signals,
            db_path=store.db_path,
            enable_notifications=False,
        )
        print(
            f"[competitor-agent] signals={len(result.signals)} "
            f"skipped={len(result.skipped)} errors={len(result.errors)}"
        )
        for key, message in result.errors:
            print(f"  error {key}: {message}")
        return result
    finally:
        store.close()


def run_research_cycle(competitor_name, raw_research_text, question):
    """
    competitor_name: e.g. "LangSmith"
    raw_research_text: pasted-in search results / article excerpts /
                        notes — this is the (possibly noisy) input
    question: what to figure out (e.g. "any pricing changes this
              month?", "what's new in their latest release?")
    """
    tracker = get_tracker(AGENT_IDS["competitor"])
    tracker.start()

    tracker.checkpoint()
    print(f"[competitor-agent] Researching: {competitor_name}")
    print(f"[competitor-agent] Question: {question}")

    tracker.checkpoint()
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
    wrapped_client = tracker.wrap(client)
    summary = summarize_research(competitor_name, raw_research_text, question, wrapped_client)

    session_id = tracker.get_session_id()
    tracker.stop()
    stats = tracker.get_stats()

    entry = log_action(
        agent_name=AGENT_IDS["competitor"],
        session_id=session_id,
        action_type="competitor_summary",
        payload={
            "competitor": competitor_name,
            "question": question,
            "raw_research_excerpt": raw_research_text[:500],  # keep log lean
            "summary": summary,
            "tracker_stats": str(stats),
            "note": "Informational summary only — not a strategic recommendation. Any suggested action requires explicit human decision.",
        },
        status=STATUS_PENDING,
    )
    queue = PendingApprovalStore(enable_default_notifier=False)
    try:
        enqueue_research_summary(
            competitor_name=competitor_name,
            question=question,
            summary=summary or "",
            store=queue,
            fingerprint=f"competitor-summary:{entry['id']}",
        )
    finally:
        queue.close()
    print("\n--- Summary ---")
    print(summary)
    print(f"\n[competitor-agent] Logged for review. Entry id: {entry['id']}")
    return entry


def summarize_research(competitor_name, raw_research_text, question, client):
    prompt = (
        "You are a market research assistant summarizing information "
        "about a competitor to an open-source LLM agent observability "
        "SDK called StreamCtx.\n\n"
        "Competitor: " + competitor_name + "\n"
        "Question to answer: " + question + "\n\n"
        "Raw research notes (may include noisy or irrelevant text, "
        "ignore anything that looks like an error page, ad, or "
        "unrelated content):\n" + raw_research_text + "\n\n"
        "Write a short factual summary (3-5 sentences) answering the "
        "question. Then, ONLY if relevant, add one line starting with "
        "'Suggestion (needs human review):' with a possible "
        "implication for our positioning — do not present this as a "
        "decision, only as something to consider."
    )

    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.choices[0].message.content


def approve_draft(
    entry_id: str,
    *,
    db_path=None,
) -> None:
    """Mark a finding as founder-reviewed. Does not publish or set strategy."""
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    try:
        store.approve(entry_id)
        print(
            f"[competitor-agent] approved {entry_id} "
            "(informational only — not a strategy decision, nothing published)"
        )
    finally:
        store.close()


def reject_draft(
    entry_id: str,
    *,
    db_path=None,
) -> None:
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    try:
        store.reject(entry_id)
        print(f"[competitor-agent] rejected {entry_id}")
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="StreamCtx Competitor & Market Strategy Agent — research summaries for human review."
    )
    parser.add_argument("--competitor", required=True, help="Competitor name, e.g. LangSmith")
    parser.add_argument("--research", required=True, help="Raw research text/notes to summarize")
    parser.add_argument("--question", required=True, help="What to find out from this research")
    args = parser.parse_args()

    run_research_cycle(
        competitor_name=args.competitor,
        raw_research_text=args.research,
        question=args.question,
    )
