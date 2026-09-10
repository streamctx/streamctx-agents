"""Marketing Agent Content Pipeline — tracking UI and local store.

Separate from the marketing agent. This module only logs and visualizes
content items (title, channel, stage, flag, notes). It does not draft,
post, approve, or change marketing-agent prompts, safety rules, or queues.
"""

from __future__ import annotations

import html
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from shared.db import connect

DEFAULT_PIPELINE_DB = (
    Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    / "content_pipeline.db"
)

STAGE_DRAFT = "Draft"
STAGE_NEEDS_REVIEW = "Needs Review"
STAGE_READY_TO_POST = "Ready to Post"
STAGE_PUBLISHED = "Published"
STAGES: tuple[str, ...] = (
    STAGE_DRAFT,
    STAGE_NEEDS_REVIEW,
    STAGE_READY_TO_POST,
    STAGE_PUBLISHED,
)

FLAG_NONE = ""
FLAG_LEGAL = "Legal review"
FLAG_FOUNDER = "Founder/Eng review"
FLAG_PRICING = "Pricing sign-off"
FLAGS: tuple[str, ...] = (FLAG_NONE, FLAG_LEGAL, FLAG_FOUNDER, FLAG_PRICING)
FLAG_CHOICES: tuple[str, ...] = (
    "None",
    FLAG_LEGAL,
    FLAG_FOUNDER,
    FLAG_PRICING,
)

# Display labels for the marketing agent's existing platforms — not a new
# channel set. GitHub is listed in marketing_agent.run_draft_cycle examples.
CHANNELS: tuple[str, ...] = (
    "Hacker News",
    "Reddit",
    "LinkedIn",
    "Twitter / X",
    "Dev.to",
    "Indie Hackers",
    "Product Hunt",
    "GitHub",
)

STAGE_ACCENT = {
    STAGE_DRAFT: "#58a6ff",
    STAGE_NEEDS_REVIEW: "#d29922",
    STAGE_READY_TO_POST: "#3fb950",
    STAGE_PUBLISHED: "#a371f7",
}

STAGE_SLUG = {
    STAGE_DRAFT: "draft",
    STAGE_NEEDS_REVIEW: "needs-review",
    STAGE_READY_TO_POST: "ready-to-post",
    STAGE_PUBLISHED: "published",
}
FOCUS_STAGE_KEY = "scp_focus_stage"
SCROLL_STAGE_KEY = "scp_scroll_stage"

FLAG_CLASS = {
    FLAG_LEGAL: "scp-flag-legal",
    FLAG_FOUNDER: "scp-flag-founder",
    FLAG_PRICING: "scp-flag-pricing",
}

DEMO_ITEMS: tuple[dict[str, str], ...] = (
    {
        "title": "HN comment: wrap() tracks per-client instance",
        "channel": "Hacker News",
        "stage": STAGE_NEEDS_REVIEW,
        "flag": FLAG_FOUNDER,
        "notes": "Reply only. No product pitch; cite the checkpoint/resume behavior.",
    },
    {
        "title": "LinkedIn: checkpoint/resume launch note",
        "channel": "LinkedIn",
        "stage": STAGE_DRAFT,
        "flag": FLAG_LEGAL,
        "notes": "Claims about persistence need a second pass before scheduling.",
    },
    {
        "title": "Dev.to: context compression walkthrough",
        "channel": "Dev.to",
        "stage": STAGE_READY_TO_POST,
        "flag": FLAG_NONE,
        "notes": "Tutorial draft is frozen; ready once the review queue clears.",
    },
    {
        "title": "Reddit reply: r/LocalLLaMA observability",
        "channel": "Reddit",
        "stage": STAGE_DRAFT,
        "flag": FLAG_NONE,
        "notes": "Answer the tooling question first; mention StreamCtx only if asked.",
    },
    {
        "title": "Product Hunt launch copy",
        "channel": "Product Hunt",
        "stage": STAGE_NEEDS_REVIEW,
        "flag": FLAG_PRICING,
        "notes": "Pricing line in the first comment still needs sign-off.",
    },
    {
        "title": "Twitter thread: attribution engine",
        "channel": "Twitter / X",
        "stage": STAGE_PUBLISHED,
        "flag": FLAG_NONE,
        "notes": "Posted. Tracking layer only — agent did not auto-publish.",
    },
)


