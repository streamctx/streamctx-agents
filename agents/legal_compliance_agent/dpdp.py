"""DPDP checklist mapped to observed StreamCtx agent/SDK behavior.

Flags gaps. Does not invent compliance coverage.
"""

from __future__ import annotations

import hashlib

from agents.legal_compliance_agent.models import (
    AREA_BREACH,
    AREA_CONSENT,
    AREA_LOCALIZATION,
    AREA_RIGHTS,
    CHECKLIST_GAP,
    CHECKLIST_UNKNOWN,
    KIND_DPDP_GAP,
    SEVERITY_HIGH,
    ScanFinding,
)
from agents.legal_compliance_agent.scan_docs import CodeFacts, PolicyDoc

DPDP_ITEMS = (
    {
        "item_id": "dpdp-localization",
        "area": AREA_LOCALIZATION,
        "requirement": (
            "Data localization — personal data of Indian data principals stored "
            "and processed as required under DPDP (and any government-notified "
            "categories that must stay in India)."
        ),
    },
    {
        "item_id": "dpdp-consent",
        "area": AREA_CONSENT,
        "requirement": (
            "Consent — free, specific, informed, unconditional, and unambiguous "
            "consent with a notice describing personal data and purpose; withdrawable."
        ),
    },
    {
        "item_id": "dpdp-breach",
        "area": AREA_BREACH,
        "requirement": (
            "Breach notification — notify the Data Protection Board and affected "
            "data principals within the timelines prescribed under DPDP rules."
        ),
    },
    {
        "item_id": "dpdp-rights",
        "area": AREA_RIGHTS,
        "requirement": (
            "Data principal rights — access, correction, erasure, and grievance "
            "redressal mechanisms for personal data the product processes."
        ),
    },
)


def _fingerprint(kind: str, source_path: str, title: str) -> str:
    raw = f"{kind}|{source_path}|{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _gap_finding(area: str, title: str, evidence: str, suggested: str) -> ScanFinding:
    return ScanFinding(
        kind=KIND_DPDP_GAP,
        severity=SEVERITY_HIGH,
        title=title,
        evidence=evidence,
        suggested_language=suggested,
        source_path=f"DPDP:{area}",
        source_fingerprint=_fingerprint(KIND_DPDP_GAP, f"DPDP:{area}", title),
    )


def _privacy_mentions_india(docs: dict[str, PolicyDoc]) -> bool:
    privacy = docs.get("PRIVACY.md")
    if privacy is None or not privacy.found:
        return False
    text = privacy.text.lower()
    return any(token in text for token in ("india", "dpdp", "data principal", "residen"))


