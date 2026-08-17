"""
Research Agent CLI — Stage 1: poll arXiv + GitHub into research_ideas.

Usage::

    python -m agents.research_agent.research_agent poll
    python -m agents.research_agent.research_agent poll --arxiv-only
    python -m agents.research_agent.research_agent poll --github-only
"""

from __future__ import annotations

import argparse
import sys

from streamctx import get_tracker

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

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
