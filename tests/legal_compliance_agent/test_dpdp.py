"""DPDP checklist maps observed behavior and does not invent coverage."""

from __future__ import annotations

from agents.legal_compliance_agent.dpdp import DPDP_ITEMS, build_dpdp_checklist
from agents.legal_compliance_agent.models import (
    AREA_BREACH,
    AREA_CONSENT,
    AREA_LOCALIZATION,
    AREA_RIGHTS,
    CHECKLIST_COVERED,
    KIND_DPDP_GAP,
)
from agents.legal_compliance_agent.scan_docs import CodeFacts, PolicyDoc


def test_dpdp_checklist_has_four_areas_and_flags_gaps():
    facts = CodeFacts(
        sqlite_refs=("shared/config.py",),
        openrouter_refs=("agents/techsupport_agent/draft.py",),
        discord_refs=("agents/techsupport_agent/discord.py",),
        github_api_refs=("agents/techsupport_agent/github_issues.py",),
        scanned_files=10,
    )
    docs = {
        name: PolicyDoc(name=name, path=None)
        for name in (
            "TERMS.md",
            "PRIVACY.md",
            "COMPLIANCE_VERIFICATION.md",
            "DEPLOYMENT.md",
        )
    }
    rows, findings = build_dpdp_checklist(facts, docs)
    areas = {row["area"] for row in rows}
    assert areas == {AREA_LOCALIZATION, AREA_CONSENT, AREA_BREACH, AREA_RIGHTS}
    assert len(DPDP_ITEMS) == 4
    assert all(row["status"] != CHECKLIST_COVERED for row in rows)
    assert all(row["status"] in {"gap", "unknown"} for row in rows)
    assert all(item.kind == KIND_DPDP_GAP for item in findings)
    loc = next(row for row in rows if row["area"] == AREA_LOCALIZATION)
    assert "openrouter" in loc["current_behavior"].lower()
    assert "not the same as dpdp" in loc["gap"].lower() or "do not claim" in loc["gap"].lower()
