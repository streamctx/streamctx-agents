"""Detect failures, attribute root cause, and verify with counterfactual replay."""

from __future__ import annotations

import re
from typing import Any, Optional

from streamctx.attribution import AttributionEngine, AttributionResult
from streamctx.replay import CounterfactualReplayer
from streamctx.storage import SessionStorage, get_storage

from agents.coding_agent.failure_detector import poll_failed_calls
from agents.coding_agent.fix_patterns import FixPatternStore
from agents.coding_agent.models import (
    DiagnosisResult,
    FailedCallRecord,
    FixPatternMatch,
    RootCauseType,
)

ROOT_CAUSE_SIGNALS = ("drift", "compression", "recency")


class FailureDiagnostician:
    """
    Stage 2 pipeline: poll failures, attribute, replay-verify, pattern-match.
    """

    def __init__(
        self,
        storage: Optional[SessionStorage] = None,
        attribution_engine: Optional[AttributionEngine] = None,
        replayer: Optional[CounterfactualReplayer] = None,
        pattern_store: Optional[FixPatternStore] = None,
    ) -> None:
        self.storage = storage or get_storage()
        self.attribution_engine = attribution_engine or AttributionEngine(
            storage=self.storage
        )
        self.replayer = replayer or CounterfactualReplayer(storage=self.storage)
        self.pattern_store = pattern_store or FixPatternStore()

    def run(self, *, limit: Optional[int] = None) -> list[DiagnosisResult]:
        """Poll failed calls and return one diagnosis per failure."""
        failures = poll_failed_calls(storage=self.storage, limit=limit)
        return [self.diagnose_failure(record) for record in failures]

    def diagnose_failure(self, failure: FailedCallRecord) -> DiagnosisResult:
        """Attribute, replay-verify, and optionally match a fix pattern."""
        attributions = self.attribution_engine.attribute_session(failure.session_id)
        attribution = _find_attribution(attributions, failure.call_id)

        if attribution is None or attribution.root_cause_call_id is None:
            return _unclear_diagnosis(
                failure,
                confidence=0.0,
                reason="No attribution result for failed call.",
            )

        root_cause = _root_cause_from_attribution(attribution)
        replay_verified = self._verify_with_replay(failure, attribution, root_cause)

        if not replay_verified:
            return _unclear_diagnosis(
                failure,
                confidence=attribution.confidence,
                reason=(
                    "Counterfactual replay did not reproduce the suspected "
                    f"{root_cause} failure pattern."
                ),
                signal_breakdown=attribution.signal_breakdown,
            )

        error_type = extract_error_type(failure.error_message)
        relevant_file = extract_relevant_file(failure.error_message)
        signature_hash = FixPatternStore.compute_signature(
            error_type, root_cause, relevant_file
        )
        matched_pattern = self.pattern_store.find_match(signature_hash)

        return DiagnosisResult(
            session_id=failure.session_id,
            failed_call_id=failure.call_id,
            root_cause=root_cause,
            confidence=attribution.confidence,
            replay_verified=True,
            reason=attribution.reason,
            error_type=error_type,
            relevant_file=relevant_file,
            signature_hash=signature_hash,
            matched_pattern=matched_pattern,
            skip_to_stage4=matched_pattern is not None,
            signal_breakdown=attribution.signal_breakdown,
        )

    def _verify_with_replay(
        self,
        failure: FailedCallRecord,
        attribution: AttributionResult,
        root_cause: RootCauseType,
    ) -> bool:
        """
        Confirm the attributed root cause by running a dry-run counterfactual
        replay that modifies context in the way the root-cause type implies.
        """
        if root_cause == "UNCLEAR":
            return False

        from_step = _resolve_replay_step(
            self.storage,
            failure.session_id,
            attribution.root_cause_call_id,
        )
        if from_step is None:
            return False

        counterfactual = _build_counterfactual(
            self.replayer,
            failure.session_id,
            from_step,
            root_cause,
        )
        if counterfactual is None:
            return False

        replace_step = isinstance(counterfactual, list)
        replay = self.replayer.replay(
            session_id=failure.session_id,
            from_step=from_step,
            with_context=counterfactual,
            dry_run=True,
            replace_step=replace_step,
        )

        if root_cause == "COMPRESSION":
            diff = self.replayer.diff_replay(
                failure.session_id,
                from_step,
                counterfactual,
            )
        else:
            diff = _diff_from_messages(
                replay.original_messages,
                replay.counterfactual_messages,
            )

        return _replay_supports_root_cause(
            root_cause=root_cause,
            diff=diff,
            replay_messages=replay.counterfactual_messages,
            original_messages=replay.original_messages,
        )


def extract_error_type(error_message: Optional[str]) -> str:
    if not error_message:
        return "UnknownError"
    first_line = error_message.strip().splitlines()[0]
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)", first_line)
    if match and (first_line.endswith("Error") or first_line.endswith("Exception")):
        return match.group(1)
    if ":" in first_line:
        return first_line.split(":", 1)[0].strip() or "UnknownError"
    return first_line[:80] or "UnknownError"


