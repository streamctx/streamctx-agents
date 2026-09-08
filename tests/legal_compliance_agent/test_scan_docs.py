"""Policy scan flags missing docs and claim-vs-code mismatches."""

from __future__ import annotations

from pathlib import Path

from agents.legal_compliance_agent.models import KIND_INCONSISTENCY, KIND_MISSING_DOC
from agents.legal_compliance_agent.scan_docs import (
    collect_code_facts,
    scan_docs,
)


def test_missing_policy_docs_are_flagged(tmp_path: Path):
    (tmp_path / "readme_stub.py").write_text(
        "STREAMCTX_HOME = '.streamctx'\nOPENROUTER_API_KEY = 'x'\n",
        encoding="utf-8",
    )
    findings, docs, _facts = scan_docs(
        agents_root=tmp_path,
        product_root=tmp_path / "missing-sdk",
    )
    missing = {doc.name for doc in docs.values() if not doc.found}
    assert missing == {
        "TERMS.md",
        "PRIVACY.md",
        "COMPLIANCE_VERIFICATION.md",
        "DEPLOYMENT.md",
    }
    kinds = {item.kind for item in findings}
    titles = {item.title for item in findings}
    assert KIND_MISSING_DOC in kinds
    assert any("TERMS.md" in title for title in titles)
    assert any("PRIVACY.md" in title for title in titles)


def test_privacy_claim_mismatches_openrouter(tmp_path: Path):
    (tmp_path / "PRIVACY.md").write_text(
        "We never share data with third parties. Data never leaves your device.\n",
        encoding="utf-8",
    )
    (tmp_path / "TERMS.md").write_text("Use at your own risk.\n", encoding="utf-8")
    (tmp_path / "COMPLIANCE_VERIFICATION.md").write_text(
        "Checks TBD.\n", encoding="utf-8"
    )
    (tmp_path / "DEPLOYMENT.md").write_text("Local install.\n", encoding="utf-8")
    (tmp_path / "storage.py").write_text(
        "url = 'https://openrouter.ai/api/v1'\nOPENROUTER_API_KEY\n",
        encoding="utf-8",
    )
    findings, docs, facts = scan_docs(agents_root=tmp_path, product_root=tmp_path)
    assert docs["PRIVACY.md"].found
    assert facts.openrouter_refs
    mismatches = [item for item in findings if item.kind == KIND_INCONSISTENCY]
    blob = " ".join(item.title + item.evidence for item in mismatches).lower()
    assert "openrouter" in blob or "third-party" in blob or "third party" in blob


def test_code_facts_see_sqlite_home(tmp_path: Path):
    (tmp_path / "storage.py").write_text(
        "from pathlib import Path\n"
        "Path(os.environ.get('STREAMCTX_HOME')) / 'sessions.db'\n",
        encoding="utf-8",
    )
    facts = collect_code_facts([tmp_path])
    assert facts.sqlite_refs
