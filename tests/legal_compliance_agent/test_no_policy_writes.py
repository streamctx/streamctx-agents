"""Agent sources never write policy files or POST to GitHub."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "agents" / "legal_compliance_agent"


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_github_module_is_get_only():
    src = _read("github_changes.py")
    assert "api.github.com/repos" in src
    assert "/pulls" in src
    assert "/commits" in src
    assert "method=\"POST\"" not in src
    assert "method='POST'" not in src
    assert "/comments" not in src
    assert "Never comments" in src or "GET only" in src


def test_agent_never_opens_policy_files_for_write():
    combined = "\n".join(
        _read(name)
        for name in (
            "legal_compliance_agent.py",
            "scan_docs.py",
            "draft.py",
            "tab.py",
        )
    )
    assert "open(" not in combined or "write_text" not in combined
    for needle in (
        'open("TERMS.md"',
        "open('TERMS.md'",
        'open("PRIVACY.md"',
        "Path(\"TERMS.md\").write",
        "Path('PRIVACY.md').write",
        "write_text(",
    ):
        assert needle not in combined
    assert "Does not write TERMS.md" in _read("legal_compliance_agent.py")
    assert "never edits" in _read("legal_compliance_agent.py").lower()


def test_notifications_webhook_is_not_github():
    src = _read("notifications.py")
    assert "LEGAL_COMPLIANCE_AGENT_WEBHOOK_URL" in src
    assert "api.github.com" not in src
