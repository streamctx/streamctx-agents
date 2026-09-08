"""Read-only GitHub PR/commit scan for data-handling changes without policy updates.

GET only. Never comments, never opens PRs, never writes files.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Optional
from urllib.parse import urlencode

from agents.competitor_agent.http import fetch_json, github_headers
from agents.competitor_agent.settings import github_token
from agents.legal_compliance_agent.models import (
    KIND_STALE_POLICY,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    ScanFinding,
)
from agents.legal_compliance_agent.settings import LegalConfig

GITHUB_API = "https://api.github.com/repos/{repo}"
FetchJsonFn = Callable[[str], Any]

DATA_FILE_RE = re.compile(
    r"(storage\.py|sessions\.db|evidence_ledger|\.env|sqlite|"
    r"pending_approval\.py|STREAMCTX_HOME)",
    re.I,
)
POLICY_FILE_RE = re.compile(
    r"(TERMS|PRIVACY|COMPLIANCE_VERIFICATION|DEPLOYMENT)\.md$",
    re.I,
)


def _fingerprint(kind: str, source_path: str, title: str) -> str:
    raw = f"{kind}|{source_path}|{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _finding(
    *,
    severity: str,
    title: str,
    evidence: str,
    suggested_language: str,
    source_path: str,
) -> ScanFinding:
    return ScanFinding(
        kind=KIND_STALE_POLICY,
        severity=severity,
        title=title,
        evidence=evidence,
        suggested_language=suggested_language,
        source_path=source_path,
        source_fingerprint=_fingerprint(KIND_STALE_POLICY, source_path, title),
    )


def _filenames(files: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(files, list):
        return names
    for row in files:
        if not isinstance(row, dict):
            continue
        name = str(row.get("filename") or "").strip()
        if name:
            names.append(name)
    return names


def _data_files(names: list[str]) -> list[str]:
    return [name for name in names if DATA_FILE_RE.search(name)]


def _policy_files(names: list[str]) -> list[str]:
    return [name for name in names if POLICY_FILE_RE.search(name.split("/")[-1])]


def _headers(config: LegalConfig, token: Optional[str] = None) -> dict[str, str]:
    return github_headers(config.user_agent, token if token is not None else github_token())


def _get(url: str, *, fetch_fn: Optional[FetchJsonFn], config: LegalConfig) -> Any:
    if fetch_fn is not None:
        return fetch_fn(url)
    return fetch_json(
        url,
        headers=_headers(config),
        max_retries=config.max_retries,
        backoff_base_seconds=config.backoff_base_seconds,
        max_backoff_seconds=config.max_backoff_seconds,
    )


def fetch_open_pulls(config: LegalConfig, *, fetch_fn: Optional[FetchJsonFn] = None) -> list[dict]:
    params = urlencode(
        {
            "state": "open",
            "per_page": str(config.github_per_page),
            "sort": "updated",
            "direction": "desc",
        }
    )
    url = f"{GITHUB_API.format(repo=config.github_repo)}/pulls?{params}"
    payload = _get(url, fetch_fn=fetch_fn, config=config)
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def fetch_pull_files(
    config: LegalConfig,
    number: int,
    *,
    fetch_fn: Optional[FetchJsonFn] = None,
) -> list[str]:
    url = f"{GITHUB_API.format(repo=config.github_repo)}/pulls/{int(number)}/files"
    return _filenames(_get(url, fetch_fn=fetch_fn, config=config))


def fetch_recent_commits(
    config: LegalConfig, *, fetch_fn: Optional[FetchJsonFn] = None
) -> list[dict]:
    params = urlencode({"per_page": str(min(config.github_per_page, 15))})
    url = f"{GITHUB_API.format(repo=config.github_repo)}/commits?{params}"
    payload = _get(url, fetch_fn=fetch_fn, config=config)
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def fetch_commit_files(
    config: LegalConfig,
    sha: str,
    *,
    fetch_fn: Optional[FetchJsonFn] = None,
) -> list[str]:
    url = f"{GITHUB_API.format(repo=config.github_repo)}/commits/{sha}"
    payload = _get(url, fetch_fn=fetch_fn, config=config)
    if not isinstance(payload, dict):
        return []
    return _filenames(payload.get("files"))


def _stale_finding(
    *,
    label: str,
    url: str,
    data_names: list[str],
    policy_names: list[str],
) -> ScanFinding:
    touched = ", ".join(data_names[:8])
    return _finding(
        severity=SEVERITY_HIGH if not policy_names else SEVERITY_MEDIUM,
        title=f"Data-handling change without TERMS/PRIVACY update ({label})",
        evidence=(
            f"{label} touches data-handling files ({touched}) but does not also "
            "change TERMS.md, PRIVACY.md, COMPLIANCE_VERIFICATION.md, or "
            f"DEPLOYMENT.md. Source: {url}"
        ),
        suggested_language=(
            "If this change alters what is stored, where it is stored, or which "
            "third party receives it, counsel should update PRIVACY.md / TERMS.md "
            "in the same change. If the behavior is unchanged, add a one-line note "
            "in COMPLIANCE_VERIFICATION.md that the claim was re-checked."
        ),
        source_path=url or label,
    )


def scan_github_changes(
    config: LegalConfig,
    *,
    fetch_fn: Optional[FetchJsonFn] = None,
) -> tuple[list[ScanFinding], list[str]]:
    """Return stale-policy findings and non-fatal errors. Read-only."""
    findings: list[ScanFinding] = []
    errors: list[str] = []
    seen: set[str] = set()

    try:
        pulls = fetch_open_pulls(config, fetch_fn=fetch_fn)
    except Exception as exc:
        errors.append(f"github pulls: {exc}")
        pulls = []

    for pull in pulls:
        number = pull.get("number")
        html_url = str(pull.get("html_url") or "").strip()
        title = str(pull.get("title") or f"PR {number}")
        if number is None:
            continue
        try:
            names = fetch_pull_files(config, int(number), fetch_fn=fetch_fn)
        except Exception as exc:
            errors.append(f"github pr#{number} files: {exc}")
            continue
        data_names = _data_files(names)
        if not data_names:
            continue
        policy_names = _policy_files(names)
        if policy_names:
            continue
        label = f"PR #{int(number)} {title}".strip()
        finding = _stale_finding(
            label=label,
            url=html_url,
            data_names=data_names,
            policy_names=policy_names,
        )
        if finding.source_fingerprint not in seen:
            seen.add(finding.source_fingerprint)
            findings.append(finding)

    try:
        commits = fetch_recent_commits(config, fetch_fn=fetch_fn)
    except Exception as exc:
        errors.append(f"github commits: {exc}")
        commits = []

    for commit in commits:
        sha = str(commit.get("sha") or "")
        html_url = str(commit.get("html_url") or "").strip()
        message = ""
        commit_obj = commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
        message = str(commit_obj.get("message") or "").splitlines()[0]
        if not sha:
            continue
        try:
            names = fetch_commit_files(config, sha, fetch_fn=fetch_fn)
        except Exception as exc:
            errors.append(f"github commit {sha[:8]} files: {exc}")
            continue
        data_names = _data_files(names)
        if not data_names:
            continue
        if _policy_files(names):
            continue
        label = f"commit {sha[:8]} {message}".strip()
        finding = _stale_finding(
            label=label,
            url=html_url or sha,
            data_names=data_names,
            policy_names=[],
        )
        if finding.source_fingerprint not in seen:
            seen.add(finding.source_fingerprint)
            findings.append(finding)

    return findings, errors
