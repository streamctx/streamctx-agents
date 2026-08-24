"""Unit tests for SeniorEngineer feature-request → package mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.coding_agent.coding_agent import main
from agents.coding_agent.models import ArchitectureSpec, CodePackage
from agents.coding_agent.senior_engineer_mode import (
    STATUS_BLOCKED,
    STATUS_NEEDS_REVIEW,
    STATUS_READY,
    SeniorEngineer,
    SeniorEngineerConfig,
    extract_json,
    parse_architecture_spec,
    run_feature,
    slug_feature_name,
    strip_code_fence,
    feature_test_path,
    write_package,
)

ARCH_PAYLOAD = {
    "feature_name": "RateLimiter",
    "description": "Token-bucket rate limiter for outbound API calls",
    "files_to_create": ["rate_limiter.py"],
    "files_to_modify": ["pipeline.py"],
    "classes": {"RateLimiter": "Tracks tokens and rejects bursts"},
    "methods": {"allow(key)": "True if the key may proceed"},
    "tests_needed": ["test_allows_within_budget", "test_blocks_burst"],
    "integration_steps": ["Import RateLimiter in pipeline.py"],
}

SOURCE = '''"""Token-bucket rate limiter."""

class RateLimiter:
    def allow(self, key: str) -> bool:
        return True
'''

TESTS = """
def test_allows_within_budget():
    assert True
