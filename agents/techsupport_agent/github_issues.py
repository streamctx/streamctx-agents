"""GitHub Issues poll — read-only. Never comments, never closes, never writes.

Reuses competitor_agent GitHub auth (``GITHUB_TOKEN`` / ``GH_TOKEN``) and
``github_headers`` / ``fetch_json``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from urllib.parse import urlencode

from agents.competitor_agent.http import fetch_json, github_headers
from agents.competitor_agent.settings import github_token
from agents.techsupport_agent.models import SOURCE_GITHUB, IncomingItem
from agents.techsupport_agent.settings import TechSupportConfig

GITHUB_ISSUES_URL = "https://api.github.com/repos/{repo}/issues"

FetchJsonFn = Callable[[str], Any]


def issues_url(repo: str, *, per_page: int, state: str = "open") -> str:
    params = urlencode(
        {
            "state": state,
            "per_page": str(per_page),
            "sort": "created",
            "direction": "desc",
        }
    )
    return f"{GITHUB_ISSUES_URL.format(repo=repo)}?{params}"


def fingerprint(repo: str, number: int) -> str:
    return f"github:{repo}#{int(number)}"


def parse_issues(payload: Any, *, repo: str) -> list[IncomingItem]:
    if not isinstance(payload, list):
        return []
    items: list[IncomingItem] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        if row.get("pull_request"):
            continue
        number = row.get("number")
        title = str(row.get("title") or "").strip()
        html_url = str(row.get("html_url") or "").strip()
        if number is None or not title or not html_url:
            continue
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        author = str(user.get("login") or "").strip()
        labels = tuple(
            str(label.get("name") or "").strip().lower()
            for label in (row.get("labels") or [])
            if isinstance(label, dict) and str(label.get("name") or "").strip()
        )
        items.append(
            IncomingItem(
                source=SOURCE_GITHUB,
                source_ref=str(int(number)),
                source_url=html_url,
                title=title,
                body=str(row.get("body") or "").strip(),
                author=author,
                source_fingerprint=fingerprint(repo, int(number)),
                labels=labels,
            )
        )
    return items


def fetch_open_issues(
    config: TechSupportConfig,
    *,
    fetch_fn: Optional[FetchJsonFn] = None,
    token: Optional[str] = None,
) -> list[IncomingItem]:
    """GET open issues. Read-only — no comment/create/patch endpoints."""
    repo = (config.github_repo or "").strip()
    if not repo:
        return []
    url = issues_url(repo, per_page=config.github_per_page)
    if fetch_fn is not None:
        payload = fetch_fn(url)
    else:
        auth = token if token is not None else github_token()
        payload = fetch_json(
            url,
            headers=github_headers(config.user_agent, auth),
            max_retries=config.max_retries,
            backoff_base_seconds=config.backoff_base_seconds,
            max_backoff_seconds=config.max_backoff_seconds,
        )
    return parse_issues(payload, repo=repo)
