"""GitHub Issues + Discord poll: persist, skip PRs, dedupe, read-only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.techsupport_agent.github_issues import parse_issues
from agents.techsupport_agent.models import SOURCE_DISCORD, SOURCE_GITHUB
from agents.techsupport_agent.poll import persist_items, run_poll
from agents.techsupport_agent.settings import TechSupportConfig
from agents.techsupport_agent.storage import TicketStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "techsupport"


@pytest.fixture
def store(tmp_path: Path):
    db = TicketStore(db_path=tmp_path / "support_tickets.db")
    yield db
    db.close()


@pytest.fixture
def config() -> TechSupportConfig:
    return TechSupportConfig(
        github_min_interval_seconds=0,
        discord_min_interval_seconds=0,
        github_repo="streamctx/streamctx-agents",
        discord_channel_id="123",
        poll_delay_seconds=0,
    )


def test_parse_issues_skips_pull_requests():
    payload = json.loads((FIXTURES / "github_issues.json").read_text(encoding="utf-8"))
    items = parse_issues(payload, repo="streamctx/streamctx-agents")
    refs = {item.source_ref for item in items}
    assert "42" in refs
    assert "43" in refs
    assert "44" in refs
    assert "45" not in refs


def test_run_poll_inserts_github_and_discord(store, config):
    github_payload = json.loads((FIXTURES / "github_issues.json").read_text(encoding="utf-8"))
    discord_payload = json.loads(
        (FIXTURES / "discord_messages.json").read_text(encoding="utf-8")
    )

    def fetch_github(_url: str):
        return github_payload

    def fetch_discord(_url: str):
        return discord_payload

    result = run_poll(
        store,
        config=config,
        fetch_github_fn=fetch_github,
        fetch_discord_fn=fetch_discord,
    )
    assert len(result.inserted) == 4  # 3 github issues + 1 discord (bots/empty skipped)
    sources = {ticket.source for ticket in result.inserted}
    assert SOURCE_GITHUB in sources
    assert SOURCE_DISCORD in sources
    again = run_poll(
        store,
        config=config,
        fetch_github_fn=fetch_github,
        fetch_discord_fn=fetch_discord,
    )
    assert again.inserted == ()
    assert len(again.skipped_duplicate) >= 4


def test_persist_items_dedupes_by_fingerprint(store):
    from agents.techsupport_agent.models import IncomingItem

    item = IncomingItem(
        source=SOURCE_GITHUB,
        source_ref="1",
        source_url="https://github.com/streamctx/streamctx-agents/issues/1",
        title="How do I run the pipeline?",
        body="how to",
        author="a",
        source_fingerprint="github:streamctx/streamctx-agents#1",
    )
    inserted, skipped = persist_items(store, [item])
    assert len(inserted) == 1
    assert skipped == ()
    again, skipped_again = persist_items(store, [item])
    assert again == ()
    assert skipped_again == (item.source_fingerprint,)
