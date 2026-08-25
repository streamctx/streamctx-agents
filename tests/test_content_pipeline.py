"""Content pipeline tracking store — isolated from the marketing agent."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agents.marketing_agent.pending_approval import (
    PLATFORM_LINKEDIN,
    PendingApprovalStore as MarketingStore,
)
from content_pipeline import (
    CHANNELS,
    DEMO_ITEMS,
    FLAG_FOUNDER,
    FLAG_LEGAL,
    FLAG_NONE,
    FLAG_PRICING,
    FLAGS,
    FOCUS_STAGE_KEY,
    STAGE_DRAFT,
    STAGE_NEEDS_REVIEW,
    STAGE_PUBLISHED,
    STAGE_READY_TO_POST,
    STAGES,
    ContentPipelineStore,
    _stage_tab_label,
    neighbor_stage,
    normalize_flag,
)


@pytest.fixture
def store(tmp_path: Path) -> ContentPipelineStore:
    db = ContentPipelineStore(db_path=tmp_path / "content_pipeline.db")
    yield db
    db.close()


def test_stage_flag_and_channel_catalog():
    assert STAGES == (
        "Draft",
        "Needs Review",
        "Ready to Post",
        "Published",
    )
    assert FLAGS == ("", "Legal review", "Founder/Eng review", "Pricing sign-off")
    assert "Hacker News" in CHANNELS
    assert "LinkedIn" in CHANNELS
    assert "Twitter / X" in CHANNELS
    assert "Dev.to" in CHANNELS
    assert "Reddit" in CHANNELS
    assert "Indie Hackers" in CHANNELS
    assert "Product Hunt" in CHANNELS
    assert "GitHub" in CHANNELS


def test_schema_is_only_content_items(store: ContentPipelineStore, tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "content_pipeline.db")
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert tables == {"content_items"}
        columns = {
            col[1] for col in conn.execute("PRAGMA table_info(content_items)").fetchall()
        }
        assert columns == {
            "item_id",
            "title",
            "channel",
            "stage",
            "flag",
            "notes",
            "created_at",
            "updated_at",
        }
    finally:
        conn.close()


def test_create_list_move_and_delete(store: ContentPipelineStore):
    item = store.create_item(
        title="HN comment draft",
        channel="Hacker News",
        notes="No self-promo",
    )
    assert item.stage == STAGE_DRAFT
    assert item.flag == FLAG_NONE
    assert store.list_by_stage(STAGE_DRAFT)[0].title == "HN comment draft"

    moved = store.move_stage(item.item_id, 1)
    assert moved.stage == STAGE_NEEDS_REVIEW
    store.move_stage(item.item_id, 1)
    store.move_stage(item.item_id, 1)
    published = store.get_item(item.item_id)
    assert published is not None
    assert published.stage == STAGE_PUBLISHED
    assert store.move_stage(item.item_id, 1).stage == STAGE_PUBLISHED

    assert store.delete_item(item.item_id) is True
    assert store.get_item(item.item_id) is None


def test_rejects_unknown_channel_and_empty_title(store: ContentPipelineStore):
    with pytest.raises(ValueError, match="channel"):
        store.create_item(title="x", channel="MySpace")
    with pytest.raises(ValueError, match="title"):
        store.create_item(title="  ", channel="LinkedIn")


def test_flag_normalization():
    assert normalize_flag("None") == FLAG_NONE
    assert normalize_flag(FLAG_LEGAL) == FLAG_LEGAL
    assert normalize_flag(FLAG_FOUNDER) == FLAG_FOUNDER
    assert normalize_flag(FLAG_PRICING) == FLAG_PRICING
    with pytest.raises(ValueError, match="flag"):
        normalize_flag("Legal")


def test_neighbor_stage_bounds():
    assert neighbor_stage(STAGE_DRAFT, -1) is None
    assert neighbor_stage(STAGE_DRAFT, 1) == STAGE_NEEDS_REVIEW
    assert neighbor_stage(STAGE_READY_TO_POST, 1) == STAGE_PUBLISHED
    assert neighbor_stage(STAGE_PUBLISHED, 1) is None


def test_seed_if_empty_only_once(store: ContentPipelineStore):
    assert store.seed_if_empty() == len(DEMO_ITEMS)
    assert store.seed_if_empty() == 0
    assert store.counts_by_stage()[STAGE_DRAFT] >= 1
    assert store.counts_by_stage()[STAGE_PUBLISHED] >= 1


def test_pipeline_db_does_not_touch_marketing_agent_db(tmp_path: Path):
    marketing_path = tmp_path / "marketing_agent.db"
    pipeline_path = tmp_path / "content_pipeline.db"
    marketing = MarketingStore(db_path=marketing_path, enable_default_notifier=False)
    try:
        entry = marketing.create_entry(
            platform=PLATFORM_LINKEDIN,
            content_type="post",
            content="Draft LinkedIn post",
        )
        assert entry.content == "Draft LinkedIn post"
    finally:
        marketing.close()

    pipeline = ContentPipelineStore(db_path=pipeline_path)
    try:
        pipeline.create_item(title="Tracked card", channel="LinkedIn")
    finally:
        pipeline.close()

    marketing_conn = sqlite3.connect(marketing_path)
    try:
        tables = {
            row[0]
            for row in marketing_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "pending_approval" in tables
        assert "content_items" not in tables
        row = marketing_conn.execute(
            "SELECT content FROM pending_approval"
        ).fetchone()
        assert row[0] == "Draft LinkedIn post"
    finally:
        marketing_conn.close()

    pipeline_conn = sqlite3.connect(pipeline_path)
    try:
        tables = {
            row[0]
            for row in pipeline_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert tables == {"content_items"}
    finally:
        pipeline_conn.close()


def test_marketing_agent_entry_points_unchanged():
    from agents.marketing_agent import marketing_agent
    from agents.marketing_agent.pending_approval import PLATFORMS, STATUSES

    assert callable(marketing_agent.run)
    assert callable(marketing_agent.run_draft_cycle)
    assert callable(marketing_agent.draft_post)
    assert "hn" in PLATFORMS
    assert "linkedin" in PLATFORMS
    assert "pending" in STATUSES
    assert "published" in STATUSES

    dashboard_src = (
        Path(__file__).resolve().parents[1] / "dashboard.py"
    ).read_text(encoding="utf-8")
    assert '["Roster", "Control", "Content Pipeline"]' in dashboard_src
    assert "render_pipeline_tab" in dashboard_src
    assert 'marketing_agent.run' in dashboard_src
    assert 'assign_mode == "marketing"' in dashboard_src


def test_streamlit_page_renders_kanban_and_logs_item(tmp_path: Path):
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "content_pipeline.db"
    script = f"""
