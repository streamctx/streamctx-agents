"""StreamCtx Agent Control Center — Streamlit dashboard for the four agents.

Run history is read from ``~/.streamctx/sessions.db`` through StreamCtx's
existing WAL-mode ``SessionStorage`` connection helpers. Pending-approval
rows live in the per-agent SQLite files under the same home directory
(``sessions.db`` has no ``pending_approval`` table).

The Roster tab is a read of that existing SQLite state (plus optional
writes into coding intake / marketing draft queues). It does not add a
scheduler or a new task table.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from streamctx import get_tracker
from streamctx.storage import get_storage

from agents.coding_agent import coding_agent
from agents.coding_agent.pending_approval import (
    DEFAULT_AGENT_DB as CODING_DB,
    ROOT_CAUSE_INTAKE,
    STATUS_APPROVED as CODING_APPROVED,
    STATUS_AUTO_FIX_FAILED,
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    STATUS_REJECTED as CODING_REJECTED,
    PendingApprovalStore as CodingStore,
    is_intake_task,
)
from agents.competitor_agent import competitor_agent
from agents.competitor_agent.storage import DEFAULT_AGENT_DB as COMPETITOR_DB
from agents.marketing_agent import marketing_agent
from agents.marketing_agent.pending_approval import (
    BRIEF_FINGERPRINT_PREFIX,
    DEFAULT_AGENT_DB as MARKETING_DB,
    STATUS_APPROVED as MARKETING_APPROVED,
    STATUS_DRAFT_FAILED,
    STATUS_PENDING as MARKETING_PENDING,
    STATUS_PUBLISHED as MARKETING_PUBLISHED,
    STATUS_REJECTED as MARKETING_REJECTED,
    PendingApprovalStore as MarketingStore,
    is_marketing_brief,
)
from agents.research_agent import research_agent
from agents.research_agent.storage import DEFAULT_AGENT_DB as RESEARCH_DB
from shared.audit_log import get_pending_actions, update_status as update_audit_status
from shared.config import AGENT_IDS, STATUS_APPROVED, STATUS_REJECTED

DASHBOARD_STATE_DB = (
    Path(os.environ.get("STREAMCTX_HOME", Path.home() / ".streamctx"))
    / "dashboard_state.db"
)
KV_LAST_DASHBOARD_OPEN = "last_dashboard_open"
QUIET_AFTER = timedelta(days=2)

AGENT_TAG_PREFIX = "streamctx-agent-id:"
RESEARCH_TASK_PREFIX = "research:"
CODING_PENDING_STATUSES = (
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
    STATUS_AUTO_FIX_FAILED,
)
MARKETING_PENDING_STATUSES = (
    MARKETING_PENDING,
    STATUS_DRAFT_FAILED,
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
    role: str
    assign_mode: str  # coding | marketing | none


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


@dataclass(frozen=True)
class RosterDbPaths:
    coding: Path = CODING_DB
    marketing: Path = MARKETING_DB
    competitor: Path = COMPETITOR_DB
    research: Path = RESEARCH_DB


@dataclass(frozen=True)
class DomainSnapshot:
    last_ts: Optional[str] = None
    last_text: str = ""
    completed_ts: Optional[str] = None
    completed_text: str = ""
    completed_week: int = 0
    errors_week: int = 0
    durable_error: bool = False
    stale_error: bool = False
    briefing_line: str = "No new completed work since last session."


@dataclass(frozen=True)
class RosterCard:
    key: str
    name: str
    role: str
    assign_mode: str
    status: str
    last_activity: str
    completed_week: int
    new_pending: int
    backlog: int
    errors_week: int
    briefing_line: str
    stale_error: bool
    error_detail: Optional[str] = None
    last_check: Optional[str] = None


@dataclass(frozen=True)
class MorningBriefing:
    lines: tuple[str, ...]
    attention: tuple[str, ...]
    stuck: tuple[str, ...]
    new_pending_total: int
    backlog_total: int


ROSTER_IDLE = "Idle"
ROSTER_WORKING = "Working"
ROSTER_WAITING = "Waiting for Approval"
ROSTER_ERROR = "Error"
ASSIGN_DISABLED_CAPTION = "No task queue yet — this agent runs autonomously"

AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        "coding",
        "Coding Agent",
        AGENT_IDS["coding"],
        coding_agent.run,
        "fixes failing tests",
        "coding",
    ),
    AgentSpec(
        "marketing",
        "Marketing Agent",
        AGENT_IDS["marketing"],
        marketing_agent.run,
        "drafts community posts",
        "marketing",
    ),
    AgentSpec(
        "competitor",
        "Competitor Agent",
        AGENT_IDS["competitor"],
        competitor_agent.run,
        "tracks competitor signals",
        "competitor",
    ),
    AgentSpec(
        "research",
        "Research Agent",
        AGENT_IDS["research"],
        research_agent.run,
        "finds papers and features",
        "none",
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
        for status in MARKETING_PENDING_STATUSES:
            for entry in store.list_by_status(status):
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


def approve_entry(
    item: PendingItem,
    *,
    coding_db: Optional[Path | str] = None,
    marketing_db: Optional[Path | str] = None,
) -> None:
    if item.store == "coding":
        store = CodingStore(db_path=coding_db, enable_default_notifier=False)
        kick_off = False
        try:
            entry = store.get_entry(item.entry_id)
            kick_off = entry is not None and is_intake_task(entry)
            store.update_status(item.entry_id, CODING_APPROVED)
        finally:
            store.close()
        if kick_off:
            submit_intake_fix(item.entry_id, db_path=coding_db)
        return
    if item.store == "marketing":
        store = MarketingStore(db_path=marketing_db, enable_default_notifier=False)
        kick_off = False
        try:
            entry = store.get_entry(item.entry_id)
            kick_off = entry is not None and is_marketing_brief(entry)
            store.approve(item.entry_id)
        finally:
            store.close()
        if kick_off:
            submit_draft_job(item.entry_id, db_path=marketing_db)
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


def submit_assigned_fix(
    text: str,
    *,
    db_path: Optional[Path | str] = None,
    source_root: Optional[Path | str] = None,
) -> None:
    """Kick off Path A gate + fix loop for a dashboard Assign with pytest nodeids."""
    from agents.coding_agent.assign_fix import failed_call_id_for_nodeids
    from agents.coding_agent.intake_fix import parse_pytest_nodeids

    nodeids = parse_pytest_nodeids(text)
    if not nodeids:
        return
    key = f"assign-fix:{failed_call_id_for_nodeids(nodeids)}"
    with _LOCK:
        future = _FUTURES.get(key)
        if future is not None and not future.done():
            return
        coding = _RUNTIME["coding"]
        coding.status = "running"
        coding.error = None
        coding.summary = "generating fix from assigned tests…"
        _FUTURES[key] = _EXECUTOR.submit(
            _execute_assigned_fix, text, db_path, source_root
        )


def _execute_assigned_fix(
    text: str,
    db_path: Optional[Path | str],
    source_root: Optional[Path | str],
) -> Any:
    from agents.coding_agent.assign_fix import process_assigned_nodeids

    try:
        result = process_assigned_nodeids(
            text,
            db_path=db_path,
            source_root=source_root,
        )
        entry = result.pending_entry
        with _LOCK:
            _RUNTIME["coding"].status = "completed"
            _RUNTIME["coding"].last_run = _now_iso()
            _RUNTIME["coding"].error = None
            if entry is not None and entry.diff:
                _RUNTIME["coding"].summary = (
                    f"{entry.status} (confidence={entry.confidence:.2f})"
                )
            else:
                _RUNTIME["coding"].summary = result.reason
        return result
    except Exception as exc:
        with _LOCK:
            _RUNTIME["coding"].status = "failed"
            _RUNTIME["coding"].last_run = _now_iso()
            _RUNTIME["coding"].error = f"{exc}\n{traceback.format_exc()}"
            _RUNTIME["coding"].summary = str(exc)
        raise


def submit_intake_fix(
    entry_id: str,
    *,
    db_path: Optional[Path | str] = None,
    source_root: Optional[Path | str] = None,
) -> None:
    """Kick off Path-B fix generation after a human approves an INTAKE ticket."""
    key = f"intake-fix:{entry_id}"
    with _LOCK:
        future = _FUTURES.get(key)
        if future is not None and not future.done():
            return
        coding = _RUNTIME["coding"]
        coding.status = "running"
        coding.error = None
        coding.summary = "generating fix from assigned task…"
        _FUTURES[key] = _EXECUTOR.submit(
            _execute_intake_fix, entry_id, db_path, source_root
        )


def _execute_intake_fix(
    entry_id: str,
    db_path: Optional[Path | str],
    source_root: Optional[Path | str],
) -> Any:
    from agents.coding_agent.intake_fix import process_approved_intake

    try:
        result = process_approved_intake(
            entry_id,
            db_path=db_path,
            source_root=source_root,
        )
        with _LOCK:
            _RUNTIME["coding"].status = "completed"
            _RUNTIME["coding"].last_run = _now_iso()
            _RUNTIME["coding"].error = None
            _RUNTIME["coding"].summary = result.reason
        return result
    except Exception as exc:
        with _LOCK:
            _RUNTIME["coding"].status = "failed"
            _RUNTIME["coding"].last_run = _now_iso()
            _RUNTIME["coding"].error = f"{exc}\n{traceback.format_exc()}"
            _RUNTIME["coding"].summary = str(exc)
        try:
            store = CodingStore(db_path=db_path, enable_default_notifier=False)
            store.merge_test_results(
                entry_id,
                {"fix_status": STATUS_AUTO_FIX_FAILED, "fix_error": str(exc)},
            )
            store.close()
        except Exception:
            pass
        raise


def submit_draft_job(
    entry_id: str,
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    """Kick off LinkedIn draft generation after a human approves a brief."""
    key = f"draft-job:{entry_id}"
    with _LOCK:
        future = _FUTURES.get(key)
        if future is not None and not future.done():
            return
        marketing = _RUNTIME["marketing"]
        marketing.status = "running"
        marketing.error = None
        marketing.summary = "generating LinkedIn draft from assigned brief…"
        _FUTURES[key] = _EXECUTOR.submit(_execute_draft_job, entry_id, db_path)


def _execute_draft_job(
    entry_id: str,
    db_path: Optional[Path | str],
) -> Any:
    from agents.marketing_agent.draft_job import process_approved_brief

    try:
        result = process_approved_brief(entry_id, db_path=db_path)
        with _LOCK:
            _RUNTIME["marketing"].status = "completed"
            _RUNTIME["marketing"].last_run = _now_iso()
            _RUNTIME["marketing"].error = None
            _RUNTIME["marketing"].summary = result.reason
        return result
    except Exception as exc:
        with _LOCK:
            _RUNTIME["marketing"].status = "failed"
            _RUNTIME["marketing"].last_run = _now_iso()
            _RUNTIME["marketing"].error = f"{exc}\n{traceback.format_exc()}"
            _RUNTIME["marketing"].summary = str(exc)
        raise


def submit_competitor_check(
    text: str,
    *,
    db_path: Optional[Path | str] = None,
    config_path: Optional[Path | str] = None,
) -> None:
    """Force one directed snapshot check after Roster Assign."""
    key = f"competitor-check:{text[:80]}"
    with _LOCK:
        future = _FUTURES.get(key)
        if future is not None and not future.done():
            return
        state = _RUNTIME["competitor"]
        state.status = "running"
        state.error = None
        state.summary = "checking assigned competitor…"
        _FUTURES[key] = _EXECUTOR.submit(
            _execute_competitor_check, text, db_path, config_path
        )


def submit_research_poll(
    *,
    arxiv: bool = True,
    github: bool = True,
    db_path: Optional[Path | str] = None,
) -> None:
    """Kick off ``research_agent.poll.run_poll`` only (no directed Assign)."""
    key = "research-poll"
    with _LOCK:
        future = _FUTURES.get(key)
        if future is not None and not future.done():
            return
        state = _RUNTIME["research"]
        state.status = "running"
        state.error = None
        state.summary = "polling arXiv / GitHub…"
        _FUTURES[key] = _EXECUTOR.submit(
            _execute_research_poll, arxiv, github, db_path
        )


def _execute_research_poll(
    arxiv: bool,
    github: bool,
    db_path: Optional[Path | str],
) -> Any:
    from agents.research_agent.poll import run_poll, summarize
    from agents.research_agent.storage import ResearchStore

    store = ResearchStore(db_path=db_path)
    try:
        result = run_poll(store, arxiv=arxiv, github=github)
        summary = summarize(result)
        with _LOCK:
            _RUNTIME["research"].status = "completed"
            _RUNTIME["research"].last_run = _now_iso()
            _RUNTIME["research"].error = None
            _RUNTIME["research"].summary = summary
        return result
    except Exception as exc:
        with _LOCK:
            _RUNTIME["research"].status = "failed"
            _RUNTIME["research"].last_run = _now_iso()
            _RUNTIME["research"].error = f"{exc}\n{traceback.format_exc()}"
            _RUNTIME["research"].summary = str(exc)
        raise
    finally:
        store.close()


def submit_weekly_drafts(
    *,
    force: bool = False,
    dry_run: bool = False,
    db_path: Optional[Path | str] = None,
) -> None:
    """Kick off ``scripts.weekly_content_draft.run_weekly_drafts``."""
    key = "weekly-drafts"
    with _LOCK:
        future = _FUTURES.get(key)
        if future is not None and not future.done():
            return
        state = _RUNTIME["marketing"]
        state.status = "running"
        state.error = None
        state.summary = "generating weekly content drafts…"
        _FUTURES[key] = _EXECUTOR.submit(
            _execute_weekly_drafts, force, dry_run, db_path
        )


def _execute_weekly_drafts(
    force: bool,
    dry_run: bool,
    db_path: Optional[Path | str],
) -> Any:
    from scripts.weekly_content_draft import run_weekly_drafts

    try:
        result = run_weekly_drafts(force=force, dry_run=dry_run, db_path=db_path)
        summary = result.log_text().strip().splitlines()[0] if result.log_text() else "weekly drafts finished"
        with _LOCK:
            _RUNTIME["marketing"].status = "completed"
            _RUNTIME["marketing"].last_run = _now_iso()
            _RUNTIME["marketing"].error = None
            _RUNTIME["marketing"].summary = summary
        return result
    except Exception as exc:
        with _LOCK:
            _RUNTIME["marketing"].status = "failed"
            _RUNTIME["marketing"].last_run = _now_iso()
            _RUNTIME["marketing"].error = f"{exc}\n{traceback.format_exc()}"
            _RUNTIME["marketing"].summary = str(exc)
        raise


def _execute_competitor_check(
    text: str,
    db_path: Optional[Path | str],
    config_path: Optional[Path | str],
) -> Any:
    from agents.competitor_agent.assign_check import run_directed_check
    from agents.competitor_agent.settings import CompetitorConfig
    from agents.competitor_agent.storage import CompetitorStore

    store = CompetitorStore(db_path=db_path)
    try:
        spec = CompetitorConfig.load(config_path) if config_path else CompetitorConfig.load()
        result = run_directed_check(text, store=store, config=spec)
        with _LOCK:
            _RUNTIME["competitor"].status = "completed" if result.ok else "failed"
            _RUNTIME["competitor"].last_run = _now_iso()
            _RUNTIME["competitor"].error = None if result.ok else result.message
            _RUNTIME["competitor"].summary = result.message
        return result
    except Exception as exc:
        with _LOCK:
            _RUNTIME["competitor"].status = "failed"
            _RUNTIME["competitor"].last_run = _now_iso()
            _RUNTIME["competitor"].error = f"{exc}\n{traceback.format_exc()}"
            _RUNTIME["competitor"].summary = str(exc)
        raise
    finally:
        store.close()


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


def _roster_status_emoji(status: str) -> str:
    return {
        ROSTER_IDLE: "⚪",
        ROSTER_WORKING: "🔵",
        ROSTER_WAITING: "🟡",
        ROSTER_ERROR: "🔴",
    }.get(status, "⚪")


def parse_iso(ts: Optional[str | datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        parsed = ts
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    text = str(ts).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def start_of_utc_day(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def week_start_utc(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)


def relative_time(ts: Optional[str | datetime], now: datetime) -> str:
    parsed = parse_iso(ts)
    if parsed is None:
        return "never"
    seconds = int((now.astimezone(timezone.utc) - parsed).total_seconds())
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


def derive_roster_status(
    *,
    runtime_status: str,
    runtime_error: Optional[str],
    pending: int,
    durable_error: bool,
    open_session: bool,
    session_started_at: Optional[str],
    now: datetime,
) -> str:
    """Idle / Working / Waiting for Approval / Error from existing state."""
    started = parse_iso(session_started_at)
    stale_open = bool(
        open_session and started is not None and started < start_of_utc_day(now)
    )
    live_open = bool(open_session and not stale_open)
    if runtime_status == "running" or live_open:
        return ROSTER_WORKING
    if runtime_status == "failed" or runtime_error or durable_error or stale_open:
        return ROSTER_ERROR
    if pending > 0:
        return ROSTER_WAITING
    return ROSTER_IDLE


def _readonly_query(
    db_path: Optional[Path | str],
    sql: str,
    params: tuple = (),
) -> list[dict[str, Any]]:
    if db_path is None:
        return []
    path = Path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _count_sql(db_path: Optional[Path | str], sql: str, params: tuple = ()) -> int:
    rows = _readonly_query(db_path, sql, params)
    if not rows:
        return 0
    return int(next(iter(rows[0].values())))


def _dashboard_state_path(db_path: Optional[Path | str] = None) -> Path:
    return Path(db_path) if db_path is not None else DASHBOARD_STATE_DB


def read_dashboard_kv(key: str, *, db_path: Optional[Path | str] = None) -> Optional[str]:
    path = _dashboard_state_path(db_path)
    if not path.exists():
        return None
    rows = _readonly_query(path, "SELECT value FROM kv WHERE key = ?", (key,))
    if not rows:
        return None
    value = rows[0].get("value")
    return str(value) if value is not None else None


def write_dashboard_kv(
    key: str,
    value: str,
    *,
    db_path: Optional[Path | str] = None,
) -> None:
    path = _dashboard_state_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def begin_dashboard_visit(
    now: datetime,
    session_state: dict[str, Any],
    *,
    db_path: Optional[Path | str] = None,
) -> Optional[str]:
    """Snapshot last-open time once per Streamlit session, then record this visit.

    Auto-refresh reruns keep the same cutoff so overnight backlog is not
    reclassified as new every few seconds.
    """
    if "roster_visit_cutoff" not in session_state:
        previous = read_dashboard_kv(KV_LAST_DASHBOARD_OPEN, db_path=db_path)
        session_state["roster_visit_cutoff"] = previous
        write_dashboard_kv(KV_LAST_DASHBOARD_OPEN, now.isoformat(), db_path=db_path)
    return session_state.get("roster_visit_cutoff")


def pending_item_is_new(item: PendingItem, cutoff_iso: Optional[str]) -> bool:
    if not cutoff_iso:
        return False
    created = parse_iso(item.created_at)
    cutoff = parse_iso(cutoff_iso)
    if created is None or cutoff is None:
        return str(item.created_at) >= cutoff_iso
    return created >= cutoff


def counts_from_pending(
    items: list[PendingItem],
    *,
    cutoff_iso: Optional[str] = None,
) -> dict[str, int]:
    counts = {spec.key: 0 for spec in AGENTS}
    for item in items:
        if cutoff_iso is not None and not pending_item_is_new(item, cutoff_iso):
            continue
        counts[item.agent_key] = counts.get(item.agent_key, 0) + 1
    return counts


def format_quiet_since(ts: Optional[str]) -> str:
    parsed = parse_iso(ts)
    if parsed is None:
        return "No activity recorded yet."
    return f"Quiet since {parsed.strftime('%b %d')}."


def compose_briefing_line(domain: DomainSnapshot, now: datetime) -> str:
    """Most recent completed action, or quiet-since if nothing recent."""
    action_ts = domain.completed_ts
    action_text = domain.completed_text
    parsed_action = parse_iso(action_ts)
    if parsed_action is not None and action_text:
        if now.astimezone(timezone.utc) - parsed_action < QUIET_AFTER:
            return f"{relative_time(action_ts, now)}: {action_text}"
        return format_quiet_since(action_ts)
    return format_quiet_since(domain.last_ts)


def _join_phrases(parts: list[str], empty: str) -> str:
    if not parts:
        return empty
    if len(parts) == 1:
        return parts[0][0].upper() + parts[0][1:] + "."
    return parts[0][0].upper() + parts[0][1:] + ", " + ", ".join(parts[1:]) + "."


def _coding_activity_text(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "")
    root = str(row.get("root_cause") or "")
    has_diff = bool(str(row.get("diff") or "").strip())
    if root == ROOT_CAUSE_INTAKE:
        if status == STATUS_READY_FOR_APPROVAL:
            return "queued a fix for approval"
        if status == STATUS_AUTO_FIX_FAILED:
            return "auto-fix failed"
        if status == CODING_APPROVED:
            return "approved a fix" if has_diff else "approved an assigned task"
        if status == CODING_REJECTED:
            return "rejected a fix"
        return "queued an assigned task"
    if status == STATUS_AUTO_FIX_FAILED:
        return "auto-fix failed"
    if status == STATUS_READY_FOR_APPROVAL:
        return "queued a fix for approval"
    if status == CODING_APPROVED:
        return "approved a fix"
    if status == CODING_REJECTED:
        return "rejected a fix"
    if status == STATUS_NEEDS_HUMAN_REVIEW:
        return f"flagged {root or 'an issue'} for review"
    return f"updated a {status or 'queue'} item"


def load_coding_domain(
    db_path: Optional[Path | str],
    *,
    now: datetime,
    since_iso: str,
    week_iso: str,
    today_iso: str,
) -> DomainSnapshot:
    latest = _readonly_query(
        db_path,
        """
        SELECT created_at, status, root_cause, diff
        FROM pending_approval
        ORDER BY created_at DESC, entry_id DESC
        LIMIT 1
        """,
    )
    last_ts = str(latest[0]["created_at"]) if latest else None
    last_text = _coding_activity_text(latest[0]) if latest else ""
    completed_row = _readonly_query(
        db_path,
        """
        SELECT created_at, status, root_cause, diff
        FROM pending_approval
        WHERE status IN (?, ?, ?)
          AND NOT (
            root_cause = ?
            AND (diff IS NULL OR TRIM(COALESCE(diff, '')) = '')
          )
        ORDER BY created_at DESC, entry_id DESC
        LIMIT 1
        """,
        (CODING_APPROVED, STATUS_READY_FOR_APPROVAL, CODING_REJECTED, ROOT_CAUSE_INTAKE),
    )
    completed_ts = str(completed_row[0]["created_at"]) if completed_row else None
    completed_text = _coding_activity_text(completed_row[0]) if completed_row else ""
    completed_week = _count_sql(
        db_path,
        """
        SELECT COUNT(*) AS n FROM pending_approval
        WHERE status = ?
          AND created_at >= ?
          AND diff IS NOT NULL
          AND TRIM(diff) != ''
        """,
        (CODING_APPROVED, week_iso),
    )
    errors_week = _count_sql(
        db_path,
        "SELECT COUNT(*) AS n FROM pending_approval WHERE status = ? AND created_at >= ?",
        (STATUS_AUTO_FIX_FAILED, week_iso),
    )
    durable_error = (
        _count_sql(
            db_path,
            "SELECT COUNT(*) AS n FROM pending_approval WHERE status = ?",
            (STATUS_AUTO_FIX_FAILED,),
        )
        > 0
    )
    stale_error = (
        _count_sql(
            db_path,
            "SELECT COUNT(*) AS n FROM pending_approval WHERE status = ? AND created_at < ?",
            (STATUS_AUTO_FIX_FAILED, today_iso),
        )
        > 0
    )
    approved = _count_sql(
        db_path,
        """
        SELECT COUNT(*) AS n FROM pending_approval
        WHERE status = ?
          AND created_at >= ?
          AND diff IS NOT NULL
          AND TRIM(diff) != ''
        """,
        (CODING_APPROVED, since_iso),
    )
    ready = _count_sql(
        db_path,
        "SELECT COUNT(*) AS n FROM pending_approval WHERE status = ? AND created_at >= ?",
        (STATUS_READY_FOR_APPROVAL, since_iso),
    )
    parts: list[str] = []
    if approved:
        parts.append(f"approved {approved} fix" + ("es" if approved != 1 else ""))
    if ready:
        parts.append(
            f"queued {ready} fix" + ("es" if ready != 1 else "") + " for approval"
        )
    return DomainSnapshot(
        last_ts=last_ts,
        last_text=last_text,
        completed_ts=completed_ts,
        completed_text=completed_text,
        completed_week=completed_week,
        errors_week=errors_week,
        durable_error=durable_error,
        stale_error=stale_error,
        briefing_line=_join_phrases(
            parts, "No new completed work since last session."
        ),
    )


def _marketing_activity_text(row: dict[str, Any]) -> str:
    platform = str(row.get("platform") or "draft")
    content_type = str(row.get("content_type") or "post")
    status = str(row.get("status") or "")
    fingerprint = str(row.get("source_fingerprint") or "")
    is_brief = fingerprint.startswith(BRIEF_FINGERPRINT_PREFIX)
    if is_brief:
        if status == MARKETING_APPROVED:
            return "approved an assigned brief"
        if status == STATUS_DRAFT_FAILED:
            return "draft generation failed"
        return "queued an assigned brief"
    if status == MARKETING_PUBLISHED:
        return f"published a {platform} {content_type}"
    if status == MARKETING_APPROVED:
        return f"approved a {platform} {content_type}"
    if status == STATUS_DRAFT_FAILED:
        return "draft generation failed"
    return f"queued a {platform} {content_type}"


def load_marketing_domain(
    db_path: Optional[Path | str],
    *,
    now: datetime,
    since_iso: str,
    week_iso: str,
    today_iso: str,
) -> DomainSnapshot:
    del now, today_iso
    brief_like = f"{BRIEF_FINGERPRINT_PREFIX}%"
    latest = _readonly_query(
        db_path,
        """
        SELECT created_at, status, platform, content_type, source_fingerprint
        FROM pending_approval
        ORDER BY created_at DESC, entry_id DESC
        LIMIT 1
        """,
    )
    last_ts = str(latest[0]["created_at"]) if latest else None
    last_text = _marketing_activity_text(latest[0]) if latest else ""
    finished = _readonly_query(
        db_path,
        """
        SELECT created_at, status, platform, content_type, source_fingerprint,
               COALESCE(published_at, created_at) AS action_ts
        FROM pending_approval
        WHERE status IN (?, ?)
          AND NOT (source_fingerprint LIKE ?)
        ORDER BY COALESCE(published_at, created_at) DESC, entry_id DESC
        LIMIT 1
        """,
        (MARKETING_PUBLISHED, MARKETING_APPROVED, brief_like),
    )
    if finished:
        row = finished[0]
        completed_ts = str(row.get("action_ts") or row["created_at"])
        completed_text = _marketing_activity_text(row)
    else:
        completed_ts = last_ts
        completed_text = last_text
    completed_week = _count_sql(
        db_path,
        """
        SELECT COUNT(*) AS n FROM pending_approval
        WHERE (status = ? AND COALESCE(published_at, created_at) >= ?)
           OR (
             status = ?
             AND created_at >= ?
             AND (source_fingerprint IS NULL OR source_fingerprint NOT LIKE ?)
           )
        """,
        (MARKETING_PUBLISHED, week_iso, MARKETING_APPROVED, week_iso, brief_like),
    )
    errors_week = _count_sql(
        db_path,
        "SELECT COUNT(*) AS n FROM pending_approval WHERE status = ? AND created_at >= ?",
        (STATUS_DRAFT_FAILED, week_iso),
    )
    published = _count_sql(
        db_path,
        """
        SELECT COUNT(*) AS n FROM pending_approval
        WHERE status = ? AND COALESCE(published_at, created_at) >= ?
        """,
        (MARKETING_PUBLISHED, since_iso),
    )
    approved = _count_sql(
        db_path,
        """
        SELECT COUNT(*) AS n FROM pending_approval
        WHERE status = ?
          AND created_at >= ?
          AND (source_fingerprint IS NULL OR source_fingerprint NOT LIKE ?)
        """,
        (MARKETING_APPROVED, since_iso, brief_like),
    )
    queued = _count_sql(
        db_path,
        "SELECT COUNT(*) AS n FROM pending_approval WHERE status = ? AND created_at >= ?",
        (MARKETING_PENDING, since_iso),
    )
    parts: list[str] = []
    if published:
        parts.append(f"published {published} draft" + ("s" if published != 1 else ""))
    if approved:
        parts.append(f"approved {approved} draft" + ("s" if approved != 1 else ""))
    if queued and not published and not approved:
        parts.append(f"queued {queued} draft" + ("s" if queued != 1 else ""))
    return DomainSnapshot(
        last_ts=last_ts,
        last_text=last_text,
        completed_ts=completed_ts,
        completed_text=completed_text,
        completed_week=completed_week,
        errors_week=errors_week,
        briefing_line=_join_phrases(
            parts, "No new completed work since last session."
        ),
    )


def load_competitor_domain(
    db_path: Optional[Path | str],
    *,
    now: datetime,
    since_iso: str,
    week_iso: str,
    today_iso: str,
) -> DomainSnapshot:
    del now, today_iso
    snapshots = _readonly_query(
        db_path,
        """
        SELECT competitor, snapshot_type, captured_at AS ts
        FROM competitor_snapshots
        ORDER BY captured_at DESC, rowid DESC
        LIMIT 1
        """,
    )
    signals = _readonly_query(
        db_path,
        """
        SELECT competitor, signal_type, summary, detected_at AS ts
        FROM competitor_signals
        ORDER BY detected_at DESC, signal_id DESC
        LIMIT 1
        """,
    )
    reports = _readonly_query(
        db_path,
        """
        SELECT created_at AS ts FROM weekly_reports
        ORDER BY created_at DESC, report_id DESC
        LIMIT 1
        """,
    )
    candidates: list[tuple[datetime, str, str]] = []
    if snapshots:
        row = snapshots[0]
        parsed = parse_iso(row.get("ts"))
        if parsed is not None:
            candidates.append(
                (
                    parsed,
                    str(row["ts"]),
                    f"captured a {row['snapshot_type']} snapshot for {row['competitor']}",
                )
            )
    if signals:
        row = signals[0]
        parsed = parse_iso(row.get("ts"))
        if parsed is not None:
            candidates.append(
                (
                    parsed,
                    str(row["ts"]),
                    f"detected {row['signal_type']} for {row['competitor']}",
                )
            )
    if reports:
        row = reports[0]
        parsed = parse_iso(row.get("ts"))
        if parsed is not None:
            candidates.append((parsed, str(row["ts"]), "wrote a weekly report"))
    last_ts = None
    last_text = ""
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        _, last_ts, last_text = candidates[0]
    n_signals_week = _count_sql(
        db_path,
        "SELECT COUNT(*) AS n FROM competitor_signals WHERE detected_at >= ?",
        (week_iso,),
    )
    n_reports_week = _count_sql(
        db_path,
        "SELECT COUNT(*) AS n FROM weekly_reports WHERE created_at >= ?",
        (week_iso,),
    )
    n_signals_since = _count_sql(
        db_path,
        "SELECT COUNT(*) AS n FROM competitor_signals WHERE detected_at >= ?",
        (since_iso,),
    )
    n_snaps_since = _count_sql(
        db_path,
        "SELECT COUNT(*) AS n FROM competitor_snapshots WHERE captured_at >= ?",
        (since_iso,),
    )
    n_reports_since = _count_sql(
        db_path,
        "SELECT COUNT(*) AS n FROM weekly_reports WHERE created_at >= ?",
        (since_iso,),
    )
    parts: list[str] = []
    if n_signals_since:
        parts.append(
            f"found {n_signals_since} signal" + ("s" if n_signals_since != 1 else "")
        )
    if n_snaps_since and not n_signals_since:
        parts.append(
            f"captured {n_snaps_since} snapshot"
            + ("s" if n_snaps_since != 1 else "")
        )
    if n_reports_since:
        parts.append(
            f"wrote {n_reports_since} weekly report"
            + ("s" if n_reports_since != 1 else "")
        )
    return DomainSnapshot(
        last_ts=last_ts,
        last_text=last_text,
        completed_ts=last_ts,
        completed_text=last_text,
        completed_week=n_signals_week + n_reports_week,
        briefing_line=_join_phrases(
            parts, "No new completed work since last session."
        ),
    )


def load_research_domain(
    db_path: Optional[Path | str],
    *,
    now: datetime,
    since_iso: str,
    week_iso: str,
    today_iso: str,
) -> DomainSnapshot:
    del now, today_iso
    latest_idea = _readonly_query(
        db_path,
        """
        SELECT detected_at, title, status
        FROM research_ideas
        ORDER BY detected_at DESC, idea_id DESC
        LIMIT 1
        """,
    )
    latest_poll = _readonly_query(
        db_path,
        """
        SELECT source_type, last_polled_at
        FROM research_poll_state
        ORDER BY last_polled_at DESC
        LIMIT 1
        """,
    )
    candidates: list[tuple[datetime, str, str]] = []
    if latest_idea:
        row = latest_idea[0]
        parsed = parse_iso(row.get("detected_at"))
        if parsed is not None:
            title = _clip(str(row.get("title") or "idea"), 72)
            candidates.append(
                (parsed, str(row["detected_at"]), f'logged "{title}"')
            )
    if latest_poll:
        row = latest_poll[0]
        parsed = parse_iso(row.get("last_polled_at"))
        if parsed is not None:
            candidates.append(
                (
                    parsed,
                    str(row["last_polled_at"]),
                    f"polled {row['source_type']}",
                )
            )
    last_ts = None
    last_text = ""
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        _, last_ts, last_text = candidates[0]
    completed_week = _count_sql(
        db_path,
        """
        SELECT COUNT(*) AS n FROM research_ideas
        WHERE status IN ('reviewed', 'prototyped') AND detected_at >= ?
        """,
        (week_iso,),
    )
    n_logged = _count_sql(
        db_path,
        "SELECT COUNT(*) AS n FROM research_ideas WHERE detected_at >= ?",
        (since_iso,),
    )
    n_reviewed = _count_sql(
        db_path,
        """
        SELECT COUNT(*) AS n FROM research_ideas
        WHERE status IN ('reviewed', 'prototyped') AND detected_at >= ?
        """,
        (since_iso,),
    )
    parts: list[str] = []
    if n_logged:
        parts.append(f"logged {n_logged} idea" + ("s" if n_logged != 1 else ""))
    if n_reviewed:
        parts.append(
            f"reviewed {n_reviewed}" + (" of them" if n_logged else " idea(s)")
        )
    return DomainSnapshot(
        last_ts=last_ts,
        last_text=last_text,
        completed_ts=last_ts,
        completed_text=last_text,
        completed_week=completed_week,
        briefing_line=_join_phrases(
            parts, "No new completed work since last session."
        ),
    )


def _domain_for_agent(
    spec: AgentSpec,
    paths: RosterDbPaths,
    *,
    now: datetime,
    since_iso: str,
    week_iso: str,
    today_iso: str,
) -> DomainSnapshot:
    loaders = {
        "coding": (load_coding_domain, paths.coding),
        "marketing": (load_marketing_domain, paths.marketing),
        "competitor": (load_competitor_domain, paths.competitor),
        "research": (load_research_domain, paths.research),
    }
    loader, db_path = loaders[spec.key]
    return loader(
        db_path,
        now=now,
        since_iso=since_iso,
        week_iso=week_iso,
        today_iso=today_iso,
    )


def _briefing_cutoff_iso(
    session: Optional[dict[str, Any]],
    now: datetime,
) -> str:
    today = start_of_utc_day(now)
    if session is None:
        return today.isoformat()
    started = parse_iso(session.get("started_at"))
    if started is None:
        return today.isoformat()
    return started.isoformat()


def _format_last_activity(
    domain: DomainSnapshot,
    now: datetime,
) -> str:
    if domain.last_ts and domain.last_text:
        return f"{relative_time(domain.last_ts, now)}: {domain.last_text}"
    return "No activity recorded yet"


def load_roster_cards(
    *,
    now: Optional[datetime] = None,
    paths: Optional[RosterDbPaths] = None,
    pending: Optional[dict[str, int]] = None,
    new_pending: Optional[dict[str, int]] = None,
    sessions: Optional[dict[str, Optional[dict[str, Any]]]] = None,
    runtimes: Optional[dict[str, RuntimeState]] = None,
    visit_cutoff: Optional[str] = None,
    pending_items: Optional[list[PendingItem]] = None,
) -> list[RosterCard]:
    now = now or datetime.now(timezone.utc)
    paths = paths or RosterDbPaths()
    if pending is not None and new_pending is None:
        new_pending = {spec.key: 0 for spec in AGENTS}
    elif pending is None or new_pending is None:
        items = pending_items if pending_items is not None else load_pending_approvals()
        if pending is None:
            pending = counts_from_pending(items)
        if new_pending is None:
            if visit_cutoff:
                new_pending = counts_from_pending(items, cutoff_iso=visit_cutoff)
            else:
                new_pending = {spec.key: 0 for spec in AGENTS}
    week_iso = week_start_utc(now).isoformat()
    today_iso = start_of_utc_day(now).isoformat()
    cards: list[RosterCard] = []
    for spec in AGENTS:
        if runtimes is not None:
            runtime = runtimes.get(spec.key) or RuntimeState()
        else:
            with _LOCK:
                runtime = RuntimeState(
                    status=_RUNTIME[spec.key].status,
                    last_run=_RUNTIME[spec.key].last_run,
                    error=_RUNTIME[spec.key].error,
                    session_id=_RUNTIME[spec.key].session_id,
                    summary=_RUNTIME[spec.key].summary,
                )
        if sessions is not None:
            session = sessions.get(spec.key)
        else:
            try:
                session = latest_session_for_agent(spec.agent_id)
            except Exception:
                session = None
        since_iso = visit_cutoff or _briefing_cutoff_iso(session, now)
        domain = _domain_for_agent(
            spec,
            paths,
            now=now,
            since_iso=since_iso,
            week_iso=week_iso,
            today_iso=today_iso,
        )
        backlog_n = pending.get(spec.key, 0)
        new_n = new_pending.get(spec.key, 0)
        open_session = bool(session and not session.get("ended_at"))
        started_at = str(session["started_at"]) if session and session.get("started_at") else None
        status = derive_roster_status(
            runtime_status=runtime.status,
            runtime_error=runtime.error,
            pending=new_n,
            durable_error=domain.durable_error,
            open_session=open_session,
            session_started_at=started_at,
            now=now,
        )
        stale_error = domain.stale_error
        if open_session and started_at and parse_iso(started_at) is not None:
            if parse_iso(started_at) < start_of_utc_day(now):
                stale_error = True
        if runtime.status == "failed" and runtime.last_run:
            last_fail = parse_iso(runtime.last_run)
            if last_fail is not None and last_fail < start_of_utc_day(now):
                stale_error = True
        cards.append(
            RosterCard(
                key=spec.key,
                name=spec.name,
                role=spec.role,
                assign_mode=spec.assign_mode,
                status=status,
                last_activity=_format_last_activity(domain, now),
                completed_week=domain.completed_week,
                new_pending=new_n,
                backlog=backlog_n,
                errors_week=domain.errors_week,
                briefing_line=compose_briefing_line(domain, now),
                stale_error=stale_error,
                error_detail=runtime.error,
                last_check=runtime.summary,
            )
        )
    return cards


def build_morning_briefing(
    cards: list[RosterCard],
) -> MorningBriefing:
    attention = tuple(
        f"{card.name} ({card.new_pending} new)"
        for card in cards
        if card.new_pending > 0
    )
    return MorningBriefing(
        lines=tuple(f"{card.name}: {card.briefing_line}" for card in cards),
        attention=attention,
        stuck=tuple(card.name for card in cards if card.stale_error),
        new_pending_total=sum(card.new_pending for card in cards),
        backlog_total=sum(card.backlog for card in cards),
    )


def assign_coding_task(
    text: str,
    *,
    db_path: Optional[Path | str] = None,
    source_root: Optional[Path | str] = None,
) -> str:
    body = (text or "").strip()
    if not body:
        raise ValueError("task is empty")
    from agents.coding_agent.assign_fix import failed_call_id_for_nodeids
    from agents.coding_agent.intake_fix import parse_pytest_nodeids

    nodeids = parse_pytest_nodeids(body)
    if nodeids:
        call_id = failed_call_id_for_nodeids(nodeids)
        store = CodingStore(db_path=db_path, enable_default_notifier=False)
        try:
            existing = store.get_by_failed_call_id(call_id)
            if existing is not None:
                return existing.entry_id
        finally:
            store.close()
        submit_assigned_fix(body, db_path=db_path, source_root=source_root)
        return f"assign:{call_id}"

    summary = body.splitlines()[0][:80]
    store = CodingStore(db_path=db_path, enable_default_notifier=False)
    try:
        entry = store.create_intake_task(
            task_ref=f"dashboard:{uuid.uuid4()}",
            summary=summary,
            request=body,
            payload={"source": "dashboard_roster"},
        )
        return entry.entry_id
    finally:
        store.close()


def assign_marketing_task(
    text: str,
    *,
    db_path: Optional[Path | str] = None,
) -> str:
    from agents.marketing_agent.draft_job import create_brief_task

    store = MarketingStore(db_path=db_path, enable_default_notifier=False)
    try:
        entry = create_brief_task(text, store=store)
        return entry.entry_id
    finally:
        store.close()


def assign_competitor_task(
    text: str,
    *,
    db_path: Optional[Path | str] = None,
    config_path: Optional[Path | str] = None,
) -> str:
    from agents.competitor_agent.assign_check import parse_directed_brief
    from agents.competitor_agent.settings import CompetitorConfig

    spec = CompetitorConfig.load(config_path) if config_path else CompetitorConfig.load()
    parsed = parse_directed_brief(text, spec)
    submit_competitor_check(text, db_path=db_path, config_path=config_path)
    return f"{parsed.competitor.name} {parsed.kind}"


def _in_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


def _render_roster_card(st: Any, card: RosterCard, *, key_prefix: str = "") -> None:
    with st.container(border=True):
        st.subheader(card.name)
        st.caption(card.role)
        st.markdown(f"{_roster_status_emoji(card.status)} **{card.status}**")
        st.caption(card.last_activity)
        metrics = st.columns(3)
        metrics[0].metric("Completed (week)", card.completed_week)
        with metrics[1]:
            st.metric("New since last visit", card.new_pending)
            st.caption(f"Total backlog: {card.backlog}")
        metrics[2].metric("Errors (week)", card.errors_week)
        if card.status == ROSTER_ERROR and card.error_detail:
            st.error(card.error_detail.splitlines()[0])
        if card.last_check:
            st.caption(card.last_check)
        if card.assign_mode == "none":
            st.text_input(
                "Assign a task",
                value="",
                disabled=True,
                key=f"{key_prefix}assign-disabled-{card.key}",
            )
            st.caption(ASSIGN_DISABLED_CAPTION)
            return
        with st.form(key=f"{key_prefix}assign-form-{card.key}", clear_on_submit=True):
            text = st.text_input("Assign a task")
            submitted = st.form_submit_button("Assign")
            if submitted:
                body = (text or "").strip()
                if not body:
                    st.warning("Enter a task first.")
                else:
                    try:
                        if card.assign_mode == "coding":
                            from agents.coding_agent.intake_fix import parse_pytest_nodeids

                            assign_coding_task(body)
                            if parse_pytest_nodeids(body):
                                st.success("Generating a fix from the assigned tests…")
                            else:
                                st.success("Queued as coding intake.")
                        elif card.assign_mode == "marketing":
                            assign_marketing_task(body)
                            st.success("Queued as a LinkedIn brief.")
                        elif card.assign_mode == "competitor":
                            label = assign_competitor_task(body)
                            st.success(f"Checking {label}…")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))


def render_roster_card(
    st: Any,
    card: RosterCard,
    *,
    key_prefix: str = "",
) -> None:
    """Public roster profile card used by dashboard and agent_manager."""
    _render_roster_card(st, card, key_prefix=key_prefix)


def _render_roster_tab(st: Any, cards: list[RosterCard], briefing: MorningBriefing) -> None:
    st.header("Morning Briefing")
    with st.container(border=True):
        for line in briefing.lines:
            st.markdown(f"- {line}")
        if briefing.attention:
            st.warning(
                "Needs your attention: "
                + ", ".join(briefing.attention)
                + " — open the **Control** tab to review."
            )
        else:
            st.caption("No new pending items since last visit.")
        if briefing.stuck:
            st.error("Stuck/erroring since before today: " + ", ".join(briefing.stuck))
        else:
            st.caption("No agent has been stuck or erroring since before today.")

    st.header("Team Roster")
    for pair in (cards[0:2], cards[2:4]):
        columns = st.columns(2)
        for column, card in zip(columns, pair):
            with column:
                _render_roster_card(st, card)


def render_pending_approvals(
    st: Any,
    *,
    agent_key: Optional[str] = None,
    key_prefix: str = "",
) -> None:
    """Pending-approval queue shared by the Control tab and Agent Manager."""
    pending = load_pending_approvals()
    if agent_key is not None:
        pending = [item for item in pending if item.agent_key == agent_key]
    if not pending:
        if agent_key is None:
            st.success("No pending_approval items across the four agents.")
        else:
            st.success("No pending_approval items for this agent.")
        return
    for item in pending:
        with st.container(border=True):
            st.markdown(f"**{item.agent_name}** · `{item.status}` · `{item.entry_id}`")
            st.caption(f"{item.title} · {item.created_at}")
            if item.preview:
                st.code(item.preview, language=None)
            actions = st.columns([1, 1, 6])
            with actions[0]:
                if st.button(
                    "Approve",
                    key=f"{key_prefix}approve-{item.store}-{item.entry_id}",
                ):
                    approve_entry(item)
                    st.rerun()
            with actions[1]:
                if st.button(
                    "Reject",
                    key=f"{key_prefix}reject-{item.store}-{item.entry_id}",
                ):
                    reject_entry(item)
                    st.rerun()


def _render_control_tab(st: Any) -> None:
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
    render_pending_approvals(st)


def render() -> None:
    import streamlit as st

    st.set_page_config(
        page_title="StreamCtx Agent Control Center",
        layout="wide",
        page_icon="🎛️",
    )
    heading, refresh = st.columns([12, 1])
    with heading:
        st.title("StreamCtx Agent Control Center")
    with refresh:
        st.markdown("<div style='height: 1.15rem'></div>", unsafe_allow_html=True)
        if st.button("🔄", help="Refresh", key="refresh-dashboard"):
            st.rerun()
    st.caption(
        "Run history from `~/.streamctx/sessions.db` (StreamCtx WAL storage). "
        "Pending approvals from the per-agent SQLite queues."
    )
    auto = st.checkbox(
        f"Auto-refresh every {REFRESH_SECONDS}s",
        value=False,
        key="auto_refresh",
    )

    now = datetime.now(timezone.utc)
    visit_cutoff = begin_dashboard_visit(now, st.session_state)
    roster_cards = load_roster_cards(now=now, visit_cutoff=visit_cutoff)
    briefing = build_morning_briefing(roster_cards)
    tab_roster, tab_control, tab_pipeline = st.tabs(
        ["Roster", "Control", "Content Pipeline"]
    )
    with tab_roster:
        _render_roster_tab(st, roster_cards, briefing)
    with tab_control:
        _render_control_tab(st)
    with tab_pipeline:
        from content_pipeline import render_pipeline_tab

        render_pipeline_tab(st)

    if auto or any_agent_running():
        time.sleep(REFRESH_SECONDS)
        st.rerun()


if _in_streamlit():
    render()
