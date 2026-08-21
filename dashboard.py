"""StreamCtx Agent Control Center — Streamlit dashboard for the four agents.

Run history is read from ``~/.streamctx/sessions.db`` through StreamCtx's
existing WAL-mode ``SessionStorage`` connection helpers. Pending-approval
rows live in the per-agent SQLite files under the same home directory
(``sessions.db`` has no ``pending_approval`` table).
"""

from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from streamctx import get_tracker
from streamctx.storage import get_storage

from agents.coding_agent import coding_agent
from agents.coding_agent.pending_approval import (
    STATUS_APPROVED as CODING_APPROVED,
    STATUS_AUTO_FIX_FAILED,
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    STATUS_REJECTED as CODING_REJECTED,
    PendingApprovalStore as CodingStore,
)
from agents.competitor_agent import competitor_agent
from agents.marketing_agent import marketing_agent
from agents.marketing_agent.pending_approval import (
    STATUS_APPROVED as MARKETING_APPROVED,
    STATUS_PENDING as MARKETING_PENDING,
    STATUS_REJECTED as MARKETING_REJECTED,
    PendingApprovalStore as MarketingStore,
)
from agents.research_agent import research_agent
from shared.audit_log import get_pending_actions, update_status as update_audit_status
from shared.config import AGENT_IDS, STATUS_APPROVED, STATUS_REJECTED

AGENT_TAG_PREFIX = "streamctx-agent-id:"
RESEARCH_TASK_PREFIX = "research:"
CODING_PENDING_STATUSES = (
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    STATUS_AUTO_FIX_FAILED,
)
REFRESH_SECONDS = 3
MAX_PREVIEW_CHARS = 480

_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_LOCK = threading.Lock()
_FUTURES: dict[str, Any] = {}


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    agent_id: str
    run: Callable[[], Any]


@dataclass
class RuntimeState:
    status: str = "idle"
    last_run: Optional[str] = None
    error: Optional[str] = None
    session_id: Optional[int] = None
    summary: Optional[str] = None


@dataclass(frozen=True)
class PendingItem:
    agent_key: str
    agent_name: str
    entry_id: str
    status: str
    created_at: str
    title: str
    preview: str
    store: str  # coding | marketing | audit


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec("coding", "Coding Agent", AGENT_IDS["coding"], coding_agent.run),
    AgentSpec(
        "marketing",
        "Marketing Agent",
        AGENT_IDS["marketing"],
        marketing_agent.run,
    ),
    AgentSpec(
        "competitor",
        "Competitor Agent",
        AGENT_IDS["competitor"],
        competitor_agent.run,
    ),
    AgentSpec(
        "research",
        "Research Agent",
        AGENT_IDS["research"],
        research_agent.run,
    ),
)
AGENT_BY_KEY = {spec.key: spec for spec in AGENTS}

_RUNTIME: dict[str, RuntimeState] = {spec.key: RuntimeState() for spec in AGENTS}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _with_session_conn(fn: Callable[[Any], Any]) -> Any:
    """Run ``fn(conn)`` on StreamCtx's existing WAL-mode connection.

    Uses ``SessionStorage._borrow_read_conn`` when the installed streamctx
    exposes a pool, otherwise ``SessionStorage._connect``. Does not open a
    new sqlite3 connection of its own.
    """
    storage = get_storage()
    borrow = getattr(storage, "_borrow_read_conn", None)
    give_back = getattr(storage, "_return_read_conn", None)
    if callable(borrow) and callable(give_back):
        conn = borrow()
        try:
            return fn(conn)
        finally:
            give_back(conn)
    connect = getattr(storage, "_connect", None)
    if not callable(connect):
        raise RuntimeError(
            "streamctx.storage.SessionStorage has no WAL connection helper "
            "(_connect / _borrow_read_conn); refusing to open a new SQLite method"
        )
    conn = connect()
    try:
        return fn(conn)
    finally:
        conn.close()


def _sessions_query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    def _run(conn: Any) -> list[dict[str, Any]]:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    return _with_session_conn(_run)


