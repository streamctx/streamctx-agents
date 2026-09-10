"""Streamlit Competitor Agent tab — signals, snapshots, pending_approval.

Named ``tab.py`` (not ``dashboard.py``) so it cannot shadow the Streamlit
entry script ``dashboard.py`` in ``sys.modules``.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Optional

from agents.competitor_agent.competitor_agent import approve_draft, reject_draft
from agents.competitor_agent.pending_approval import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    PendingApprovalStore,
)
from agents.competitor_agent.storage import DEFAULT_AGENT_DB, CompetitorStore

TAB_CSS = """
<style>
.cp-kicker {
  color: #d29922;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  font-weight: 700;
  text-transform: uppercase;
}
</style>
"""


def render_competitor_tab(
    st: Any,
    *,
    db_path: Optional[Path | str] = None,
    key_prefix: str = "cp-",
) -> None:
    st.markdown(TAB_CSS, unsafe_allow_html=True)
    st.markdown(
        '<div class="cp-kicker">Competitor · Informational only · Not a strategy decision</div>',
        unsafe_allow_html=True,
    )
    st.header("Competitor Agent")
    st.caption(
        "Polls pricing, GitHub releases, RSS, and mentions. New signals and "
        "research summaries queue in `pending_approval`. Approve / Reject only "
        "update the review row — nothing is published and this is not a strategy decision."
    )

    path = Path(db_path) if db_path is not None else DEFAULT_AGENT_DB
    signals_store = CompetitorStore(db_path=path)
    queue = PendingApprovalStore(db_path=path, enable_default_notifier=False)
    try:
        _render_body(st, signals_store, queue, key_prefix=key_prefix)
    finally:
        queue.close()
        signals_store.close()


def _render_body(
    st: Any,
    signals_store: CompetitorStore,
    queue: PendingApprovalStore,
    *,
    key_prefix: str,
) -> None:
    snapshots = signals_store.list_snapshots(limit=500)
    signals = signals_store.list_signals()
    pending = queue.list_by_status(STATUS_PENDING)
    approved = queue.list_by_status(STATUS_APPROVED)
    rejected = queue.list_by_status(STATUS_REJECTED)

    metrics = st.columns(5)
    metrics[0].metric("Pending review", len(pending))
    metrics[1].metric("Signals", len(signals))
    metrics[2].metric("Snapshots", len(snapshots))
    metrics[3].metric("Approved", len(approved))
    metrics[4].metric("Rejected", len(rejected))

    actions = st.columns(2)
    if actions[0].button("Run full snapshot poll", key=f"{key_prefix}run"):
        from dashboard import submit_agent

        submit_agent("competitor")
        st.success("Started competitor snapshot poll — new signals still go to pending_approval.")
        st.rerun()
    actions[1].caption("Writes competitor_signals and queues pending_approval rows.")

    st.subheader("Pending Approval")
    st.caption(
        "Detected signals and research summaries. Approve/Reject do not delete "
        "signals and do not set strategy."
    )
    if not pending:
        st.success("No competitor pending_approval rows.")
    else:
        for entry in pending:
            with st.container(border=True):
                name = entry.competitor_name or "competitor"
                st.markdown(
                    f"**{html.escape(entry.title or name)}** · "
                    f"`{html.escape(entry.status)}` · `{html.escape(entry.entry_id)}`",
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"{entry.created_at} · competitor={name} · "
                    f"target={entry.target or '—'}"
                )
                st.write(entry.content[:800])
                cols = st.columns([1, 1, 6])
                with cols[0]:
                    if st.button("Approve", key=f"{key_prefix}approve-{entry.entry_id}"):
                        approve_draft(entry.entry_id, db_path=queue.db_path)
                        st.rerun()
                with cols[1]:
                    if st.button("Reject", key=f"{key_prefix}reject-{entry.entry_id}"):
                        reject_draft(entry.entry_id, db_path=queue.db_path)
                        st.rerun()

    st.subheader("Recent signals")
    if not signals:
        st.info("No competitor_signals yet. Run a snapshot poll or Assign check.")
    else:
        table = [
            {
                "Detected": signal.detected_at,
                "Competitor": signal.competitor,
                "Type": signal.signal_type,
                "Summary": (signal.summary or "")[:120],
                "URL": signal.source_url or "",
            }
            for signal in signals[:40]
        ]
        st.dataframe(table, use_container_width=True, hide_index=True)

    st.subheader("Recent snapshots")
    if not snapshots:
        st.info("No competitor_snapshots yet.")
        return
    snap_table = [
        {
            "Captured": snap.captured_at,
            "Competitor": snap.competitor,
            "Type": snap.snapshot_type,
            "Hash": snap.content_hash[:12],
        }
        for snap in snapshots[:40]
    ]
    st.dataframe(snap_table, use_container_width=True, hide_index=True)
