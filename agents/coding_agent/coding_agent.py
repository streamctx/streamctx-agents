"""
Coding Agent CLI — runs the full six-stage pipeline or human follow-up actions.

Usage::

    python -m agents.coding_agent.coding_agent run [--limit N]
    python -m agents.coding_agent.coding_agent approve <entry_id> [--commit SHA]
    python -m agents.coding_agent.coding_agent reject <entry_id> --reason "..."
    python -m agents.coding_agent.coding_agent revert <entry_id>
"""

from __future__ import annotations

import argparse
import sys

from streamctx import get_tracker

from agents.coding_agent.pipeline import CodingAgentPipeline
from agents.coding_agent.models import PipelineRunResult
from shared.config import AGENT_IDS, BASE_DIR


def run(
    *,
    source_root: str | None = None,
    limit: int | None = None,
    enable_notifications: bool = True,
    storage=None,
    db_path=None,
    fix_generator=None,
) -> PipelineRunResult:
    """Run the detect→diagnose→fix pipeline. Tracker is owned by the caller."""
    with CodingAgentPipeline(
        source_root=source_root or BASE_DIR,
        enable_notifications=enable_notifications,
        storage=storage,
        db_path=db_path,
        fix_generator=fix_generator,
    ) as pipeline:
        result = pipeline.run(limit=limit)
        print(f"[coding-agent] {pipeline.summarize(result)}")
        for item in result.items:
            entry = item.fix.pending_entry or item.gate.pending_entry
            if entry is None:
                continue
            print(
                f"  session={entry.session_id} status={entry.status} "
                f"id={entry.entry_id}"
            )
        return result


def cmd_run(args: argparse.Namespace) -> int:
    tracker = get_tracker(AGENT_IDS["coding"])
    tracker.start()
    try:
        run(
            source_root=args.source_root,
            limit=args.limit,
            enable_notifications=not args.no_notify,
        )
    finally:
        tracker.stop()
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    with CodingAgentPipeline(source_root=args.source_root) as pipeline:
        pattern = pipeline.approve(
            args.entry_id,
            applied_commit=args.commit,
        )
    print(
        f"[coding-agent] Approved {args.entry_id} "
        f"(pattern success_count={pattern.success_count})"
    )
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    with CodingAgentPipeline(source_root=args.source_root) as pipeline:
        pattern = pipeline.reject(args.entry_id, args.reason)
    print(
        f"[coding-agent] Rejected {args.entry_id} "
        f"(pattern reject_count={pattern.reject_count})"
    )
    return 0


def cmd_revert(args: argparse.Namespace) -> int:
    with CodingAgentPipeline(source_root=args.source_root) as pipeline:
        commit_sha = pipeline.revert_fix(args.entry_id, repo_root=args.repo_root)
    print(f"[coding-agent] Reverted commit {commit_sha} for entry {args.entry_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="StreamCtx coding agent — autonomous detect, diagnose, fix, review.",
    )
    parser.add_argument(
        "--source-root",
        default=BASE_DIR,
        help="Repository root for sandbox validation and git revert",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the full detect→fix pipeline")
    run_parser.add_argument("--limit", type=int, default=None)
    run_parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Disable webhook notifications for this run",
    )
    run_parser.set_defaults(func=cmd_run)

    approve_parser = subparsers.add_parser("approve", help="Approve a pending fix")
    approve_parser.add_argument("entry_id")
    approve_parser.add_argument(
        "--commit",
        default=None,
        help="Git commit SHA to associate with this approval (for revert)",
    )
    approve_parser.set_defaults(func=cmd_approve)

    reject_parser = subparsers.add_parser("reject", help="Reject a pending fix")
    reject_parser.add_argument("entry_id")
    reject_parser.add_argument("--reason", required=True)
    reject_parser.set_defaults(func=cmd_reject)

    revert_parser = subparsers.add_parser("revert", help="Revert an approved fix commit")
    revert_parser.add_argument("entry_id")
    revert_parser.add_argument(
        "--repo-root",
        default=None,
        help="Git repo root (defaults to --source-root)",
    )
    revert_parser.set_defaults(func=cmd_revert)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
