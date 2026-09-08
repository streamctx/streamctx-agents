"""GitHub PR/commit scan flags data-handling changes without policy updates."""

from __future__ import annotations

from agents.legal_compliance_agent.github_changes import scan_github_changes
from agents.legal_compliance_agent.models import KIND_STALE_POLICY
from agents.legal_compliance_agent.settings import LegalConfig


def test_pr_touching_storage_without_privacy_is_flagged():
    def fetch(url: str):
        if url.endswith("/pulls?state=open&per_page=20&sort=updated&direction=desc") or (
            "/pulls?" in url and "/files" not in url
        ):
            return [
                {
                    "number": 9,
                    "title": "store tickets in sqlite",
                    "html_url": "https://github.com/streamctx/streamctx-agents/pull/9",
                }
            ]
        if url.endswith("/pulls/9/files"):
            return [{"filename": "agents/techsupport_agent/storage.py"}]
        if "/commits?" in url:
            return []
        return []

    findings, errors = scan_github_changes(
        LegalConfig(github_repo="streamctx/streamctx-agents", github_per_page=20),
        fetch_fn=fetch,
    )
    assert errors == []
    assert findings
    assert all(item.kind == KIND_STALE_POLICY for item in findings)
    assert "storage.py" in findings[0].evidence
    assert "PRIVACY.md" in findings[0].evidence or "TERMS.md" in findings[0].evidence


def test_pr_that_also_updates_privacy_is_not_flagged():
    def fetch(url: str):
        if "/pulls?" in url and "/files" not in url:
            return [
                {
                    "number": 3,
                    "title": "document sqlite",
                    "html_url": "https://github.com/example/pr/3",
                }
            ]
        if url.endswith("/pulls/3/files"):
            return [
                {"filename": "agents/foo/storage.py"},
                {"filename": "PRIVACY.md"},
            ]
        if "/commits?" in url:
            return []
        return []

    findings, errors = scan_github_changes(LegalConfig(), fetch_fn=fetch)
    assert errors == []
    assert findings == []
