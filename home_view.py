"""Morning command-center home view — unified pending-approval inbox.

Dashboard-layer only. Reads each agent's existing SQLite queue (and the
marketing content-pipeline tracking board) and routes Approve / Decline /
Ready-to-publish to functions that already exist. Does not add a shared
schema, a second command bar, or any auto-publish path.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from dashboard import (
    AGENT_BY_KEY,
    PendingItem,
    approve_entry,
    is_marketing_brief_item,
    mark_ready_to_publish,
    reject_entry,
    relative_time,
)

AGENT_ACCENT = {
    "coding": "#58a6ff",
    "marketing": "#3fb950",
    "research": "#a371f7",
    "competitor": "#d29922",
    "presales": "#f0883e",
    "techsupport": "#39d0d8",
    "legal": "#f778ba",
}

HOME_INBOX_CSS = """
<style>
.home-kicker {
  color: #f0883e;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  font-weight: 700;
  text-transform: uppercase;
  margin-bottom: 0.15rem;
}
.home-title {
  color: #e6edf3;
  font-size: 1.45rem;
  font-weight: 650;
  margin: 0 0 0.2rem 0;
  letter-spacing: -0.02em;
}
.home-sub {
  color: #8b949e;
  font-size: 0.9rem;
  margin-bottom: 0.75rem;
}
.home-row {
  background: linear-gradient(180deg, #161b22 0%, #12161c 100%);
  border: 1px solid #30363d;
  border-left: 3px solid #58a6ff;
  border-radius: 10px;
  padding: 0.75rem 0.9rem 0.55rem 0.9rem;
  margin-bottom: 0.45rem;
}
.home-row-name {
  color: #e6edf3;
  font-weight: 650;
  font-size: 0.95rem;
}
.home-row-summary {
  color: #c9d1d9;
  font-size: 0.92rem;
  margin-top: 0.25rem;
}
.home-row-meta {
  color: #8b949e;
  font-size: 0.78rem;
  margin-top: 0.2rem;
}
.home-empty {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 10px;
  padding: 0.85rem 0.95rem;
  color: #8b949e;
}
.home-draft {
  color: #c9d1d9;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.82rem;
  white-space: pre-wrap;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 0.7rem 0.8rem;
  margin-top: 0.45rem;
  max-height: 22rem;
  overflow: auto;
}
</style>
"""


def inbox_summary(item: PendingItem) -> str:
    """One-line founder-facing summary. Prefers the loader-provided summary."""
    if (item.summary or "").strip():
        return item.summary.strip()
    name = item.agent_name.replace(" Agent", "").replace(" / Compliance", "")
    title = (item.title or "").strip() or "item awaiting review"
    return f"{name}: {title}"


def is_marketing_content_draft(item: PendingItem) -> bool:
    """True for generated copy (not an assigned brief) that can be posted by hand."""
    if item.store == "pipeline":
        return True
    if item.store != "marketing":
        return False
    return not is_marketing_brief_item(item)


def _inject_css(st: Any) -> None:
    st.markdown(HOME_INBOX_CSS, unsafe_allow_html=True)


def _row_html(item: PendingItem, now: datetime) -> str:
    accent = AGENT_ACCENT.get(item.agent_key, "#58a6ff")
    when = relative_time(item.created_at, now)
    return (
        f'<div class="home-row" style="border-left-color:{accent}">'
        f'<div class="home-row-name">{html.escape(item.agent_name)}</div>'
        f'<div class="home-row-summary">{html.escape(inbox_summary(item))}</div>'
        f'<div class="home-row-meta">{html.escape(when)} · '
        f'<code>{html.escape(item.status)}</code></div>'
        f"</div>"
    )


def render_unified_inbox(
    st: Any,
    items: list[PendingItem],
    *,
    key_prefix: str = "home-",
    on_open_agent: Optional[Callable[[str], None]] = None,
    now: Optional[datetime] = None,
) -> None:
    """Chronological pending-approval inbox used by Home / Today."""
    _inject_css(st)
    now = now or datetime.now(timezone.utc)
    st.markdown('<div class="home-kicker">Today</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="home-title">Pending approval</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="home-sub">Every queue in one list. Approve / Decline '
        "calls each agent’s existing gate — nothing auto-publishes or auto-sends.</div>",
        unsafe_allow_html=True,
    )
    if not items:
        st.markdown(
            '<div class="home-empty">Nothing waiting. All pending_approval '
            "queues are clear.</div>",
            unsafe_allow_html=True,
        )
        return

    for item in items:
        with st.container(border=False):
            st.markdown(_row_html(item, now), unsafe_allow_html=True)
            if is_marketing_content_draft(item) and (item.body or item.preview):
                st.markdown("**Full draft** — review before you approve.")
                st.markdown(
                    f'<div class="home-draft">{html.escape(item.body or item.preview)}</div>',
                    unsafe_allow_html=True,
                )
            elif item.preview and item.store != "marketing":
                with st.expander("Preview", expanded=False):
                    st.code(item.preview, language=None)

            actions = st.columns([1, 1, 1.4, 1.4, 4])
            with actions[0]:
                if st.button(
                    "Approve",
                    key=f"{key_prefix}approve-{item.store}-{item.entry_id}",
                ):
                    approve_entry(item)
                    st.rerun()
            with actions[1]:
                if st.button(
                    "Decline",
                    key=f"{key_prefix}decline-{item.store}-{item.entry_id}",
                ):
                    reject_entry(item)
                    st.rerun()
            with actions[2]:
                if is_marketing_content_draft(item) and st.button(
                    "Ready to publish",
                    key=f"{key_prefix}ready-{item.store}-{item.entry_id}",
                    help="Mark copy ready for you to post. Does not publish to any platform.",
                ):
                    mark_ready_to_publish(item)
                    st.rerun()
            with actions[3]:
                if on_open_agent is not None and st.button(
                    "Open agent",
                    key=f"{key_prefix}open-{item.store}-{item.entry_id}",
                ):
                    on_open_agent(item.agent_key)
                    st.rerun()
            spec = AGENT_BY_KEY.get(item.agent_key)
            if spec is not None:
                st.caption(spec.role)


def render_home_tab(
    st: Any,
    items: list[PendingItem],
    *,
    key_prefix: str = "dash-home-",
    on_open_agent: Optional[Callable[[str], None]] = None,
) -> None:
    """Dashboard Home / Today tab — same inbox as Agent Manager."""
    render_unified_inbox(
        st,
        items,
        key_prefix=key_prefix,
        on_open_agent=on_open_agent,
    )
