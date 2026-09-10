"""Unified Home / Today inbox: merge queues, summaries, ready-to-publish."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agents.marketing_agent.pending_approval import (
    CONTENT_POST,
    MODE_DRAFT_ONLY,
    PLATFORM_LINKEDIN,
    STATUS_PENDING,
    PendingApprovalStore as MarketingStore,
    is_marketing_brief,
)
from content_pipeline import (
    STAGE_NEEDS_REVIEW,
    STAGE_READY_TO_POST,
    ContentPipelineStore,
)
from dashboard import (
    PendingItem,
    _presales_inbox_summary,
    approve_entry,
    is_marketing_brief_item,
    load_pending_approvals,
    mark_ready_to_publish,
    reject_entry,
)
from home_view import inbox_summary, is_marketing_content_draft


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _item(**kwargs) -> PendingItem:
    defaults = dict(
        agent_key="marketing",
        agent_name="Marketing Agent",
        entry_id="e1",
        status="pending",
        created_at=NOW.isoformat(),
        title="linkedin post",
        preview="clip",
        store="marketing",
    )
    defaults.update(kwargs)
    return PendingItem(**defaults)


def test_pending_item_defaults_keep_old_constructors():
    item = PendingItem(
        agent_key="coding",
        agent_name="Coding Agent",
        entry_id="old",
        status="needs_human_review",
        created_at=NOW.isoformat(),
        title="old",
        preview="",
        store="coding",
    )
    assert item.summary == ""
    assert item.body == ""
    assert item.kind == ""


def test_inbox_summary_prefers_loader_summary():
    item = _item(summary="Marketing: LinkedIn post draft for review")
    assert inbox_summary(item) == "Marketing: LinkedIn post draft for review"


def test_inbox_summary_falls_back_to_title():
    item = _item(summary="", title="linkedin post")
    assert inbox_summary(item) == "Marketing: linkedin post"


def test_competitor_inbox_summary():
    item = PendingItem(
        agent_key="competitor",
        agent_name="Competitor Agent",
        entry_id="c1",
        status="pending",
        created_at=NOW.isoformat(),
        title="Langfuse: mention",
        preview="HN thread",
        store="competitor",
        summary="Competitor: Langfuse: mention",
        body="HN thread comparing tracing.",
        kind="competitor_signal",
    )
    assert inbox_summary(item) == "Competitor: Langfuse: mention"
    assert not is_marketing_content_draft(item)


def test_presales_summary_uses_name_role_company():
    entry = SimpleNamespace(title="Jane Doe · CTO @ Acme")
    assert (
        _presales_inbox_summary(entry)
        == "Presales: outreach email to Jane Doe, CTO at Acme"
    )


def test_marketing_brief_is_not_a_content_draft():
    brief = _item(kind="marketing_brief", store="marketing")
    draft = _item(kind="marketing_draft", store="marketing", body="Full LinkedIn copy")
    pipeline = _item(kind="pipeline_draft", store="pipeline", body="Weekly notes")
    assert is_marketing_brief_item(brief)
    assert not is_marketing_content_draft(brief)
    assert is_marketing_content_draft(draft)
    assert is_marketing_content_draft(pipeline)


def test_mark_ready_to_publish_uses_pipeline_not_adapter(tmp_path: Path, monkeypatch):
    marketing_db = tmp_path / "marketing_agent.db"
    pipeline_db = tmp_path / "content_pipeline.db"
    store = MarketingStore(db_path=marketing_db, enable_default_notifier=False)
    try:
        entry = store.create_entry(
            platform=PLATFORM_LINKEDIN,
            content_type=CONTENT_POST,
            content="Full changelog post the founder will paste by hand.",
            mode=MODE_DRAFT_ONLY,
        )
    finally:
        store.close()

    def boom(*args, **kwargs):
        raise AssertionError("platform publish/adapter must not run")

    monkeypatch.setattr(
        "agents.marketing_agent.adapters.base.AutoAdapter.publish", boom, raising=False
    )
    item = _item(
        entry_id=entry.entry_id,
        status=STATUS_PENDING,
        kind="marketing_draft",
        body=entry.content,
        summary="Marketing: LinkedIn post draft for review",
    )
    mark_ready_to_publish(item, marketing_db=marketing_db, pipeline_db=pipeline_db)

    store = MarketingStore(db_path=marketing_db, enable_default_notifier=False)
    try:
        updated = store.get_entry(entry.entry_id)
        assert updated is not None
        assert updated.status == "approved"
        assert not is_marketing_brief(updated)
    finally:
        store.close()

    pipe = ContentPipelineStore(db_path=pipeline_db)
    try:
        ready = pipe.list_by_stage(STAGE_READY_TO_POST)
        assert len(ready) == 1
        assert "changelog post" in ready[0].notes
        assert ready[0].channel == "LinkedIn"
    finally:
        pipe.close()

    src = inspect.getsource(mark_ready_to_publish)
    assert ".publish(" not in src
    assert "adapters" not in src


def test_mark_ready_skips_marketing_briefs(tmp_path: Path):
    marketing_db = tmp_path / "marketing_agent.db"
    pipeline_db = tmp_path / "content_pipeline.db"
    from agents.marketing_agent.pending_approval import BRIEF_FINGERPRINT_PREFIX

    store = MarketingStore(db_path=marketing_db, enable_default_notifier=False)
    try:
        entry = store.create_entry(
            platform=PLATFORM_LINKEDIN,
            content_type=CONTENT_POST,
            content="Write a post about the roster.",
            mode=MODE_DRAFT_ONLY,
            source_fingerprint=f"{BRIEF_FINGERPRINT_PREFIX}ticket",
        )
    finally:
        store.close()

    mark_ready_to_publish(
        _item(entry_id=entry.entry_id, kind="marketing_brief"),
        marketing_db=marketing_db,
        pipeline_db=pipeline_db,
    )
    store = MarketingStore(db_path=marketing_db, enable_default_notifier=False)
    try:
        assert store.get_entry(entry.entry_id).status == STATUS_PENDING
    finally:
        store.close()
    pipe = ContentPipelineStore(db_path=pipeline_db)
    try:
        assert pipe.list_by_stage(STAGE_READY_TO_POST) == []
    finally:
        pipe.close()


def test_pipeline_approve_moves_to_ready_to_post(tmp_path: Path):
    db = tmp_path / "content_pipeline.db"
    store = ContentPipelineStore(db_path=db)
    try:
        card = store.create_item(
            title="HN comment: wrap() tracks per-client instance",
            channel="Hacker News",
            stage=STAGE_NEEDS_REVIEW,
            notes="Reply only. No product pitch.",
        )
    finally:
        store.close()

    item = _item(
        entry_id=card.item_id,
        store="pipeline",
        kind="pipeline_draft",
        title=card.title,
        status=STAGE_NEEDS_REVIEW,
        body=card.notes,
    )
    approve_entry(item, pipeline_db=db)
    store = ContentPipelineStore(db_path=db)
    try:
        updated = store.get_item(card.item_id)
        assert updated is not None
        assert updated.stage == STAGE_READY_TO_POST
    finally:
        store.close()


def test_pipeline_decline_deletes_tracking_card(tmp_path: Path):
    db = tmp_path / "content_pipeline.db"
    store = ContentPipelineStore(db_path=db)
    try:
        card = store.create_item(
            title="Skip this draft",
            channel="LinkedIn",
            stage=STAGE_NEEDS_REVIEW,
            notes="not shipping",
        )
    finally:
        store.close()

    reject_entry(
        _item(entry_id=card.item_id, store="pipeline", kind="pipeline_draft"),
        pipeline_db=db,
    )
    store = ContentPipelineStore(db_path=db)
    try:
        assert store.get_item(card.item_id) is None
    finally:
        store.close()


def test_load_pending_approvals_includes_pipeline_needs_review(
    tmp_path: Path, monkeypatch
):
    db = tmp_path / "content_pipeline.db"
    store = ContentPipelineStore(db_path=db)
    try:
        store.create_item(
            title="Weekly HN comment",
            channel="Hacker News",
            stage=STAGE_NEEDS_REVIEW,
            notes="draft body for review",
        )
        store.create_item(
            title="Already ready",
            channel="LinkedIn",
            stage=STAGE_READY_TO_POST,
            notes="should not appear",
        )
    finally:
        store.close()

    class Store(ContentPipelineStore):
        def __init__(self, db_path=None, **kwargs):
            super().__init__(db_path=db)

    monkeypatch.setattr("dashboard.ContentPipelineStore", Store)
    items = load_pending_approvals()
    pipe = [item for item in items if item.store == "pipeline"]
    assert len(pipe) == 1
    assert "weekly content draft for review" in pipe[0].summary.lower()
    assert "draft body" in pipe[0].body
    assert pipe[0].agent_key == "marketing"


def test_dashboard_home_tab_is_first():
    src = Path("dashboard.py").read_text(encoding="utf-8")
    home_at = src.find('"Home"')
    roster_at = src.find('"Roster"')
    assert home_at != -1 and roster_at != -1
    assert home_at < roster_at
    assert "render_home_tab" in src


def test_refresh_dashboard_key_is_unique():
    src = Path("dashboard.py").read_text(encoding="utf-8")
    home = Path("home_view.py").read_text(encoding="utf-8")
    assert src.count('key="refresh-dashboard"') == 1
    assert 'key="refresh-dashboard"' not in home
    assert 'if __name__ == "__main__" and _in_streamlit():' in src


def test_dashboard_home_loads_without_duplicate_keys():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("dashboard.py", default_timeout=25)
    at.run()
    assert not at.exception
    body = "\n".join(str(m.value) for m in at.markdown)
    assert "Pending approval" in body
    refresh_keys = [btn.key for btn in at.button if getattr(btn, "key", None) == "refresh-dashboard"]
    assert len(refresh_keys) <= 1


def test_ready_to_publish_never_calls_social_adapters():
    dash = Path("dashboard.py").read_text(encoding="utf-8")
    home = Path("home_view.py").read_text(encoding="utf-8")
    assert "adapters" not in dash.split("def mark_ready_to_publish")[1].split("def approve_entry")[0]
    assert "publish(" not in dash.split("def mark_ready_to_publish")[1].split("def approve_entry")[0]
    assert "Ready to publish" in home
    assert "Does not publish" in home
