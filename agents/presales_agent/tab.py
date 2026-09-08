"""Streamlit Pre-Sales tab — leads table, CSV import, pending_approval queue.

Named ``tab.py`` (not ``dashboard.py``) so it cannot shadow the Streamlit
entry script ``dashboard.py`` in ``sys.modules``.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Optional

from agents.presales_agent.csv_import import import_csv_text
from agents.presales_agent.models import PIPELINE_IMPORTED, PIPELINE_ORDER
from agents.presales_agent.outreach import generate_and_queue, score_all_leads
from agents.presales_agent.pending_approval import (
    STATUS_PENDING,
    PendingApprovalStore,
)
from agents.presales_agent.presales_agent import approve_draft, reject_draft, save_edited_draft
from agents.presales_agent.storage import LeadStore

TAB_CSS = """
<style>
.ps-kicker {
  color: #f0883e;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  font-weight: 700;
  text-transform: uppercase;
}
.ps-muted { color: #8b949e; font-size: 0.85rem; }
.ps-flag {
  display: inline-block;
  font-size: 0.72rem;
  font-weight: 700;
  border-radius: 999px;
  padding: 0.1rem 0.5rem;
  border: 1px solid #d2992255;
  color: #e3b341;
  background: #2a2111;
}
</style>
"""


def render_presales_tab(
    st: Any,
    *,
    db_path: Optional[Path | str] = None,
    key_prefix: str = "ps-",
    chat_fn=None,
) -> None:
    st.markdown(TAB_CSS, unsafe_allow_html=True)
    st.markdown('<div class="ps-kicker">Pre-Sales · Draft only</div>', unsafe_allow_html=True)
    st.header("Pre-Sales")
    st.caption(
        "CSV import from LinkedIn Sales Navigator. Personalized drafts queue in "
        "`pending_approval`. Nothing is sent — advance Contacted after you send it yourself."
    )

    leads = LeadStore(db_path=db_path)
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    try:
        _render_body(st, leads, store, key_prefix=key_prefix, chat_fn=chat_fn)
    finally:
        store.close()
        leads.close()


def _render_body(
    st: Any,
    leads: LeadStore,
    store: PendingApprovalStore,
    *,
    key_prefix: str,
    chat_fn,
) -> None:
    counts = leads.counts()
    metrics = st.columns(5)
    metrics[0].metric("Leads", counts.get("total", 0))
    metrics[1].metric("Pending drafts", counts.get("pending_approval", 0))
    metrics[2].metric("Contacted", counts.get("contacted", 0))
    metrics[3].metric("Meetings", counts.get("meeting", 0))
    metrics[4].metric("Closed", counts.get("closed", 0))

    st.subheader("Import Sales Navigator CSV")
    uploaded = st.file_uploader(
        "CSV export (name, title, company, company_size, industry, linkedin_url — extra columns ignored)",
        type=["csv"],
        key=f"{key_prefix}csv",
    )
    actions = st.columns(3)
    if actions[0].button("Import CSV", key=f"{key_prefix}import"):
        if uploaded is None:
            st.warning("Choose a CSV first.")
        else:
            text = uploaded.getvalue().decode("utf-8-sig", errors="replace")
            result = import_csv_text(text, store=leads)
            st.success(
                f"Imported {result.imported}, updated {result.updated}, "
                f"skipped {result.skipped}."
            )
            for note in result.errors[:8]:
                st.caption(note)
            st.rerun()
    if actions[1].button("Score leads", key=f"{key_prefix}score"):
        n = score_all_leads(leads)
        st.success(f"Scored {n} lead(s).")
        st.rerun()
    if actions[2].button("Generate drafts above threshold", key=f"{key_prefix}draft"):
        try:
            summary = generate_and_queue(leads, store, chat_fn=chat_fn)
        except Exception as exc:
            st.error(str(exc))
        else:
            st.success(
                f"Queued {summary.queued} draft(s) for approval "
                f"(skipped {summary.skipped}, flagged {summary.flagged}). "
                "Nothing was sent."
            )
            for err in summary.errors[:6]:
                st.caption(err)
            st.rerun()

    st.subheader("Leads")
    rows = leads.list_leads()
    if not rows:
        st.info("No leads yet. Import a Sales Navigator CSV.")
    else:
        table = [
            {
                "Name": lead.name,
                "Title": lead.title,
                "Company": lead.company,
                "Score": "" if lead.score is None else f"{lead.score:.2f}",
                "Pipeline": lead.pipeline_status,
                "Draft": lead.draft_status,
                "Preview": (lead.draft_text or "")[:120],
                "Flag": lead.flag or "",
            }
            for lead in rows
        ]
        st.dataframe(table, use_container_width=True, hide_index=True)
        for lead in rows:
            label = f"{lead.name} · {lead.company or '—'} · {lead.pipeline_status}"
            with st.expander(label):
                st.caption(
                    f"score={lead.score if lead.score is not None else 'unscored'} · "
                    f"{lead.score_rationale or 'not scored yet'}"
                )
                if lead.linkedin_url:
                    st.markdown(f"[LinkedIn profile]({lead.linkedin_url})")
                if lead.flag:
                    st.markdown(
                        f'<span class="ps-flag">{html.escape(lead.flag)}</span>',
                        unsafe_allow_html=True,
                    )
                    st.caption("Legal/Pricing copy needs founder review — agent will not send.")
                if lead.draft_text:
                    st.code(lead.draft_text, language=None)
                status = st.selectbox(
                    "Pipeline status (founder-advanced after real outreach)",
                    options=list(PIPELINE_ORDER),
                    index=list(PIPELINE_ORDER).index(lead.pipeline_status)
                    if lead.pipeline_status in PIPELINE_ORDER
                    else 0,
                    key=f"{key_prefix}pipe-{lead.lead_id}",
                )
                if status != lead.pipeline_status:
                    if st.button(
                        "Save pipeline status",
                        key=f"{key_prefix}save-pipe-{lead.lead_id}",
                    ):
                        leads.set_pipeline_status(lead.lead_id, status)
                        st.rerun()

    st.subheader("Pending Approval")
    st.caption("Identical gate to coding/marketing: view, edit, approve, or reject. No send.")
    pending = store.list_by_status(STATUS_PENDING)
    if not pending:
        st.success("No pending_approval outreach drafts.")
        return
    for entry in pending:
        lead = leads.get_lead(entry.lead_id)
        with st.container(border=True):
            st.markdown(f"**{entry.title or entry.lead_id}** · `{entry.status}` · `{entry.entry_id}`")
            if entry.flag:
                st.markdown(
                    f'<span class="ps-flag">{html.escape(entry.flag)}</span> — human review, not a send.',
                    unsafe_allow_html=True,
                )
            edited = st.text_area(
                "Draft",
                value=entry.content,
                key=f"{key_prefix}edit-{entry.entry_id}",
                height=160,
            )
            cols = st.columns([1, 1, 1, 5])
            with cols[0]:
                if st.button("Save edits", key=f"{key_prefix}save-{entry.entry_id}"):
                    save_edited_draft(entry.entry_id, edited, db_path=leads.db_path)
                    st.rerun()
            with cols[1]:
                if st.button("Approve", key=f"{key_prefix}approve-{entry.entry_id}"):
                    if edited.strip() != entry.content.strip():
                        save_edited_draft(entry.entry_id, edited, db_path=leads.db_path)
                    approve_draft(entry.entry_id, db_path=leads.db_path)
                    st.success("Approved copy only — not sent. Mark Contacted after you send it.")
                    st.rerun()
            with cols[2]:
                if st.button("Reject", key=f"{key_prefix}reject-{entry.entry_id}"):
                    reject_draft(entry.entry_id, db_path=leads.db_path)
                    st.rerun()
            if lead and lead.pipeline_status != PIPELINE_IMPORTED:
                st.caption(f"Pipeline already {lead.pipeline_status} (founder-set).")
            else:
                st.caption("Pipeline stays imported until you mark Contacted.")
