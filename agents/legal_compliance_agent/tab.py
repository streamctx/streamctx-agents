"""Streamlit Legal / Compliance tab — findings, DPDP checklist, drafts-only queue.

Named ``tab.py`` (not ``dashboard.py``) so it cannot shadow the Streamlit
entry script ``dashboard.py`` in ``sys.modules``.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Optional

from agents.legal_compliance_agent.models import STATUS_NEEDS_MANUAL_REVIEW
from agents.legal_compliance_agent.pending_approval import (
    STATUS_PENDING,
    PendingApprovalStore,
)
from agents.legal_compliance_agent.storage import FindingStore
from agents.legal_compliance_agent.legal_compliance_agent import (
    approve_draft,
    reject_draft,
    run,
    save_edited_draft,
)

TAB_CSS = """
<style>
.lc-kicker {
  color: #f778ba;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  font-weight: 700;
  text-transform: uppercase;
}
.lc-muted { color: #8b949e; font-size: 0.85rem; }
.lc-gap {
  display: inline-block;
  font-size: 0.72rem;
  font-weight: 700;
  border-radius: 999px;
  padding: 0.1rem 0.5rem;
  border: 1px solid #d2992255;
  color: #e3b341;
  background: #2a2111;
}
.lc-draft {
  display: inline-block;
  font-size: 0.72rem;
  font-weight: 700;
  border-radius: 999px;
  padding: 0.1rem 0.5rem;
  border: 1px solid #f778ba55;
  color: #f778ba;
  background: #2a1220;
}
</style>
"""


def render_legal_tab(
    st: Any,
    *,
    db_path: Optional[Path | str] = None,
    key_prefix: str = "lc-",
    fetch_github_fn=None,
    agents_root: Optional[Path | str] = None,
    product_root: Optional[Path | str] = None,
) -> None:
    st.markdown(TAB_CSS, unsafe_allow_html=True)
    st.markdown(
        '<div class="lc-kicker">Legal / Compliance · Draft only · Not legal advice</div>',
        unsafe_allow_html=True,
    )
    st.header("Legal / Compliance")
    st.caption(
        "Scans TERMS.md, PRIVACY.md, COMPLIANCE_VERIFICATION.md, and DEPLOYMENT.md "
        "against actual storage and third-party calls. Drafts a DPDP checklist. "
        "Everything queues in `pending_approval`. This agent never edits legal docs "
        "and never publishes. Output is not legal advice."
    )

    findings = FindingStore(db_path=db_path)
    store = PendingApprovalStore(db_path=db_path, enable_default_notifier=False)
    try:
        _render_body(
            st,
            findings,
            store,
            key_prefix=key_prefix,
            fetch_github_fn=fetch_github_fn,
            agents_root=agents_root,
            product_root=product_root,
        )
    finally:
        store.close()
        findings.close()


def _render_body(
    st: Any,
    findings: FindingStore,
    store: PendingApprovalStore,
    *,
    key_prefix: str,
    fetch_github_fn,
    agents_root: Optional[Path | str],
    product_root: Optional[Path | str],
) -> None:
    counts = findings.counts()
    metrics = st.columns(5)
    metrics[0].metric("Findings", counts.get("total", 0))
    metrics[1].metric("Pending drafts", counts.get("drafted", 0))
    metrics[2].metric("DPDP gaps", counts.get("dpdp_gaps", 0))
    metrics[3].metric("Approved copy", counts.get("approved", 0))
    metrics[4].metric("Rejected", counts.get("rejected", 0))

    actions = st.columns(2)
    if actions[0].button("Scan policies + DPDP checklist", key=f"{key_prefix}scan"):
        try:
            summary = run(
                db_path=findings.db_path,
                enable_notifications=False,
                fetch_github_fn=fetch_github_fn,
                agents_root=agents_root,
                product_root=product_root,
            )
        except Exception as exc:
            st.error(str(exc))
        else:
            st.success(
                f"Findings {summary.findings}, drafted {summary.drafted}, "
                f"DPDP gaps {summary.dpdp_gaps}. Policy files were not edited. "
                "Not legal advice."
            )
            for err in summary.errors[:6]:
                st.caption(err)
            st.rerun()

    rows = findings.list_findings()
    st.subheader("Findings")
    if not rows:
        st.info("No compliance findings yet. Run a scan against TERMS/PRIVACY/code.")
    else:
        table = [
            {
                "Kind": row.kind,
                "Severity": row.severity,
                "Status": row.status,
                "Title": row.title[:80],
                "Source": row.source_path[:60],
            }
            for row in rows
        ]
        st.dataframe(table, use_container_width=True, hide_index=True)
        for row in rows:
            with st.expander(f"{row.kind} · {row.title[:70]}"):
                st.caption(
                    f"severity={row.severity} · status={row.status} · `{row.source_path}`"
                )
                st.write(row.evidence[:1200])
                if row.suggested_language:
                    st.code(row.suggested_language[:1500], language=None)

    st.subheader("DPDP checklist")
    st.caption(
        "Mapped to observed StreamCtx agent behavior. Gaps are gaps — this view "
        "does not invent coverage. Not legal advice."
    )
    checklist = findings.list_checklist()
    if not checklist:
        st.info("No DPDP checklist yet. Run a scan.")
    else:
        st.dataframe(
            [
                {
                    "Area": item.area,
                    "Status": item.status,
                    "Requirement": item.requirement[:80],
                    "Current behavior": item.current_behavior[:120],
                    "Gap": item.gap[:120],
                }
                for item in checklist
            ],
            use_container_width=True,
            hide_index=True,
        )
        for item in checklist:
            badge = "gap" if item.status == "gap" else item.status
            with st.container(border=True):
                st.markdown(
                    f'<span class="lc-gap">{html.escape(badge)}</span> '
                    f"**{html.escape(item.area)}**",
                    unsafe_allow_html=True,
                )
                st.write(item.requirement)
                st.caption(item.current_behavior)
                st.write(item.gap)

    review = [row for row in rows if row.status == STATUS_NEEDS_MANUAL_REVIEW]
    st.subheader("Needs Manual Review")
    st.caption(
        "Findings that could not be drafted automatically. They are not mixed "
        "into the approval queue."
    )
    if not review:
        st.success("No findings waiting for manual review.")
    else:
        for row in review:
            with st.container(border=True):
                st.markdown(
                    f'<span class="lc-gap">needs manual review</span> '
                    f"**{html.escape(row.title)}**",
                    unsafe_allow_html=True,
                )
                st.write(row.evidence[:600])

    st.subheader("Pending Approval")
    st.caption(
        "Suggested language only. View, edit, approve, or reject. Approve does "
        "not write TERMS.md, PRIVACY.md, or any other legal doc. Not legal advice."
    )
    pending = store.list_by_status(STATUS_PENDING)
    if not pending:
        st.success("No pending_approval compliance drafts.")
        return
    for entry in pending:
        with st.container(border=True):
            st.markdown(
                f'<span class="lc-draft">draft only</span> '
                f"**{html.escape(entry.title or entry.finding_id)}** · "
                f"`{html.escape(entry.status)}` · `{html.escape(entry.entry_id)}`",
                unsafe_allow_html=True,
            )
            if entry.target:
                st.caption(f"Target (not written): {entry.target}")
            edited = st.text_area(
                "Draft",
                value=entry.content,
                key=f"{key_prefix}edit-{entry.entry_id}",
                height=180,
            )
            cols = st.columns([1, 1, 1, 5])
            with cols[0]:
                if st.button("Save edits", key=f"{key_prefix}save-{entry.entry_id}"):
                    save_edited_draft(entry.entry_id, edited, db_path=findings.db_path)
                    st.rerun()
            with cols[1]:
                if st.button("Approve", key=f"{key_prefix}approve-{entry.entry_id}"):
                    if edited.strip() != entry.content.strip():
                        save_edited_draft(
                            entry.entry_id, edited, db_path=findings.db_path
                        )
                    approve_draft(entry.entry_id, db_path=findings.db_path)
                    st.success(
                        "Approved copy only — TERMS.md/PRIVACY.md were not written. "
                        "Not legal advice."
                    )
                    st.rerun()
            with cols[2]:
                if st.button("Reject", key=f"{key_prefix}reject-{entry.entry_id}"):
                    reject_draft(entry.entry_id, db_path=findings.db_path)
                    st.rerun()
