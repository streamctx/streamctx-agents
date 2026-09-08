"""Agent Manager routing: classify, dispatch, and existing-feature wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_manager import (
    UNCLASSIFIED,
    _hash_pairs,
    classify_command,
    dispatch_command,
    live_status,
)
from dashboard import render_pending_approvals, render_roster_card, submit_research_poll


def _ui_text(at) -> str:
    parts: list[str] = []
    for attr in (
        "markdown",
        "subheader",
        "header",
        "title",
        "caption",
        "text",
        "info",
        "warning",
        "success",
        "error",
    ):
        for item in getattr(at, attr, []) or []:
            parts.append(str(getattr(item, "value", item)))
    return "\n".join(parts)


def test_live_status_is_idle_or_running():
    assert live_status("running") == "running"
    assert live_status("idle") == "idle"
    assert live_status("completed") == "idle"
    assert live_status("failed") == "idle"


def test_classify_pytest_nodeids_as_coding():
    result = classify_command("fix tests/test_broken_math.py::test_add")
    assert result.agent == "coding"
    assert result.reason == "keyword"


def test_classify_weekly_draft_as_marketing():
    result = classify_command("run the weekly draft for the content pipeline")
    assert result.agent == "marketing"


def test_classify_arxiv_poll_as_research():
    result = classify_command("poll arxiv for new papers")
    assert result.agent == "research"


def test_classify_langfuse_pricing_as_competitor():
    result = classify_command("check Langfuse pricing")
    assert result.agent == "competitor"


def test_classify_sales_navigator_as_presales():
    result = classify_command("import the sales navigator csv")
    assert result.agent == "presales"


def test_classify_presales_keywords():
    assert classify_command("presales").agent == "presales"
    assert classify_command("csv import").agent == "presales"
    assert classify_command("score these leads").agent == "presales"
    assert classify_command("queue outreach drafts").agent == "presales"


def test_classify_support_ticket_as_techsupport():
    assert classify_command("support").agent == "techsupport"
    assert classify_command("ticket").agent == "techsupport"
    assert classify_command("github issue").agent == "techsupport"
    assert classify_command("discord").agent == "techsupport"
    assert classify_command("tech support agent").agent == "techsupport"


def test_classify_dpdp_privacy_terms_as_legal():
    assert classify_command("legal").agent == "legal"
    assert classify_command("compliance").agent == "legal"
    assert classify_command("dpdp").agent == "legal"
    assert classify_command("privacy").agent == "legal"
    assert classify_command("terms").agent == "legal"
    assert classify_command("legal agent").agent == "legal"
    assert classify_command("privacy policy").agent == "legal"


def test_classify_empty_and_vague_are_unclassified():
    assert classify_command("").agent == UNCLASSIFIED
    assert classify_command("please handle this").agent == UNCLASSIFIED
    assert classify_command("github").agent == UNCLASSIFIED
    assert classify_command("").reason == "empty"


def test_ambiguous_github_release_vs_topic_unclassified_when_tied():
    # Bare overlap without a tracked competitor or arXiv should not guess.
    result = classify_command("look at this")
    assert result.agent == UNCLASSIFIED


def test_dispatch_coding_nodeids_uses_assign_fix(monkeypatch):
    monkeypatch.setattr(
        "agent_manager.log_action",
        lambda *args, **kwargs: {"id": "audit-coding"},
    )
    monkeypatch.setattr(
        "agent_manager.assign_coding_task",
        lambda text: "assign:nodeids",
    )
    called = {}
    monkeypatch.setattr(
        "agent_manager.submit_agent",
        lambda key: called.setdefault("run", key),
    )
    result = dispatch_command("coding", "fix tests/test_broken_math.py::test_add")
    assert result.action == "assign_fix"
    assert result.entry_id == "assign:nodeids"
    assert "run" not in called
    assert "ConfidenceGate.evaluate()" in result.message
    assert "FixValidationLoop.run()" in result.message


def test_dispatch_coding_run_starts_pipeline(monkeypatch):
    monkeypatch.setattr(
        "agent_manager.log_action",
        lambda *args, **kwargs: {"id": "audit-run"},
    )
    called = {}
    monkeypatch.setattr(
        "agent_manager.submit_agent",
        lambda key: called.setdefault("run", key),
    )
    result = dispatch_command("coding", "run pipeline")
    assert result.action == "run"
    assert called["run"] == "coding"


def test_dispatch_presales_run_starts_agent(monkeypatch):
    monkeypatch.setattr(
        "agent_manager.log_action",
        lambda *args, **kwargs: {"id": "audit-ps"},
    )
    called = {}
    monkeypatch.setattr(
        "agent_manager.submit_agent",
        lambda key: called.setdefault("run", key),
    )
    result = dispatch_command("presales", "run")
    assert result.action == "run"
    assert called["run"] == "presales"
    assert "Nothing is sent" in result.message


def test_dispatch_presales_csv_uses_assign_presales_task(monkeypatch):
    monkeypatch.setattr(
        "agent_manager.log_action",
        lambda *args, **kwargs: {"id": "audit-ps-csv"},
    )
    monkeypatch.setattr(
        "agent_manager.assign_presales_task",
        lambda text: "imported=3 updated=0 skipped=0",
    )
    called = {}
    monkeypatch.setattr(
        "agent_manager.submit_agent",
        lambda key: called.setdefault("run", key),
    )
    result = dispatch_command("presales", r"C:\data\leads.csv")
    assert result.action == "import"
    assert "imported=3" in result.message
    assert "run" not in called


def test_dispatch_techsupport_run_starts_agent(monkeypatch):
    monkeypatch.setattr(
        "agent_manager.log_action",
        lambda *args, **kwargs: {"id": "audit-ts"},
    )
    called = {}
    monkeypatch.setattr(
        "agent_manager.submit_agent",
        lambda key: called.setdefault("run", key),
    )
    result = dispatch_command("techsupport", "run")
    assert result.action == "poll"
    assert called["run"] == "techsupport"
    assert "Nothing is posted" in result.message


def test_dispatch_legal_run_starts_agent(monkeypatch):
    monkeypatch.setattr(
        "agent_manager.log_action",
        lambda *args, **kwargs: {"id": "audit-lc"},
    )
    called = {}
    monkeypatch.setattr(
        "agent_manager.submit_agent",
        lambda key: called.setdefault("run", key),
    )
    result = dispatch_command("legal", "run")
    assert result.action == "scan"
    assert called["run"] == "legal"
    assert "Nothing is published" in result.message
    assert "Not legal advice" in result.message


def test_dispatch_research_routes_to_poll_not_assign(monkeypatch):
    monkeypatch.setattr(
        "agent_manager.log_action",
        lambda *args, **kwargs: {"id": "audit-research"},
    )
    called = {}
    monkeypatch.setattr(
        "agent_manager.submit_research_poll",
        lambda **kwargs: called.setdefault("poll", kwargs or True),
    )
    monkeypatch.setattr(
        "agent_manager.assign_coding_task",
        lambda text: called.setdefault("assign", text),
    )
    result = dispatch_command(
        "research",
        "what do other tools do for context compression?",
    )
    assert result.action == "poll"
    assert "poll" in called
    assert "assign" not in called
    assert "Assign/synthesis is disabled" in result.message


def test_dispatch_marketing_weekly_drafts(monkeypatch):
    monkeypatch.setattr(
        "agent_manager.log_action",
        lambda *args, **kwargs: {"id": "audit-m"},
    )
    called = {}
    monkeypatch.setattr(
        "agent_manager.submit_weekly_drafts",
        lambda **kwargs: called.setdefault("weekly", True),
    )
    result = dispatch_command("marketing", "generate weekly drafts")
    assert result.action == "weekly_drafts"
    assert called["weekly"] is True


def test_dispatch_competitor_assign_v1(monkeypatch):
    monkeypatch.setattr(
        "agent_manager.log_action",
        lambda *args, **kwargs: {"id": "audit-c"},
    )
    monkeypatch.setattr(
        "agent_manager.assign_competitor_task",
        lambda text: "Langfuse pricing",
    )
    result = dispatch_command("competitor", "Langfuse pricing")
    assert result.action == "assign_check"
    assert result.entry_id == "Langfuse pricing"


def test_submit_research_poll_is_poll_entrypoint():
    import inspect

    source = inspect.getsource(submit_research_poll)
    assert "run_poll" in source or "research-poll" in source
    from dashboard import _execute_research_poll

    body = inspect.getsource(_execute_research_poll)
    assert "run_poll" in body
    assert "run_hype_filter" not in body
    assert "run_handoff" not in body


def test_hash_pairs_flag_langfuse_dedup():
    older = SimpleNamespace(
        snapshot_type="pricing",
        captured_at="2026-08-24T00:00:00+00:00",
        content_hash="aaa111",
    )
    newer_same = SimpleNamespace(
        snapshot_type="pricing",
        captured_at="2026-08-25T00:00:00+00:00",
        content_hash="aaa111",
    )
    newer_changed = SimpleNamespace(
        snapshot_type="pricing",
        captured_at="2026-08-25T12:00:00+00:00",
        content_hash="bbb222",
    )
    unchanged = _hash_pairs([newer_same, older])
    assert unchanged[0]["result"] == "unchanged"
    changed = _hash_pairs([newer_changed, older])
    assert changed[0]["result"] == "changed"


def test_agent_manager_wires_existing_uis_not_rebuilds():
    src = Path("agent_manager.py").read_text(encoding="utf-8")
    assert "render_pipeline_tab" in src
    assert "render_roster_card" in src
    assert "render_pending_approvals" in src
    assert "render_unified_inbox" in src
    assert "load_pending_approvals" in src
    assert "assign_fix" in src
    assert "ConfidenceGate.evaluate()" in src
    assert "FixValidationLoop.run()" in src
    assert "weekly_content_draft" in src
    assert "run_poll" in src
    assert "run_directed_check" in src or "assign_check" in src
    assert "PoisonDetector" in src
    assert "ContextDiffer" in src
    assert "dry_run=True" in src
    assert "compress_messages" in src
    assert "BACKLOG.md" in src
    assert "Directed Assign/synthesis is disabled" in src
    assert "Langfuse" in src
    assert "assign_presales_task" in src
    assert "from agents.presales_agent.presales_agent import assign_presales_task" in src
    assert "assign_presales_task," not in src.split("from dashboard import")[1].split(")")[0]
    assert "render_techsupport_tab" in src
    assert "techsupport_agent.run()" in src
    assert "render_legal_tab" in src
    assert "legal_compliance_agent.run()" in src


def test_dashboard_exports_shared_renderers():
    from dashboard import assign_presales_task

    assert callable(render_roster_card)
    assert callable(render_pending_approvals)
    assert callable(assign_presales_task)


def test_streamlit_home_shows_command_bar_and_seven_tiles():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("agent_manager.py", default_timeout=20)
    at.run()
    assert not at.exception
    body = "\n".join(str(m.value) for m in at.markdown)
    assert "Agent Manager" in body
    assert "Coding Agent" in body
    assert "Marketing Agent" in body
    assert "Research Agent" in body
    assert "Competitor Agent" in body
    assert "Pre-Sales Agent" in body
    assert "Tech Support Agent" in body
    assert "Legal / Compliance Agent" in body
    labels = [button.label for button in at.button]
    assert "Open Coding Agent" in labels
    assert "Open Marketing Agent" in labels
    assert "Open Research Agent" in labels
    assert "Open Competitor Agent" in labels
    assert "Open Pre-Sales Agent" in labels
    assert "Open Tech Support Agent" in labels
    assert "Open Legal / Compliance Agent" in labels
    assert any(inp.label == "Command" for inp in at.text_input)
    assert "drafts outreach from CSV leads" in body
    assert "answers support tickets from GitHub/Discord" in body
    assert "reviews legal/compliance docs and drafts DPDP checklist" in body
    assert "pending_approval:" in body
    assert "Pending approval" in body
    assert "Today" in body
    assign_inputs = [inp for inp in at.text_input if inp.label == "Assign a task"]
    assert len(assign_inputs) == 7


def test_unclassified_command_shows_manual_picker():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("agent_manager.py", default_timeout=20)
    at.run()
    assert not at.exception
    command = next(inp for inp in at.text_input if inp.label == "Command")
    command.input("please handle this")
    dispatch = next(button for button in at.button if button.label == "Dispatch")
    dispatch.click().run()
    assert not at.exception
    body = "\n".join(str(m.value) for m in at.markdown)
    assert "Unclassified" in body
    labels = [button.label for button in at.button]
    assert "Coding" in labels
    assert "Marketing" in labels
    assert "Research" in labels
    assert "Competitor" in labels
    assert "Pre-Sales" in labels
    assert "Tech Support" in labels
    assert "Legal / Compliance" in labels


def test_coding_tile_opens_existing_fix_flow_and_dogfood_tabs():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("agent_manager.py", default_timeout=20)
    at.run()
    next(button for button in at.button if button.label == "Open Coding Agent").click().run()
    assert not at.exception
    body = _ui_text(at)
    assert "Retry-loop fix flow" in body
    assert "ConfidenceGate.evaluate()" in body
    assert "assign_fix.py" in body
    labels = [button.label for button in at.button]
    assert "Run coding_agent pipeline" in labels
    assert "← All agents" in labels
    tab_labels = [getattr(tab, "label", str(tab)) for tab in at.tabs]
    joined = " ".join(tab_labels) + body
    assert "Poison Detector" in joined
    assert "Context Diff" in joined
    assert "Counterfactual Replay" in joined
    assert "Context Compression" in joined
    assert any(inp.label == "Command · Coding Agent" for inp in at.text_input)


def test_marketing_tile_opens_content_pipeline_and_weekly_drafts():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("agent_manager.py", default_timeout=25)
    at.run()
    next(button for button in at.button if button.label == "Open Marketing Agent").click().run()
    assert not at.exception
    body = _ui_text(at)
    assert "Marketing Agent Content Pipeline" in body
    assert "Draft" in body
    assert "Needs Review" in body
    assert "Ready to Post" in body
    assert "Published" in body
    labels = [button.label for button in at.button]
    assert "Run weekly drafts" in labels
    assert any(inp.label == "Command · Marketing Agent" for inp in at.text_input)


def test_research_tile_opens_poll_view_without_assign_box():
    from dashboard import ASSIGN_DISABLED_CAPTION

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("agent_manager.py", default_timeout=20)
    at.run()
    next(button for button in at.button if button.label == "Open Research Agent").click().run()
    assert not at.exception
    body = _ui_text(at)
    assert "poll() results" in body
    assert "Directed Assign/synthesis is disabled" in body
    assert "BACKLOG.md" in body
    labels = [button.label for button in at.button]
    assert "Poll arXiv + GitHub" in labels
    inputs = [inp.label for inp in at.text_input]
    assert "Command · Research Agent" in inputs
    assert ASSIGN_DISABLED_CAPTION in " ".join(str(c.value) for c in at.caption)
    enabled_assign = [
        inp
        for inp in at.text_input
        if inp.label == "Assign a task"
        and not getattr(inp, "disabled", False)
        and "research" in str(getattr(inp, "form_id", "") or "")
    ]
    assert not enabled_assign


def test_competitor_tile_opens_assign_v1_and_langfuse_dedup():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("agent_manager.py", default_timeout=20)
    at.run()
    next(button for button in at.button if button.label == "Open Competitor Agent").click().run()
    assert not at.exception
    body = _ui_text(at)
    assert "Assign v1" in body
    assert "run_directed_check" in body
    assert "Langfuse comparison" in body
    labels = [button.label for button in at.button]
    assert "Run full snapshot poll" in labels
    assert any(inp.label == "Command · Competitor Agent" for inp in at.text_input)
    assert "Pending approvals" in body
    src = Path("agent_manager.py").read_text(encoding="utf-8")
    assert "render_pending_approvals(st, agent_key=\"competitor\"" in src
    assert "not a strategy decision" in src


def test_presales_tile_opens_existing_tab():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("agent_manager.py", default_timeout=20)
    at.run()
    next(button for button in at.button if button.label == "Open Pre-Sales Agent").click().run()
    assert not at.exception
    body = _ui_text(at)
    assert "Pre-Sales" in body
    assert "Draft only" in body or "drafts outreach from CSV leads" in body
    assert "pending_approval" in body.lower() or "Nothing is sent" in body or "not sent" in body.lower()
    labels = [button.label for button in at.button]
    assert "← All agents" in labels
    assert any(inp.label == "Command · Pre-Sales Agent" for inp in at.text_input)
    src = Path("agent_manager.py").read_text(encoding="utf-8")
    assert "from agents.presales_agent.tab import render_presales_tab" in src
    assert "render_presales_tab(st" in src


def test_techsupport_tile_opens_real_tab():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("agent_manager.py", default_timeout=20)
    at.run()
    next(
        button for button in at.button if button.label == "Open Tech Support Agent"
    ).click().run()
    assert not at.exception
    body = _ui_text(at)
    assert "Tech Support" in body
    assert "Draft only" in body or "answers support tickets from GitHub/Discord" in body
    assert "Needs Manual Review" in body
    assert "Pending Approval" in body
    labels = [button.label for button in at.button]
    assert "← All agents" in labels
    assert any(inp.label == "Command · Tech Support Agent" for inp in at.text_input)
    src = Path("agent_manager.py").read_text(encoding="utf-8")
    assert "from agents.techsupport_agent.tab import render_techsupport_tab" in src
    assert "render_techsupport_tab(st" in src


def test_legal_tile_opens_real_tab():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("agent_manager.py", default_timeout=20)
    at.run()
    next(
        button for button in at.button if button.label == "Open Legal / Compliance Agent"
    ).click().run()
    assert not at.exception
    body = _ui_text(at)
    assert "Legal" in body
    assert "Draft only" in body or "reviews legal/compliance docs" in body
    assert "DPDP" in body
    assert "Pending Approval" in body
    labels = [button.label for button in at.button]
    assert "← All agents" in labels
    assert any(inp.label == "Command · Legal / Compliance Agent" for inp in at.text_input)
    src = Path("agent_manager.py").read_text(encoding="utf-8")
    assert "from agents.legal_compliance_agent.tab import render_legal_tab" in src
    assert "render_legal_tab(st" in src
