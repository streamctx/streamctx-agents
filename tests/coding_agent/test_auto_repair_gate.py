"""Layer 3 verified auto-repair gate and diagnose_and_propose integration."""

from __future__ import annotations

import importlib.util
import json
from unittest.mock import patch

import pytest

from agents.coding_agent.auto_repair_gate import (
    AUTO_APPLY_CONFIDENCE,
    ConfidenceGate,
    DISPOSITION_AUTO,
    DISPOSITION_PENDING,
    DISPOSITION_REJECT,
    VERIFICATION_DRY_RUN_FAILED,
    VERIFICATION_DRY_RUN_PASSED,
    audit_message,
)
from agents.coding_agent.diagnose import FailureDiagnostician, parse_candidates_from_text
from agents.coding_agent.models import FailedCallRecord, FixCandidate
from agents.coding_agent.pending_approval import (
    STATUS_AUTO_APPLIED,
    STATUS_READY_FOR_APPROVAL,
    STATUS_REJECTED,
    PendingApprovalStore,
)
from agents.coding_agent.retry_engine import APIRetryHandler
from streamctx.storage import SessionStorage
from tests.test_diagnose import _seed_drift_failure


def _handler(tmp_path=None):
    return APIRetryHandler(sleep_fn=lambda _: None, checkpoint_dir=tmp_path)


def _candidate(**overrides) -> FixCandidate:
    base = dict(
        candidate_code="value = mapping.get(key, default)",
        confidence=0.85,
        reason="use dict.get",
        attestation="test",
        error_type="KeyError",
        failed_call_id="1",
        generation_mode="api",
    )
    base.update(overrides)
    return FixCandidate(**base)


def _seed_keyerror_failure(
    storage: SessionStorage, relevant_file: str = "lookup.py"
) -> FailedCallRecord:
    session_id = storage.start_session()
    error_message = (
        "KeyError: 'missing'\n"
        f'  File "{relevant_file}", line 2, in lookup_token'
    )
    messages_by_step = [
        [{"role": "user", "content": "Look up a missing token key"}],
        [
            {"role": "user", "content": "Look up a missing token key"},
            {"role": "assistant", "content": "session_tokens[key] raises KeyError"},
        ],
        [
            {"role": "user", "content": "Look up a missing token key"},
            {"role": "assistant", "content": "session_tokens[key] raises KeyError"},
            {"role": "user", "content": "pytest failed on lookup_token missing key"},
        ],
    ]
    for step, messages in enumerate(messages_by_step):
        storage.record_call(
            session_id=session_id,
            provider="openrouter",
            model="test-model",
            input_tokens=100 + (step * 250),
            output_tokens=25,
            cost=0.0,
            reused_tokens=10,
            waste_category="drift" if step == 2 else None,
            messages=messages,
            failed=step == 2,
            error_message=error_message if step == 2 else None,
        )
        storage.save_checkpoint(
            session_id=session_id,
            step_number=step,
            messages=messages,
        )
    rows = storage.get_calls_for_session(session_id)
    failed = next(row for row in rows if row["failed"])
    return FailedCallRecord(
        call_id=int(failed["id"]),
        session_id=session_id,
        error_message=str(failed["error_message"]),
        timestamp=str(failed["timestamp"]),
        messages=messages_by_step[-1],
    )


def _api_fn(failure: FailedCallRecord, *, code: str, confidence: float):
    payload = {
        "candidates": [
            {
                "candidate_code": code,
                "confidence": confidence,
                "reason": "test candidate",
            }
        ]
    }

    def api_fn(*_args):
        return parse_candidates_from_text(
            json.dumps(payload),
            failed_call_id=str(failure.call_id),
            error_type="KeyError",
        )

    return api_fn


def _store(tmp_path) -> PendingApprovalStore:
    return PendingApprovalStore(
        db_path=tmp_path / "coding_agent.db",
        notifier=lambda _entry: None,
        enable_default_notifier=False,
    )


def test_should_auto_apply_requires_dry_run_and_high_confidence():
    gate = ConfidenceGate()
    assert gate.should_auto_apply(0.80, True) is True
    assert gate.should_auto_apply(0.85, True) is True
    assert gate.should_auto_apply(0.65, True) is False
    assert gate.should_auto_apply(0.40, True) is False
    assert gate.should_auto_apply(0.99, False) is False
    assert gate.should_auto_apply(AUTO_APPLY_CONFIDENCE, False) is False