def extract_relevant_file(error_message: Optional[str]) -> str:
    if not error_message:
        return "unknown"

    py_file = re.search(r'File "([^"]+\.py)"', error_message)
    if py_file:
        return py_file.group(1)

    generic_file = re.search(r"([A-Za-z0-9_./\\-]+\.py)", error_message)
    if generic_file:
        return generic_file.group(1)

    return "unknown"


def _find_attribution(
    attributions: list[AttributionResult],
    failed_call_id: int,
) -> Optional[AttributionResult]:
    for result in attributions:
        if result.failed_call_id == failed_call_id:
            return result
    return None


def _root_cause_from_attribution(attribution: AttributionResult) -> RootCauseType:
    breakdown = attribution.signal_breakdown
    if not breakdown:
        return "UNCLEAR"

    candidates = list(ROOT_CAUSE_SIGNALS)
    if attribution.root_cause_step_offset == 0:
        # Recency is always 1.0 for the failing call itself — ignore it.
        candidates = ["drift", "compression"]

    dominant = max(
        candidates,
        key=lambda signal: float(breakdown.get(signal, 0.0)),
    )
    if float(breakdown.get(dominant, 0.0)) <= 0.0:
        return "UNCLEAR"
    return dominant.upper()


def _resolve_replay_step(
    storage: SessionStorage,
    session_id: int,
    root_cause_call_id: Optional[int],
) -> Optional[int]:
    if root_cause_call_id is None:
        return None

    calls = storage.get_calls_for_session(session_id)
    for index, call in enumerate(calls):
        if int(call["id"]) == int(root_cause_call_id):
            return index
    return None


def _build_counterfactual(
    replayer: CounterfactualReplayer,
    session_id: int,
    from_step: int,
    root_cause: RootCauseType,
) -> Optional[list[dict[str, Any]] | dict[str, Any]]:
    checkpoints = replayer.list_checkpoints(session_id)
    if not checkpoints:
        return None

    if root_cause == "DRIFT":
        prior_step = max(0, from_step - 1)
        prior = replayer.replay(session_id=session_id, from_step=prior_step, dry_run=True)
        if not prior.original_messages:
            return None
        return prior.original_messages

    if root_cause == "COMPRESSION":
        return {
            "role": "system",
            "content": (
                "Restore full uncompressed context for this step. "
                "Do not rely on reused or truncated message history."
            ),
        }

    if root_cause == "RECENCY":
        earlier_step = max(0, from_step - 2)
        earlier = replayer.replay(session_id=session_id, from_step=earlier_step, dry_run=True)
        if not earlier.original_messages:
            return None
        return earlier.original_messages

    return None


def _diff_from_messages(
    original_messages: list[dict[str, Any]],
    counterfactual_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    import json

    orig_set = {json.dumps(m, sort_keys=True) for m in original_messages}
    cf_set = {json.dumps(m, sort_keys=True) for m in counterfactual_messages}
    added = [m for m in counterfactual_messages if json.dumps(m, sort_keys=True) not in orig_set]
    removed = [m for m in original_messages if json.dumps(m, sort_keys=True) not in cf_set]
    return {
        "added_messages": added,
        "removed_messages": removed,
        "unchanged_count": len(orig_set & cf_set),
    }


def _replay_supports_root_cause(
    *,
    root_cause: RootCauseType,
    diff: dict[str, Any],
    replay_messages: list[dict[str, Any]],
    original_messages: list[dict[str, Any]],
) -> bool:
    if not replay_messages or not original_messages:
        return False

    added = len(diff.get("added_messages") or [])
    removed = len(diff.get("removed_messages") or [])
    has_diff = (added + removed) > 0

    if root_cause == "DRIFT":
        return has_diff and replay_messages != original_messages

    if root_cause == "COMPRESSION":
        return added > 0

    if root_cause == "RECENCY":
        return has_diff

    return False


def _unclear_diagnosis(
    failure: FailedCallRecord,
    *,
    confidence: float,
    reason: str,
    signal_breakdown: Optional[dict[str, float]] = None,
) -> DiagnosisResult:
    error_type = extract_error_type(failure.error_message)
    relevant_file = extract_relevant_file(failure.error_message)
    signature_hash = FixPatternStore.compute_signature(
        error_type, "UNCLEAR", relevant_file
    )
    return DiagnosisResult(
        session_id=failure.session_id,
        failed_call_id=failure.call_id,
        root_cause="UNCLEAR",
        confidence=confidence,
        replay_verified=False,
        reason=reason,
        error_type=error_type,
        relevant_file=relevant_file,
        signature_hash=signature_hash,
        matched_pattern=None,
        skip_to_stage4=False,
        signal_breakdown=signal_breakdown or {},
    )
