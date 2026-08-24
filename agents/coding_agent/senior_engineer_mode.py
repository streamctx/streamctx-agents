"""SeniorEngineer mode: feature request → complete production-ready package.

Takes a short feature description, designs architecture, generates source
files, tests, and an integration guide. Packages are written to an output
directory for human review — they are not merged into the live repo.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agents.coding_agent.models import ArchitectureSpec, CodePackage
from agents.coding_agent.retry_engine import APIRetryHandler

logger = logging.getLogger(__name__)

MAX_API_ATTEMPTS = 3
STATUS_READY = "ready"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_BLOCKED = "blocked"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
MANIFEST_NAME = "manifest.json"
INTEGRATION_NAME = "INTEGRATION.md"

# generated_features/<feature_slug>/implementation/*.py
GENERATED_FEATURES_DIR = Path(__file__).resolve().parents[2] / "generated_features"
IMPLEMENTATION_DIR = Path("implementation")
TESTS_DIR = Path("tests")

CompletionFn = Callable[[str], str]


@dataclass(frozen=True)
class SeniorEngineerConfig:
    model: str
    api_key: Optional[str]
    base_url: str = "https://openrouter.ai/api/v1"


class SeniorEngineer:
    """Acts as a senior engineer: feature request → complete codebase.

    LLM calls go through OpenRouter (same stack as the rest of coding_agent).
    Pass ``llm_fn`` to inject a completion function for tests.
    """

    def __init__(
        self,
        llm_fn: Optional[CompletionFn] = None,
        config: Optional[SeniorEngineerConfig] = None,
        retry_handler: Optional[APIRetryHandler] = None,
    ) -> None:
        self._llm_fn = llm_fn
        self._config = config
        self._retry_handler = retry_handler

    def think_architecture(self, feature_request: str) -> ArchitectureSpec:
        """Call the LLM to design architecture for ``feature_request``."""
        request = feature_request.strip()
        if not request:
            raise ValueError("feature_request must be a non-empty description.")

        prompt = _architecture_prompt(request)
        raw = self._complete(prompt, max_tokens=2000, json_mode=True)
        return parse_architecture_spec(raw)

    def generate_complete_codebase(self, spec: ArchitectureSpec) -> dict[str, str]:
        """Generate complete source for every file in ``spec.files_to_create``."""
        files_code: dict[str, str] = {}
        for filename in spec.files_to_create:
            prompt = _code_prompt(spec, filename)
            files_code[filename] = strip_code_fence(
                self._complete(prompt, max_tokens=3000)
            )
        return files_code

    def generate_tests(
        self,
        spec: ArchitectureSpec,
        codebase: dict[str, str],
    ) -> dict[str, str]:
        """Generate a pytest module covering the architecture's listed tests."""
        prompt = _tests_prompt(spec, codebase)
        test_code = strip_code_fence(self._complete(prompt, max_tokens=2000))
        return {feature_test_path(spec.feature_name): test_code}

    def generate_integration_guide(self, spec: ArchitectureSpec) -> str:
        """Generate a markdown integration guide for merging the feature."""
        prompt = _integration_prompt(spec)
        return strip_code_fence(self._complete(prompt, max_tokens=1500))

    def package_feature(
        self,
        feature_request: str,
        *,
        output_dir: Optional[Path | str] = None,
        output_parent: Optional[Path | str] = None,
    ) -> CodePackage:
        """End-to-end: feature request → production-ready package.

        When ``output_dir`` or ``output_parent`` is set, files are written under
        ``<feature>/implementation/``, ``<feature>/tests/``, and ``INTEGRATION.md``.
        """
        logger.info("Thinking architecture for: %s", feature_request.strip())
        spec = self.think_architecture(feature_request)

        logger.info("Generating code for %s files", len(spec.files_to_create))
        codebase = self.generate_complete_codebase(spec)
        codebase = {
            implementation_relpath(filename): code
            for filename, code in codebase.items()
        }

        logger.info("Writing tests")
        tests = self.generate_tests(spec, codebase)

        logger.info("Creating integration guide")
        integration = self.generate_integration_guide(spec)

        package = CodePackage(
            feature_name=spec.feature_name,
            files=codebase,
            tests=tests,
            integration_md=integration,
            status=_package_status(codebase, tests, integration),
            original_prompt=feature_request.strip(),
            created_at=_utc_now(),
            architecture=asdict(spec),
        )
        dest = _resolve_feature_dir(
            package.feature_name,
            output_dir=output_dir,
            output_parent=output_parent,
        )
        if dest is not None:
            persist_feature_package(package, dest)
        return package

    def _complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        json_mode: bool = False,
    ) -> str:
        if self._llm_fn is not None:
            return self._llm_fn(prompt)

        handler = self._retry_handler or APIRetryHandler()
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_API_ATTEMPTS + 1):
            try:
                return self._call_openrouter(
                    prompt,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
            except Exception as exc:
                last_error = exc
                if not handler.should_retry(exc) or attempt >= MAX_API_ATTEMPTS:
                    raise
                handler.wait_and_retry(attempt, str(exc))
        raise RuntimeError("LLM completion failed") from last_error

    def _call_openrouter(
        self,
        prompt: str,
        *,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        config = self._config or _load_default_config()
        if not config.api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is required for SeniorEngineer mode."
            )

        from openai import OpenAI

        kwargs: dict[str, Any] = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": _system_prompt(json_mode)},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        response = client.chat.completions.create(**kwargs)
        return (response.choices[0].message.content or "").strip()


def run_feature(
    request: str,
    *,
    output_dir: Optional[Path | str] = None,
    engineer: Optional[SeniorEngineer] = None,
    base_dir: Optional[Path | str] = None,
) -> tuple[CodePackage, Path]:
    """Package a feature request and write it under ``output_dir``.

    When ``output_dir`` is omitted, files go to
    ``<base_dir>/generated_features/<feature_slug>/`` (``BASE_DIR`` by default).
    """
    engineer = engineer or SeniorEngineer()
    if output_dir:
        package = engineer.package_feature(request, output_dir=output_dir)
        dest = Path(output_dir).resolve()
        return package, dest
    parent = Path(base_dir or _default_base_dir()) / "generated_features"
    package = engineer.package_feature(request, output_parent=parent)
    dest = (parent / slug_feature_name(package.feature_name)).resolve()
    return package, dest


def write_package(package: CodePackage, output_dir: Path | str) -> Path:
    """Persist source under implementation/, plus tests, INTEGRATION.md, and manifest."""
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)

    existing = _read_manifest(root)
    impl_dir = root / IMPLEMENTATION_DIR
    impl_dir.mkdir(parents=True, exist_ok=True)

    relocated_files: dict[str, str] = {}
    for relative, content in package.files.items():
        dest_rel = implementation_relpath(relative)
        path = _safe_join(root, dest_rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        relocated_files[dest_rel] = content

    for relative, content in package.tests.items():
        path = _safe_join(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    guide = package.integration_md.strip()
    if guide:
        (root / INTEGRATION_NAME).write_text(guide + "\n", encoding="utf-8")

    if relocated_files:
        package.files = relocated_files

    manifest = build_manifest(package, existing=existing)
    (root / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return root


def persist_feature_package(package: CodePackage, feature_dir: Path | str) -> Path:
    """Create implementation/, tests/, INTEGRATION.md, and manifest.json on disk."""
    dest = Path(feature_dir)
    if dest.exists():
        shutil.rmtree(dest)
    return write_package(package, dest)


def _resolve_feature_dir(
    feature_name: str,
    *,
    output_dir: Optional[Path | str],
    output_parent: Optional[Path | str],
) -> Optional[Path]:
    if output_dir is not None:
        return Path(output_dir)
    if output_parent is not None:
        return Path(output_parent) / slug_feature_name(feature_name)
    return None


def build_manifest(
    package: CodePackage,
    *,
    existing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Serialize a package into the on-disk review manifest."""
    previous = existing or {}
    created_at = (
        package.created_at.strip()
        or str(previous.get("created_at") or "").strip()
        or _utc_now()
    )
    prompt = (
        package.original_prompt.strip()
        or str(previous.get("original_prompt") or previous.get("prompt") or "").strip()
    )
    architecture = package.architecture or previous.get("architecture") or {}
    return {
        "feature_name": package.feature_name,
        "slug": slug_feature_name(package.feature_name),
        "original_prompt": prompt,
        "prompt": prompt,
        "status": package.status,
        "created_at": created_at,
        "updated_at": _utc_now(),
        "architecture": architecture,
        "files": list(package.files.keys()),
        "tests": list(package.tests.keys()),
        "integration_guide": INTEGRATION_NAME if package.integration_md.strip() else None,
        "rejection_reason": previous.get("rejection_reason"),
    }


def _read_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_architecture_spec(raw: str) -> ArchitectureSpec:
    """Parse an architecture JSON blob, including fenced markdown wrappers."""
    payload = extract_json(raw)
    feature_name = str(payload.get("feature_name") or "").strip() or "UnnamedFeature"
    description = str(payload.get("description") or "").strip()
    return ArchitectureSpec(
        feature_name=feature_name,
        description=description,
        files_to_create=_as_str_list(payload.get("files_to_create")),
        files_to_modify=_as_str_list(payload.get("files_to_modify")),
        classes=_as_str_dict(payload.get("classes")),
        methods=_as_str_dict(payload.get("methods")),
        tests_needed=_as_str_list(payload.get("tests_needed")),
        integration_steps=_as_str_list(payload.get("integration_steps")),
    )


def extract_json(raw: str) -> dict[str, Any]:
    """Load a JSON object from raw model output, stripping markdown fences."""
    text = strip_code_fence(raw)
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        payload = json.loads(text[start : end + 1])
        if isinstance(payload, dict):
            return payload
    raise ValueError("Architecture response was not valid JSON.")


def strip_code_fence(raw: str) -> str:
    """Remove a wrapping markdown code fence if the model added one."""
    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def implementation_relpath(filename: str) -> str:
    """Place an implementation file under ``implementation/`` if it is not already."""
    rel = str(filename).replace("\\", "/").lstrip("/")
    impl = IMPLEMENTATION_DIR.as_posix()
    if rel.startswith(f"{impl}/"):
        return rel
    return f"{impl}/{rel}"


def slug_feature_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "feature"
    return slug


def feature_test_path(feature_name: str) -> str:
    return f"tests/test_{slug_feature_name(feature_name)}.py"


def _default_base_dir() -> Path:
    from shared.config import BASE_DIR

    return Path(BASE_DIR)


def _package_status(
    files: dict[str, str],
    tests: dict[str, str],
    integration_md: str,
) -> str:
    has_files = any(content.strip() for content in files.values())
    has_tests = any(content.strip() for content in tests.values())
    has_guide = bool(integration_md.strip())
    if has_files and has_tests and has_guide:
        return STATUS_READY
    if has_files or has_tests or has_guide:
        return STATUS_NEEDS_REVIEW
    return STATUS_BLOCKED


def _safe_join(root: Path, relative: str) -> Path:
    rel = Path(str(relative).replace("\\", "/"))
    if not str(relative).strip() or rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Refusing unsafe package path: {relative!r}")
    dest = (root / rel).resolve()
    dest.relative_to(root)
    return dest


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        item = value.strip()
        return [item] if item else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _as_str_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        name = str(key).strip()
        if name:
            result[name] = str(item).strip()
    return result


def _load_default_config() -> SeniorEngineerConfig:
    from shared.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL

    return SeniorEngineerConfig(
        model=OPENROUTER_MODEL,
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
    )


def _system_prompt(json_mode: bool) -> str:
    if json_mode:
        return (
            "You are a senior software engineer. Respond with JSON only. "
            "No markdown, no commentary."
        )
    return (
        "You are a senior software engineer writing production Python. "
        "Output only the requested artifact — no markdown fences, no commentary."
    )


def _architecture_prompt(feature_request: str) -> str:
    return f"""You are a senior software engineer designing a new feature.

Feature request: {feature_request}

Respond ONLY with valid JSON (no markdown, no extra text):

{{
  "feature_name": "FeatureName",
  "description": "What it does",
  "files_to_create": ["new_file_1.py", "new_file_2.py"],
  "files_to_modify": ["existing_file.py"],
  "classes": {{
    "ClassName1": "Purpose of this class",
    "ClassName2": "Purpose of this class"
  }},
  "methods": {{
    "method_name(args)": "What it does, returns what",
    "another_method(x, y)": "Signature and purpose"
  }},
  "tests_needed": ["test_feature_1", "test_edge_case_2"],
  "integration_steps": [
    "In existing_file.py line 45, add: import new_module",
    "In class OldClass, add call to new_feature.init()",
    "Update config.yaml with new settings"
  ]
}}
"""


def _code_prompt(spec: ArchitectureSpec, filename: str) -> str:
    return f"""Write complete, production-ready Python code for {filename}

Context:
- Feature: {spec.feature_name}
- Purpose: {spec.description}
- Required classes: {", ".join(spec.classes.keys()) or "(none)"}

Classes to implement (signatures):
{json.dumps(spec.classes, indent=2)}

Methods needed:
{json.dumps(spec.methods, indent=2)}

Requirements:
1. Complete, working code (no TODOs or placeholders)
2. Full docstrings (Google style)
3. Error handling + logging
4. Type hints throughout
5. No external dependencies unless necessary
6. Ready for production

Output ONLY the code (no markdown, no extra text):"""


def _tests_prompt(spec: ArchitectureSpec, codebase: dict[str, str]) -> str:
    snippets = []
    for filename, code in codebase.items():
        preview = code[:500]
        suffix = "..." if len(code) > 500 else ""
        snippets.append(f"# {filename}\n{preview}{suffix}")
    code_summary = "\n".join(snippets) if snippets else "(no source files generated)"
    tests = ", ".join(spec.tests_needed) or "(derive coverage from the feature)"
    return f"""Write comprehensive pytest tests for this feature:

Feature: {spec.feature_name}
Tests needed: {tests}

Code to test:
{code_summary}

Write pytest-compatible tests that cover:
1. Happy path (normal use)
2. Edge cases (empty inputs, None, etc.)
3. Error cases (exceptions, invalid data)
4. Integration with existing code

Output ONLY test code (no markdown):"""


def _integration_prompt(spec: ArchitectureSpec) -> str:
    created = ", ".join(spec.files_to_create) or "(none)"
    modified = ", ".join(spec.files_to_modify) or "(none)"
    steps = "\n".join(f"- {step}" for step in spec.integration_steps) or "- (none listed)"
    test_path = feature_test_path(spec.feature_name)
    return f"""Write a clear integration guide for merging this feature:

Feature: {spec.feature_name}

Files to add: {created}
Files to modify: {modified}

Integration steps (from architecture):
{steps}

Format:
## Integration Guide: {spec.feature_name}

### Files Added
- file1.py: purpose
- file2.py: purpose

### Files Modified
- existing.py: exact line numbers and changes

### Step-by-step
1. Create files
2. Apply modifications (with exact line numbers)
3. Update imports
4. Run tests
5. Verify

### Testing
- Run: pytest {test_path}
- Confirm all pass

Output ONLY markdown:"""