def test_confidence_gate_dispositions():
    gate = ConfidenceGate()
    auto = gate.decide(0.85, True)
    pending = gate.decide(0.65, True)
    reject = gate.decide(0.40, True)
    skipped = gate.decide(0.85, False)

    assert auto.auto_apply is True
    assert auto.disposition == DISPOSITION_AUTO
    assert auto.verification_mode == VERIFICATION_DRY_RUN_PASSED

    assert pending.auto_apply is False
    assert pending.disposition == DISPOSITION_PENDING
    assert pending.verification_mode == VERIFICATION_DRY_RUN_PASSED

    assert reject.auto_apply is False
    assert reject.disposition == DISPOSITION_REJECT
    assert reject.verification_mode == VERIFICATION_DRY_RUN_PASSED

    assert skipped.auto_apply is False
    assert skipped.disposition == DISPOSITION_PENDING
    assert skipped.verification_mode == VERIFICATION_DRY_RUN_FAILED


def test_audit_message_formats():
    auto = audit_message(
        auto_applied=True,
        candidate_source="api",
        confidence=0.85,
        dry_run_passed=True,
    )
    pending = audit_message(
        auto_applied=False,
        candidate_source="fallback",
        confidence=0.65,
        dry_run_passed=True,
    )
    assert auto == "Auto-repaired via api, verified dry_run, confidence 0.85"
    assert pending == (
        "Proposed for approval via fallback, dry_run passed but low confidence"
    )


def test_apply_fix_autonomously_confirms_keyerror_resolution(tmp_path):
    target = tmp_path / "lookup.py"
    target.write_text(
        "def lookup(tokens, key):\n    return tokens[key]\n",
        encoding="utf-8",
    )
    gate = ConfidenceGate(source_root=tmp_path)
    context = (
        f'File "{target.name}"\n'
        "def lookup(tokens, key):\n    return tokens[key]\n"
    )
    assert (
        gate.apply_fix_autonomously(_candidate(), context, session_id="1") is True
    )
    patched = target.read_text(encoding="utf-8")
    assert ".get(" in patched

    spec = importlib.util.spec_from_file_location("lookup", target)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    assert module.lookup({}, "missing") is None


@patch(
    "agents.coding_agent.diagnose.log_action",
    side_effect=lambda *args, **kwargs: {"id": "audit-auto"},
)
def test_dry_run_pass_triggers_auto_apply(mock_log, storage, tmp_path):
    failure = _seed_drift_failure(storage)
    store = _store(tmp_path)
    result = FailureDiagnostician(
        storage=storage,
        approval_store=store,
    ).diagnose_and_propose(
        failure,
        api_fn=_api_fn(
            failure,
            code="value = mapping.get(key, None)",
            confidence=0.85,
        ),
        retry_handler=_handler(tmp_path),
    )
    assert result.dry_run_passed is True
    assert result.auto_apply is True
    assert result.auto_applied is True
    assert result.verification_mode == VERIFICATION_DRY_RUN_PASSED
    assert result.pending_entry is not None
    assert result.pending_entry.auto_applied is True
    assert result.pending_entry.verification_mode == VERIFICATION_DRY_RUN_PASSED
    assert result.pending_entry.status == STATUS_AUTO_APPLIED
    assert result.audit_entry["message"] == (
        "Auto-repaired via api, verified dry_run, confidence 0.85"
    )
    mock_log.assert_called_once()
    store.close()


