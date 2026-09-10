"""Streamlit Coding Agent tab — pending_approval queue and recent fix activity.

Named ``tab.py`` (not ``dashboard.py``) so it cannot shadow the Streamlit
entry script ``dashboard.py`` in ``sys.modules``.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Optional

from agents.coding_agent.pending_approval import (
    DEFAULT_AGENT_DB,
    ROOT_CAUSE_INTAKE,
    STATUS_APPROVED,
    STATUS_AUTO_FIX_FAILED,
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    STATUS_REJECTED,
    PendingApprovalEntry,
    PendingApprovalStore,
    is_intake_task,
)

PENDING_STATUSES = (
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    STATUS_AUTO_FIX_FAILED,
)

TAB_CSS = """
<style>
.ca-kicker {
  color: #58a6ff;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  font-weight: 700;
  text-transform: uppercase;
}
</style>
"""


def render_coding_tab(
    st: Any,
    *,
    db_path: Optional[Path | str] = None,
    key_prefix: str = "ca-",
) -> None:
    st.markdown(TAB_CSS, unsafe_allow_html=True)
    st.markdown(
        '<div class="ca-kicker">Coding · Human review before apply</div>',
        unsafe_allow_html=True,
    )
    st.header("Coding Agent")
    st.caption(
        "Diagnoses StreamCtx WAL failures, gates low-confidence cases into "
        "`pending_approval`, and runs sandbox fix retries. Approve marks the "
        "row ready (intake kicks off a fix job); Reject closes it. Nothing is "
        "auto-applied from this tab."
    )

    path = Path(db_path) if db_path is not None else DEFAULT_AGENT_DB
    store = PendingApprovalStore(db_path=path, enable_default_notifier=False)
    try:
        _render_body(st, store, key_prefix=key_prefix)
    finally:
        store.close()


def _render_body(st: Any, store: PendingApprovalStore, *, key_prefix: str) -> None:
    counts = {
        status: len(store.list_by_status(status))
        for status in (
            STATUS_NEEDS_HUMAN_REVIEW,
            STATUS_READY_FOR_APPROVAL,
            STATUS_AUTO_FIX_FAILED,
            STATUS_APPROVED,
            STATUS_REJECTED,
        )
    }
    pending_n = sum(counts[s] for s in PENDING_STATUSES)
    metrics = st.columns(5)
    metrics[0].metric("Pending review", pending_n)
    metrics[1].metric("Needs human review", counts[STATUS_NEEDS_HUMAN_REVIEW])
    metrics[2].metric("Ready for approval", counts[STATUS_READY_FOR_APPROVAL])
    metrics[3].metric("Auto-fix failed", counts[STATUS_AUTO_FIX_FAILED])
    metrics[4].metric("Rejected (closed)", counts[STATUS_REJECTED])

    actions = st.columns(2)
    if actions[0].button("Run coding pipeline", key=f"{key_prefix}run"):
        from dashboard import submit_agent

        submit_agent("coding")
        st.success("Started coding_agent pipeline — new rows still go to pending_approval.")
        st.rerun()
    actions[1].caption("Detect → diagnose → ConfidenceGate → sandbox retries.")

    st.subheader("Pending Approval")
    st.caption(
        "Statuses: needs_human_review, ready_for_approval, auto_fix_failed. "
        "Approve / Reject mirror the Home / Control buttons."
    )
    pending: list[PendingApprovalEntry] = []
    for status in PENDING_STATUSES:
        pending.extend(store.list_by_status(status))
    pending.sort(key=lambda e: e.created_at, reverse=True)
    if not pending:
        st.success("No coding pending_approval rows.")
    else:
        for entry in pending:
            _render_entry(st, store, entry, key_prefix=key_prefix)

    st.subheader("Recent activity")
    st.caption("Latest rows from `coding_agent.db` (any status), newest first.")
    recent: list[PendingApprovalEntry] = []
    for status in (
        STATUS_NEEDS_HUMAN_REVIEW,
        STATUS_READY_FOR_APPROVAL,
        STATUS_AUTO_FIX_FAILED,
        STATUS_APPROVED,
        STATUS_REJECTED,
    ):
        recent.extend(store.list_by_status(status))
    recent.sort(key=lambda e: e.created_at, reverse=True)
    recent = recent[:25]
    if not recent:
        st.info("No pending_approval history yet.")
        return
    table = [
        {
            "Created": entry.created_at,
            "Status": entry.status,
            "Root cause": entry.root_cause,
            "Session": entry.session_id,
            "Confidence": (
                f"{entry.confidence:.2f}" if entry.confidence is not None else "—"
            ),
            "Entry": entry.entry_id[:8],
        }
        for entry in recent
    ]
    st.dataframe(table, use_container_width=True, hide_index=True)


def _render_entry(
    st: Any,
    store: PendingApprovalStore,
    entry: PendingApprovalEntry,
    *,
    key_prefix: str,
) -> None:
    ctx = _context(entry.test_results)
    with st.container(border=True):
        st.markdown(
            f"**{html.escape(entry.root_cause or 'fix')}** · "
            f"`{html.escape(entry.status)}` · session `{html.escape(str(entry.session_id))}` · "
            f"`{html.escape(entry.entry_id)}`",
            unsafe_allow_html=True,
        )
        st.caption(
            f"{entry.created_at} · confidence="
            f"{entry.confidence if entry.confidence is not None else '—'} · "
            f"retries={entry.retries_used} · "
            f"verification={entry.verification_mode or '—'} · "
            f"failed_call_id={ctx.get('failed_call_id', '—')}"
        )
        if entry.root_cause == ROOT_CAUSE_INTAKE:
            st.caption("Intake task — Approve kicks off the intake fix job.")
        reason = ctx.get("reason") or ctx.get("error_type")
        if reason:
            st.write(str(reason)[:500])
        if entry.diff:
            with st.expander("Diff", expanded=False):
                st.code(entry.diff, language="diff")
        elif entry.test_results:
            with st.expander("Diagnosis context", expanded=False):
                st.code(entry.test_results[:4000], language="json")
        cols = st.columns([1, 1, 6])
        with cols[0]:
            if st.button("Approve", key=f"{key_prefix}approve-{entry.entry_id}"):
                kick_off = is_intake_task(entry)
                store.update_status(entry.entry_id, STATUS_APPROVED)
                if kick_off:
                    from dashboard import submit_intake_fix

                    submit_intake_fix(entry.entry_id, db_path=store.db_path)
                st.rerun()
        with cols[1]:
            if st.button("Reject", key=f"{key_prefix}reject-{entry.entry_id}"):
                store.update_status(entry.entry_id, STATUS_REJECTED)
                st.rerun()


def _context(raw: Optional[str]) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "")
        return payload if isinstance(payload, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