def _tag_current_session(agent_id: str) -> Optional[int]:
    tracker = get_tracker(agent_id)
    if tracker.get_session_id() is None:
        tracker.start()
    session_id = tracker.get_session_id()
    if session_id is None:
        return None
    storage = get_storage()
    storage.save_checkpoint(
        int(session_id),
        0,
        [{"role": "system", "content": f"{AGENT_TAG_PREFIX}{agent_id}"}],
    )
    return int(session_id)


def latest_session_for_agent(agent_id: str) -> Optional[dict[str, Any]]:
    tag = f"%{AGENT_TAG_PREFIX}{agent_id}%"
    rows = _sessions_query(
        """
        SELECT s.id, s.started_at, s.ended_at
        FROM sessions s
        WHERE EXISTS (
            SELECT 1 FROM checkpoints c
            WHERE c.session_id = s.id
              AND c.messages_json LIKE ?
        )
        ORDER BY s.id DESC
        LIMIT 1
        """,
        (tag,),
    )
    return rows[0] if rows else None


def _is_research_intake(session_id: str) -> bool:
    return str(session_id).startswith(RESEARCH_TASK_PREFIX)


def _clip(text: Optional[str], limit: int = MAX_PREVIEW_CHARS) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[: limit - 1] + "…"


def _load_coding_pending() -> list[PendingItem]:
    store = CodingStore(enable_default_notifier=False)
    try:
        items: list[PendingItem] = []
        for status in CODING_PENDING_STATUSES:
            for entry in store.list_by_status(status):
                research = _is_research_intake(entry.session_id)
                agent_key = "research" if research else "coding"
                agent_name = AGENT_BY_KEY[agent_key].name
                title = (
                    f"{entry.root_cause} · session {entry.session_id}"
                    if not research
                    else f"Research intake · {entry.session_id}"
                )
                preview = entry.diff or entry.test_results or entry.regression_test or ""
                items.append(
                    PendingItem(
                        agent_key=agent_key,
                        agent_name=agent_name,
                        entry_id=entry.entry_id,
                        status=entry.status,
                        created_at=entry.created_at,
                        title=title,
                        preview=_clip(preview),
                        store="coding",
                    )
                )
        return items
    finally:
        store.close()


def _load_marketing_pending() -> list[PendingItem]:
    store = MarketingStore(enable_default_notifier=False)
    try:
        items: list[PendingItem] = []
        for entry in store.list_by_status(MARKETING_PENDING):
            target = f" → {entry.target}" if entry.target else ""
            items.append(
                PendingItem(
                    agent_key="marketing",
                    agent_name=AGENT_BY_KEY["marketing"].name,
                    entry_id=entry.entry_id,
                    status=entry.status,
                    created_at=entry.created_at,
                    title=f"{entry.platform} {entry.content_type}{target}",
                    preview=_clip(entry.content),
                    store="marketing",
                )
            )
        return items
    finally:
        store.close()


def _load_audit_pending() -> list[PendingItem]:
    """Fallback for the older JSONL audit log (competitor/marketing CLI drafts)."""
    items: list[PendingItem] = []
    try:
        pending = get_pending_actions()
    except Exception:
        return items
    known_ids = {spec.agent_id for spec in AGENTS}
    key_by_id = {spec.agent_id: spec.key for spec in AGENTS}
    for entry in pending:
        agent_id = str(entry.get("agent") or "")
        if agent_id not in known_ids:
            continue
        key = key_by_id[agent_id]
        payload = entry.get("payload") or {}
        preview = (
            payload.get("summary")
            or payload.get("draft_text")
            or payload.get("proposed_patch")
            or str(payload)
        )
        items.append(
            PendingItem(
                agent_key=key,
                agent_name=AGENT_BY_KEY[key].name,
                entry_id=str(entry["id"]),
                status=str(entry.get("status") or "pending_approval"),
                created_at=str(entry.get("timestamp") or ""),
                title=str(entry.get("action_type") or "audit entry"),
                preview=_clip(str(preview)),
                store="audit",
            )
        )
    return items


