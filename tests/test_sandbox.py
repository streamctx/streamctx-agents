"""Unit tests for the coding-agent sandbox runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.coding_agent.sandbox import DiffApplyError, Sandbox

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "sandbox_project"

FIX_DIFF = """\
diff --git a/broken_math.py b/broken_math.py
--- a/broken_math.py
+++ b/broken_math.py
@@ -4,3 +4,3 @@
 
 def add(a: int, b: int) -> int:
-    return a - b
+    return a + b
"""


@pytest.fixture(scope="module")
def sandbox():
    with Sandbox(source_root=FIXTURE_ROOT) as sb:
        yield sb


@pytest.fixture(autouse=True)
def _reset_sandbox_after_test(sandbox: Sandbox):
    yield
    sandbox.reset()


def test_run_tests_fails_on_baseline_bug(sandbox: Sandbox):
    result = sandbox.run_tests()
    assert result.passed is False
    assert "AssertionError" in result.output or "assert" in result.output.lower()


def test_apply_diff_and_run_tests_passes(sandbox: Sandbox):
    sandbox.apply_diff(FIX_DIFF)
    result = sandbox.run_tests()
    assert result.passed is True


def test_reset_restores_failing_state(sandbox: Sandbox):
    sandbox.apply_diff(FIX_DIFF)
    assert sandbox.run_tests().passed is True

    sandbox.reset()
    result = sandbox.run_tests()
    assert result.passed is False


def test_apply_invalid_diff_raises(sandbox: Sandbox):
    with pytest.raises(DiffApplyError):
        sandbox.apply_diff("not a valid unified diff")


def test_test_result_has_expected_fields(sandbox: Sandbox):
    result = sandbox.run_tests()
    assert isinstance(result.passed, bool)
    assert isinstance(result.output, str)
    assert isinstance(result.coverage_delta, float)
