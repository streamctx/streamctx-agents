"""Detect failures, attribute root cause, and verify with counterfactual replay."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterator, Optional

from streamctx.attribution import AttributionEngine, AttributionResult
from streamctx.storage import SessionStorage, get_storage

try:
    from streamctx.replayer import CounterfactualReplayer
except ModuleNotFoundError:  # streamctx < 0.4.7 still exports replay.py
    from streamctx.replay import CounterfactualReplayer

from agents.coding_agent.auto_repair_gate import (
    ConfidenceGate,
    DISPOSITION_REJECT,
    audit_message,
)
from agents.coding_agent.failure_detector import poll_failed_calls
from agents.coding_agent.fallback_candidates import LocalFallbackGenerator
from agents.coding_agent.fix_patterns import FixPatternStore
from agents.coding_agent.models import (
    DiagnoseProposeResult,
    DiagnosisResult,
    FailedCallRecord,
    FixCandidate,
    FixPatternMatch,
    PendingApprovalEntry,
    RootCauseType,
)
from agents.coding_agent.pending_approval import (
    STATUS_AUTO_APPLIED,
    STATUS_READY_FOR_APPROVAL,
    STATUS_REJECTED,
    PendingApprovalStore,
    VERIFICATION_PENDING,
)
from agents.coding_agent.retry_engine import APIRetryHandler
from shared.audit_log import log_action
from shared.config import AGENT_IDS, STATUS_APPROVED, STATUS_PENDING
from shared.config import STATUS_REJECTED as AUDIT_REJECTED

MAX_API_ATTEMPTS = 3
API_ATTESTATION = "OpenRouter API"

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
        approval_store: Optional[PendingApprovalStore] = None,
        auto_repair_gate: Optional[ConfidenceGate] = None,
    ) -> None:
        self.storage = storage or get_storage()
        self.attribution_engine = attribution_engine or AttributionEngine(
            storage=self.storage
        )
        self.replayer = replayer or CounterfactualReplayer(storage=self.storage)
        self.pattern_store = pattern_store or FixPatternStore()
        self.approval_store = approval_store
        self.auto_repair_gate = auto_repair_gate or ConfidenceGate()

    def run(
        self,
        *,
        limit: Optional[int] = None,
        skip_call_ids: Optional[set[int]] = None,
    ) -> list[DiagnosisResult]:
        """Poll failed calls and return one diagnosis per unprocessed failure.

        Historical scan of ``sessions.db`` is deliberate (see README): this
        agent diagnoses other sessions' failed LLM calls. ``skip_call_ids``
        drops failures that already have a ``pending_approval`` row so a
        later ``run()`` does not re-diagnose them. ``limit`` then applies to
        remaining new failures.
        """
        skip = {int(call_id) for call_id in (skip_call_ids or set())}
        fetch_limit = None if skip else limit
        failures = poll_failed_calls(storage=self.storage, limit=fetch_limit)
        diagnoses: list[DiagnosisResult] = []
        for record in failures:
            if record.call_id in skip:
                continue
            diagnoses.append(self.diagnose_failure(record))
            if limit is not None and len(diagnoses) >= limit:
                break
        return diagnoses

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

    def diagnose_and_propose(
        self,
        failure: FailedCallRecord,
        *,
        code_context: str = "",
        error_message: Optional[str] = None,
        api_fn: Optional[Callable[..., list[FixCandidate]]] = None,
        stream_fn: Optional[Callable[..., Iterator[Any]]] = None,
        retry_handler: Optional[APIRetryHandler] = None,
        fallback_gen: Optional[LocalFallbackGenerator] = None,
        approval_store: Optional[PendingApprovalStore] = None,
        auto_repair_gate: Optional[ConfidenceGate] = None,
    ) -> DiagnoseProposeResult:
        """Diagnose a failure, then propose fix candidates with retry + fallback.

        Flow: diagnose → OpenRouter (up to 3 retries + backoff) → local
        heuristics if the API stays unavailable → dry-run verify →
        auto-apply when confidence is high. Auth / malformed errors
        fail hard instead of falling back.
        """
        diagnosis = self.diagnose_failure(failure)
        message = (
            error_message
            if error_message is not None
            else (failure.error_message or "")
        )
        context = code_context or _messages_as_context(failure)
        retry_handler = retry_handler or APIRetryHandler()
        fallback_gen = fallback_gen or LocalFallbackGenerator()
        error_type = diagnosis.error_type
        failed_call_id = str(failure.call_id)
        session_id = str(failure.session_id)
        is_stream = stream_fn is not None
        fix_candidates: Optional[list[FixCandidate]] = None
        final_mode = "api"

        for attempt in range(1, MAX_API_ATTEMPTS + 1):
            try:
                if is_stream:
                    response_stream = stream_fn(
                        failed_call_id, error_type, context, session_id
                    )
                    fix_text, _tokens = retry_handler.checkpoint_streaming_output(
                        response_stream,
                        checkpoint_id=failed_call_id,
                    )
                    fix_candidates = parse_candidates_from_text(
                        fix_text,
                        failed_call_id=failed_call_id,
                        error_type=error_type,
                    )
                elif api_fn is not None:
                    fix_candidates = api_fn(
                        failed_call_id, error_type, context, session_id
                    )
                else:
                    fix_candidates = call_openrouter_for_fix_candidates(
                        failed_call_id,
                        error_type,
                        context,
                        session_id,
                        error_message=message,
                    )
                break
            except Exception as exc:
                if not retry_handler.should_retry(exc):
                    raise
                if attempt < MAX_API_ATTEMPTS:
                    retry_handler.wait_and_retry(attempt, str(exc))
                    continue
                fix_candidates = fallback_gen.generate_heuristic_fixes(
                    error_type=error_type,
                    error_message=message,
                    code_context=context,
                    failed_call_id=failed_call_id,
                )
                final_mode = "fallback"
                break

        if fix_candidates is None:
            fix_candidates = []

        source_note = (
            "Generated via fallback heuristics"
            if final_mode == "fallback"
            else "Generated via API"
        )
        gate = auto_repair_gate or self.auto_repair_gate
        store = approval_store or self.approval_store
        best = _best_candidate(fix_candidates)
        dry_run_passed = (
            self._dry_run_verify_candidate(failure, best) if best else False
        )
        confidence = best.confidence if best else 0.0
        decision = gate.decide(confidence, dry_run_passed)
        auto_applied = False
        if decision.auto_apply and best is not None:
            auto_applied = bool(
                gate.apply_fix_autonomously(best, context, session_id)
            )
        disposition = (
            decision.disposition
            if auto_applied or not decision.auto_apply
            else "pending"
        )
        message_text = audit_message(
            auto_applied=auto_applied,
            candidate_source=final_mode,
            confidence=confidence,
            dry_run_passed=dry_run_passed,
            disposition=disposition,
        )
        audit_status = (
            STATUS_APPROVED
            if auto_applied
            else (
                AUDIT_REJECTED
                if disposition == DISPOSITION_REJECT
                else STATUS_PENDING
            )
        )
        audit_entry = {
            "mode": final_mode,
            "candidates_count": len(fix_candidates),
            "confidence_boost": (
                "degraded" if final_mode == "fallback" else "normal"
            ),
            "source_note": source_note,
            "generation_mode": final_mode,
            "failed_call_id": failure.call_id,
            "auto_apply": decision.auto_apply,
            "auto_applied": auto_applied,
            "dry_run_passed": dry_run_passed,
            "verification_mode": decision.verification_mode,
            "candidate_source": final_mode,
            "message": message_text,
        }
        pending_entry = None
        if store is not None:
            pending_entry = _record_auto_repair_entry(
                store,
                diagnosis=diagnosis,
                candidate=best,
                generation_mode=final_mode,
                auto_applied=auto_applied,
                verification_mode=decision.verification_mode,
                disposition=disposition,
            )
        logged = log_action(
            AGENT_IDS["coding"],
            session_id,
            "fix_candidates",
            {
                **audit_entry,
                "candidates": [
                    {
                        "candidate_code": item.candidate_code,
                        "confidence": item.confidence,
                        "reason": item.reason,
                        "attestation": item.attestation,
                    }
                    for item in fix_candidates
                ],
            },
            status=audit_status,
        )
        audit_entry["id"] = logged.get("id")
        return DiagnoseProposeResult(
            diagnosis=diagnosis,
            candidates=list(fix_candidates),
            generation_mode=final_mode,
            audit_entry=audit_entry,
            auto_apply=decision.auto_apply,
            auto_applied=auto_applied,
            dry_run_passed=dry_run_passed,
            verification_mode=decision.verification_mode,
            pending_entry=pending_entry,
        )

    def _dry_run_verify_candidate(
        self,
        failure: FailedCallRecord,
        candidate: FixCandidate,
    ) -> bool:
        """Replay the failed step with the candidate injected (no LLM calls)."""
        try:
            from_step = _resolve_replay_step(
                self.storage, failure.session_id, failure.call_id
            )
            if from_step is None:
                return False
            replay = self.replayer.replay(
                session_id=failure.session_id,
                from_step=from_step,
                with_context={
                    "role": "assistant",
                    "content": (
                        "Proposed fix candidate:\n"
                        f"{candidate.candidate_code}"
                    ),
                },
                dry_run=True,
            )
            if not replay.original_messages:
                return False
            if not replay.counterfactual_messages:
                return False
            return True
        except Exception:
            return False


def _best_candidate(candidates: list[FixCandidate]) -> Optional[FixCandidate]:
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.confidence)


def _record_auto_repair_entry(
    store: PendingApprovalStore,
    *,
    diagnosis: DiagnosisResult,
    candidate: Optional[FixCandidate],
    generation_mode: str,
    auto_applied: bool,
    verification_mode: str,
    disposition: str,
) -> Optional[PendingApprovalEntry]:
    existing = store.get_by_failed_call_id(diagnosis.failed_call_id)
    if existing is not None:
        return existing
    if auto_applied:
        status = STATUS_AUTO_APPLIED
    elif disposition == DISPOSITION_REJECT:
        status = STATUS_REJECTED
    else:
        status = STATUS_READY_FOR_APPROVAL
    return store.create_entry(
        session_id=str(diagnosis.session_id),
        root_cause=diagnosis.root_cause,
        confidence=(
            candidate.confidence if candidate is not None else diagnosis.confidence
        ),
        matched_pattern_id=(
            diagnosis.matched_pattern.signature_hash
            if diagnosis.matched_pattern
            else None
        ),
        diff=candidate.candidate_code if candidate is not None else None,
        regression_test=None,
        test_results={
            "failed_call_id": diagnosis.failed_call_id,
            "error_type": diagnosis.error_type,
            "relevant_file": diagnosis.relevant_file,
            "signature_hash": diagnosis.signature_hash,
            "replay_verified": diagnosis.replay_verified,
            "auto_applied": auto_applied,
            "verification_mode": verification_mode or VERIFICATION_PENDING,
            "generation_mode": generation_mode,
        },
        retries_used=0,
        status=status,
        generation_mode=generation_mode,
        auto_applied=auto_applied,
        verification_mode=verification_mode or VERIFICATION_PENDING,
    )


def parse_candidates_from_text(
    text: str,
    *,
    failed_call_id: str,
    error_type: str,
) -> list[FixCandidate]:
    """Parse an LLM JSON payload into ``FixCandidate`` rows."""
    payload = _extract_json_payload(text)
    raw_items: list[Any]
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        items = payload.get("candidates", payload.get("fixes"))
        if isinstance(items, list):
            raw_items = items
        else:
            raw_items = [payload]
    else:
        raise ValueError("Fix candidate payload must be a JSON object or list.")

    candidates: list[FixCandidate] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        code = str(
            item.get("candidate_code") or item.get("code") or item.get("diff") or ""
        ).strip()
        if not code:
            continue
        confidence = float(item.get("confidence") or 0.7)
        candidates.append(
            FixCandidate(
                candidate_code=code,
                confidence=confidence,
                reason=str(item.get("reason") or "OpenRouter candidate"),
                attestation=str(item.get("attestation") or API_ATTESTATION),
                error_type=str(item.get("error_type") or error_type),
                failed_call_id=str(item.get("failed_call_id") or failed_call_id),
                generation_mode="api",
            )
        )
    if not candidates:
        raise ValueError("Fix candidate JSON produced no usable candidates.")
    return candidates


def call_openrouter_for_fix_candidates(
    failed_call_id: str,
    error_type: str,
    code_context: str,
    session_id: str,
    *,
    error_message: str = "",
) -> list[FixCandidate]:
    """Ask OpenRouter for structured fix candidates (non-streaming)."""
    from openai import OpenAI

    from shared.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL

    if not OPENROUTER_API_KEY:
        raise RuntimeError("invalid api key: OPENROUTER_API_KEY is required")

    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a careful Python bug-fixing assistant. "
                    "Respond with JSON only: "
                    '{"candidates": [{"candidate_code": "...", '
                    '"confidence": 0.0, "reason": "..."}]} '
                    "Each candidate_code must be a 2-4 line snippet."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"failed_call_id={failed_call_id}\n"
                    f"session_id={session_id}\n"
                    f"error_type={error_type}\n"
                    f"error_message={error_message}\n"
                    f"code_context:\n{code_context}"
                ),
            },
        ],
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    return parse_candidates_from_text(
        raw,
        failed_call_id=str(failed_call_id),
        error_type=error_type,
    )


def _extract_json_payload(raw: str) -> Any:
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    return json.loads(text)


def _messages_as_context(failure: FailedCallRecord) -> str:
    if not failure.messages:
        return failure.error_message or ""
    parts: list[str] = []
    for message in failure.messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


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