def load_pending_approvals() -> list[PendingItem]:
    items = _load_coding_pending() + _load_marketing_pending() + _load_audit_pending()
    items.sort(key=lambda item: item.created_at, reverse=True)
    return items


def pending_counts() -> dict[str, int]:
    counts = {spec.key: 0 for spec in AGENTS}
    for item in load_pending_approvals():
        counts[item.agent_key] = counts.get(item.agent_key, 0) + 1
    return counts


def approve_entry(item: PendingItem) -> None:
    if item.store == "coding":
        store = CodingStore(enable_default_notifier=False)
        try:
            store.update_status(item.entry_id, CODING_APPROVED)
        finally:
            store.close()
        return
    if item.store == "marketing":
        store = MarketingStore(enable_default_notifier=False)
        try:
            store.approve(item.entry_id)
        finally:
            store.close()
        return
    update_audit_status(item.entry_id, STATUS_APPROVED, approved_by="dashboard")


def reject_entry(item: PendingItem) -> None:
    if item.store == "coding":
        store = CodingStore(enable_default_notifier=False)
        try:
            store.update_status(item.entry_id, CODING_REJECTED)
        finally:
            store.close()
        return
    if item.store == "marketing":
        store = MarketingStore(enable_default_notifier=False)
        try:
            store.reject(item.entry_id)
        finally:
            store.close()
        return
    update_audit_status(item.entry_id, STATUS_REJECTED, approved_by="dashboard")


def _summarize_result(result: Any) -> str:
    if result is None:
        return "finished"
    if hasattr(result, "signals") and hasattr(result, "errors"):
        return (
            f"signals={len(result.signals)} skipped={len(result.skipped)} "
            f"errors={len(result.errors)}"
        )
    if hasattr(result, "failures_detected"):
        return (
            f"failures={result.failures_detected} "
            f"ready={result.fixes_ready} blocked={result.blocked_for_review}"
        )
    if isinstance(result, list):
        return f"queued {len(result)} draft(s)"
    if isinstance(result, dict):
        errors = result.get("stage_errors") or []
        ok = [name for name, value in result.items() if name != "stage_errors" and value is not None]
        if errors:
            return f"stages ok={','.join(ok) or 'none'}; errors={len(errors)}"
        return f"stages ok={','.join(ok) or 'none'}"
    return str(result)[:200]


def _execute_agent(key: str) -> Any:
    spec = AGENT_BY_KEY[key]
    tracker = get_tracker(spec.agent_id)
    tracker.start()
    session_id = _tag_current_session(spec.agent_id)
    with _LOCK:
        _RUNTIME[key].session_id = session_id
        _RUNTIME[key].status = "running"
        _RUNTIME[key].error = None
        _RUNTIME[key].summary = None
    try:
        result = spec.run()
        summary = _summarize_result(result)
        with _LOCK:
            _RUNTIME[key].status = "completed"
            _RUNTIME[key].last_run = _now_iso()
            _RUNTIME[key].error = None
            _RUNTIME[key].summary = summary
        return result
    except Exception as exc:
        with _LOCK:
            _RUNTIME[key].status = "failed"
            _RUNTIME[key].last_run = _now_iso()
            _RUNTIME[key].error = f"{exc}\n{traceback.format_exc()}"
            _RUNTIME[key].summary = str(exc)
        raise
    finally:
        try:
            tracker.stop()
        except Exception:
            pass


def submit_agent(key: str) -> None:
    with _LOCK:
        current = _RUNTIME[key]
        future = _FUTURES.get(key)
        if current.status == "running" or (future is not None and not future.done()):
            return
        current.status = "running"
        current.error = None
        current.summary = "starting…"
        _FUTURES[key] = _EXECUTOR.submit(_execute_agent, key)


def submit_all_agents() -> None:
    for spec in AGENTS:
        submit_agent(spec.key)