"""

GUIDE = "## Integration Guide: RateLimiter\n\n1. Add rate_limiter.py\n"


def _engineer_from_prompts() -> SeniorEngineer:
    def llm_fn(prompt: str) -> str:
        if "designing a new feature" in prompt:
            return json.dumps(ARCH_PAYLOAD)
        if "production-ready Python code" in prompt:
            return f"```python\n{SOURCE}\n```"
        if "comprehensive pytest tests" in prompt:
            return TESTS
        if "integration guide" in prompt.lower() or "Integration Guide" in prompt:
            return GUIDE
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    return SeniorEngineer(llm_fn=llm_fn)


def test_parse_architecture_spec_strips_markdown_fence():
    fenced = "```json\n" + json.dumps(ARCH_PAYLOAD) + "\n```"
    spec = parse_architecture_spec(fenced)
    assert spec.feature_name == "RateLimiter"
    assert spec.files_to_create == ["rate_limiter.py"]
    assert spec.classes["RateLimiter"].startswith("Tracks tokens")


def test_extract_json_reads_embedded_object():
    raw = "Here you go:\n" + json.dumps(ARCH_PAYLOAD) + "\nThanks"
    payload = extract_json(raw)
    assert payload["feature_name"] == "RateLimiter"


def test_strip_code_fence_leaves_plain_text():
    assert strip_code_fence("def f():\n    return 1") == "def f():\n    return 1"


def test_think_architecture_rejects_empty_request():
    engineer = SeniorEngineer(llm_fn=lambda _prompt: "{}")
    with pytest.raises(ValueError, match="non-empty"):
        engineer.think_architecture("   ")


def test_think_architecture_returns_spec():
    spec = _engineer_from_prompts().think_architecture("Add a rate limiter")
    assert spec.feature_name == "RateLimiter"
    assert "allow(key)" in spec.methods


def test_package_feature_is_ready_end_to_end():
    package = _engineer_from_prompts().package_feature("Add a rate limiter")
    assert package.status == STATUS_READY
    assert "implementation/rate_limiter.py" in package.files
    assert "class RateLimiter" in package.files["implementation/rate_limiter.py"]
    assert feature_test_path("RateLimiter") in package.tests
    assert "Integration Guide" in package.integration_md


def test_package_feature_strips_fences_from_generated_code():
    package = _engineer_from_prompts().package_feature("Add a rate limiter")
    assert not package.files["implementation/rate_limiter.py"].startswith("```")


def test_implementation_dir_constant_is_importable():
    from agents.coding_agent.senior_engineer_mode import (
        GENERATED_FEATURES_DIR,
        IMPLEMENTATION_DIR,
    )

    assert Path(IMPLEMENTATION_DIR).as_posix() == "implementation"
    assert GENERATED_FEATURES_DIR.name == "generated_features"


def test_package_feature_writes_implementation_dir(tmp_path: Path):
    package = _engineer_from_prompts().package_feature(
        "Add a rate limiter",
        output_parent=tmp_path,
    )
    dest = tmp_path / "ratelimiter"
    impl = dest / "implementation" / "rate_limiter.py"
    assert impl.is_file()
    assert "class RateLimiter" in impl.read_text(encoding="utf-8")
    assert (dest / "tests" / "test_ratelimiter.py").is_file()
    assert (dest / "INTEGRATION.md").is_file()
    assert package.files["implementation/rate_limiter.py"]


def test_generate_complete_codebase_calls_once_per_file():
    calls: list[str] = []

    def llm_fn(prompt: str) -> str:
        calls.append(prompt)
        return "x = 1\n"

    spec = ArchitectureSpec(
        feature_name="Demo",
        description="demo",
        files_to_create=["a.py", "b.py"],
        files_to_modify=[],
        classes={},
        methods={},
        tests_needed=[],
        integration_steps=[],
    )
    files = SeniorEngineer(llm_fn=llm_fn).generate_complete_codebase(spec)
    assert list(files) == ["a.py", "b.py"]
    assert len(calls) == 2
    assert "a.py" in calls[0]
    assert "b.py" in calls[1]


def test_write_package_persists_files_tests_and_guide(tmp_path: Path):
    package = CodePackage(
        feature_name="RateLimiter",
        files={"rate_limiter.py": SOURCE},
        tests={"tests/test_rate_limiter.py": TESTS},
        integration_md=GUIDE,
        status=STATUS_READY,
    )
    dest = write_package(package, tmp_path / "pkg")
    assert (dest / "implementation" / "rate_limiter.py").read_text(encoding="utf-8") == SOURCE
    assert (dest / "tests" / "test_rate_limiter.py").read_text(encoding="utf-8") == TESTS
    assert "RateLimiter" in (dest / "INTEGRATION.md").read_text(encoding="utf-8")
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["feature_name"] == "RateLimiter"
    assert manifest["status"] == STATUS_READY
    assert manifest["files"] == ["implementation/rate_limiter.py"]
    assert manifest["tests"] == ["tests/test_rate_limiter.py"]


def test_write_package_rejects_path_traversal(tmp_path: Path):
    package = CodePackage(
        feature_name="Bad",
        files={"../escape.py": "x = 1\n"},
        tests={},
        integration_md="",
        status=STATUS_BLOCKED,
    )
    with pytest.raises(ValueError, match="unsafe"):
        write_package(package, tmp_path / "pkg")


def test_run_feature_writes_under_output_dir(tmp_path: Path):
    package, dest = run_feature(
        "Add a rate limiter",
        output_dir=tmp_path / "out",
        engineer=_engineer_from_prompts(),
    )
    assert package.status == STATUS_READY
    assert dest == (tmp_path / "out").resolve()
    assert (dest / "implementation" / "rate_limiter.py").is_file()
    assert (dest / "INTEGRATION.md").is_file()
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["original_prompt"] == "Add a rate limiter"
    assert manifest["architecture"]["feature_name"] == "RateLimiter"


def test_run_feature_defaults_to_generated_features(tmp_path: Path):
    _, dest = run_feature(
        "Add a rate limiter",
        engineer=_engineer_from_prompts(),
        base_dir=tmp_path,
    )
    assert dest == (tmp_path / "generated_features" / "ratelimiter").resolve()


def test_openrouter_requires_api_key():
    engineer = SeniorEngineer(config=SeniorEngineerConfig(model="x", api_key=None))
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        engineer.think_architecture("Add caching")


def test_package_status_needs_review_when_guide_missing():
    def llm_fn(prompt: str) -> str:
        if "designing a new feature" in prompt:
            return json.dumps(ARCH_PAYLOAD)
        if "production-ready" in prompt:
            return SOURCE
        if "comprehensive pytest tests" in prompt:
            return TESTS
        if "integration guide" in prompt.lower():
            return "   "
        raise AssertionError(prompt[:80])

    package = SeniorEngineer(llm_fn=llm_fn).package_feature("Add a rate limiter")
    assert package.status == STATUS_NEEDS_REVIEW


def test_slug_and_test_filename():
    assert slug_feature_name("Rate Limiter") == "rate_limiter"
    assert feature_test_path("Rate Limiter") == "tests/test_rate_limiter.py"


def test_cli_feature_command_writes_package(tmp_path: Path, monkeypatch, capsys):
    dest = tmp_path / "cli-out"

    def fake_run_feature(request, output_dir=None, engineer=None, base_dir=None):
        assert request == "Add a rate limiter"
        assert output_dir == str(dest)
        package = CodePackage(
            feature_name="RateLimiter",
            files={"rate_limiter.py": SOURCE},
            tests={"tests/test_rate_limiter.py": TESTS},
            integration_md=GUIDE,
            status=STATUS_READY,
        )
        return package, dest

    monkeypatch.setattr(
        "agents.coding_agent.coding_agent.run_feature",
        fake_run_feature,
    )
    monkeypatch.setattr(
        "agents.coding_agent.coding_agent.get_tracker",
        lambda _agent_id: _FakeTracker(),
    )
    code = main(["feature", "Add a rate limiter", "--output", str(dest)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "feature=RateLimiter" in captured
    assert "status=ready" in captured


class _FakeTracker:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None
