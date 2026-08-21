"""Isolated sandbox for applying unified diffs and running pytest."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from shared.config import BASE_DIR


@dataclass(frozen=True)
class TestResult:
    passed: bool
    output: str
    coverage_delta: float


class DiffApplyError(RuntimeError):
    """Raised when a unified diff cannot be applied cleanly."""


_IGNORE_COPY = shutil.ignore_patterns(
    "venv",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".git",
    "logs",
    "*.pyc",
    ".env",
)


class Sandbox:
    """
    Disposable clone of a project tree with its own venv.

    Diffs are applied with ``git apply`` against an internal baseline commit
    so ``reset()`` can restore the tree without touching the real working copy.
    """

    def __init__(
        self,
        source_root: Optional[Path | str] = None,
        *,
        keep: bool = False,
    ) -> None:
        self.source_root = Path(source_root or BASE_DIR).resolve()
        self._keep = keep
        self._tmpdir = Path(tempfile.mkdtemp(prefix="coding_agent_sandbox_"))
        self._root = self._tmpdir / "workspace"
        self._applied_diffs: list[str] = []
        self._venv_python = self._bootstrap()
        self._baseline_coverage = self._measure_coverage()

    @property
    def root(self) -> Path:
        return self._root

    def apply_diff(self, diff: str) -> None:
        """Apply a unified diff inside the sandbox working tree."""
        if not diff.strip():
            raise DiffApplyError("Diff is empty.")

        diff_path = self._root / ".sandbox_patch.diff"
        diff_path.write_text(diff, encoding="utf-8")
        result = subprocess.run(
            ["git", "apply", "--whitespace=fix", str(diff_path)],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            combined = (result.stderr or "") + (result.stdout or "")
            raise DiffApplyError(combined.strip() or "git apply failed.")

        self._applied_diffs.append(diff)

    def run_tests(self) -> TestResult:
        """Run the full pytest suite in the sandbox venv."""
        return self.run_pytest()

    def run_pytest(
        self,
        extra_args: Optional[list[str]] = None,
        *,
        coverage: bool = True,
    ) -> TestResult:
        """Run pytest with optional extra args (nodeids, ``--ignore``, …)."""
        output, returncode = self._run_pytest(extra_args=extra_args, coverage=coverage)
        measured = self._parse_coverage(output) if coverage else self._baseline_coverage
        return TestResult(
            passed=returncode == 0,
            output=output,
            coverage_delta=measured - self._baseline_coverage,
        )

    def reset(self) -> None:
        """Restore the sandbox tree to the post-bootstrap baseline."""
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=str(self._root),
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=str(self._root),
            check=True,
            capture_output=True,
            text=True,
        )
        self._applied_diffs.clear()

    def close(self) -> None:
        """Remove the sandbox directory."""
        if self._tmpdir.exists():
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._keep:
            self.close()

    def _bootstrap(self) -> Path:
        shutil.copytree(
            self.source_root,
            self._root,
            ignore=_IGNORE_COPY,
            dirs_exist_ok=False,
        )
        self._init_git_baseline()
        venv_python = self._create_venv()
        self._install_dependencies(venv_python)
        return venv_python

    def _init_git_baseline(self) -> None:
        env = self._git_env()
        subprocess.run(
            ["git", "init"],
            cwd=str(self._root),
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(self._root),
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        subprocess.run(
            ["git", "-c", "user.email=sandbox@streamctx.local", "-c", "user.name=Sandbox", "commit", "-m", "baseline"],
            cwd=str(self._root),
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

    def _create_venv(self) -> Path:
        venv_dir = self._root / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        python_name = "python.exe" if os.name == "nt" else "python"
        return venv_dir / ("Scripts" if os.name == "nt" else "bin") / python_name

    def _install_dependencies(self, python: Path) -> None:
        commands = [
            [str(python), "-m", "pip", "install", "-q", "--upgrade", "pip"],
            [str(python), "-m", "pip", "install", "-q", "pytest", "pytest-cov"],
        ]
        requirements = self._root / "requirements.txt"
        if requirements.exists():
            commands.append(
                [str(python), "-m", "pip", "install", "-q", "-r", str(requirements)]
            )

        for cmd in commands:
            subprocess.run(cmd, check=True, capture_output=True, text=True)

    def _run_pytest(
        self,
        extra_args: Optional[list[str]] = None,
        *,
        coverage: bool = True,
    ) -> tuple[str, int]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self._root)
        cmd = [str(self._venv_python), "-m", "pytest"]
        if extra_args:
            cmd.extend(extra_args)
        if coverage:
            cmd.extend(["--cov=.", "--cov-report=term-missing"])
        cmd.append("-q")
        result = subprocess.run(
            cmd,
            cwd=str(self._root),
            capture_output=True,
            text=True,
            env=env,
        )
        return result.stdout + result.stderr, result.returncode

    def _measure_coverage(self) -> float:
        output, _ = self._run_pytest()
        return self._parse_coverage(output)

    @staticmethod
    def _parse_coverage(pytest_output: str) -> float:
        match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+(?:\.\d+)?)%", pytest_output)
        if match:
            return float(match.group(1))
        return 0.0

    @staticmethod
    def _git_env() -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        return env
