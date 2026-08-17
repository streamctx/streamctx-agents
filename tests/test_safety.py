"""Unit tests for Stage 4 safety rules (self-promo + duplicate stagger)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents.marketing_agent.adapters.hn import HackerNewsAdapter
from agents.marketing_agent.adapters.linkedin import LinkedInAdapter
from agents.marketing_agent.adapters.twitter import TwitterAdapter, TwitterCredentials
from agents.marketing_agent.models import Story
from agents.marketing_agent.pending_approval import PendingApprovalStore
from agents.marketing_agent.safety import (
    DuplicateContentError,
    SafetyGate,
    SafetyRules,
    SelfPromoError,
    review_self_promo,
    rewrite_self_promo,
    story_fingerprint,
)
from agents.marketing_agent.sources import parse_changelog
from agents.marketing_agent.story import generate_story

FIXTURE_CHANGELOG = Path(__file__).resolve().parent / "fixtures" / "marketing" / "CHANGELOG.md"
SHIPPED_RULES = Path(__file__).resolve().parents[1] / "agents" / "marketing_agent" / "safety_rules.json"


@pytest.fixture
def store(tmp_path):
    db = PendingApprovalStore(db_path=tmp_path / "marketing_agent.db")
    yield db
    db.close()


@pytest.fixture
def changelog_story() -> Story:
    return generate_story(parse_changelog(FIXTURE_CHANGELOG)[0])


def test_shipped_rules_file_is_json():
    rules = SafetyRules.load(SHIPPED_RULES)
    assert "hn" in rules.self_promo.platforms
    assert "reddit" in rules.self_promo.platforms
    assert rules.self_promo.phrases
    assert rules.duplicate.similarity_threshold > 0


def test_self_promo_rewrites_then_blocks_hn(store):
    adapter = HackerNewsAdapter(store)
    with pytest.raises(SelfPromoError, match="self-promo"):
        adapter.submit(
            "Show HN: check out our StreamCtx. StreamCtx is our new SDK. "
            "Please upvote StreamCtx and follow us. Shameless plug for StreamCtx."
        )
    assert store.list_by_status("pending") == []


def test_self_promo_rewrite_can_salvage_mild_pitch():
    rules = SafetyRules.load(SHIPPED_RULES)
    original = "Check out our wrap() tracking fix: per-client instance, not a global."
    rewritten, score, _reasons = review_self_promo(original, rules.self_promo)
    assert "check out our" not in rewritten.lower()
    assert "wrap()" in rewritten
    assert score < rules.self_promo.threshold


def test_technical_hn_changelog_story_is_not_flagged(store, changelog_story):
    adapter = HackerNewsAdapter(store)
    entry_id = adapter.queue(changelog_story)
    row = store.get_entry(entry_id)
    assert row is not None
    assert row.platform == "hn"
    assert "please upvote" not in row.content.lower()


def test_self_promo_does_not_apply_to_linkedin(store):
    adapter = LinkedInAdapter(store)
    entry_id = adapter.submit(
        "We just launched StreamCtx. Check out our new SDK and please upvote."
    )
    row = store.get_entry(entry_id)
    assert row is not None
    assert "Check out our" in row.content


def test_rules_file_override_adds_phrase_without_code_change(store, tmp_path):
    data = json.loads(SHIPPED_RULES.read_text(encoding="utf-8"))
    data["self_promo"]["on_fail"] = "block"
    data["self_promo"]["phrases"] = [{"pattern": "zzzxpromo", "weight": 5.0}]
    data["self_promo"]["rewrite_replacements"] = []
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    gate = SafetyGate(rules=SafetyRules.load(path))
    adapter = HackerNewsAdapter(store, safety=gate)

    adapter.submit("A technical note about wrap() per-client tracking.")
    with pytest.raises(SelfPromoError, match="zzzxpromo"):
        adapter.submit("This zzzxpromo line should be blocked by config.")
    assert len(store.list_by_status("pending")) == 1


def test_duplicate_guard_staggers_same_story_across_platforms(store, changelog_story):
    LinkedInAdapter(store).queue(changelog_story)
    twitter = TwitterAdapter(
        store, credentials=TwitterCredentials(bearer_token="x"), http=object()
    )
    with pytest.raises(DuplicateContentError, match="linkedin"):
        twitter.queue(changelog_story)
    assert len(store.list_by_status("pending")) == 1


def test_duplicate_guard_allows_same_story_next_utc_day(store, changelog_story):
    LinkedInAdapter(store).queue(changelog_story)
    yesterday = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    row = store.list_by_status("pending")[0]
    store._conn.execute(
        "UPDATE pending_approval SET created_at = ? WHERE entry_id = ?",
        (yesterday.isoformat(), row.entry_id),
    )
    store._conn.commit()

    later = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
    gate = SafetyGate(rules=SafetyRules.load(SHIPPED_RULES), now_fn=lambda: later)
    twitter = TwitterAdapter(
        store,
        credentials=TwitterCredentials(bearer_token="x"),
        http=object(),
        safety=gate,
    )
    entry_id = twitter.queue(changelog_story)
    assert store.get_entry(entry_id).platform == "twitter"
    platforms = {row.platform for row in store.list_by_status("pending")}
    assert platforms == {"linkedin", "twitter"}


def test_duplicate_guard_allows_a_different_story_same_day(store, changelog_story):
    LinkedInAdapter(store).queue(changelog_story)
    other = Story(
        headline="AttributionEngine scores DRIFT vs COMPRESSION",
        key_facts=["Weighted heuristic verified at 0.82 confidence on a real failure."],
        proof_point="0.82 confidence on a real failure",
        tone_tags=["observability", "technical"],
    )
    TwitterAdapter(
        store, credentials=TwitterCredentials(bearer_token="x"), http=object()
    ).queue(other)
    platforms = {row.platform for row in store.list_by_status("pending")}
    assert platforms == {"linkedin", "twitter"}


def test_rejected_entries_do_not_block_duplicates(store, changelog_story):
    entry_id = LinkedInAdapter(store).queue(changelog_story)
    store.reject(entry_id)
    TwitterAdapter(
        store, credentials=TwitterCredentials(bearer_token="x"), http=object()
    ).queue(changelog_story)
    pending = store.list_by_status("pending")
    assert len(pending) == 1
    assert pending[0].platform == "twitter"


def test_story_fingerprint_is_stable(changelog_story):
    assert story_fingerprint(changelog_story) == story_fingerprint(changelog_story)
    assert "wrap" in story_fingerprint(changelog_story)


def test_rewrite_uses_config_replacements_not_python_literals():
    rules = SafetyRules.load(SHIPPED_RULES)
    cleaned = rewrite_self_promo("Shameless plug: try our wrap() fix.", rules.self_promo)
    assert "shameless plug" not in cleaned.lower()
    assert "try our" not in cleaned.lower()
    assert "wrap() fix" in cleaned
