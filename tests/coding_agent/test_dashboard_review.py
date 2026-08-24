"""Tests for SeniorEngineer feature-review dashboard backend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.coding_agent.dashboard_review import (
    approve_feature,
    copy_package_text,
    count_test_cases,
    feature_stats,
    find_feature,
    generate_feature,
    list_features,
    load_feature,
    regenerate_feature,
    reject_feature,
)
from agents.coding_agent.models import CodePackage
from agents.coding_agent.senior_engineer_mode import (
    STATUS_APPROVED,
    STATUS_READY,
    STATUS_REJECTED,
    SeniorEngineer,
    write_package,
)

SOURCE = "def allow(key: str) -> bool:\n    return True\n"
TESTS = (
    "def test_allows_within_budget():\n    assert True\n\n"
    "def test_blocks_burst():\n    assert True\n"
)
GUIDE = "## Integration Guide: RateLimiter\n\n1. Add rate_limiter.py\n"
PROMPT = "Add a token-bucket rate limiter"


def _package(**overrides) -> CodePackage:
    base = dict(
        feature_name="RateLimiter",
        files={"rate_limiter.py": SOURCE},
        tests={"tests/test_rate_limiter.py": TESTS},
        integration_md=GUIDE,
        status=STATUS_READY,
        original_prompt=PROMPT,
        created_at="2026-08-24T10:00:00+00:00",
        architecture={
            "feature_name": "RateLimiter",
            "description": "Token-bucket limiter",
            "files_to_create": ["rate_limiter.py"],
            "files_to_modify": ["pipeline.py"],
            "classes": {"RateLimiter": "Tracks tokens"},
            "methods": {"allow(key)": "True if allowed"},
            "tests_needed": ["test_allows_within_budget"],
            "integration_steps": ["Import RateLimiter"],
        },
    )
    base.update(overrides)
    return CodePackage(**base)


def _seed(root: Path, *, slug: str = "ratelimiter", **overrides) -> Path:
    dest = root / slug
    write_package(_package(**overrides), dest)
    return dest


def test_list_features_includes_pending_approved_and_rejected(tmp_path: Path):
    _seed(tmp_path, slug="ratelimiter")
    write_package(
        _package(feature_name="CacheLayer", original_prompt="Add a cache"),
        tmp_path / "cachelayer",
    )
    approve_feature("cachelayer", root=tmp_path, log_fn=lambda *a, **k: {})
    write_package(
        _package(feature_name="RetryBudget", original_prompt="Add retries"),
        tmp_path / "retrybudget",
    )
    reject_feature(
        "retrybudget",
        "too broad",
        root=tmp_path,
        log_fn=lambda *a, **k: {},
    )

    listed = list_features(tmp_path)
    by_slug = {rec.slug: rec for rec in listed}
    assert by_slug["ratelimiter"].status == STATUS_READY
    assert by_slug["cachelayer"].status == STATUS_APPROVED
    assert by_slug["retrybudget"].status == STATUS_REJECTED
    assert listed[0].slug == "ratelimiter"


def test_approve_moves_to_approved_and_logs(tmp_path: Path):
    _seed(tmp_path)
    audits: list[dict] = []

    def log_fn(agent, session_id, action_type, payload, status="pending_approval"):
        entry = {
            "agent": agent,
            "session_id": session_id,
            "action_type": action_type,
            "payload": payload,
            "status": status,
        }
        audits.append(entry)
        return entry

    record = approve_feature("ratelimiter", root=tmp_path, log_fn=log_fn)
    assert record.status == STATUS_APPROVED
    assert record.path == (tmp_path / "approved" / "ratelimiter").resolve()
    assert not (tmp_path / "ratelimiter").exists()
    manifest = json.loads((record.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == STATUS_APPROVED
    assert audits[0]["action_type"] == "senior_engineer_approve"
    assert audits[0]["status"] == "approved"
    assert audits[0]["payload"]["slug"] == "ratelimiter"


def test_reject_requires_reason_and_moves(tmp_path: Path):
    _seed(tmp_path)
    with pytest.raises(ValueError, match="reason"):
        reject_feature("ratelimiter", "  ", root=tmp_path, log_fn=lambda *a, **k: {})

    record = reject_feature(
        "ratelimiter",
        "missing edge cases",
        root=tmp_path,
        log_fn=lambda *a, **k: {},
    )
    assert record.status == STATUS_REJECTED
    assert record.rejection_reason == "missing edge cases"
    assert record.path == (tmp_path / "rejected" / "ratelimiter").resolve()


def test_regenerate_rewrites_pending_from_original_prompt(tmp_path: Path):
    _seed(tmp_path)
    calls: list[str] = []

    def llm_fn(prompt: str) -> str:
        calls.append(prompt)
        if "designing a new feature" in prompt:
            assert "token-bucket" in prompt
            return json.dumps(
                {
                    "feature_name": "RateLimiter",
                    "description": "regen",
                    "files_to_create": ["rate_limiter.py"],
                    "files_to_modify": [],
                    "classes": {},
                    "methods": {},
                    "tests_needed": ["test_ok"],
                    "integration_steps": [],
                }
            )
        if "production-ready" in prompt:
            return "def allow(key: str) -> bool:\n    return False\n"
        if "comprehensive pytest tests" in prompt:
            return "def test_ok():\n    assert True\n"
        return "## Integration Guide\n\nregen\n"

    record = regenerate_feature(
        "ratelimiter",
        root=tmp_path,
        engineer=SeniorEngineer(llm_fn=llm_fn),
    )
    assert record.path == (tmp_path / "ratelimiter").resolve()
    assert "return False" in record.files["implementation/rate_limiter.py"]
    assert record.original_prompt == PROMPT
    assert record.status == STATUS_READY
    assert calls


def test_copy_package_text_includes_files_tests_guide_and_manifest(tmp_path: Path):
    _seed(tmp_path)
    record = load_feature(tmp_path / "ratelimiter", root=tmp_path)
    blob = copy_package_text(record)
    assert "# implementation/rate_limiter.py" in blob
    assert "def allow" in blob
    assert "# tests/test_rate_limiter.py" in blob
    assert "# INTEGRATION.md" in blob
    assert '"feature_name": "RateLimiter"' in blob
    assert count_test_cases(record.tests) == 2


def test_load_feature_without_manifest_still_works(tmp_path: Path):
    folder = tmp_path / "legacy"
    folder.mkdir()
    (folder / "widget.py").write_text("x = 1\n", encoding="utf-8")
    (folder / "tests").mkdir()
    (folder / "tests" / "test_widget.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    record = load_feature(folder, root=tmp_path)
    assert record.feature_name == "legacy"
    assert "widget.py" in record.files
    assert "tests/test_widget.py" in record.tests


def test_find_feature_prefers_pending_over_approved(tmp_path: Path):
    _seed(tmp_path)
    approve_feature("ratelimiter", root=tmp_path, log_fn=lambda *a, **k: {})
    _seed(tmp_path)
    found = find_feature("ratelimiter", root=tmp_path)
    assert found.status == STATUS_READY
    assert found.path == (tmp_path / "ratelimiter").resolve()


def test_reserved_dirs_are_not_listed_as_features(tmp_path: Path):
    (tmp_path / "approved").mkdir()
    (tmp_path / "rejected").mkdir()
    assert list_features(tmp_path) == []


def test_load_feature_reads_prompt_alias_and_implementation_dir(tmp_path: Path):
    folder = tmp_path / "legacy_prompt"
    impl = folder / "implementation"
    impl.mkdir(parents=True)
    (impl / "widget.py").write_text("x = 1\n", encoding="utf-8")
    (folder / "tests").mkdir()
    (folder / "tests" / "test_widget.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    (folder / "manifest.json").write_text(
        json.dumps(
            {
                "feature_name": "LegacyPrompt",
                "prompt": "Add a widget",
                "status": "ready",
            }
        ),
        encoding="utf-8",
    )
    record = load_feature(folder, root=tmp_path)
    assert record.original_prompt == "Add a widget"
    assert "implementation/widget.py" in record.files
    assert "tests/test_widget.py" in record.tests


def test_feature_stats_counts_statuses(tmp_path: Path):
    _seed(tmp_path, slug="ratelimiter")
    write_package(
        _package(feature_name="CacheLayer", original_prompt="Add a cache"),
        tmp_path / "cachelayer",
    )
    approve_feature("cachelayer", root=tmp_path, log_fn=lambda *a, **k: {})
    stats = feature_stats(list_features(tmp_path))
    assert stats["total"] == 2
    assert stats["ready"] == 1
    assert stats["approved"] == 1
    assert stats["rejected"] == 0


def test_generate_feature_writes_package_from_prompt(tmp_path: Path):
    audits: list[dict] = []

    def llm_fn(prompt: str) -> str:
        if "designing a new feature" in prompt:
            return json.dumps(
                {
                    "feature_name": "RateLimiter",
                    "description": "limiter",
                    "files_to_create": ["rate_limiter.py"],
                    "files_to_modify": [],
                    "classes": {},
                    "methods": {},
                    "tests_needed": ["test_ok"],
                    "integration_steps": [],
                }
            )
        if "production-ready" in prompt:
            return SOURCE
        if "comprehensive pytest tests" in prompt:
            return "def test_ok():\n    assert True\n"
        return "## Integration Guide\n\ngenerated\n"

    def log_fn(agent, session_id, action_type, payload, status="pending_approval"):
        entry = {
            "agent": agent,
            "action_type": action_type,
            "payload": payload,
            "status": status,
        }
        audits.append(entry)
        return entry

    record = generate_feature(
        "Add a token-bucket rate limiter",
        root=tmp_path,
        engineer=SeniorEngineer(llm_fn=llm_fn),
        log_fn=log_fn,
    )
    assert record.feature_name == "RateLimiter"
    assert record.original_prompt == "Add a token-bucket rate limiter"
    assert (tmp_path / "ratelimiter" / "implementation" / "rate_limiter.py").is_file()
    assert audits[0]["action_type"] == "senior_engineer_generate"
    assert audits[0]["status"] == "pending_approval"
    assert "elapsed_seconds" in audits[0]["payload"]


def test_generate_feature_requires_prompt(tmp_path: Path):
    with pytest.raises(ValueError, match="required"):
        generate_feature("  ", root=tmp_path, engineer=SeniorEngineer(llm_fn=lambda p: p))


def test_streamlit_dashboard_renders_and_approves(tmp_path: Path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("SENIOR_ENGINEER_FEATURES_DIR", str(tmp_path))
    _seed(tmp_path)
    at = AppTest.from_file(
        "agents/coding_agent/dashboard_review.py",
        default_timeout=15,
    )
    at.run()
    assert at.title[0].value == "🤖 Coding Agent Feature Review Dashboard"
    assert any("Generate New Feature" in str(s.value) for s in at.subheader)
    assert any("Feature request" in str(t.label) for t in at.text_input)
    assert at.header[0].value == "RateLimiter"
    assert any("def allow" in str(block.value) for block in at.code)
    labels = [b.label for b in at.button]
    assert "Generate" in labels
    assert "✅ Approve Feature" in labels
    assert "❌ Reject Feature" in labels
    assert "🔄 Regenerate" in labels
    assert "📁 Open in File Explorer" in labels
    approve = next(b for b in at.button if b.label == "✅ Approve Feature")
    approve.click().run()
    assert (tmp_path / "approved" / "ratelimiter").is_dir()
    assert not (tmp_path / "ratelimiter").exists()
    assert any("approved" in str(m.value).lower() for m in at.metric)
