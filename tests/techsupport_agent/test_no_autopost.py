"""Tech-support sources are GET-only — no GitHub comment or Discord send."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "agents" / "techsupport_agent"


def test_github_and_discord_modules_do_not_write():
    github = (ROOT / "github_issues.py").read_text(encoding="utf-8")
    discord = (ROOT / "discord.py").read_text(encoding="utf-8")
    assert "api.github.com/repos" in github
    assert "/issues" in github
    assert "/comments" not in github
    assert "method=\"POST\"" not in github
    assert "method='POST'" not in github
    assert "discord.com/api" in discord
    assert "create_message" not in discord
    assert "method=\"POST\"" not in discord
    # GET messages URL is allowed; POST to the same path is not.
    assert "Bot " in discord


def test_approve_docstring_forbids_posting():
    src = (ROOT / "techsupport_agent.py").read_text(encoding="utf-8")
    assert "Does not post to GitHub or Discord" in src
    assert "Never posts a GitHub comment" in src
    assert "Never sends a Discord message" in src
