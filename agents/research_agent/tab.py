"""Streamlit Research Agent tab — poll ideas and research intake queue.

Named ``tab.py`` (not ``dashboard.py``) so it cannot shadow the Streamlit
entry script ``dashboard.py`` in ``sys.modules``.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Optional

from agents.coding_agent.pending_approval import (
    STATUS_APPROVED,
    STATUS_AUTO_FIX_FAILED,
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    STATUS_REJECTED,
    PendingApprovalEntry,
    PendingApprovalStore as CodingStore,
    DEFAULT_AGENT_DB as CODING_DB,
    is_intake_task,
)
from agents.research_agent.models import SOURCE_TYPE_ARXIV, SOURCE_TYPE_GITHUB
from agents.research_agent.settings import default_config
from agents.research_agent.storage import DEFAULT_AGENT_DB, ResearchStore

RESEARCH_TASK_PREFIX = "research:"
CODING_PENDING = (
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    STATUS_AUTO_FIX_FAILED,
)

TAB_CSS = """
<style>
.ra-kicker {
  color: #a371f7;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  font-weight: 700;
  text-transform: uppercase;
}
</style>
"""


def render_research_tab(
    st: Any,
    *,
    db_path: Optional[Path | str] = None,
    coding_db: Optional[Path | str] = None,
    key_prefix: str = "ra-",
) -> None:
    st.markdown(TAB_CSS, unsafe_allow_html=True)
    st.markdown(
        '<div class="ra-kicker">Research · Poll-only assign · Ideas queue</div>',
        unsafe_allow_html=True,
    )
    st.header("Research Agent")
    st.caption(
        "Polls arXiv + GitHub into `research_ideas`. Directed Assign is poll-only "
        "(no free-text research intake from the command bar). Research handoffs "
        "that land as coding intake rows (`session_id` starts with `research:`) "
        "appear below for Approve / Reject."
    )

    research_path = Path(db_path) if db_path is not None else DEFAULT_AGENT_DB
    coding_path = Path(coding_db) if coding_db is not None else CODING_DB
    store = ResearchStore(db_path=research_path)
    coding = CodingStore(db_path=coding_path, enable_default_notifier=False)
    try:
        _render_body(st, store, coding, key_prefix=key_prefix)
    finally:
        coding.close()
        store.close()


def _render_body(
    st: Any,
    store: ResearchStore,
    coding: CodingStore,
    *,
    key_prefix: str,
) -> None:
    spec = default_config()
    ideas = store.list_ideas(limit=80)
    arxiv_at = store.last_polled_at(SOURCE_TYPE_ARXIV)
    github_at = store.last_polled_at(SOURCE_TYPE_GITHUB)
    intake = _research_intake(coding)

    metrics = st.columns(4)
    metrics[0].metric("Ideas", len(ideas))
    metrics[1].metric("Research intake pending", len(intake))
    metrics[2].caption(f"arXiv last poll: `{arxiv_at or 'never'}`")
    metrics[3].caption(f"GitHub last poll: `{github_at or 'never'}`")
    st.caption(
        "Relevance: arXiv keywords "
        + ", ".join(spec.arxiv_keywords[:6])
        + " · GitHub topics "
        + ", ".join(spec.github_topics[:6])
    )

    actions = st.columns(3)
    if actions[0].button("Poll arXiv + GitHub", key=f"{key_prefix}poll"):
        from dashboard import submit_research_poll

        submit_research_poll()
        st.rerun()
    if actions[1].button("Poll arXiv only", key=f"{key_prefix}arxiv"):
        from dashboard import submit_research_poll

        submit_research_poll(github=False)
        st.rerun()
    if actions[2].button("Poll GitHub only", key=f"{key_prefix}github"):
        from dashboard import submit_research_poll

        submit_research_poll(arxiv=False)
        st.rerun()

    st.subheader("Research intake (coding pending_approval)")
    st.caption(
        "Rows in `coding_agent.db` whose `session_id` starts with `research:`. "
        "Approve / Reject use the coding store paths."
    )
    if not intake:
        st.success("No research intake rows awaiting review.")
    else:
        for entry in intake:
            with st.container(border=True):
                st.markdown(
                    f"**{html.escape(entry.root_cause or 'intake')}** · "
                    f"`{html.escape(entry.status)}` · "
                    f"`{html.escape(str(entry.session_id))}`",
                    unsafe_allow_html=True,
                )
                st.caption(f"{entry.created_at} · `{entry.entry_id}`")
                preview = entry.diff or entry.test_results or entry.regression_test or ""
                if preview:
                    with st.expander("Preview", expanded=False):
                        st.code(str(preview)[:4000], language=None)
                cols = st.columns([1, 1, 6])
                with cols[0]:
                    if st.button("Approve", key=f"{key_prefix}approve-{entry.entry_id}"):
                        kick_off = is_intake_task(entry)
                        coding.update_status(entry.entry_id, STATUS_APPROVED)
                        if kick_off:
                            from dashboard import submit_intake_fix

                            submit_intake_fix(entry.entry_id, db_path=coding.db_path)
                        st.rerun()
                with cols[1]:
                    if st.button("Reject", key=f"{key_prefix}reject-{entry.entry_id}"):
                        coding.update_status(entry.entry_id, STATUS_REJECTED)
                        st.rerun()

    st.subheader("research_ideas")
    if not ideas:
        st.info("No research_ideas yet. Run poll().")
        return
    for idea in ideas:
        score = (
            f"{idea.composite_score:.2f}"
            if idea.composite_score is not None
            else "unscored"
        )
        with st.container(border=True):
            st.markdown(f"**{html.escape(idea.title)}**", unsafe_allow_html=True)
            st.caption(
                f"{idea.source_type} · {idea.status} · score={score} · "
                f"hype={idea.hype_label or 'unfiltered'} · "
                f"{idea.classification or 'unclassified'} · {idea.detected_at}"
            )
            if idea.content_excerpt:
                st.write(idea.content_excerpt[:400])
            if idea.gap_description:
                st.caption(idea.gap_description[:240])
            st.markdown(f"[{idea.source_url}]({idea.source_url})")


def _research_intake(coding: CodingStore) -> list[PendingApprovalEntry]:
    rows: list[PendingApprovalEntry] = []
    for status in CODING_PENDING:
        for entry in coding.list_by_status(status):
            if str(entry.session_id or "").startswith(RESEARCH_TASK_PREFIX):
                rows.append(entry)
    rows.sort(key=lambda e: e.created_at, reverse=True)
    return rows
