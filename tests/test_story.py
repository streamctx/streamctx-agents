"""Unit tests for marketing_agent Stage 1 content core."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.marketing_agent.models import SourceData, Story
from agents.marketing_agent.sources import (
    collect_sources,
    is_significant_commit,
    load_significant_commits,
    parse_changelog,
)
from agents.marketing_agent.story import generate_story, generate_stories

FIXTURE_CHANGELOG = Path(__file__).resolve().parent / "fixtures" / "marketing" / "CHANGELOG.md"
PRODUCT_CHANGELOG = Path(r"C:\Users\ray9094\streamctx\CHANGELOG.md")

GIT_LOG = (
    "abc123def456\x1ffix: wrap() tracks per-client instance instead of global _originals"
    "\x1f2026-07-31\x1fDuck-typed clients were silently untracked, so get_stats() and "
    "resume() returned empty data. Closes #5.\x1e"
    "def456abc789\x1fchore: bump version\x1f2026-07-30\x1f\x1e"
    "111222333444\x1fMerge branch 'main' into feature\x1f2026-07-29\x1f\x1e"
    "aaa111bbb222\x1fwip\x1f2026-07-28\x1f\x1e"
    "ccc333ddd444\x1fAdd AttributionEngine scoring for DRIFT / COMPRESSION / RECENCY"
    "\x1f2026-06-20\x1fWeighted heuristic scoring verified at 0.82 confidence on a real failure.\x1e"
)


def test_parse_changelog_one_entry_per_version():
    entries = parse_changelog(FIXTURE_CHANGELOG)
    assert [entry.version for entry in entries] == ["0.4.4", "0.4.3", "0.4.2"]
    assert all(entry.kind == "changelog" for entry in entries)


def test_parse_changelog_joins_wrapped_bullets():
    entry = parse_changelog(FIXTURE_CHANGELOG)[0]
    assert entry.version == "0.4.4"
    assert entry.category == "Fixed"
    assert len(entry.items) == 1
    assert "per-client instance" in entry.items[0]
    assert "(#5)" in entry.items[0]


def test_parse_changelog_handles_bare_category_headers_and_mixed_types():
    entry = parse_changelog(FIXTURE_CHANGELOG)[2]
    assert entry.version == "0.4.2"
    assert entry.category is None
    assert "Fixed" in entry.categories
    assert "Security" in entry.categories
    assert "Added" in entry.categories
    assert any("50 concurrent" in item for item in entry.items)


def test_generate_story_from_changelog_entry():
    entry = parse_changelog(FIXTURE_CHANGELOG)[0]
    story = generate_story(entry)

    assert isinstance(story, Story)
    assert story.headline.startswith("0.4.4:")
    assert "wrap()" in story.headline
    assert "…" not in story.headline
    assert "_originals" in story.headline
    assert len(story.key_facts) == 1
    assert "get_stats()" in story.key_facts[0]
    assert "getstats()" not in story.key_facts[0]
    assert "_originals" in story.key_facts[0]
    assert "get_stats()" in story.proof_point
    assert "#5" in story.proof_point
    assert story.proof_point.strip() not in {"#5", "(#5)", "(#5)."}
    assert "bugfix" in story.tone_tags
    assert "from_changelog" in story.tone_tags
    assert "community_safe" in story.tone_tags


def test_generate_story_from_mapping():
    story = generate_story(
        {
            "kind": "changelog",
            "title": "StreamCtx 0.4.3",
            "version": "0.4.3",
            "items": ["CI matrix covers Ubuntu, macOS, and Windows (12/12 passing)."],
            "category": "Added",
        }
    )
    assert story.headline.startswith("0.4.3:")
    assert "12/12 passing" in story.proof_point
    assert "quality" in story.tone_tags


def test_generate_story_from_commit():
    commits = load_significant_commits(raw_log=GIT_LOG)
    wrap_fix = next(c for c in commits if c.identifier == "abc123def456")
    story = generate_story(wrap_fix)

    assert "wrap()" in story.headline
    assert not story.headline.lower().startswith("fix:")
    assert any("Closes #5" in fact or "#5" in fact for fact in story.key_facts) or "#5" in story.proof_point
    assert "from_commit" in story.tone_tags
    assert wrap_fix.kind == "commit"


def test_insignificant_commits_are_filtered():
    commits = load_significant_commits(raw_log=GIT_LOG)
    shas = {c.identifier for c in commits}
    assert "abc123def456" in shas
    assert "ccc333ddd444" in shas
    assert "def456abc789" not in shas
    assert "111222333444" not in shas
    assert "aaa111bbb222" not in shas


def test_is_significant_commit_keeps_feat_without_body():
    source = SourceData(kind="commit", title="feat: add poison detector", body="", identifier="x")
    assert is_significant_commit(source) is True


def test_generate_stories_maps_one_to_one():
    entries = parse_changelog(FIXTURE_CHANGELOG)
    stories = generate_stories(entries)
    assert len(stories) == len(entries)
    assert stories[2].proof_point
    assert "under 5 seconds" in stories[2].proof_point or "50 concurrent" in stories[2].proof_point


def test_collect_sources_changelog_then_commits():
    sources = collect_sources(
        changelog_path=FIXTURE_CHANGELOG,
        raw_log=GIT_LOG,
    )
    kinds = [s.kind for s in sources]
    assert kinds[:3] == ["changelog", "changelog", "changelog"]
    assert "commit" in kinds
    assert kinds.index("commit") > kinds.index("changelog")


def test_generate_story_rejects_empty_source():
    with pytest.raises(ValueError, match="no title"):
        generate_story(SourceData(kind="changelog", title="", body=""))


@pytest.mark.skipif(not PRODUCT_CHANGELOG.exists(), reason="streamctx checkout not present")
def test_parse_real_product_changelog():
    entries = parse_changelog(PRODUCT_CHANGELOG)
    assert entries
    assert entries[0].version
    story = generate_story(entries[0])
    assert "…" not in story.headline
    assert "get_stats()" in " ".join(story.key_facts)
    assert "getstats()" not in " ".join(story.key_facts)
    assert story.proof_point.strip() not in {"#5", "(#5)", "(#5)."}
    assert story.tone_tags