from content_pipeline import render_pipeline_tab
import streamlit as st
render_pipeline_tab(st, db_path=r"{db}", seed_demo=True)
"""
    at = AppTest.from_string(script)
    at.run(timeout=10)
    assert not at.exception
    body = "\n".join(m.value for m in at.markdown)
    assert "Marketing Agent Content Pipeline" in body
    assert "Draft" in body
    assert "Needs Review" in body
    assert "Ready to Post" in body
    assert "Published" in body
    assert "Legal review" in body
    assert "Founder/Eng review" in body
    assert "Pricing sign-off" in body
    assert "Hacker News" in body
    assert "LinkedIn" in body
    assert "Twitter / X" in body

    at.text_input[0].input("GitHub reply: issue #12")
    at.selectbox[0].select("GitHub")
    at.selectbox[1].select("Draft")
    at.selectbox[2].select("Legal review")
    at.text_area[0].input("Needs legal pass on the wording.")
    log_btn = next(button for button in at.button if button.label == "Log item")
    log_btn.click()
    at.run(timeout=10)
    assert not at.exception
    assert at.success[0].value == "Logged. The marketing agent was not invoked."
    body = "\n".join(m.value for m in at.markdown)
    assert "GitHub reply: issue #12" in body
    draft_tab = next(button for button in at.button if button.label == _stage_tab_label(3, STAGE_DRAFT))
    assert draft_tab is not None


def test_stage_tabs_select_and_icon_actions_are_compact(tmp_path: Path):
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "content_pipeline.db"
    script = f"""
from content_pipeline import render_pipeline_tab
import streamlit as st
render_pipeline_tab(st, db_path=r"{db}", seed_demo=True)
"""
    at = AppTest.from_string(script)
    at.run(timeout=10)
    assert not at.exception

    tab_labels = [_stage_tab_label(2, STAGE_DRAFT), _stage_tab_label(2, STAGE_NEEDS_REVIEW)]
    labels = [button.label for button in at.button]
    for expected in tab_labels:
        assert expected in labels
    assert any(button.label == "←" for button in at.button)
    assert any(button.label == "→" for button in at.button)
    assert any(button.label == "×" for button in at.button)

    needs = next(
        button for button in at.button if button.label == _stage_tab_label(2, STAGE_NEEDS_REVIEW)
    )
    needs.click()
    at.run(timeout=10)
    assert not at.exception
    assert at.session_state[FOCUS_STAGE_KEY] == STAGE_NEEDS_REVIEW
    body = "\n".join(m.value for m in at.markdown)
    assert "scp-col-focused" in body
    assert 'id="scp-col-needs-review"' in body


def test_stage_tab_label_format():
    assert _stage_tab_label(2, STAGE_DRAFT) == "2\nDraft"
