"""CLI to revert git commits tied to approved coding-agent fixes."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Optional

from agents.coding_agent.pending_approval import PendingApprovalStore
from shared.config import BASE_DIR


def revert(
    entry_id: str,
    *,
    repo_root: Optional[Path | str] = None,
    approval_store: Optional[PendingApprovalStore] = None,
) -> str:
    """
    Look up the commit tied to ``entry_id`` and run a scoped ``git revert``.

    Returns the reverted commit SHA.
    """
    owns_store = approval_store is None
    store = approval_store or PendingApprovalStore()
    try:
        entry = store.get_entry(entry_id)
        if entry is None:
            raise KeyError(f"pending_approval entry not found: {entry_id}")
        if not entry.applied_commit:
            raise ValueError(
                f"Entry {entry_id} has no applied_commit recorded; cannot revert."
            )

        root = Path(repo_root or BASE_DIR).resolve()
        commit_sha = entry.applied_commit
        _run_git(["git", "revert", "--no-edit", commit_sha], cwd=root)
        return commit_sha
    finally:
        if owns_store:
            store.close()


def _run_git(command: list[str], *, cwd: Path) -> None:
    result = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        combined = (result.stderr or "") + (result.stdout or "")
        raise RuntimeError(combined.strip() or "git revert failed")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Revert a git commit associated with a coding-agent approval entry."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    revert_parser = subparsers.add_parser(
        "revert",
        help="Revert the commit tied to a pending_approval entry",
    )
    revert_parser.add_argument("entry_id", help="pending_approval entry_id")
    revert_parser.add_argument(
        "--repo-root",
        default=str(BASE_DIR),
        help="Git repository root (defaults to streamctx-agents base dir)",
    )

    args = parser.parse_args(argv)
    if args.command == "revert":
        commit_sha = revert(args.entry_id, repo_root=args.repo_root)
        print(f"Reverted commit {commit_sha} for entry {args.entry_id}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
