"""Dedicated Coding / Competitor / Research dashboard tabs."""

from __future__ import annotations

from pathlib import Path

from agents.coding_agent.pending_approval import (
    STATUS_NEEDS_HUMAN_REVIEW,
    PendingApprovalStore as CodingStore,
)
from agents.competitor_agent.models import SIGNAL_TYPE_MENTION
from agents.competitor_agent.pending_approval import queue_signals
from agents.competitor_agent.storage import CompetitorStore
from agents.research_agent.models import SOURCE_TYPE_ARXIV, STATUS_NEW
from agents.research_agent.storage import ResearchStore


def _ui_text(at) -> str:
    chunks: list[str] = []
    for attr in (
        "markdown",
        "caption",
        "text",
        "title",
        "header",
        "subheader",
        "success",
        "info",
    ):
        for item in getattr(at, attr, []):
            chunks.append(str(getattr(item, "value", item)))
    for metric in getattr(at, "metric", []):
        chunks.append(str(getattr(metric, "label", "")))
        chunks.append(str(getattr(metric, "value", "")))
    return "\n".join(chunks)


def test_dashboard_wires_coding_competitor_research_tabs():
    src = Path("dashboard.py").read_text(encoding="utf-8")
    for label in ("Coding", "Competitor", "Research"):
        assert f'"{label}"' in src
    assert "render_coding_tab" in src
    assert "render_competitor_tab" in src
    assert "render_research_tab" in src
    assert src.find('"Coding"') < src.find('"Pre-Sales"')


def test_coding_tab_lists_pending_and_rejects(tmp_path: Path):
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "coding_agent.db"
    store = CodingStore(db_path=db, enable_default_notifier=False)
    try:
        entry = store.create_entry(
            session_id="99",
            root_cause="UNCLEAR",
            confidence=0.2,
            matched_pattern_id=None,
            diff=None,
            regression_test=None,
            test_results='{"failed_call_id": 7, "reason": "test unclear"}',
            retries_used=0,
            status=STATUS_NEEDS_HUMAN_REVIEW,
        )
    finally:
        store.close()

    script = f"""
from agents.coding_agent.tab import render_coding_tab
import streamlit as st
render_coding_tab(st, db_path=r"{db}", key_prefix="t-ca-")
"""
    at = AppTest.from_string(script)
    at.run(timeout=15)
    assert not at.exception
    body = _ui_text(at)
    assert "Coding Agent" in body
    assert "Pending Approval" in body
    assert "UNCLEAR" in body
    reject = next(b for b in at.button if b.label == "Reject")
    reject.click().run()
    assert not at.exception
    store = CodingStore(db_path=db, enable_default_notifier=False)
    try:
        assert store.get_entry(entry.entry_id).status == "rejected"
        assert store.list_by_status(STATUS_NEEDS_HUMAN_REVIEW) == []
    finally:
        store.close()


def test_competitor_tab_lists_pending_signal(tmp_path: Path):
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "competitor_agent.db"
    store = CompetitorStore(db_path=db)
    try:
        signal = store.insert_signal(
            competitor="Langfuse",
            signal_type=SIGNAL_TYPE_MENTION,
            summary="HN thread about tracing.",
            source_url="https://news.ycombinator.com/item?id=1",
        )
        queue_signals([signal], db_path=db)
    finally:
        store.close()

    script = f"""
from agents.competitor_agent.tab import render_competitor_tab
import streamlit as st
render_competitor_tab(st, db_path=r"{db}", key_prefix="t-cp-")
"""
    at = AppTest.from_string(script)
    at.run(timeout=15)
    assert not at.exception
    body = _ui_text(at)
    assert "Competitor Agent" in body
    assert "Langfuse" in body
    assert "Pending Approval" in body


def test_research_tab_shows_ideas_and_empty_intake(tmp_path: Path):
    from streamlit.testing.v1 import AppTest

    research_db = tmp_path / "research_agent.db"
    coding_db = tmp_path / "coding_agent.db"
    store = ResearchStore(db_path=research_db)
    try:
        store.insert_idea(
            source_url="https://arxiv.org/abs/1",
            source_type=SOURCE_TYPE_ARXIV,
            title="Context compression for agents",
            gap_description="Need better WAL compression",
            status=STATUS_NEW,
            content_excerpt="abstract text",
        )
    finally:
        store.close()
    CodingStore(db_path=coding_db, enable_default_notifier=False).close()

    script = f"""
from agents.research_agent.tab import render_research_tab
import streamlit as st
render_research_tab(
    st,
    db_path=r"{research_db}",
    coding_db=r"{coding_db}",
    key_prefix="t-ra-",
)
"""
    at = AppTest.from_string(script)
    at.run(timeout=15)
    assert not at.exception
    body = _ui_text(at)
    assert "Research Agent" in body
    assert "Context compression" in body
    assert "research intake" in body.lower() or "research_ideas" in body.lower()
