"""LLM-backed fix proposal generation returning structured JSON."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from agents.coding_agent.models import DiagnosisResult, FixProposal, FixRequest

FixGeneratorFn = Callable[[FixRequest], FixProposal]


@dataclass(frozen=True)
class FixGeneratorConfig:
    model: str
    api_key: Optional[str]
    base_url: str = "https://openrouter.ai/api/v1"


class FixGenerator:
    """Produce a unified diff and regression test in one structured response."""

    def __init__(
        self,
        llm_fn: Optional[FixGeneratorFn] = None,
        config: Optional[FixGeneratorConfig] = None,
    ) -> None:
        self._llm_fn = llm_fn
        self._config = config

    def generate(self, request: FixRequest) -> FixProposal:
        if request.pattern_template and request.attempt == 0:
            return FixProposal(
                diff=request.pattern_template,
                regression_test=request.pattern_regression_test or "",
                regression_test_path=request.regression_test_path,
            )

        if self._llm_fn is not None:
            return self._llm_fn(request)

        return self._call_llm(request)

    def _call_llm(self, request: FixRequest) -> FixProposal:
        config = self._config or _load_default_config()
        if not config.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for fix generation.")

        from openai import OpenAI

        client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        response = client.chat.completions.create(
            model=config.model,
            messages=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _build_prompt(request)},
            ],
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        return parse_fix_proposal(raw, default_test_path=request.regression_test_path)


def build_fix_request(
    diagnosis: DiagnosisResult,
    *,
    error_message: Optional[str],
    source_contents: dict[str, str],
    attempt: int = 0,
    previous_failure_output: Optional[str] = None,
    regression_test_path: str = "tests/test_regression_auto.py",
    rejection_hint: Optional[str] = None,
) -> FixRequest:
    pattern_template = None
    pattern_regression_test = None
    if diagnosis.matched_pattern is not None:
        pattern_template = diagnosis.matched_pattern.fix_diff_template

    return FixRequest(
        diagnosis=diagnosis,
        error_message=error_message or "",
        source_contents=source_contents,
        pattern_template=pattern_template,
        pattern_regression_test=pattern_regression_test,
        attempt=attempt,
        previous_failure_output=previous_failure_output,
        regression_test_path=regression_test_path,
        rejection_hint=rejection_hint,
    )


def parse_fix_proposal(
    raw: str,
    *,
    default_test_path: str = "tests/test_regression_auto.py",
) -> FixProposal:
    payload = _extract_json(raw)
    diff = str(payload.get("diff", "")).strip()
    regression_test = str(payload.get("regression_test", "")).strip()
    regression_test_path = str(
        payload.get("regression_test_path") or default_test_path
    ).strip()
    if not diff:
        raise ValueError("Fix proposal JSON must include a non-empty 'diff'.")
    return FixProposal(
        diff=diff,
        regression_test=regression_test,
        regression_test_path=regression_test_path,
    )


def _load_default_config() -> FixGeneratorConfig:
    from shared.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL

    return FixGeneratorConfig(
        model=OPENROUTER_MODEL,
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
    )


def _system_prompt() -> str:
    return (
        "You are a careful Python bug-fixing assistant. "
        "Respond with JSON only using keys: diff, regression_test, regression_test_path. "
        "The diff must be a valid unified diff (patch) against the provided source files. "
        "Never rewrite whole files; produce the smallest diff that fixes the bug. "
        "The regression_test must be a standalone pytest file that reproduces and verifies "
        "the specific failure signature."
    )


def _build_prompt(request: FixRequest) -> str:
    diagnosis = request.diagnosis
    if diagnosis.root_cause == "INTAKE":
        parts = [
            "Human-approved task (skip diagnosis; fix the named pytest failures):",
            diagnosis.reason,
            f"Error trace:\n{request.error_message}",
            f"Relevant file(s): {diagnosis.relevant_file}",
        ]
    else:
        parts = [
            f"Error trace:\n{request.error_message}",
            f"Root cause: {diagnosis.root_cause}",
            f"Confidence: {diagnosis.confidence}",
            f"Error type: {diagnosis.error_type}",
            f"Relevant file(s): {diagnosis.relevant_file}",
            f"Attribution reason: {diagnosis.reason}",
        ]

    if request.pattern_template and request.attempt > 0:
        parts.append(f"Prior fix template:\n{request.pattern_template}")

    for path, content in request.source_contents.items():
        parts.append(f"Source file {path}:\n```python\n{content}\n```")

    if request.previous_failure_output:
        parts.append(
            "Previous pytest run failed after applying your last diff. "
            f"Failure output:\n{request.previous_failure_output}"
        )

    if request.rejection_hint:
        parts.append(
            "A prior fix for this bug signature was rejected by a human reviewer. "
            f"Avoid repeating this approach:\n{request.rejection_hint}"
        )

    parts.append(
        'Return JSON: {"diff": "<unified diff>", '
        '"regression_test": "<pytest file content>", '
        f'"regression_test_path": "{request.regression_test_path}"}}'
    )
    return "\n\n".join(parts)


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    return json.loads(text)


def read_source_files(base_dir: Path, relative_paths: list[str]) -> dict[str, str]:
    contents: dict[str, str] = {}
    for rel in relative_paths:
        path = base_dir / rel
        if path.is_file():
            contents[rel.replace("\\", "/")] = path.read_text(encoding="utf-8")
    return contents