@dataclass(frozen=True)
class ContentItem:
    item_id: str
    title: str
    channel: str
    stage: str
    flag: str
    notes: str
    created_at: str
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_item(row: sqlite3.Row) -> ContentItem:
    return ContentItem(
        item_id=row["item_id"],
        title=row["title"],
        channel=row["channel"],
        stage=row["stage"],
        flag=row["flag"] or "",
        notes=row["notes"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def normalize_flag(flag: Optional[str]) -> str:
    value = (flag or "").strip()
    if value in {"None", "none", FLAG_NONE}:
        return FLAG_NONE
    if value not in FLAGS:
        raise ValueError(f"unknown flag: {flag!r}")
    return value


def neighbor_stage(stage: str, delta: int) -> Optional[str]:
    if stage not in STAGES:
        raise ValueError(f"unknown stage: {stage!r}")
    index = STAGES.index(stage) + delta
    if 0 <= index < len(STAGES):
        return STAGES[index]
    return None


class ContentPipelineStore:
    """SQLite log of pipeline cards. Isolated from marketing_agent.db."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        self.db_path = Path(db_path or DEFAULT_PIPELINE_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = connect(
            schema="content_pipeline",
            db_path=self.db_path,
            check_same_thread=False,
        )
        self._init_db()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS content_items (
                    item_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    flag TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def seed_if_empty(self, items: Sequence[dict[str, str]] = DEMO_ITEMS) -> int:
        if self.list_items():
            return 0
        created = 0
        for item in items:
            self.create_item(
                title=item["title"],
                channel=item["channel"],
                stage=item["stage"],
                flag=item.get("flag", FLAG_NONE),
                notes=item.get("notes", ""),
            )
            created += 1
        return created

    def create_item(
        self,
        *,
        title: str,
        channel: str,
        stage: str = STAGE_DRAFT,
        flag: str = FLAG_NONE,
        notes: str = "",
    ) -> ContentItem:
        cleaned_title = (title or "").strip()
        if not cleaned_title:
            raise ValueError("title must not be empty")
        if channel not in CHANNELS:
            raise ValueError(f"unknown channel: {channel!r}")
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage!r}")
        cleaned_flag = normalize_flag(flag)
        now = _now_iso()
        item = ContentItem(
            item_id=str(uuid.uuid4()),
            title=cleaned_title,
            channel=channel,
            stage=stage,
            flag=cleaned_flag,
            notes=(notes or "").strip(),
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO content_items (
                    item_id, title, channel, stage, flag, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.item_id,
                    item.title,
                    item.channel,
                    item.stage,
                    item.flag,
                    item.notes,
                    item.created_at,
                    item.updated_at,
                ),
            )
            self._conn.commit()
        return item

    def get_item(self, item_id: str) -> Optional[ContentItem]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM content_items WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        return _row_to_item(row) if row is not None else None

    def list_items(self) -> list[ContentItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM content_items ORDER BY created_at ASC"
            ).fetchall()
        return [_row_to_item(row) for row in rows]

    def list_by_stage(self, stage: str) -> list[ContentItem]:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage!r}")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM content_items
                WHERE stage = ?
                ORDER BY created_at ASC
                """,
                (stage,),
            ).fetchall()
        return [_row_to_item(row) for row in rows]

    def counts_by_stage(self) -> dict[str, int]:
        counts = {stage: 0 for stage in STAGES}
        with self._lock:
            rows = self._conn.execute(
                "SELECT stage, COUNT(*) AS n FROM content_items GROUP BY stage"
            ).fetchall()
        for row in rows:
            if row["stage"] in counts:
                counts[row["stage"]] = int(row["n"])
        return counts

    def update_item(
        self,
        item_id: str,
        *,
        title: Optional[str] = None,
        channel: Optional[str] = None,
        stage: Optional[str] = None,
        flag: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> ContentItem:
        current = self.get_item(item_id)
        if current is None:
            raise KeyError(item_id)
        new_title = current.title if title is None else title.strip()
        if not new_title:
            raise ValueError("title must not be empty")
        new_channel = current.channel if channel is None else channel
        if new_channel not in CHANNELS:
            raise ValueError(f"unknown channel: {new_channel!r}")
        new_stage = current.stage if stage is None else stage
        if new_stage not in STAGES:
            raise ValueError(f"unknown stage: {new_stage!r}")
        new_flag = current.flag if flag is None else normalize_flag(flag)
        new_notes = current.notes if notes is None else notes.strip()
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE content_items
                SET title = ?, channel = ?, stage = ?, flag = ?, notes = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (new_title, new_channel, new_stage, new_flag, new_notes, now, item_id),
            )
            self._conn.commit()
        updated = self.get_item(item_id)
        assert updated is not None
        return updated

    def move_stage(self, item_id: str, delta: int) -> ContentItem:
        current = self.get_item(item_id)
        if current is None:
            raise KeyError(item_id)
        nxt = neighbor_stage(current.stage, delta)
        if nxt is None:
            return current
        return self.update_item(item_id, stage=nxt)

    def delete_item(self, item_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM content_items WHERE item_id = ?",
                (item_id,),
            )
            self._conn.commit()
            return cursor.rowcount > 0


def _relative_time(ts: str, now: Optional[datetime] = None) -> str:
    current = now or datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    seconds = int((current.astimezone(timezone.utc) - parsed).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    days = seconds // 86400
    if days < 7:
        return f"{days}d ago"
    return f"{days // 7}w ago"


def _stage_tab_label(count: int, stage: str) -> str:
    return f"{count}\n{stage}"


def _card_html(item: ContentItem) -> str:
    title = html.escape(item.title)
    channel = html.escape(item.channel)
    notes = html.escape(item.notes) if item.notes else "No notes."
    notes_class = "scp-notes" if item.notes else "scp-notes scp-muted"
    flag_html = ""
    if item.flag:
        flag_class = FLAG_CLASS.get(item.flag, "scp-flag-founder")
        flag_html = (
            f'<span class="scp-pill {flag_class}">{html.escape(item.flag)}</span>'
        )
    accent = STAGE_ACCENT.get(item.stage, "#58a6ff")
    logged = html.escape(_relative_time(item.updated_at))
    return f"""
<div class="scp-card" style="border-left-color:{accent}">
  <div class="scp-card-title">{title}</div>
  <div class="scp-pills">
    <span class="scp-pill scp-channel">{channel}</span>
    {flag_html}
  </div>
  <div class="{notes_class}">{notes}</div>
  <div class="scp-meta">checkpoint logged {logged}</div>
</div>
"""


PIPELINE_CSS = """
<style>
.scp-kicker {
  color: #f0883e;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  font-weight: 700;
  text-transform: uppercase;
  margin-bottom: 0.15rem;
}
.scp-title {
  color: #e6edf3;
  font-size: 1.55rem;
  font-weight: 650;
  margin: 0 0 0.25rem 0;
  letter-spacing: -0.02em;
}
.scp-sub {
  color: #8b949e;
  font-size: 0.9rem;
  margin-bottom: 0.85rem;
}
.scp-col-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.35rem 0.15rem 0.55rem 0.15rem;
}
.scp-col-head.scp-col-focused {
  border-bottom: 2px solid #58a6ff;
  padding-bottom: 0.35rem;
}
.scp-col-anchor { scroll-margin-top: 0.75rem; }
.scp-col-name {
  color: #e6edf3;
  font-weight: 650;
  font-size: 0.95rem;
}
.scp-col-count {
  background: #21262d;
  color: #8b949e;
  border-radius: 999px;
  font-size: 0.75rem;
  padding: 0.1rem 0.5rem;
}
.scp-card {
  background: linear-gradient(180deg, #161b22 0%, #12161c 100%);
  border: 1px solid #30363d;
  border-left: 3px solid #58a6ff;
  border-radius: 10px 10px 0 0;
  padding: 0.75rem 0.85rem 0.4rem 0.85rem;
  margin-bottom: 0;
}
.scp-card-title {
  color: #e6edf3;
  font-weight: 600;
  font-size: 0.92rem;
  line-height: 1.35;
  margin-bottom: 0.45rem;
}
.scp-pills { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-bottom: 0.45rem; }
.scp-pill {
  font-size: 0.72rem;
  border-radius: 999px;
  padding: 0.12rem 0.5rem;
  border: 1px solid #30363d;
}
.scp-channel { color: #79c0ff; background: #122033; border-color: #1f6feb55; }
.scp-flag-legal { color: #d29922; background: #2a2111; border-color: #d2992255; }
.scp-flag-founder { color: #58a6ff; background: #122033; border-color: #58a6ff55; }
.scp-flag-pricing { color: #f0883e; background: #2a1810; border-color: #f0883e55; }
.scp-notes {
  color: #c9d1d9;
  font-size: 0.82rem;
  line-height: 1.4;
  margin-bottom: 0.4rem;
}
.scp-muted { color: #8b949e; font-style: italic; }
.scp-meta {
  color: #8b949e;
  font-size: 0.72rem;
  letter-spacing: 0.02em;
}
.scp-empty {
  color: #8b949e;
  font-size: 0.82rem;
  border: 1px dashed #30363d;
  border-radius: 10px;
  padding: 0.85rem;
  background: #0e1117;
}
.scp-card-tail {
  height: 8px;
  background: linear-gradient(180deg, #12161c 0%, #12161c 100%);
  border: 1px solid #30363d;
  border-top: none;
  border-radius: 0 0 10px 10px;
  margin: -0.35rem 0 0.55rem 0;
}
div[class*="st-key-scp-stage-tab-"] button {
  background: #161b22 !important;
  border: 1px solid #30363d !important;
  border-top-width: 2px !important;
  border-radius: 10px !important;
  color: #e6edf3 !important;
  min-height: 4.35rem !important;
  padding: 0.55rem 0.55rem 0.5rem !important;
  white-space: pre-line !important;
  font-weight: 700 !important;
  font-size: 1.35rem !important;
  line-height: 1.15 !important;
  box-shadow: none !important;
}
div[class*="st-key-scp-stage-tab-"] button p,
div[class*="st-key-scp-stage-tab-"] button div {
  white-space: pre-line !important;
  color: inherit !important;
}
div[class*="st-key-scp-stage-tab-draft"] button { border-top-color: #58a6ff !important; }
div[class*="st-key-scp-stage-tab-needs-review"] button { border-top-color: #d29922 !important; }
div[class*="st-key-scp-stage-tab-ready-to-post"] button { border-top-color: #3fb950 !important; }
div[class*="st-key-scp-stage-tab-published"] button { border-top-color: #a371f7 !important; }
div[class*="st-key-scp-stage-tab-"] button[kind="primary"],
div[class*="st-key-scp-stage-tab-"] button[data-testid="baseButton-primary"] {
  background: #21262d !important;
  color: #e6edf3 !important;
  box-shadow: 0 0 0 1px #58a6ff88, inset 0 0 0 1px #58a6ff44 !important;
}
div[class*="st-key-scp-stage-tab-needs-review"] button[kind="primary"],
div[class*="st-key-scp-stage-tab-needs-review"] button[data-testid="baseButton-primary"] {
  box-shadow: 0 0 0 1px #d2992288, inset 0 0 0 1px #d2992244 !important;
}
div[class*="st-key-scp-stage-tab-ready-to-post"] button[kind="primary"],
div[class*="st-key-scp-stage-tab-ready-to-post"] button[data-testid="baseButton-primary"] {
  box-shadow: 0 0 0 1px #3fb95088, inset 0 0 0 1px #3fb95044 !important;
}
div[class*="st-key-scp-stage-tab-published"] button[kind="primary"],
div[class*="st-key-scp-stage-tab-published"] button[data-testid="baseButton-primary"] {
  box-shadow: 0 0 0 1px #a371f788, inset 0 0 0 1px #a371f744 !important;
}
div[class*="st-key-scp-icon-"] {
  width: 28px !important;
  min-width: 28px !important;
  flex: 0 0 28px !important;
  margin: 0 !important;
  padding: 0 !important;
}
div[class*="st-key-scp-icon-"] button {
  width: 26px !important;
  min-width: 26px !important;
  max-width: 26px !important;
  height: 26px !important;
  min-height: 26px !important;
  padding: 0 !important;
  line-height: 26px !important;
  font-size: 0.72rem !important;
  border-radius: 6px !important;
  background: #21262d !important;
  border: 1px solid #30363d !important;
  color: #c9d1d9 !important;
}
div[class*="st-key-scp-icon-del-"] button {
  color: #f85149 !important;
}
div[class*="st-key-scp-icon-"] button:disabled {
  opacity: 0.35 !important;
}
</style>
"""


def _inject_pipeline_css(st: Any) -> None:
    st.markdown(PIPELINE_CSS, unsafe_allow_html=True)


def _scroll_to_stage_column(slug: str) -> None:
    import streamlit.components.v1 as components

    components.html(
        f"""
        <script>
        const doc = window.parent.document;
        const el = doc.getElementById("scp-col-{slug}");
        if (el) {{
          el.scrollIntoView({{ behavior: "smooth", block: "nearest", inline: "center" }});
        }}
        </script>
        """,
        height=0,
    )


def _render_stage_tabs(st: Any, counts: dict[str, int]) -> None:
    focus = st.session_state.get(FOCUS_STAGE_KEY)
    tab_cols = st.columns(len(STAGES))
    for column, stage in zip(tab_cols, STAGES):
        slug = STAGE_SLUG[stage]
        active = focus == stage
        with column:
            if st.button(
                _stage_tab_label(counts[stage], stage),
                key=f"scp-stage-tab-{slug}",
                use_container_width=True,
                type="primary" if active else "secondary",
                help=f"Jump to the {stage} column",
            ):
                if st.session_state.get(FOCUS_STAGE_KEY) == stage:
                    st.session_state[FOCUS_STAGE_KEY] = None
                    st.session_state.pop(SCROLL_STAGE_KEY, None)
                else:
                    st.session_state[FOCUS_STAGE_KEY] = stage
                    st.session_state[SCROLL_STAGE_KEY] = slug
                st.rerun()


def _render_card_actions(st: Any, store: ContentPipelineStore, item: ContentItem) -> None:
    prev_enabled = neighbor_stage(item.stage, -1) is not None
    next_enabled = neighbor_stage(item.stage, 1) is not None
    _spacer, back, forward, delete = st.columns([1, 0.12, 0.12, 0.12], gap="small")
    if back.button(
        "←",
        key=f"scp-icon-back-{item.item_id}",
        disabled=not prev_enabled,
        help="Move to previous stage",
    ):
        store.move_stage(item.item_id, -1)
        st.rerun()
    if forward.button(
        "→",
        key=f"scp-icon-fwd-{item.item_id}",
        disabled=not next_enabled,
        help="Move to next stage",
    ):
        store.move_stage(item.item_id, 1)
        st.rerun()
    if delete.button(
        "×",
        key=f"scp-icon-del-{item.item_id}",
        help="Remove from tracking board",
    ):
        store.delete_item(item.item_id)
        st.rerun()
    st.markdown('<div class="scp-card-tail"></div>', unsafe_allow_html=True)


def render_pipeline_tab(
    st: Any,
    *,
    db_path: Optional[Path | str] = None,
    seed_demo: bool = True,
) -> None:
    """Kanban tracking board. Does not call the marketing agent."""
    _inject_pipeline_css(st)
    store = ContentPipelineStore(db_path=db_path)
    try:
        if seed_demo:
            store.seed_if_empty()
        _render_pipeline_body(st, store)
    finally:
        store.close()


def _render_pipeline_body(st: Any, store: ContentPipelineStore) -> None:
    st.markdown('<div class="scp-kicker">Trace · Checkpoint</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="scp-title">Marketing Agent Content Pipeline</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="scp-sub">Tracking layer only — logs title, channel, stage, '
        "flag, and notes. It does not draft, post, or change marketing-agent "
        "guardrails.</div>",
        unsafe_allow_html=True,
    )

    counts = store.counts_by_stage()
    _render_stage_tabs(st, counts)
    scroll_slug = st.session_state.pop(SCROLL_STAGE_KEY, None)
    if scroll_slug:
        _scroll_to_stage_column(scroll_slug)

    with st.expander("Log a content item", expanded=False):
        with st.form("scp-new-item", clear_on_submit=True):
            title = st.text_input("Title")
            row = st.columns(3)
            channel = row[0].selectbox("Channel", CHANNELS)
            stage = row[1].selectbox("Stage", STAGES)
            flag_label = row[2].selectbox("Flag", FLAG_CHOICES)
            notes = st.text_area("Notes", height=80)
            submitted = st.form_submit_button("Log item")
            if submitted:
                try:
                    store.create_item(
                        title=title,
                        channel=channel,
                        stage=stage,
                        flag=FLAG_NONE if flag_label == "None" else flag_label,
                        notes=notes,
                    )
                    st.success("Logged. The marketing agent was not invoked.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    focus = st.session_state.get(FOCUS_STAGE_KEY)
    columns = st.columns(len(STAGES))
    for column, stage in zip(columns, STAGES):
        items = store.list_by_stage(stage)
        slug = STAGE_SLUG[stage]
        accent = STAGE_ACCENT[stage]
        focused = focus == stage
        head_class = "scp-col-head scp-col-focused" if focused else "scp-col-head"
        with column:
            st.markdown(
                f'<div id="scp-col-{slug}" class="scp-col-anchor {head_class}" '
                f'style="{"border-bottom-color:" + accent if focused else ""}">'
                f'<span class="scp-col-name">{html.escape(stage)}</span>'
                f'<span class="scp-col-count">{len(items)}</span>'
                f"</div>",
                unsafe_allow_html=True,
            )
            if not items:
                st.markdown(
                    '<div class="scp-empty">No items in this stage.</div>',
                    unsafe_allow_html=True,
                )
            for item in items:
                st.markdown(_card_html(item), unsafe_allow_html=True)
                _render_card_actions(st, store, item)
