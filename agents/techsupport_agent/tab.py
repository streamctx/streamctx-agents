"""Streamlit Tech Support tab — ticket table, manual-review lane, pending_approval.

Named ``tab.py`` (not ``dashboard.py``) so it cannot shadow the Streamlit
entry script ``dashboard.py`` in ``sys.modules``.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Optional

from agents.techsupport_agent.models import STATUS_NEEDS_MANUAL_REVIEW
from agents.techsupport_agent.pending_approval import (
    STATUS_PENDING,
    PendingApprovalStore,
)
from agents.techsupport_agent.storage import TicketStore
from agents.techsupport_agent.techsupport_agent import (
    approve_draft,
    reject_draft,
    run,
    save_edited_draft,
)

TAB_CSS = """
<style>
.ts-kicker {
  color: #39d0d8;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  font-weight: 700;
  text-transform: uppercase;
}
.ts-muted { color: #8b949e; font-size: 0.85rem; }
.ts-review {
  display: inline-block;
  font-size: 0.72rem;
  font-weight: 700;
  border-radius: 999px;
  padding: 0.1rem 0.5rem;
  border: 1px solid #d2992255;
  color: #e3b341;
  background: #2a2111;
}
.ts-matched {
  display: inline-block;
  font-size: 0.72rem;
  font-weight: 700;
  border-radius: 999px;
  padding: 0.1rem 0.5rem;
  border: 1px solid #3fb95055;
  color: #3fb950;
  background: #122017;
}
</style>
"""


def render_techsupport_tab(
    st: Any,
    *,
    db_path: Optional[Path | str] = None,
    key_prefix: str = "ts-",
    chat_fn=None,
    kb_root: Optional[Path | str] = None,
) -> None:
    st.markdown(TAB_CSS, unsafe_allow_html=True)
    st.markdown(
        '<div class="ts-kicker">Tech Support · Draft only</div>',
        unsafe_allow_html=True,
    )
    st.header("Tech Support")
    st.caption(
        "Polls GitHub issues and a Discord support channel. Matched replies queue "
        "in `pending_approval`. Unmatched tickets go to Needs Manual Review — they "
        "are not mixed into the approval queue. Nothing is posted."
    )

    tickets = TicketStore(db_path=db_path)
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    try:
        _render_body(
            st,
            tickets,
            store,
            key_prefix=key_prefix,
            chat_fn=chat_fn,
            kb_root=kb_root,
        )
    finally:
        store.close()
        tickets.close()


def _render_body(
    st: Any,
    tickets: TicketStore,
    store: PendingApprovalStore,
    *,
    key_prefix: str,
    chat_fn,
    kb_root: Optional[Path | str],
) -> None:
    counts = tickets.counts()
    metrics = st.columns(5)
    metrics[0].metric("Tickets", counts.get("total", 0))
    metrics[1].metric("Pending drafts", counts.get("drafted", 0))
    metrics[2].metric("Needs manual review", counts.get(STATUS_NEEDS_MANUAL_REVIEW, 0))
    metrics[3].metric("Approved copy", counts.get("approved", 0))
    metrics[4].metric("Rejected", counts.get("rejected", 0))

    actions = st.columns(2)
    if actions[0].button("Poll GitHub + Discord", key=f"{key_prefix}poll"):
        try:
            summary = run(
                db_path=tickets.db_path,
                chat_fn=chat_fn,
                kb_root=kb_root,
                enable_notifications=False,
            )
        except Exception as exc:
            st.error(str(exc))
        else:
            st.success(
                f"Ingested {summary.ingested}, drafted {summary.drafted}, "
                f"needs review {summary.needs_review}. Nothing was posted."
            )
            for err in summary.errors[:6]:
                st.caption(err)
            st.rerun()

    rows = tickets.list_tickets()
    st.subheader("Tickets")
    if not rows:
        st.info("No support tickets yet. Poll GitHub issues or the Discord channel.")
    else:
        table = [
            {
                "Source": ticket.source,
                "Type": ticket.ticket_type or "",
                "Severity": ticket.severity or "",
                "KB match": _kb_status(ticket),
                "Status": ticket.status,
                "Title": ticket.title[:80],
                "Draft preview": (ticket.draft_text or "")[:120],
            }
            for ticket in rows
        ]
        st.dataframe(table, use_container_width=True, hide_index=True)
        for ticket in rows:
            if ticket.status == STATUS_NEEDS_MANUAL_REVIEW:
                continue
            label = f"{ticket.source} · {ticket.title[:60] or ticket.ticket_id}"
            with st.expander(label):
                st.caption(
                    f"type={ticket.ticket_type or '—'} · severity={ticket.severity or '—'} · "
                    f"class_conf={_fmt_conf(ticket.classification_confidence)} · "
                    f"kb={_kb_status(ticket)}"
                )
                if ticket.source_url:
                    st.markdown(f"[Source]({ticket.source_url})")
                st.write(ticket.body[:800] or "(empty body)")
                if ticket.kb_match_path:
                    st.caption(
                        f"KB: {ticket.kb_match_path} § {ticket.kb_match_heading} "
                        f"(conf={_fmt_conf(ticket.kb_match_confidence)})"
                    )
                if ticket.draft_text:
                    st.code(ticket.draft_text, language=None)

    st.subheader("Needs Manual Review")
    st.caption(
        "No knowledge-base match (or confidence below the gate). No draft was "
        "generated. These are not in the pending_approval queue."
    )
    review = [ticket for ticket in rows if ticket.status == STATUS_NEEDS_MANUAL_REVIEW]
    if not review:
        st.success("No tickets waiting for manual review.")
    else:
        for ticket in review:
            with st.container(border=True):
                st.markdown(
                    f'<span class="ts-review">needs manual review</span> '
                    f"**{html.escape(ticket.title or ticket.ticket_id)}** · "
                    f"`{html.escape(ticket.source)}`",
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"type={ticket.ticket_type or '—'} · severity={ticket.severity or '—'} · "
                    f"class_conf={_fmt_conf(ticket.classification_confidence)} · "
                    f"kb_conf={_fmt_conf(ticket.kb_match_confidence)}"
                )
                if ticket.source_url:
                    st.markdown(f"[Source]({ticket.source_url})")
                st.write(ticket.body[:600] or "(empty body)")
                st.caption(ticket.classification_rationale or "unmatched")

    st.subheader("Pending Approval")
    st.caption(
        "KB-matched reply drafts only. View, edit, approve, or reject. "
        "Approve does not post to GitHub or Discord."
    )
    pending = store.list_by_status(STATUS_PENDING)
    if not pending:
        st.success("No pending_approval reply drafts.")
        return
    for entry in pending:
        ticket = tickets.get_ticket(entry.ticket_id)
        with st.container(border=True):
            st.markdown(
                f'<span class="ts-matched">kb matched</span> '
                f"**{html.escape(entry.title or entry.ticket_id)}** · "
                f"`{html.escape(entry.status)}` · `{html.escape(entry.entry_id)}`",
                unsafe_allow_html=True,
            )
            if entry.kb_citation:
                st.caption(f"Based on {entry.kb_citation}")
            edited = st.text_area(
                "Draft",
                value=entry.content,
                key=f"{key_prefix}edit-{entry.entry_id}",
                height=160,
            )
            cols = st.columns([1, 1, 1, 5])
            with cols[0]:
                if st.button("Save edits", key=f"{key_prefix}save-{entry.entry_id}"):
                    save_edited_draft(entry.entry_id, edited, db_path=tickets.db_path)
                    st.rerun()
            with cols[1]:
                if st.button("Approve", key=f"{key_prefix}approve-{entry.entry_id}"):
                    if edited.strip() != entry.content.strip():
                        save_edited_draft(entry.entry_id, edited, db_path=tickets.db_path)
                    approve_draft(entry.entry_id, db_path=tickets.db_path)
                    st.success("Approved copy only — not posted to GitHub or Discord.")
                    st.rerun()
            with cols[2]:
                if st.button("Reject", key=f"{key_prefix}reject-{entry.entry_id}"):
                    reject_draft(entry.entry_id, db_path=tickets.db_path)
                    st.rerun()
            if ticket:
                st.caption(
                    f"{ticket.source} · {ticket.ticket_type or 'untyped'} · "
                    f"{ticket.severity or 'unscored'}"
                )


def _kb_status(ticket) -> str:
    conf = ticket.kb_match_confidence
    if ticket.status == STATUS_NEEDS_MANUAL_REVIEW:
        return "unmatched"
    if conf is not None and conf > 0 and ticket.kb_match_path:
        return "matched"
    if ticket.draft_text:
        return "matched"
    return "pending"


def _fmt_conf(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):.2f}"