@patch(
    "agents.coding_agent.diagnose.log_action",
    side_effect=lambda *args, **kwargs: {"id": "audit-skip"},
)
def test_dry_run_fail_skips_auto_apply(mock_log, storage, tmp_path):
    failure = _seed_drift_failure(storage)
    store = _store(tmp_path)
    diagnostician = FailureDiagnostician(
        storage=storage,
        approval_store=store,
    )
    with patch.object(
        diagnostician, "_dry_run_verify_candidate", return_value=False
    ):
        result = diagnostician.diagnose_and_propose(
            failure,
            api_fn=_api_fn(
                failure,
                code="value = mapping.get(key, None)",
                confidence=0.85,
            ),
            retry_handler=_handler(tmp_path),
        )
    assert result.dry_run_passed is False
    assert result.auto_apply is False
    assert result.auto_applied is False
    assert result.verification_mode == VERIFICATION_DRY_RUN_FAILED
    assert result.pending_entry is not None
    assert result.pending_entry.auto_applied is False
    assert result.pending_entry.verification_mode == VERIFICATION_DRY_RUN_FAILED
    assert result.pending_entry.status == STATUS_READY_FOR_APPROVAL
    assert "dry_run failed" in result.audit_entry["message"]
    mock_log.assert_called_once()
    store.close()


@patch(
    "agents.coding_agent.diagnose.log_action",
    side_effect=lambda *args, **kwargs: {"id": "audit-gate"},
)
@pytest.mark.parametrize(
    ("confidence", "expect_auto", "expect_status", "expect_disposition"),
    [
        (0.85, True, STATUS_AUTO_APPLIED, DISPOSITION_AUTO),
        (0.65, False, STATUS_READY_FOR_APPROVAL, DISPOSITION_PENDING),
        (0.40, False, STATUS_REJECTED, DISPOSITION_REJECT),
    ],
)
def test_confidence_gate_auto_pending_reject(
    mock_log,
    storage,
    tmp_path,
    confidence,
    expect_auto,
    expect_status,
    expect_disposition,
):
    failure = _seed_drift_failure(storage)
    store = _store(tmp_path)
    result = FailureDiagnostician(
        storage=storage,
        approval_store=store,
    ).diagnose_and_propose(
        failure,
        api_fn=_api_fn(
            failure,
            code="value = mapping.get(key, None)",
            confidence=confidence,
        ),
        retry_handler=_handler(tmp_path),
    )
    assert result.dry_run_passed is True
    assert result.auto_apply is expect_auto
    assert result.auto_applied is expect_auto
    assert result.pending_entry is not None
    assert result.pending_entry.status == expect_status
    if expect_disposition == DISPOSITION_PENDING:
        assert result.audit_entry["message"] == (
            "Proposed for approval via api, dry_run passed but low confidence"
        )
    elif expect_disposition == DISPOSITION_REJECT:
        assert "Rejected via api" in result.audit_entry["message"]
    else:
        assert result.audit_entry["message"].startswith("Auto-repaired via api")
    store.close()


@patch(
    "agents.coding_agent.diagnose.log_action",
    side_effect=lambda *args, **kwargs: {"id": "audit-keyerror"},
)
def test_keyerror_autonomous_flow_logged_and_verified(mock_log, storage, tmp_path):
    target = tmp_path / "token_utils.py"
    target.write_text(
        "def lookup_token(session_tokens, key):\n"
        "    return session_tokens[key]\n",
        encoding="utf-8",
    )
    source = target.read_text(encoding="utf-8")
    failure = _seed_keyerror_failure(storage, relevant_file=target.name)
    store = _store(tmp_path)
    gate = ConfidenceGate(source_root=tmp_path)

    result = FailureDiagnostician(
        storage=storage,
        approval_store=store,
        auto_repair_gate=gate,
    ).diagnose_and_propose(
        failure,
        code_context=f'File "{target.name}"\n{source}',
        error_message="KeyError: 'missing'",
        api_fn=_api_fn(
            failure,
            code="value = mapping.get(key, default)",
            confidence=0.92,
        ),
        retry_handler=_handler(tmp_path),
    )

    assert result.dry_run_passed is True
    assert result.auto_apply is True
    assert result.auto_applied is True
    assert result.pending_entry is not None
    assert result.pending_entry.auto_applied is True
    assert result.pending_entry.verification_mode == VERIFICATION_DRY_RUN_PASSED
    assert result.pending_entry.status == STATUS_AUTO_APPLIED
    assert result.audit_entry["message"] == (
        "Auto-repaired via api, verified dry_run, confidence 0.92"
    )
    patched = target.read_text(encoding="utf-8")
    assert ".get(" in patched

    spec = importlib.util.spec_from_file_location("token_utils_fixed", target)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    assert module.lookup_token({}, "missing") is None
    mock_log.assert_called_once()
    store.close()
