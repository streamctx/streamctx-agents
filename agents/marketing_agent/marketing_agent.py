"""
agents/marketing_agent/marketing_agent.py
Marketing & Community Agent — drafts HN/Reddit comments, LinkedIn/X
posts, and GitHub issue/discussion replies. Uses StreamCtx to exercise
Context Compression as it reads through source material (docs, past
threads) and drafts across multiple turns.
Never auto-posts anywhere — every draft lands in the audit log as
pending_approval, same as the Coding Agent.
"""

import argparse
import os

from openai import OpenAI
from streamctx import get_tracker
from agents.marketing_agent.outreach import Outreach
from agents.marketing_agent.pending_approval import PendingApprovalStore
from shared.audit_log import log_action
from shared.config import (
    AGENT_IDS,
    STATUS_PENDING,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    MARKETING_DRAFTS_DIR,
)


def run(*, min_score=None, limit=20):
    """Discover public posts and queue draft-only outreach DMs for approval.

    This is the parameter-free dashboard/CLI entry point. ``run_draft_cycle``
    remains available when a human supplies platform/context/goal.
    """
    store = PendingApprovalStore()
    try:
        queued = Outreach(store).run(min_score=min_score, limit=limit)
        print(f"[marketing-agent] queued {len(queued)} outreach draft(s)")
        for entry_id in queued:
            print(f"  pending_approval id={entry_id}")
        return queued
    finally:
        store.close()


def run_draft_cycle(platform, context_text, goal, dry_run=True):
    """
    platform: e.g. "hn_comment", "reddit_comment", "linkedin_post",
              "github_reply"
    context_text: the source material to read (a thread, an issue,
                  docs excerpt, etc.) — this is what gets compressed
    goal: what the draft should accomplish (e.g. "answer this
          technical question using our docs, no self-promotion")
    """
    tracker = get_tracker(AGENT_IDS["marketing"])
    tracker.start()

    tracker.checkpoint()
    print(f"[marketing-agent] Platform: {platform}")
    print(f"[marketing-agent] Goal: {goal}")

    tracker.checkpoint()
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
    wrapped_client = tracker.wrap(client)  # exercises Context Compression
    draft_text = draft_post(platform, context_text, goal, wrapped_client)

    session_id = tracker.get_session_id()
    tracker.stop()
    stats = tracker.get_stats()

    os.makedirs(MARKETING_DRAFTS_DIR, exist_ok=True)
    draft_filename = f"{platform}_{session_id}.txt"
    draft_path = os.path.join(MARKETING_DRAFTS_DIR, draft_filename)
    with open(draft_path, "w", encoding="utf-8") as f:
        f.write(draft_text)

    entry = log_action(
        agent_name=AGENT_IDS["marketing"],
        session_id=session_id,
        action_type="community_post",
        payload={
            "platform": platform,
            "goal": goal,
            "draft_path": draft_path,
            "draft_text": draft_text,
            "tracker_stats": str(stats),
        },
        status=STATUS_PENDING,
    )
    print(f"[marketing-agent] Draft saved to {draft_path}")
    print(f"[marketing-agent] Logged for approval. Entry id: {entry['id']}")
    return entry


def draft_post(platform, context_text, goal, client):
    prompt = (
        "You are a technical community-engagement assistant for an "
        "open-source project called StreamCtx (an LLM agent "
        "observability SDK).\n\n"
        "Platform: " + platform + "\n"
        "Goal: " + goal + "\n\n"
        "Source material / context:\n" + context_text + "\n\n"
        "Write a short, technically substantive draft appropriate for "
        "this platform. No direct self-promotion, no hype language. "
        "Return ONLY the draft text, nothing else."
    )

    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.choices[0].message.content


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="StreamCtx Marketing/Community Agent — drafts posts/replies for human approval."
    )
    parser.add_argument(
        "--platform", required=True,
        help="e.g. hn_comment, reddit_comment, linkedin_post, github_reply"
    )
    parser.add_argument(
        "--context", required=True,
        help="Source material text (thread, issue, docs excerpt) to draft from"
    )
    parser.add_argument(
        "--goal", required=True,
        help="What the draft should accomplish"
    )
    args = parser.parse_args()

    run_draft_cycle(
        platform=args.platform,
        context_text=args.context,
        goal=args.goal,
    )
