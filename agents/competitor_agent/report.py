"""Weekly competitive summary: signals + StreamCtx health + gap analysis.

Renders the markdown template, writes ``weekly_reports``, then pushes the
finished report through ``marketing_agent.notifications`` (the same
Slack/Telegram webhook marketing drafts use).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from agents.competitor_agent.health import HealthSnapshot, collect_health
from agents.competitor_agent.models import CompetitorSignal, WeeklyReport
from agents.competitor_agent.settings import CompetitorConfig
from agents.competitor_agent.storage import CompetitorStore
from agents.marketing_agent.notifications import notify_text

LlmFn = Callable[[str], str]
NowFn = Callable[[], datetime]
HealthFn = Callable[[str, str], HealthSnapshot]
NotifierFn = Callable[[str], None]

NO_CHANGES = "No changes detected"
LOOKBACK_DAYS = 7
GAP_FALLBACK = "No gap-analysis recommendation this week (LLM unavailable)."


def generate_weekly_report(
    store: CompetitorStore,
    *,
    config: Optional[CompetitorConfig] = None,
    now_fn: Optional[NowFn] = None,
    week_start: Optional[str] = None,
    llm_fn: Optional[LlmFn] = None,
    health_fn: Optional[HealthFn] = None,
    notifier: Optional[NotifierFn] = None,
    notify: bool = True,
    force: bool = False,
    session_storage=None,
    coding_db_path=None,
) -> WeeklyReport:
    """Build, persist, and notify one weekly report for the last 7 days."""
    spec = config or CompetitorConfig.load()
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    week, since, until = resolve_week_window(now, week_start=week_start)
    existing = store.get_report_for_week(week)
    if existing is not None and not force:
        return existing

    grouped = store.list_signals_grouped_by_competitor(since=since, until=until)
    health = _resolve_health(
        health_fn,
        since,
        until,
        session_storage=session_storage,
        coding_db_path=coding_db_path,
    )
    moves = render_competitor_moves(spec.names(), grouped)
    gap = generate_gap_analysis(
        spec.roadmap,
        grouped,
        names=spec.names(),
        llm_fn=llm_fn,
    )
    markdown = render_report(
        week_start=week,
        poison_summary=health.poison_summary,
        attribution_summary=health.attribution_summary,
        competitor_moves=moves,
        gap_analysis=gap,
    )
    report = store.insert_weekly_report(week_start=week, content_markdown=markdown)
    if notify:
        try:
            (notifier or notify_text)(report.content_markdown)
        except Exception:
            pass
    return report


def resolve_week_window(
    now: datetime,
    *,
    week_start: Optional[str] = None,
) -> tuple[str, str, str]:
    """Return ``(week_start, since_iso, until_iso)`` covering the past 7 days."""
    if week_start:
        start = datetime.strptime(week_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = start + timedelta(days=LOOKBACK_DAYS)
        if end > now:
            end = now
        return week_start, start.isoformat(), end.isoformat()

    start = now - timedelta(days=LOOKBACK_DAYS)
    week = start.date().isoformat()
    since = datetime(
        start.year, start.month, start.day, tzinfo=timezone.utc
    ).isoformat()
    return week, since, now.isoformat()


def render_competitor_moves(
    names: tuple[str, ...],
    grouped: dict[str, list[CompetitorSignal]],
) -> str:
    """One markdown line per config competitor — never hardcoded names."""
    lines: list[str] = []
    for name in names:
        signals = grouped.get(name) or []
        if not signals:
            lines.append(f"**{name}** — {NO_CHANGES}")
            continue
        summaries = "; ".join(_signal_line(item) for item in signals)
        lines.append(f"**{name}** — {summaries}")
    return "\n".join(lines)


def render_report(
    *,
    week_start: str,
    poison_summary: str,
    attribution_summary: str,
    competitor_moves: str,
    gap_analysis: str,
) -> str:
    return (
        f"## Week of {week_start}\n"
        "\n"
        "### StreamCtx Health\n"
        f"- Poison Detector: {poison_summary}\n"
        f"- Attribution Engine: {attribution_summary}\n"
        "\n"
        "### Competitor Moves\n"
        f"{competitor_moves}\n"
        "\n"
        "### Gap Analysis\n"
        f"{gap_analysis.rstrip()}\n"
    )


def generate_gap_analysis(
    roadmap: tuple[str, ...],
    grouped: dict[str, list[CompetitorSignal]],
    *,
    names: tuple[str, ...],
    llm_fn: Optional[LlmFn] = None,
) -> str:
    """One LLM call comparing this week's signals to the paid roadmap order."""
    prompt = _gap_prompt(roadmap, grouped, names)
    writer = llm_fn or _default_llm
    try:
        text = (writer(prompt) or "").strip()
    except Exception:
        text = ""
    return text or GAP_FALLBACK


def _gap_prompt(
    roadmap: tuple[str, ...],
    grouped: dict[str, list[CompetitorSignal]],
    names: tuple[str, ...],
) -> str:
    numbered = "\n".join(
        f"{index}. {item}" for index, item in enumerate(roadmap, start=1)
    ) or "(no roadmap items configured)"
    blocks: list[str] = []
    for name in names:
        signals = grouped.get(name) or []
        if not signals:
            blocks.append(f"{name}: {NO_CHANGES}")
            continue
        details = "; ".join(_signal_line(item) for item in signals)
        blocks.append(f"{name}: {details}")
    signal_text = "\n".join(blocks) if blocks else NO_CHANGES
    return (
        "You write a short Gap Analysis for StreamCtx, an open-source LLM "
        "agent observability SDK. Compare this week's competitor signals "
        "against the paid-feature roadmap (soonest first) and flag anything "
        "that suggests reprioritizing. Be factual. If nothing warrants a "
        "change, say so in 2-4 sentences. Do not invent competitor moves "
        "that are not listed.\n\n"
        f"Paid roadmap (build order):\n{numbered}\n\n"
        f"This week's competitor signals:\n{signal_text}"
    )


def _signal_line(signal: CompetitorSignal) -> str:
    summary = (signal.summary or "").strip() or signal.signal_type
    if signal.source_url:
        return f"{summary} ({signal.source_url})"
    return summary


def _resolve_health(
    health_fn: Optional[HealthFn],
    since: str,
    until: str,
    *,
    session_storage=None,
    coding_db_path=None,
) -> HealthSnapshot:
    if health_fn is not None:
        return health_fn(since, until)
    return collect_health(
        since=since,
        until=until,
        session_storage=session_storage,
        coding_db_path=coding_db_path,
    )


def _default_llm(prompt: str) -> str:
    from openai import OpenAI

    from shared.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL

    if not OPENROUTER_API_KEY:
        return ""
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate the weekly competitor report and notify via marketing webhook."
    )
    parser.add_argument("--config", help="Path to competitors.json")
    parser.add_argument(
        "--db",
        help="SQLite path (default: ~/.streamctx/competitor_agent.db)",
    )
    parser.add_argument(
        "--week",
        help="Week start YYYY-MM-DD (default: 7 days before now)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Insert a new report even if one already exists for the week",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Skip the marketing_agent webhook",
    )
    args = parser.parse_args(argv)
    spec = CompetitorConfig.load(args.config) if args.config else CompetitorConfig.load()
    store = CompetitorStore(db_path=args.db)
    try:
        report = generate_weekly_report(
            store,
            config=spec,
            week_start=args.week,
            force=args.force,
            notify=not args.no_notify,
        )
    finally:
        store.close()
    print(f"[competitor-agent] report={report.report_id} week={report.week_start}")
    print(report.content_markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