def build_dpdp_checklist(
    facts: CodeFacts,
    docs: dict[str, PolicyDoc],
) -> tuple[list[dict], list[ScanFinding]]:
    """Return checklist rows (for storage) and gap findings.

    Status is gap unless a real control is observed. Local SQLite is noted as
    behavior, not as DPDP localization compliance.
    """
    rows: list[dict] = []
    findings: list[ScanFinding] = []

    sqlite_note = (
        ", ".join(facts.sqlite_refs[:6])
        if facts.sqlite_refs
        else "(no SQLite home references found)"
    )
    openrouter_note = (
        ", ".join(facts.openrouter_refs[:6])
        if facts.openrouter_refs
        else "(no OpenRouter references found)"
    )

    loc_behavior = (
        f"Default agent stores are local SQLite under STREAMCTX_HOME / ~/.streamctx "
        f"({sqlite_note}). OpenRouter is a remote LLM API ({openrouter_note}). "
        "No India-region hosting flag or data-residency switch was found in this "
        "agents checkout."
    )
    loc_gap = (
        "Local-first SQLite is not the same as DPDP localization. Third-party "
        "OpenRouter processing is not documented as India-resident. Do not claim "
        "a DPDP localization edition until residency, subprocessors, and "
        "significant data are mapped by counsel."
    )
    rows.append(
        {
            "item_id": "dpdp-localization",
            "area": AREA_LOCALIZATION,
            "requirement": DPDP_ITEMS[0]["requirement"],
            "current_behavior": loc_behavior,
            "gap": loc_gap,
            "status": CHECKLIST_GAP,
        }
    )
    findings.append(
        _gap_finding(
            AREA_LOCALIZATION,
            "DPDP gap: no data-localization / India-residency control",
            loc_behavior,
            "For an India DPDP Compliance Edition, document: where each SQLite file "
            "lives, which subprocessors (OpenRouter, Discord, GitHub) receive which "
            "fields, and whether any significant personal data is transferred outside "
            "India. Do not publish 'data stays in India' unless that is true for every "
            "enabled integration.",
        )
    )

    consent_status = CHECKLIST_GAP
    consent_behavior = (
        "No product consent-notice or withdraw-consent UI was found in scanned "
        "agent code. GitHub issues and Discord messages can be ingested without a "
        "DPDP-style notice in this repo. "
        f"Consent-related hits outside the legal agent: {facts.consent_refs or 'none'}."
    )
    if facts.consent_refs:
        consent_status = CHECKLIST_UNKNOWN
        consent_behavior += (
            " Hits exist but were not verified as a DPDP consent manager — treat as "
            "unknown, not covered."
        )
    consent_gap = (
        "There is no implementable consent record (purpose, notice, withdrawal) "
        "for agent ingest of Discord/GitHub content or OpenRouter prompts."
    )
    rows.append(
        {
            "item_id": "dpdp-consent",
            "area": AREA_CONSENT,
            "requirement": DPDP_ITEMS[1]["requirement"],
            "current_behavior": consent_behavior,
            "gap": consent_gap,
            "status": consent_status,
        }
    )
    findings.append(
        _gap_finding(
            AREA_CONSENT,
            "DPDP gap: no consent notice or withdrawal mechanism",
            consent_behavior,
            "Draft a notice covering: what personal data agents store locally, what "
            "is sent to OpenRouter, and that Discord/GitHub polling is optional. "
            "Counsel must confirm whether Discord/GitHub ToS plus this product need "
            "separate DPDP consent. Do not ship a fake 'I agree' checkbox as coverage.",
        )
    )

    breach_behavior = (
        "No breach-notification playbook, timer, or Data Protection Board contact "
        f"flow was found. Hits: {facts.breach_refs or 'none'}."
    )
    rows.append(
        {
            "item_id": "dpdp-breach",
            "area": AREA_BREACH,
            "requirement": DPDP_ITEMS[2]["requirement"],
            "current_behavior": breach_behavior,
            "gap": (
                "DPDP rules will set notification timelines. This agents repo has no "
                "incident-response automation or documented Board notification SLA."
            ),
            "status": CHECKLIST_GAP,
        }
    )
    findings.append(
        _gap_finding(
            AREA_BREACH,
            "DPDP gap: no breach-notification timeline or playbook",
            breach_behavior,
            "Prepare a founder-owned incident checklist: detect, contain, assess "
            "personal data involved (local SQLite + any OpenRouter logs), notify the "
            "Board and principals within the statutory window once rules are in force. "
            "This agent will not invent a timeline that is not in the Act/rules.",
        )
    )

    rights_behavior = (
        "No data-principal access/correction/erasure API or export of ~/.streamctx "
        f"SQLite was found. Hits: {facts.rights_refs or 'none'}."
    )
    rows.append(
        {
            "item_id": "dpdp-rights",
            "area": AREA_RIGHTS,
            "requirement": DPDP_ITEMS[3]["requirement"],
            "current_behavior": rights_behavior,
            "gap": (
                "Operators can delete local files by hand; that is not a productized "
                "rights workflow or grievance officer path."
            ),
            "status": CHECKLIST_GAP,
        }
    )
    findings.append(
        _gap_finding(
            AREA_RIGHTS,
            "DPDP gap: no data-principal access/erasure workflow",
            rights_behavior,
            "For a DPDP edition, specify how a principal requests a copy or erasure of "
            "agent-stored data (sessions.db, support_tickets.db, leads.db, etc.) and "
            "who the grievance contact is. Until that exists, list it as a gap.",
        )
    )

    if not _privacy_mentions_india(docs):
        # Already covered by missing PRIVACY / localization gap; no extra row.
        pass

    return rows, findings
