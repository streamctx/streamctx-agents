"""StreamCtx Agent Manager — command center over the four existing agents.

Routing/UI layer only. Dispatches to dashboard + agent entry points that
already exist; it does not change coding/marketing/research/competitor
internals. Visual language matches the dark trace/checkpoint board in
``content_pipeline.py``.

Run:  streamlit run agent_manager.py
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from dashboard import (
    AGENT_BY_KEY,
    AGENTS,
    REFRESH_SECONDS,
    RosterCard,
    _in_streamlit,
    any_agent_running,
    assign_coding_task,
    assign_competitor_task,
    assign_marketing_task,
    begin_dashboard_visit,
    load_agent_statuses,
    load_roster_cards,
    render_pending_approvals,
    render_roster_card,
    submit_agent,
    submit_research_poll,
    submit_weekly_drafts,
)
from shared.audit_log import log_action
from shared.config import AGENT_IDS, COMPETITORS_TRACKED, STATUS_PENDING

UNCLASSIFIED = "unclassified"
VIEW_KEY = "am_view"
UNCLASSIFIED_KEY = "am_unclassified"
LAST_DISPATCH_KEY = "am_last_dispatch"
SESSION_PICK_KEY = "am_dogfood_session"

AGENT_KEYS = tuple(spec.key for spec in AGENTS)

# Phrase hits first; longer phrases are checked as substrings of lowercased text.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "coding": (
        "coding agent",
        "confidence gate",
        "fixvalidationloop",
        "fix validation",
        "auto-repair",
        "auto repair",
        "poison detector",
        "context diff",
        "counterfactual",
        "failing test",
        "pytest",
        "nodeid",
        "token_utils",
        "sandbox",
        "diagnose",
        "proposed patch",
        "intake",
    ),
    "marketing": (
        "marketing agent",
        "content pipeline",
        "weekly draft",
        "weekly-draft",
        "ready to post",
        "needs review",
        "indie hackers",
        "product hunt",
        "hacker news",
        "linkedin",
        "dev.to",
        "community post",
        "outreach",
        "twitter",
        "reddit",
    ),
    "research": (
        "research agent",
        "poll arxiv",
        "poll papers",
        "github topic",
        "arxiv",
        "research idea",
        "hype-filter",
        "gap-map",
        "research poll",
    ),
    "competitor": (
        "competitor agent",
        "github release",
        "pricing page",
        "snapshot",
        "changelog",
        "competitor",
        "langfuse",
        "langsmith",
        "helicone",
        "braintrust",
        "laminar",
        "latitude",
    ),
}

RUN_COMMANDS = frozenset(
    {
        "run",
        "start",
        "go",
        "poll",
        "run agent",
        "run pipeline",
        "start agent",
        "run now",
    }
)
WEEKLY_DRAFT_RE = re.compile(r"\bweekly\b.*\bdraft|\bdraft\b.*\bweekly\b", re.I)

ASSIGN_DISABLED_NOTE = (
    "Directed Assign/synthesis is disabled (BACKLOG.md): poll() is undirected "
    "discovery (arXiv + GitHub topics) and cannot answer questions like "
    "“what do other tools do for X.” Commands on this agent route to poll() only."
)


@dataclass(frozen=True)
class Classification:
    agent: str
    scores: dict[str, int]
    reason: str
    matched: tuple[str, ...] = ()


@dataclass(frozen=True)
class DispatchResult:
    agent: str
    action: str
    message: str
    entry_id: Optional[str] = None
    audit_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


MANAGER_CSS = """
<style>
.am-kicker {
  color: #f0883e;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  font-weight: 700;
  text-transform: uppercase;
  margin-bottom: 0.15rem;
}
.am-title {
  color: #e6edf3;
  font-size: 1.7rem;
  font-weight: 650;
  margin: 0 0 0.25rem 0;
  letter-spacing: -0.02em;
}
.am-sub {
  color: #8b949e;
  font-size: 0.9rem;
  margin-bottom: 0.85rem;
}
.am-tile {
  background: linear-gradient(180deg, #161b22 0%, #12161c 100%);
  border: 1px solid #30363d;
  border-left: 3px solid #58a6ff;
  border-radius: 10px;
  padding: 0.85rem 0.95rem 0.7rem 0.95rem;
  margin-bottom: 0.35rem;
  min-height: 8.5rem;
}
.am-tile-coding { border-left-color: #58a6ff; }
.am-tile-marketing { border-left-color: #3fb950; }
.am-tile-research { border-left-color: #a371f7; }
.am-tile-competitor { border-left-color: #d29922; }
.am-tile-name {
  color: #e6edf3;
  font-weight: 650;
  font-size: 1.05rem;
  margin-bottom: 0.2rem;
}
.am-tile-role { color: #8b949e; font-size: 0.82rem; margin-bottom: 0.55rem; }
.am-status {
  display: inline-block;
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-weight: 700;
  border-radius: 999px;
  padding: 0.12rem 0.55rem;
  border: 1px solid #30363d;
}
.am-idle { color: #8b949e; background: #21262d; }
.am-running { color: #58a6ff; background: #122033; border-color: #58a6ff55; }
.am-activity { color: #c9d1d9; font-size: 0.82rem; margin-top: 0.45rem; }
.am-muted { color: #8b949e; font-size: 0.78rem; }
.am-banner {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 10px;
  padding: 0.75rem 0.9rem;
  color: #c9d1d9;
  margin-bottom: 0.6rem;
}
.am-warn {
  background: #2a2111;
  border: 1px solid #d2992255;
  color: #e6edf3;
  border-radius: 10px;
  padding: 0.75rem 0.9rem;
  margin-bottom: 0.6rem;
}
div[class*="st-key-am-open-"] button {
  background: #161b22 !important;
  border: 1px solid #30363d !important;
  color: #e6edf3 !important;
  font-weight: 650 !important;
}
div[class*="st-key-am-open-coding"] button { border-top: 2px solid #58a6ff !important; }
div[class*="st-key-am-open-marketing"] button { border-top: 2px solid #3fb950 !important; }
div[class*="st-key-am-open-research"] button { border-top: 2px solid #a371f7 !important; }
div[class*="st-key-am-open-competitor"] button { border-top: 2px solid #d29922 !important; }
</style>
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def live_status(runtime_status: str) -> str:
    return "running" if runtime_status == "running" else "idle"


def _competitor_name_hit(text: str) -> Optional[str]:
    haystack = (text or "").lower()
    for name in sorted(COMPETITORS_TRACKED, key=len, reverse=True):
        if name.lower() in haystack:
            return name
    return None


def classify_command(text: str) -> Classification:
    """Keyword classifier. Ambiguous or empty input stays unclassified."""
    body = (text or "").strip()
    if not body:
        return Classification(UNCLASSIFIED, {key: 0 for key in AGENT_KEYS}, "empty")

    scores = {key: 0 for key in AGENT_KEYS}
    matched: list[str] = []
    lowered = body.lower()

    from agents.coding_agent.intake_fix import parse_pytest_nodeids

    if parse_pytest_nodeids(body):
        scores["coding"] += 10
        matched.append("pytest-nodeid")

    competitor = _competitor_name_hit(body)
    if competitor:
        scores["competitor"] += 5
        matched.append(f"competitor:{competitor}")

    for agent, phrases in KEYWORDS.items():
        for phrase in phrases:
            if phrase in lowered:
                scores[agent] += 1
                matched.append(f"{agent}:{phrase}")

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_agent, top = ranked[0]
    second = ranked[1][1]
    if top <= 0:
        return Classification(UNCLASSIFIED, scores, "no_keyword_match", tuple(matched))
    if top == second:
        return Classification(UNCLASSIFIED, scores, "ambiguous", tuple(matched))
    return Classification(top_agent, scores, "keyword", tuple(matched))


def _is_run_command(text: str) -> bool:
    stripped = (text or "").strip().lower()
    if stripped in RUN_COMMANDS:
        return True
    return bool(re.match(r"^(run|start)\s+(the\s+)?(agent|pipeline)\b", stripped))


def _is_weekly_draft_command(text: str) -> bool:
    return bool(WEEKLY_DRAFT_RE.search(text or ""))


def _audit_command(agent_key: str, text: str, action: str) -> Optional[str]:
    try:
        entry = log_action(
            agent_name=AGENT_IDS[agent_key],
            session_id="agent-manager",
            action_type="agent_manager_command",
            payload={
                "text": text,
                "agent": agent_key,
                "action": action,
                "source": "agent_manager",
            },
            status=STATUS_PENDING,
        )
        return str(entry.get("id") or "")
    except Exception:
        return None


def dispatch_command(agent_key: str, text: str) -> DispatchResult:
    """Send a command to an existing agent entry point. Does not guess agent."""
    if agent_key not in AGENT_BY_KEY:
        raise ValueError(f"unknown agent: {agent_key}")
    body = (text or "").strip()
    if agent_key == "coding":
        from agents.coding_agent.intake_fix import parse_pytest_nodeids

        if parse_pytest_nodeids(body):
            audit_id = _audit_command(agent_key, body, "assign_fix")
            entry_id = assign_coding_task(body)
            return DispatchResult(
                agent_key,
                "assign_fix",
                "Path A: ConfidenceGate.evaluate() + FixValidationLoop.run() "
                f"(assign_fix) queued as {entry_id}.",
                entry_id=entry_id,
                audit_id=audit_id,
            )
        if _is_run_command(body):
            audit_id = _audit_command(agent_key, body, "run")
            submit_agent("coding")
            return DispatchResult(
                agent_key,
                "run",
                "Started coding_agent.run() (detect → diagnose → gate → fix loop).",
                audit_id=audit_id,
            )
        audit_id = _audit_command(agent_key, body, "intake")
        entry_id = assign_coding_task(body)
        return DispatchResult(
            agent_key,
            "intake",
            f"Queued coding intake {entry_id} (approve to run intake_fix).",
            entry_id=entry_id,
            audit_id=audit_id,
        )

    if agent_key == "marketing":
        if _is_weekly_draft_command(body):
            audit_id = _audit_command(agent_key, body, "weekly_drafts")
            submit_weekly_drafts()
            return DispatchResult(
                agent_key,
                "weekly_drafts",
                "Started scripts.weekly_content_draft.run_weekly_drafts().",
                audit_id=audit_id,
            )
        if _is_run_command(body):
            audit_id = _audit_command(agent_key, body, "run")
            submit_agent("marketing")
            return DispatchResult(
                agent_key,
                "run",
                "Started marketing_agent.run() (outreach drafts → pending_approval).",
                audit_id=audit_id,
            )
        audit_id = _audit_command(agent_key, body, "brief")
        entry_id = assign_marketing_task(body)
        return DispatchResult(
            agent_key,
            "brief",
            f"Queued LinkedIn brief {entry_id}.",
            entry_id=entry_id,
            audit_id=audit_id,
        )

    if agent_key == "competitor":
        if _is_run_command(body) and _competitor_name_hit(body) is None:
            audit_id = _audit_command(agent_key, body, "run")
            submit_agent("competitor")
            return DispatchResult(
                agent_key,
                "run",
                "Started competitor_agent.run() (snapshot poll / Control Run).",
                audit_id=audit_id,
            )
        audit_id = _audit_command(agent_key, body, "assign_check")
        label = assign_competitor_task(body)
        return DispatchResult(
            agent_key,
            "assign_check",
            f"Assign v1 directed check started: {label}.",
            entry_id=label,
            audit_id=audit_id,
        )

    if agent_key == "research":
        audit_id = _audit_command(agent_key, body, "poll")
        submit_research_poll()
        return DispatchResult(
            agent_key,
            "poll",
            "Routed to research_agent.poll.run_poll() only — directed "
            "Assign/synthesis is disabled.",
            audit_id=audit_id,
        )

    raise ValueError(f"unsupported agent: {agent_key}")


def _inject_css(st: Any) -> None:
    st.markdown(MANAGER_CSS, unsafe_allow_html=True)


def _roster_by_key(cards: list[RosterCard]) -> dict[str, RosterCard]:
    return {card.key: card for card in cards}


def _status_by_key() -> dict[str, dict[str, Any]]:
    return {card["key"]: card for card in load_agent_statuses()}


def _tile_html(spec: Any, status: str, last_activity: str, pending: int) -> str:
    live = live_status(status)
    return (
        f'<div class="am-tile am-tile-{html.escape(spec.key)}">'
        f'<div class="am-tile-name">{html.escape(spec.name)}</div>'
        f'<div class="am-tile-role">{html.escape(spec.role)}</div>'
        f'<span class="am-status am-{live}">{html.escape(live)}</span>'
        f'<div class="am-activity">{html.escape(last_activity)}</div>'
        f'<div class="am-muted">pending_approval: {int(pending)}</div>'
        f"</div>"
    )


def _handle_dispatch(st: Any, agent_key: str, text: str) -> None:
    try:
        result = dispatch_command(agent_key, text)
    except Exception as exc:
        st.error(str(exc))
        return
    st.session_state[LAST_DISPATCH_KEY] = {
        "agent": result.agent,
        "action": result.action,
        "message": result.message,
        "entry_id": result.entry_id,
        "audit_id": result.audit_id,
        "text": text,
        "at": _now_iso(),
    }
    st.session_state[VIEW_KEY] = agent_key
    st.session_state.pop(UNCLASSIFIED_KEY, None)
    st.rerun()


def _render_command_bar(st: Any, *, scoped_agent: Optional[str] = None) -> None:
    label = (
        f"Command · {AGENT_BY_KEY[scoped_agent].name}"
        if scoped_agent
        else "Command"
    )
    with st.form("am-command" if scoped_agent is None else f"am-command-{scoped_agent}"):
        text = st.text_input(
            label,
            placeholder=(
                "e.g. fix tests/test_foo.py::test_bar  ·  poll arxiv  ·  "
                "Langfuse pricing  ·  draft a LinkedIn post"
                if scoped_agent is None
                else f"Command for {AGENT_BY_KEY[scoped_agent].name} only"
            ),
        )
        submitted = st.form_submit_button("Dispatch", use_container_width=True)
        if not submitted:
            return
        body = (text or "").strip()
        if not body:
            st.warning("Enter a command first.")
            return
        if scoped_agent:
            _handle_dispatch(st, scoped_agent, body)
            return
        classified = classify_command(body)
        if classified.agent == UNCLASSIFIED:
            st.session_state[UNCLASSIFIED_KEY] = body
            st.rerun()
            return
        _handle_dispatch(st, classified.agent, body)


def _render_unclassified(st: Any) -> None:
    pending = st.session_state.get(UNCLASSIFIED_KEY)
    if not pending:
        return
    st.markdown(
        f'<div class="am-warn"><strong>Unclassified</strong> — this command did not '
        f"clearly match one agent, so it was not dispatched. Pick an agent "
        f"or edit the command.<br/><code>{html.escape(str(pending))}</code></div>",
        unsafe_allow_html=True,
    )
    cols = st.columns(len(AGENTS) + 1)
    for column, spec in zip(cols, AGENTS):
        with column:
            if st.button(
                spec.name.replace(" Agent", ""),
                key=f"am-pick-{spec.key}",
                use_container_width=True,
            ):
                _handle_dispatch(st, spec.key, str(pending))
    with cols[-1]:
        if st.button("Dismiss", key="am-pick-dismiss", use_container_width=True):
            st.session_state.pop(UNCLASSIFIED_KEY, None)
            st.rerun()


def _render_last_dispatch(st: Any) -> None:
    last = st.session_state.get(LAST_DISPATCH_KEY)
    if not last:
        return
    st.markdown(
        f'<div class="am-banner">Last dispatch · <code>{html.escape(str(last.get("agent")))}</code> '
        f'· <code>{html.escape(str(last.get("action")))}</code> · '
        f'{html.escape(str(last.get("message")))}</div>',
        unsafe_allow_html=True,
    )


def _render_tiles(
    st: Any,
    roster: dict[str, RosterCard],
    statuses: dict[str, dict[str, Any]],
) -> None:
    pairs = (AGENTS[0:2], AGENTS[2:4])
    for pair in pairs:
        columns = st.columns(2)
        for column, spec in zip(columns, pair):
            card = roster.get(spec.key)
            status = statuses.get(spec.key) or {}
            last_activity = card.last_activity if card else "No activity recorded yet"
            pending = int(status.get("pending") or 0)
            runtime = str(status.get("status") or "idle")
            with column:
                st.markdown(
                    _tile_html(spec, runtime, last_activity, pending),
                    unsafe_allow_html=True,
                )
                if st.button(
                    f"Open {spec.name}",
                    key=f"am-open-{spec.key}",
                    use_container_width=True,
                ):
                    st.session_state[VIEW_KEY] = spec.key
                    st.rerun()


def _render_scoped_command(st: Any, agent_key: str) -> None:
    st.subheader("Agent command")
    if agent_key == "research":
        st.caption(ASSIGN_DISABLED_NOTE)
    _render_command_bar(st, scoped_agent=agent_key)


def _coding_entry_extras(entry_id: str) -> Optional[dict[str, Any]]:
    from agents.coding_agent.pending_approval import PendingApprovalStore

    store = PendingApprovalStore(enable_default_notifier=False)
    try:
        entry = store.get_entry(entry_id)
        if entry is None:
            return None
        return {
            "confidence": entry.confidence,
            "verification_mode": entry.verification_mode,
            "auto_applied": entry.auto_applied,
            "root_cause": entry.root_cause,
            "retries_used": entry.retries_used,
        }
    finally:
        store.close()


def _render_coding_fix_flow(st: Any) -> None:
    st.subheader("Retry-loop fix flow")
    st.caption(
        "Existing Path A: ``ConfidenceGate.evaluate()`` then "
        "``FixValidationLoop.run()``, including the ``assign_fix.py`` nodeid path. "
        "Free-text without nodeids still becomes coding intake."
    )
    cols = st.columns(2)
    with cols[0]:
        if st.button("Run coding_agent pipeline", key="am-coding-run"):
            submit_agent("coding")
            st.rerun()
    with cols[1]:
        st.caption("Detect → diagnose → confidence gate → sandbox retries.")


def _list_dogfood_sessions(limit: int = 12) -> list[int]:
    from agents.coding_agent.failure_detector import poll_failed_sessions

    try:
        sessions = poll_failed_sessions()
    except Exception:
        sessions = []
    if sessions:
        return list(sessions)[:limit]
    try:
        latest = None
        from dashboard import latest_session_for_agent

        latest = latest_session_for_agent(AGENT_IDS["coding"])
        if latest and latest.get("id"):
            return [int(latest["id"])]
    except Exception:
        pass
    return []


def _checkpoint_messages(session_id: int, step: Optional[int] = None) -> list[dict[str, Any]]:
    from streamctx import list_checkpoints, replay as replay_fn

    sid = int(session_id)
    if step is None:
        checkpoints = list_checkpoints(sid)
        if not checkpoints:
            return []
        step = int(checkpoints[-1]["step_number"])
    result = replay_fn(sid, from_step=int(step), dry_run=True)
    messages = getattr(result, "original_messages", None)
    return list(messages or [])


def _render_dogfood_tools(st: Any) -> None:
    st.subheader("Dogfooded StreamCtx features")
    st.caption(
        "These call the same SDK surfaces already verified in this repo: "
        "``PoisonDetector``, ``ContextDiffer``, ``replay(..., dry_run=True)``, "
        "and ``compress_messages``."
    )
    sessions = _list_dogfood_sessions()
    default = sessions[0] if sessions else 1
    session_id = int(
        st.number_input(
            "Session id",
            min_value=1,
            value=int(st.session_state.get(SESSION_PICK_KEY, default)),
            key=SESSION_PICK_KEY,
        )
    )
    if sessions:
        st.caption("Failed-call sessions: " + ", ".join(str(item) for item in sessions[:8]))

    poison, diff, replay_tab, compress = st.tabs(
        ["Poison Detector", "Context Diff", "Counterfactual Replay", "Context Compression"]
    )
    with poison:
        if st.button("Scan session", key="am-poison-run"):
            from streamctx import PoisonDetector

            messages = _checkpoint_messages(session_id)
            result = PoisonDetector().scan(messages)
            st.json(result)
            if not messages:
                st.info("No checkpoint messages for that session.")
    with diff:
        steps = st.columns(2)
        step_a = int(steps[0].number_input("From step", min_value=0, value=0, key="am-diff-a"))
        step_b = int(steps[1].number_input("To step", min_value=0, value=1, key="am-diff-b"))
        if st.button("Diff checkpoints", key="am-diff-run"):
            from streamctx import ContextDiffer

            messages_a = _checkpoint_messages(session_id, step_a)
            messages_b = _checkpoint_messages(session_id, step_b)
            result = ContextDiffer().diff(
                messages_a, messages_b, step_a=step_a, step_b=step_b
            )
            st.write(result.get("summary") or "")
            st.json(
                {
                    "drift_score": result.get("drift_score"),
                    "token_delta": result.get("token_delta"),
                    "added": len(result.get("added") or []),
                    "removed": len(result.get("removed") or []),
                    "warnings": result.get("warnings"),
                }
            )
    with replay_tab:
        from_step = int(
            st.number_input("Replay from step", min_value=0, value=0, key="am-replay-step")
        )
        if st.button("Dry-run replay", key="am-replay-run"):
            from streamctx import replay as replay_fn

            result = replay_fn(session_id, from_step=from_step, dry_run=True)
            st.caption("dry_run=True — no LLM calls.")
            st.json(
                {
                    "session_id": getattr(result, "session_id", session_id),
                    "from_step": getattr(result, "from_step", from_step),
                    "original_messages": len(getattr(result, "original_messages", []) or []),
                    "counterfactual_messages": len(
                        getattr(result, "counterfactual_messages", []) or []
                    ),
                    "dry_run": True,
                }
            )
    with compress:
        if st.button("Compress session context", key="am-compress-run"):
            from streamctx import compress_messages

            messages = _checkpoint_messages(session_id)
            compressed, original_tokens, compressed_tokens = compress_messages(messages)
            st.metric("Original tokens", original_tokens)
            st.metric("Compressed tokens", compressed_tokens)
            st.caption(f"{len(messages)} → {len(compressed)} messages")
            st.json(compressed[:8])


def _render_coding_view(st: Any, card: RosterCard) -> None:
    render_roster_card(st, card, key_prefix="am-")
    _render_scoped_command(st, "coding")
    _render_coding_fix_flow(st)
    st.subheader("Pending approvals")
    render_pending_approvals(st, agent_key="coding", key_prefix="am-coding-")
    from dashboard import load_pending_approvals

    for item in load_pending_approvals():
        if item.agent_key != "coding":
            continue
        extras = _coding_entry_extras(item.entry_id)
        if extras:
            st.caption(
                f"{item.entry_id}: confidence={extras['confidence']:.2f} · "
                f"verification={extras['verification_mode']} · "
                f"auto_applied={extras['auto_applied']} · "
                f"retries={extras['retries_used']}"
            )
    _render_dogfood_tools(st)


def _render_marketing_view(st: Any, card: RosterCard) -> None:
    from content_pipeline import render_pipeline_tab

    render_roster_card(st, card, key_prefix="am-")
    _render_scoped_command(st, "marketing")
    st.subheader("Weekly content drafts")
    st.caption("Triggers ``scripts/weekly_content_draft.py`` against the existing pipeline store.")
    opts = st.columns(3)
    force = opts[0].checkbox("Force", value=False, key="am-weekly-force")
    dry_run = opts[1].checkbox("Dry run", value=False, key="am-weekly-dry")
    if opts[2].button("Run weekly drafts", key="am-weekly-run"):
        submit_weekly_drafts(force=force, dry_run=dry_run)
        st.rerun()
    render_pipeline_tab(st)


def _render_research_poll_results(st: Any) -> None:
    from agents.research_agent.settings import default_config
    from agents.research_agent.storage import ResearchStore
    from agents.research_agent.models import SOURCE_TYPE_ARXIV, SOURCE_TYPE_GITHUB

    spec = default_config()
    store = ResearchStore()
    try:
        arxiv_at = store.last_polled_at(SOURCE_TYPE_ARXIV)
        github_at = store.last_polled_at(SOURCE_TYPE_GITHUB)
        ideas = store.list_ideas(limit=40)
    finally:
        store.close()

    st.markdown(
        f'<div class="am-warn">{html.escape(ASSIGN_DISABLED_NOTE)}</div>',
        unsafe_allow_html=True,
    )
    metrics = st.columns(3)
    metrics[0].metric("Ideas", len(ideas))
    metrics[1].caption(f"arXiv last poll: `{arxiv_at or 'never'}`")
    metrics[2].caption(f"GitHub last poll: `{github_at or 'never'}`")
    st.caption(
        "Relevance: arXiv keywords "
        + ", ".join(spec.arxiv_keywords[:6])
        + " · GitHub topics "
        + ", ".join(spec.github_topics[:6])
    )
    actions = st.columns(3)
    if actions[0].button("Poll arXiv + GitHub", key="am-research-poll"):
        submit_research_poll()
        st.rerun()
    if actions[1].button("Poll arXiv only", key="am-research-arxiv"):
        submit_research_poll(github=False)
        st.rerun()
    if actions[2].button("Poll GitHub only", key="am-research-github"):
        submit_research_poll(arxiv=False)
        st.rerun()

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
            st.markdown(f"**{idea.title}**")
            st.caption(
                f"{idea.source_type} · {idea.status} · score={score} · "
                f"hype={idea.hype_label or 'unfiltered'} · "
                f"{idea.classification or 'unclassified'} · {idea.detected_at}"
            )
            if idea.content_excerpt:
                st.write(idea.content_excerpt[:400])
            st.markdown(f"[{idea.source_url}]({idea.source_url})")


def _render_research_view(st: Any, card: RosterCard) -> None:
    render_roster_card(st, card, key_prefix="am-")
    _render_scoped_command(st, "research")
    st.subheader("poll() results")
    _render_research_poll_results(st)


def _hash_pairs(snapshots: list[Any]) -> list[dict[str, Any]]:
    """Show existing snapshot-hash dedup comparisons, newest pair first."""
    by_type: dict[str, list[Any]] = {}
    for snap in snapshots:
        by_type.setdefault(snap.snapshot_type, []).append(snap)
    rows: list[dict[str, Any]] = []
    for snapshot_type, items in by_type.items():
        ordered = sorted(items, key=lambda item: item.captured_at, reverse=True)
        for newer, older in zip(ordered, ordered[1:]):
            same = newer.content_hash == older.content_hash
            rows.append(
                {
                    "snapshot_type": snapshot_type,
                    "newer_at": newer.captured_at,
                    "older_at": older.captured_at,
                    "newer_hash": newer.content_hash[:12],
                    "older_hash": older.content_hash[:12],
                    "result": "unchanged" if same else "changed",
                }
            )
    return rows


def _render_competitor_dedup(st: Any) -> None:
    from agents.competitor_agent.storage import CompetitorStore

    st.subheader("Dedup verification · Langfuse comparison")
    st.caption(
        "Reads ``competitor_snapshots`` hashes written by the existing "
        "``snapshot_and_diff`` / Assign v1 path. Same-hash rows are the "
        "dedup skip; hash changes are the verified diff."
    )
    store = CompetitorStore()
    try:
        langfuse = store.list_snapshots(competitor="Langfuse", limit=40)
        signals = store.list_signals(competitor="Langfuse")
        others = store.list_snapshots(limit=20)
    finally:
        store.close()

    pairs = _hash_pairs(langfuse)
    if not pairs:
        st.info("No Langfuse snapshot pairs yet. Run Assign v1 or Control Run.")
    for row in pairs[:12]:
        with st.container(border=True):
            st.markdown(
                f"**Langfuse {row['snapshot_type']}** · `{row['result']}`"
            )
            st.caption(
                f"{row['older_at']} `{row['older_hash']}` → "
                f"{row['newer_at']} `{row['newer_hash']}`"
            )

    if signals:
        st.markdown("**Langfuse signals**")
        for signal in signals[:8]:
            st.caption(
                f"{signal.detected_at} · {signal.signal_type} · {signal.summary}"
            )

    if others:
        st.markdown("**Latest snapshots (all competitors)**")
        for snap in others[:8]:
            st.caption(
                f"{snap.captured_at} · {snap.competitor} {snap.snapshot_type} "
                f"`{snap.content_hash[:12]}`"
            )


def _render_competitor_view(st: Any, card: RosterCard) -> None:
    render_roster_card(st, card, key_prefix="am-")
    _render_scoped_command(st, "competitor")
    st.subheader("Assign v1")
    st.caption(
        "Forced one-shot version of Control Run: ``assign_check.run_directed_check`` "
        "(interval bypass, single competitor + snapshot kind)."
    )
    if st.button("Run full snapshot poll", key="am-competitor-run"):
        submit_agent("competitor")
        st.rerun()
    _render_competitor_dedup(st)
    st.subheader("Pending approvals")
    render_pending_approvals(st, agent_key="competitor", key_prefix="am-comp-")


def _render_agent_view(
    st: Any,
    agent_key: str,
    roster: dict[str, RosterCard],
) -> None:
    spec = AGENT_BY_KEY[agent_key]
    back, title = st.columns([1, 8])
    with back:
        if st.button("← All agents", key="am-back"):
            st.session_state[VIEW_KEY] = None
            st.rerun()
    with title:
        st.markdown(
            f'<div class="am-kicker">Trace · Checkpoint · {html.escape(spec.key)}</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="am-title">{html.escape(spec.name)}</div>',
            unsafe_allow_html=True,
        )
    card = roster.get(agent_key)
    if card is None:
        st.error("Roster card missing for this agent.")
        return
    if agent_key == "coding":
        _render_coding_view(st, card)
        return
    if agent_key == "marketing":
        _render_marketing_view(st, card)
        return
    if agent_key == "research":
        _render_research_view(st, card)
        return
    if agent_key == "competitor":
        _render_competitor_view(st, card)


def render() -> None:
    import streamlit as st

    st.set_page_config(
        page_title="StreamCtx Agent Manager",
        layout="wide",
        page_icon="🎛️",
    )
    _inject_css(st)
    heading, refresh = st.columns([12, 1])
    with heading:
        st.markdown(
            '<div class="am-kicker">Trace · Checkpoint</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="am-title">Agent Manager</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="am-sub">Command bar routes to the four existing agents. '
            "Tiles open each agent’s real feature set — not a generic log.</div>",
            unsafe_allow_html=True,
        )
    with refresh:
        st.markdown("<div style='height: 1.15rem'></div>", unsafe_allow_html=True)
        if st.button("🔄", help="Refresh", key="am-refresh"):
            st.rerun()

    auto = st.checkbox(
        f"Auto-refresh every {REFRESH_SECONDS}s",
        value=False,
        key="am_auto_refresh",
    )
    now = datetime.now(timezone.utc)
    visit_cutoff = begin_dashboard_visit(now, st.session_state)
    roster_cards = load_roster_cards(now=now, visit_cutoff=visit_cutoff)
    roster = _roster_by_key(roster_cards)
    statuses = _status_by_key()

    view = st.session_state.get(VIEW_KEY)
    _render_command_bar(st)
    _render_unclassified(st)
    _render_last_dispatch(st)

    if view in AGENT_BY_KEY:
        _render_agent_view(st, view, roster)
    else:
        _render_tiles(st, roster, statuses)

    if auto or any_agent_running():
        time.sleep(REFRESH_SECONDS)
        st.rerun()


if _in_streamlit():
    render()
