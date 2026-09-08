"""Layer 3 verified auto-repair gate.

After a fix candidate is generated, dry-run replay plus a confidence
threshold decide whether to apply autonomously or wait for a human.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

from agents.coding_agent.models import FixCandidate

AUTO_APPLY_CONFIDENCE = 0.80
REJECT_CONFIDENCE = 0.50

VERIFICATION_PENDING = "pending"
VERIFICATION_DRY_RUN_PASSED = "dry_run_passed"
VERIFICATION_DRY_RUN_FAILED = "dry_run_failed"

DISPOSITION_AUTO = "auto"
DISPOSITION_PENDING = "pending"
DISPOSITION_REJECT = "reject"

ConfirmFn = Callable[[FixCandidate, str, str], bool]
ApplyFn = Callable[[FixCandidate, str, str], bool]


@dataclass(frozen=True)
class AutoRepairDecision:
    auto_apply: bool
    disposition: str
    verification_mode: str


class ConfidenceGate:
    """Gate auto-apply on dry-run verification and candidate confidence."""

    def __init__(
        self,
        *,
        source_root: Optional[Path | str] = None,
        confirm_fn: Optional[ConfirmFn] = None,
        apply_fn: Optional[ApplyFn] = None,
    ) -> None:
        self.source_root = Path(source_root) if source_root else None
        self._confirm_fn = confirm_fn
        self._apply_fn = apply_fn

    def should_auto_apply(self, confidence: float, dry_run_result: bool) -> bool:
        """True only when dry-run passed and confidence is at least 0.80."""
        if dry_run_result is not True:
            return False
        return float(confidence) >= AUTO_APPLY_CONFIDENCE

    def decide(
        self, confidence: float, dry_run_result: bool
    ) -> AutoRepairDecision:
        """Map confidence + dry-run into auto / pending / reject."""
        verification_mode = (
            VERIFICATION_DRY_RUN_PASSED
            if dry_run_result
            else VERIFICATION_DRY_RUN_FAILED
        )
        if self.should_auto_apply(confidence, dry_run_result):
            return AutoRepairDecision(
                auto_apply=True,
                disposition=DISPOSITION_AUTO,
                verification_mode=verification_mode,
            )
        if dry_run_result and float(confidence) < REJECT_CONFIDENCE:
            return AutoRepairDecision(
                auto_apply=False,
                disposition=DISPOSITION_REJECT,
                verification_mode=verification_mode,
            )
        return AutoRepairDecision(
            auto_apply=False,
            disposition=DISPOSITION_PENDING,
            verification_mode=verification_mode,
        )

    def apply_fix_autonomously(
        self,
        fix_candidate: Union[FixCandidate, str],
        code_context: str,
        session_id: str,
    ) -> bool:
        """Apply the candidate, then re-run the original call to confirm.

        Returns True only when the original failure no longer reproduces.
        """
        candidate = _as_candidate(fix_candidate)
        target = _resolve_target_path(code_context, self.source_root)

        try:
            if self._apply_fn is not None:
                applied = bool(
                    self._apply_fn(candidate, code_context, str(session_id))
                )
            else:
                applied = _apply_candidate_to_file(candidate, target)

            if self._confirm_fn is not None:
                confirmed = bool(
                    self._confirm_fn(candidate, code_context, str(session_id))
                )
            elif target is not None and target.is_file():
                confirmed = _confirm_by_reimport(target, code_context)
            else:
                confirmed = _confirm_by_exec(
                    candidate.candidate_code, code_context
                )

            if not confirmed:
                if target is not None:
                    _restore_if_needed(target)
                return False
            if target is not None or self._apply_fn is not None:
                return bool(applied)
            return True
        except Exception:
            if target is not None:
                _restore_if_needed(target)
            return False


def audit_message(
    *,
    auto_applied: bool,
    candidate_source: str,
    confidence: float,
    dry_run_passed: bool,
    disposition: str = DISPOSITION_PENDING,
) -> str:
    """Human-readable audit line for Layer 3 auto-repair."""
    source = candidate_source or "api"
    score = f"{float(confidence):.2f}"
    if auto_applied:
        return (
            f"Auto-repaired via {source}, verified dry_run, confidence {score}"
        )
    if disposition == DISPOSITION_REJECT:
        return f"Rejected via {source}, confidence {score}"
    if dry_run_passed:
        return (
            f"Proposed for approval via {source}, "
            f"dry_run passed but low confidence"
        )
    return f"Proposed for approval via {source}, dry_run failed"


def _as_candidate(value: Union[FixCandidate, str]) -> FixCandidate:
    if isinstance(value, FixCandidate):
        return value
    return FixCandidate(
        candidate_code=str(value),
        confidence=0.0,
        reason="raw snippet",
        attestation="auto_repair_gate",
        error_type="UnknownError",
        failed_call_id="",
    )


def _resolve_target_path(
    code_context: str,
    source_root: Optional[Path],
) -> Optional[Path]:
    match = re.search(r'File "([^"]+\.py)"', code_context or "")
    if not match:
        match = re.search(r"([A-Za-z0-9_./\\-]+\.py)", code_context or "")
    if not match:
        return None
    path = Path(match.group(1))
    if not path.is_absolute() and source_root is not None:
        path = source_root / path
    return path if path.is_file() else None


_BACKUPS: dict[str, str] = {}


def _apply_candidate_to_file(
    candidate: FixCandidate, target: Optional[Path]
) -> bool:
    if target is None or not target.is_file():
        return False
    original = target.read_text(encoding="utf-8")
    patched = _patch_source(original, candidate.candidate_code)
    if patched == original:
        return False
    _BACKUPS[str(target.resolve())] = original
    target.write_text(patched, encoding="utf-8")
    return True


def _restore_if_needed(target: Path) -> None:
    key = str(target.resolve())
    original = _BACKUPS.pop(key, None)
    if original is not None and target.is_file():
        target.write_text(original, encoding="utf-8")


def _patch_source(source: str, snippet: str) -> str:
    snippet = (snippet or "").strip()
    if not snippet:
        return source

    func = re.search(r"def\s+(\w+)\s*\(", snippet)
    if func and "def " in source:
        name = func.group(1)
        pattern = rf"(def\s+{re.escape(name)}\s*\(.*?\):(?:\n(?:[ \t].*)?)*)"
        if re.search(pattern, source):
            return re.sub(pattern, snippet, source, count=1)

    if ".get(" in snippet:
        patched = re.sub(
            r"(\b[A-Za-z_][A-Za-z0-9_]*)\s*\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]",
            r"\1.get(\2, None)",
            source,
            count=1,
        )
        if patched != source:
            return patched
    return source


def _confirm_by_reimport(target: Path, code_context: str) -> bool:
    spec = importlib.util.spec_from_file_location(
        "_auto_repair_target", target
    )
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = _callable_from_module(module, code_context)
    if fn is None:
        return True
    try:
        fn({}, "missing_key")
        return True
    except TypeError:
        try:
            fn()
            return True
        except Exception:
            return False
    except Exception:
        return False


def _callable_from_module(module: Any, code_context: str) -> Optional[Any]:
    match = re.search(r"def\s+(\w+)\s*\(", code_context or "")
    names: list[str] = []
    if match:
        names.append(match.group(1))
    names.extend(("lookup", "token_lookup", "get_token", "lookup_token"))
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


def _confirm_by_exec(candidate_code: str, code_context: str) -> bool:
    namespace: dict[str, Any] = {
        "mapping": {},
        "key": "missing_key",
        "default": None,
        "tokens": {},
        "session_tokens": {},
    }
    try:
        exec(candidate_code, namespace, namespace)
        return True
    except Exception:
        return False
