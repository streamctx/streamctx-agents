"""Locate policy markdown and compare claims to observed code behavior."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from agents.legal_compliance_agent.models import (
    KIND_INCONSISTENCY,
    KIND_MISSING_DOC,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    ScanFinding,
)
from agents.legal_compliance_agent.settings import POLICY_FILENAMES

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        "dist",
        "build",
    }
)

OPENROUTER_RE = re.compile(r"openrouter\.ai|OPENROUTER_API_KEY", re.I)
DISCORD_RE = re.compile(r"discord\.com/api|DISCORD_BOT_TOKEN", re.I)
GITHUB_TOKEN_RE = re.compile(r"GITHUB_TOKEN|GH_TOKEN|api\.github\.com", re.I)
SQLITE_HOME_RE = re.compile(r"STREAMCTX_HOME|\.streamctx|sessions\.db", re.I)
EVIDENCE_LEDGER_RE = re.compile(r"evidence_ledger", re.I)
CONSENT_RE = re.compile(r"\b(consent(?:_|\s)?(?:notice|manager|given|withdraw)|opt[- ]in)\b", re.I)
BREACH_RE = re.compile(r"\bbreach notification\b", re.I)
ERASURE_RE = re.compile(r"\b(right to erasure|data export|delete my data|dsar)\b", re.I)


@dataclass
class CodeFacts:
    sqlite_refs: tuple[str, ...] = ()
    openrouter_refs: tuple[str, ...] = ()
    discord_refs: tuple[str, ...] = ()
    github_api_refs: tuple[str, ...] = ()
    evidence_ledger_refs: tuple[str, ...] = ()
    consent_refs: tuple[str, ...] = ()
    breach_refs: tuple[str, ...] = ()
    rights_refs: tuple[str, ...] = ()
    scanned_files: int = 0


@dataclass
class PolicyDoc:
    name: str
    path: Optional[Path]
    text: str = ""

    @property
    def found(self) -> bool:
        return self.path is not None and self.path.is_file()


def _fingerprint(kind: str, source_path: str, title: str) -> str:
    raw = f"{kind}|{source_path}|{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def scan_roots(agents_root: Path | str, product_root: Path | str) -> list[Path]:
    roots: list[Path] = []
    for raw in (agents_root, product_root):
        path = Path(raw)
        if path.exists() and path not in roots:
            roots.append(path)
    return roots


def locate_policy_docs(roots: Iterable[Path | str]) -> dict[str, PolicyDoc]:
    found: dict[str, PolicyDoc] = {
        name: PolicyDoc(name=name, path=None) for name in POLICY_FILENAMES
    }
    for raw in roots:
        root = Path(raw)
        if not root.exists():
            continue
        candidates = [
            root / name for name in POLICY_FILENAMES
        ] + [
            root / "docs" / name for name in POLICY_FILENAMES
        ]
        for path in candidates:
            if path.is_file() and found[path.name].path is None:
                text = path.read_text(encoding="utf-8", errors="replace")
                found[path.name] = PolicyDoc(name=path.name, path=path, text=text)
    return found


def _rel(path: Path, roots: Iterable[Path]) -> str:
    for root in roots:
        try:
            return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
        except ValueError:
            continue
    return str(path).replace("\\", "/")


def collect_code_facts(roots: Iterable[Path | str], *, max_files: int = 400) -> CodeFacts:
    sqlite: list[str] = []
    openrouter: list[str] = []
    discord: list[str] = []
    github: list[str] = []
    ledger: list[str] = []
    consent: list[str] = []
    breach: list[str] = []
    rights: list[str] = []
    scanned = 0
    root_paths = [Path(raw) for raw in roots if Path(raw).exists()]

    for root in root_paths:
        for path in root.rglob("*"):
            if scanned >= max_files:
                break
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            if path.suffix.lower() not in {".py", ".md"}:
                continue
            if "tests" in path.parts and path.suffix == ".py":
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            label = _rel(path, root_paths)
            if SQLITE_HOME_RE.search(text):
                sqlite.append(label)
            if OPENROUTER_RE.search(text):
                openrouter.append(label)
            if DISCORD_RE.search(text):
                discord.append(label)
            if GITHUB_TOKEN_RE.search(text):
                github.append(label)
            if "legal_compliance_agent" not in path.parts:
                if EVIDENCE_LEDGER_RE.search(text):
                    ledger.append(label)
                if CONSENT_RE.search(text) and path.suffix == ".py":
                    consent.append(label)
                if BREACH_RE.search(text):
                    breach.append(label)
                if ERASURE_RE.search(text):
                    rights.append(label)
        if scanned >= max_files:
            break

    def _uniq(items: list[str], limit: int = 12) -> tuple[str, ...]:
        seen: list[str] = []
        for item in items:
            if item not in seen:
                seen.append(item)
            if len(seen) >= limit:
                break
        return tuple(seen)

    return CodeFacts(
        sqlite_refs=_uniq(sqlite),
        openrouter_refs=_uniq(openrouter),
        discord_refs=_uniq(discord),
        github_api_refs=_uniq(github),
        evidence_ledger_refs=_uniq(ledger),
        consent_refs=_uniq(consent),
        breach_refs=_uniq(breach),
        rights_refs=_uniq(rights),
        scanned_files=scanned,
    )


def _finding(
    *,
    kind: str,
    severity: str,
    title: str,
    evidence: str,
    suggested_language: str,
    source_path: str,
) -> ScanFinding:
    return ScanFinding(
        kind=kind,
        severity=severity,
        title=title,
        evidence=evidence,
        suggested_language=suggested_language,
        source_path=source_path,
        source_fingerprint=_fingerprint(kind, source_path, title),
    )


def missing_doc_findings(
    docs: dict[str, PolicyDoc],
    facts: Optional[CodeFacts] = None,
) -> list[ScanFinding]:
    facts = facts or CodeFacts()
    processors: list[str] = []
    if facts.sqlite_refs:
        processors.append("local SQLite under ~/.streamctx/")
    if facts.openrouter_refs:
        processors.append("OpenRouter (LLM drafts)")
    if facts.discord_refs:
        processors.append("Discord (read-only poll)")
    if facts.github_api_refs:
        processors.append("GitHub API (read-only)")
    processor_note = (
        " Observed processors/stores: " + "; ".join(processors) + "."
        if processors
        else ""
    )
    extra = {
        "PRIVACY.md": (
            " Describe local SQLite, optional OpenRouter, and optional GitHub/"
            "Discord polling. Do not claim local-only processing if those APIs run."
            + processor_note
        ),
        "TERMS.md": (
            " State that agent output is draft-only, not legal advice, and is not "
            "published until a human approves it."
        ),
        "COMPLIANCE_VERIFICATION.md": (
            " Map each privacy/terms claim to the file that implements it. Mark "
            "unimplemented controls as gaps."
        ),
        "DEPLOYMENT.md": (
            " Document STREAMCTX_HOME / ~/.streamctx, third-party API env vars, "
            "and that this agents repo has no India data-residency switch."
        ),
    }
    findings: list[ScanFinding] = []
    for name, doc in docs.items():
        if doc.found:
            continue
        findings.append(
            _finding(
                kind=KIND_MISSING_DOC,
                severity=SEVERITY_HIGH,
                title=f"Missing policy document: {name}",
                evidence=(
                    f"{name} was not found in the agents repo, docs/, or the "
                    "StreamCtx product root. A compliance scan cannot verify "
                    "claims that do not exist as published policy."
                    + processor_note
                ),
                suggested_language=(
                    f"Draft a {name} (or confirm the canonical location) describing "
                    "actual StreamCtx behavior only. Do not copy competitor policies. "
                    "Have qualified counsel review before publishing."
                    + extra.get(name, "")
                ),
                source_path=name,
            )
        )
    return findings


def _mentions(text: str, *needles: str) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


def inconsistency_findings(
    docs: dict[str, PolicyDoc],
    facts: CodeFacts,
) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    privacy = docs.get("PRIVACY.md")
    terms = docs.get("TERMS.md")
    compliance = docs.get("COMPLIANCE_VERIFICATION.md")
    deployment = docs.get("DEPLOYMENT.md")

    if privacy and privacy.found:
        if facts.openrouter_refs and not _mentions(
            privacy.text, "openrouter", "llm provider", "llm api"
        ):
            findings.append(
                _finding(
                    kind=KIND_INCONSISTENCY,
                    severity=SEVERITY_HIGH,
                    title="PRIVACY.md does not disclose OpenRouter as a processor",
                    evidence=(
                        "Code sends prompts/draft text to OpenRouter "
                        f"({', '.join(facts.openrouter_refs[:5])}). PRIVACY.md does "
                        "not mention OpenRouter or equivalent third-party LLM processing."
                    ),
                    suggested_language=(
                        "When you use StreamCtx agents that generate drafts, ticket "
                        "text or prompts may be sent to OpenRouter (or another LLM "
                        "provider you configure) to produce suggested copy. That "
                        "provider's processing is outside StreamCtx's local SQLite "
                        "store. Do not claim local-only processing if this path is enabled."
                    ),
                    source_path=str(privacy.path),
                )
            )
        if facts.sqlite_refs and not _mentions(
            privacy.text, "sqlite", ".streamctx", "local", "device"
        ):
            findings.append(
                _finding(
                    kind=KIND_INCONSISTENCY,
                    severity=SEVERITY_MEDIUM,
                    title="PRIVACY.md does not describe local SQLite storage",
                    evidence=(
                        "Agents persist data under STREAMCTX_HOME / ~/.streamctx "
                        f"({', '.join(facts.sqlite_refs[:5])}). PRIVACY.md does not "
                        "describe this local store."
                    ),
                    suggested_language=(
                        "By default, StreamCtx agents store operational data in local "
                        "SQLite files under ~/.streamctx/ (for example sessions.db and "
                        "per-agent queues). This is on the machine that runs the agents, "
                        "not a StreamCtx-hosted cloud database."
                    ),
                    source_path=str(privacy.path),
                )
            )
        if facts.discord_refs and not _mentions(privacy.text, "discord"):
            findings.append(
                _finding(
                    kind=KIND_INCONSISTENCY,
                    severity=SEVERITY_MEDIUM,
                    title="PRIVACY.md does not mention Discord message ingest",
                    evidence=(
                        "Tech support polls Discord and stores message bodies "
                        f"({', '.join(facts.discord_refs[:4])}). PRIVACY.md does not "
                        "mention Discord."
                    ),
                    suggested_language=(
                        "If the tech-support agent is enabled, Discord channel messages "
                        "may be fetched (read-only) and stored locally so a human can "
                        "review a drafted reply. StreamCtx agents do not post to Discord."
                    ),
                    source_path=str(privacy.path),
                )
            )
        if _mentions(privacy.text, "we do not share", "never leave", "no third party"):
            if facts.openrouter_refs or facts.discord_refs:
                findings.append(
                    _finding(
                        kind=KIND_INCONSISTENCY,
                        severity=SEVERITY_HIGH,
                        title="PRIVACY.md local-only claim conflicts with third-party APIs",
                        evidence=(
                            "PRIVACY.md contains language implying data does not leave "
                            "the device or is not shared, but the codebase calls "
                            "OpenRouter and/or Discord."
                        ),
                        suggested_language=(
                            "Replace absolute 'never leaves the device' wording with a "
                            "list of processors that actually run when those agents are "
                            "configured (OpenRouter, Discord, GitHub). Counsel should "
                            "confirm the final list."
                        ),
                        source_path=str(privacy.path),
                    )
                )
        if _mentions(privacy.text, "evidence_ledger") and not facts.evidence_ledger_refs:
            findings.append(
                _finding(
                    kind=KIND_INCONSISTENCY,
                    severity=SEVERITY_MEDIUM,
                    title="PRIVACY.md references evidence_ledger.db but code scan found none",
                    evidence=(
                        "PRIVACY.md mentions evidence_ledger, but no matching file or "
                        "symbol was found in the scanned agents/product trees."
                    ),
                    suggested_language=(
                        "Either document the real evidence/session store (today: "
                        "sessions.db under ~/.streamctx) or point at the SDK path that "
                        "owns evidence_ledger.db. Do not describe a ledger that this "
                        "checkout does not implement."
                    ),
                    source_path=str(privacy.path),
                )
            )
    if terms and terms.found:
        if facts.openrouter_refs and not _mentions(
            terms.text, "openrouter", "llm", "ai provider"
        ):
            findings.append(
                _finding(
                    kind=KIND_INCONSISTENCY,
                    severity=SEVERITY_MEDIUM,
                    title="TERMS.md does not mention third-party LLM processing",
                    evidence=(
                        "Agents call OpenRouter for generation. TERMS.md does not "
                        "disclose that dependency."
                    ),
                    suggested_language=(
                        "Optional agent features may send user-provided or ingested text "
                        "to a third-party LLM API (currently OpenRouter unless you "
                        "configure otherwise). StreamCtx is not a law firm and agent "
                        "output is not legal, medical, or financial advice."
                    ),
                    source_path=str(terms.path),
                )
            )
    if compliance and compliance.found:
        if _mentions(compliance.text, "evidence_ledger") and not facts.evidence_ledger_refs:
            findings.append(
                _finding(
                    kind=KIND_INCONSISTENCY,
                    severity=SEVERITY_HIGH,
                    title="COMPLIANCE_VERIFICATION.md cites evidence_ledger.db but it is absent",
                    evidence=(
                        "COMPLIANCE_VERIFICATION.md discusses evidence_ledger.db, but "
                        "the scanned trees have no evidence_ledger symbol or file. "
                        "This agents repo uses sessions.db via streamctx.storage."
                    ),
                    suggested_language=(
                        "Update COMPLIANCE_VERIFICATION.md to the stores that actually "
                        "exist (sessions.db and per-agent SQLite files under ~/.streamctx). "
                        "If evidence_ledger.db lives only in the SDK, say so and link that "
                        "path. Do not imply this agents checkout writes that file."
                    ),
                    source_path=str(compliance.path),
                )
            )
    if deployment and deployment.found:
        if facts.openrouter_refs and _mentions(
            deployment.text, "air-gapped", "no network", "offline only"
        ):
            findings.append(
                _finding(
                    kind=KIND_INCONSISTENCY,
                    severity=SEVERITY_HIGH,
                    title="DEPLOYMENT.md offline claim conflicts with OpenRouter usage",
                    evidence=(
                        "DEPLOYMENT.md suggests an offline/air-gapped mode, but the "
                        "agents call OpenRouter when keys are set."
                    ),
                    suggested_language=(
                        "If a fully offline mode exists, name the flags that disable "
                        "OpenRouter, Discord, and GitHub. If those calls still happen "
                        "when keys are present, do not claim air-gapped operation."
                    ),
                    source_path=str(deployment.path),
                )
            )

    return findings


def scan_docs(
    *,
    agents_root: Path | str,
    product_root: Path | str,
    facts: Optional[CodeFacts] = None,
) -> tuple[list[ScanFinding], dict[str, PolicyDoc], CodeFacts]:
    roots = scan_roots(agents_root, product_root)
    docs = locate_policy_docs(roots)
    code_facts = facts or collect_code_facts(roots)
    findings = missing_doc_findings(docs, code_facts) + inconsistency_findings(
        docs, code_facts
    )
    return findings, docs, code_facts