def any_agent_running() -> bool:
    with _LOCK:
        return any(state.status == "running" for state in _RUNTIME.values())


def load_agent_statuses() -> list[dict[str, Any]]:
    counts = pending_counts()
    cards: list[dict[str, Any]] = []
    for spec in AGENTS:
        with _LOCK:
            runtime = RuntimeState(
                status=_RUNTIME[spec.key].status,
                last_run=_RUNTIME[spec.key].last_run,
                error=_RUNTIME[spec.key].error,
                session_id=_RUNTIME[spec.key].session_id,
                summary=_RUNTIME[spec.key].summary,
            )
        session = None
        try:
            session = latest_session_for_agent(spec.agent_id)
        except Exception:
            session = None
        last_run = runtime.last_run
        if last_run is None and session is not None:
            last_run = session.get("ended_at") or session.get("started_at")
        status = runtime.status
        if status == "idle" and session is not None:
            if session.get("ended_at"):
                status = "completed"
            else:
                status = "running"
        cards.append(
            {
                "key": spec.key,
                "name": spec.name,
                "agent_id": spec.agent_id,
                "status": status,
                "last_run": last_run,
                "pending": counts.get(spec.key, 0),
                "error": runtime.error,
                "summary": runtime.summary,
                "session_id": runtime.session_id
                or (session.get("id") if session else None),
            }
        )
    return cards


def _status_emoji(status: str) -> str:
    return {
        "idle": "⚪",
        "running": "🔵",
        "completed": "🟢",
        "failed": "🔴",
    }.get(status, "⚪")


def _in_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


def render() -> None:
    import streamlit as st

    st.set_page_config(
        page_title="StreamCtx Agent Control Center",
        layout="wide",
        page_icon="🎛️",
    )
    st.title("StreamCtx Agent Control Center")
    st.caption(
        "Run history from `~/.streamctx/sessions.db` (StreamCtx WAL storage). "
        "Pending approvals from the per-agent SQLite queues."
    )

    top = st.columns([1, 1, 2])
    with top[0]:
        if st.button("Run All", type="primary", use_container_width=True):
            submit_all_agents()
            st.rerun()
    with top[1]:
        if st.button("Refresh", use_container_width=True):
            st.rerun()
    with top[2]:
        auto = st.checkbox(
            f"Auto-refresh every {REFRESH_SECONDS}s",
            value=False,
            key="auto_refresh",
        )

    cards = load_agent_statuses()
    columns = st.columns(4)
    for column, card in zip(columns, cards):
        with column:
            st.subheader(card["name"])
            st.markdown(f"{_status_emoji(card['status'])} **{card['status']}**")
            last_run = card["last_run"] or "never"
            st.caption(f"Last run: `{last_run}`")
            st.metric("Pending approval", card["pending"])
            if card["summary"]:
                st.caption(card["summary"])
            if card["status"] == "running":
                st.info("Running…")
            elif card["status"] == "failed" and card["error"]:
                st.error(card["error"].splitlines()[0])
            if st.button("Run", key=f"run-{card['key']}", use_container_width=True):
                submit_agent(card["key"])
                st.rerun()

    st.divider()
    st.header("Pending Approvals")
    pending = load_pending_approvals()
    if not pending:
        st.success("No pending_approval items across the four agents.")
    for item in pending:
        with st.container(border=True):
            st.markdown(f"**{item.agent_name}** · `{item.status}` · `{item.entry_id}`")
            st.caption(f"{item.title} · {item.created_at}")
            if item.preview:
                st.code(item.preview, language=None)
            actions = st.columns([1, 1, 6])
            with actions[0]:
                if st.button("Approve", key=f"approve-{item.store}-{item.entry_id}"):
                    approve_entry(item)
                    st.rerun()
            with actions[1]:
                if st.button("Reject", key=f"reject-{item.store}-{item.entry_id}"):
                    reject_entry(item)
                    st.rerun()

    if auto or any_agent_running():
        time.sleep(REFRESH_SECONDS)
        st.rerun()


if _in_streamlit():
    render()
