"""Outreach drafts: OpenRouter-style generation, queue only, no send."""

from __future__ import annotations

import inspect
from pathlib import Path

from agents.presales_agent.csv_import import import_csv
from agents.presales_agent.flags import flag_draft
from agents.presales_agent.models import FLAG_PRICING, PIPELINE_IMPORTED
from agents.presales_agent.outreach import generate_and_queue, generate_outreach_draft
from agents.presales_agent.pending_approval import STATUS_PENDING, PendingApprovalStore
from agents.presales_agent.storage import LeadStore

NAV_CSV = Path(__file__).resolve().parents[1] / "fixtures" / "presales" / "sales_navigator_export.csv"
MODULE_ROOT = Path(__file__).resolve().parents[2] / "agents" / "presales_agent"


def _chat(_model: str, _key: str, messages) -> str:
    user = messages[-1]["content"]
    name = title = company = "them"
    for line in user.splitlines():
        if line.startswith("- Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("- Title:"):
            title = line.split(":", 1)[1].strip()
        elif line.startswith("- Company:"):
            company = line.split(":", 1)[1].strip()
    return (
        f"{name} — as {title} at {company}, when an agent run silently drops an "
        f"earlier tool result the usual instinct is to add more prompt text. "
        f"StreamCtx checkpoints the transcript and attributes the rotten turn "
        f"so you can replay from that step instead of guessing."
    )


def test_draft_references_role_and_company():
    from agents.presales_agent.models import DRAFT_NONE, Lead

    lead = Lead(
        lead_id="1",
        name="Avery Chen",
        title="CTO",
        company="Northwind AI",
        company_size="51-200",
        industry="Artificial Intelligence",
        linkedin_url="https://www.linkedin.com/in/avery-chen-example",
        score=0.96,
        score_rationale="role high",
        pipeline_status=PIPELINE_IMPORTED,
        draft_status=DRAFT_NONE,
        draft_text="",
        draft_entry_id=None,
        flag="",
        source_fingerprint="x",
        created_at="2026-08-25T00:00:00+00:00",
        updated_at="2026-08-25T00:00:00+00:00",
    )
    text, model = generate_outreach_draft(lead, chat_fn=_chat)
    assert model == "stub"
    assert "Avery Chen" in text
    assert "CTO" in text
    assert "Northwind AI" in text
    assert "hope this finds you well" not in text.lower()
    assert "just wanted to reach out" not in text.lower()


def test_generate_and_queue_fills_pending_not_contacted(tmp_path: Path):
    db = tmp_path / "leads.db"
    leads = LeadStore(db_path=db)
    queue = PendingApprovalStore(db_path=db, enable_default_notifier=False)
    try:
        imported = import_csv(NAV_CSV, store=leads)
        assert imported.imported >= 6
        summary = generate_and_queue(leads, queue, chat_fn=_chat)
        assert summary.queued >= 3
        pending = queue.list_by_status(STATUS_PENDING)
        assert len(pending) == summary.queued
        assert all(entry.mode == "draft_only" for entry in pending)
        names = {lead.name for lead in leads.list_leads()}
        assert "Avery Chen" in names
        for lead in leads.list_leads():
            assert lead.pipeline_status == PIPELINE_IMPORTED
            if lead.draft_status == "pending_approval":
                assert lead.draft_text
                assert lead.draft_entry_id
        morgan = next(lead for lead in leads.list_leads() if lead.name == "Morgan Ellis")
        assert morgan.score is not None
        assert morgan.draft_status == "none"
    finally:
        queue.close()
        leads.close()


def test_pricing_language_is_flagged_not_sent():
    assert flag_draft("Our paid plan is $99/seat") == FLAG_PRICING
    assert flag_draft("Checkpoint resume preserves the transcript.") == ""


def test_module_has_no_send_or_linkedin_scrape():
    blob = "\n".join(path.read_text(encoding="utf-8") for path in MODULE_ROOT.glob("*.py"))
    for needle in (
        "smtplib",
        "linkedin.com/voyager",
        "linkedin.com/sales-api",
        "playwright",
        "selenium",
    ):
        assert needle not in blob
    assert "def send(" not in blob
    assert "def publish(" not in blob
    src = inspect.getsource(generate_and_queue)
    assert "pending_approval" in src or "enqueue_draft" in src
    assert "smtp" not in src.lower()
