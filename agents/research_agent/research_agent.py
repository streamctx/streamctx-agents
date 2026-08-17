"""
Research Agent CLI — poll, hype-filter, then gap-map unlabeled substance.

Usage::

    python -m agents.research_agent.research_agent poll
    python -m agents.research_agent.research_agent poll --arxiv-only
    python -m agents.research_agent.research_agent poll --github-only
    python -m agents.research_agent.research_agent hype-filter
    python -m agents.research_agent.research_agent hype-filter --limit 10
    python -m agents.research_agent.research_agent gap-map
    python -m agents.research_agent.research_agent gap-map --limit 10
"""

from __future__ import annotations

import argparse
import sys

from streamctx import get_tracker

from agents.research_agent.gap import run_gap_map
from agents.research_agent.gap import summarize as summarize_gap
from agents.research_agent.hype import run_hype_filter
from agents.research_agent.hype import summarize as summarize_hype
from agents.research_agent.poll import run_poll, summarize
from agents.research_agent.settings import default_config
from agents.research_agent.storage import ResearchStore
from shared.config import AGENT_IDS


def cmd_poll(args: argparse.Namespace) -> int:
    tracker = get_tracker(AGENT_IDS["research"])
    tracker.start()
    try:
        tracker.checkpoint()
        store = ResearchStore(db_path=args.db)
        config = default_config()
        try:
            result = run_poll(
                store,
                config=config,
                arxiv=not args.github_only,
                github=not args.arxiv_only,
            )
        finally:
            store.close()
        tracker.checkpoint()
        print(f"[research-agent] {summarize(result)}")
        for idea in result.inserted:
            print(f"  + {idea.source_type} {idea.title} ({idea.source_url})")
        for source, error in result.errors:
            print(f"  ! {source}: {error}", file=sys.stderr)
        return 1 if result.errors else 0
    finally:
        tracker.stop()


def cmd_hype_filter(args: argparse.Namespace) -> int:
    tracker = get_tracker(AGENT_IDS["research"])
    tracker.start()
    try:
        tracker.checkpoint()
        store = ResearchStore(db_path=args.db)
        try:
            result = run_hype_filter(store, limit=args.limit)
        finally:
            store.close()
        tracker.checkpoint()
        print(f"[research-agent] hype-filter {summarize_hype(result)}")
        for idea in result.kept:
            print(f"  keep {idea.title}")
        for idea in result.discarded:
            print(f"  discard {idea.title}")
        for idea_id, error in result.errors:
            print(f"  ! {idea_id}: {error}", file=sys.stderr)
        return 1 if result.errors else 0
    finally:
        tracker.stop()


def cmd_gap_map(args: argparse.Namespace) -> int:
    tracker = get_tracker(AGENT_IDS["research"])
    tracker.start()
    try:
        tracker.checkpoint()
        store = ResearchStore(db_path=args.db)
        try:
            result = run_gap_map(store, limit=args.limit)
        finally:
            store.close()
        tracker.checkpoint()
        print(f"[research-agent] gap-map {summarize_gap(result)}")
        for idea in result.mapped:
            print(
                f"  {idea.composite_score:.2f} {idea.title} "
                f"(pain={idea.pain_match_score} feas={idea.feasibility_score} "
                f"nov={idea.novelty_score})"
            )
        for idea_id, error in result.errors:
            print(f"  ! {idea_id}: {error}", file=sys.stderr)
        return 1 if result.errors else 0
    finally:
        tracker.stop()


def _add_db_limit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: ~/.streamctx/research_agent.db)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max items to process this run",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="StreamCtx research agent — poll papers and topic-tagged repos.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    poll_parser = sub.add_parser("poll", help="Fetch arXiv + GitHub into research_ideas")
    poll_parser.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: ~/.streamctx/research_agent.db)",
    )
    exclusive = poll_parser.add_mutually_exclusive_group()
    exclusive.add_argument("--arxiv-only", action="store_true")
    exclusive.add_argument("--github-only", action="store_true")
    poll_parser.set_defaults(func=cmd_poll)

    hype_parser = sub.add_parser(
        "hype-filter",
        help="Classify unlabeled ideas as technical_substance vs marketing_hype",
    )
    _add_db_limit(hype_parser)
    hype_parser.set_defaults(func=cmd_hype_filter)

    gap_parser = sub.add_parser(
        "gap-map",
        help="Score hype-filtered ideas against StreamCtx feature gaps",
    )
    _add_db_limit(gap_parser)
    gap_parser.set_defaults(func=cmd_gap_map)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
