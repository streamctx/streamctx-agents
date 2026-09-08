"""Retry + fallback integration for diagnose_and_propose."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agents.coding_agent.diagnose import FailureDiagnostician, parse_candidates_from_text
from agents.coding_agent.fix_generator import FixGenerator, build_fix_request
from agents.coding_agent.models import DiagnosisResult, FixProposal
from agents.coding_agent.retry_engine import APIRetryHandler, RetryableAPIError
from tests.test_diagnose import _seed_drift_failure


def _handler(tmp_path=None):
    return APIRetryHandler(sleep_fn=lambda _: None, checkpoint_dir=tmp_path)


@patch(
    "agents.coding_agent.diagnose.log_action",
    side_effect=lambda *args, **kwargs: {"id": "audit-1"},
)
def test_diagnose_and_propose_uses_api_on_success(mock_log, storage, tmp_path):
    failure = _seed_drift_failure(storage)
    candidate_json = {
        "candidates": [
            {
                "candidate_code": "value = mapping.get(key, None)",
                "confidence": 0.82,
                "reason": "use dict.get",
            }
        ]
    }

    def api_fn(*_args):
        return parse_candidates_from_text(
            json.dumps(candidate_json),
            failed_call_id=str(failure.call_id),
            error_type="RuntimeError",
        )

    diagnostician = FailureDiagnostician(storage=storage)
    result = diagnostician.diagnose_and_propose(
        failure,
        api_fn=api_fn,
        retry_handler=_handler(tmp_path),
    )

    assert result.generation_mode == "api"
    assert len(result.candidates) == 1
    assert result.candidates[0].candidate_code.startswith("value = mapping.get")
    assert result.audit_entry["confidence_boost"] == "normal"
    assert result.audit_entry["source_note"] == "Generated via API"
    mock_log.assert_called_once()


@patch(
    "agents.coding_agent.diagnose.log_action",
    side_effect=lambda *args, **kwargs: {"id": "audit-2"},
)
def test_diagnose_and_propose_retries_then_succeeds(mock_log, storage, tmp_path):
    failure = _seed_drift_failure(storage)
    calls = {"n": 0}

    def api_fn(*_args):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableAPIError("rate limited", 429)
        return parse_candidates_from_text(
            json.dumps(
                {
                    "candidates": [
                        {
                            "candidate_code": "ok = True",
                            "confidence": 0.9,
                            "reason": "recovered",
                        }
                    ]
                }
            ),
            failed_call_id=str(failure.call_id),
            error_type="RuntimeError",
        )

    result = FailureDiagnostician(storage=storage).diagnose_and_propose(
        failure,
        api_fn=api_fn,
        retry_handler=_handler(tmp_path),
    )
    assert calls["n"] == 3
    assert result.generation_mode == "api"
    assert result.candidates[0].candidate_code == "ok = True"


@patch(
    "agents.coding_agent.diagnose.log_action",
    side_effect=lambda *args, **kwargs: {"id": "audit-3"},
)
def test_diagnose_and_propose_falls_back_after_three_retries(mock_log, storage, tmp_path):
    failure = _seed_drift_failure(storage)
    calls = {"n": 0}

    def api_fn(*_args):
        calls["n"] += 1
        raise RetryableAPIError("rate limited", 429)

    result = FailureDiagnostician(storage=storage).diagnose_and_propose(
        failure,
        error_message="RecursionError: maximum recursion depth exceeded",
        code_context="def walk(node): return walk(node.child)",
        api_fn=api_fn,
        retry_handler=_handler(tmp_path),
    )
    assert calls["n"] == 3
    assert result.generation_mode == "fallback"
    assert result.audit_entry["generation_mode"] == "fallback"
    assert result.candidates
    assert result.candidates[0].confidence == 0.55
    assert "fallback" in result.candidates[0].reason
    assert result.audit_entry["confidence_boost"] == "degraded"
    assert result.audit_entry["source_note"] == "Generated via fallback heuristics"


@patch(
    "agents.coding_agent.diagnose.log_action",
    side_effect=lambda *args, **kwargs: {"id": "audit-4"},
)
def test_diagnose_and_propose_raises_on_auth_error(mock_log, storage, tmp_path):
    failure = _seed_drift_failure(storage)

    def api_fn(*_args):
        raise RetryableAPIError("unauthorized", 401)

    with pytest.raises(RetryableAPIError):
        FailureDiagnostician(storage=storage).diagnose_and_propose(
            failure,
            api_fn=api_fn,
            retry_handler=_handler(tmp_path),
        )
    mock_log.assert_not_called()


@patch(
    "agents.coding_agent.diagnose.log_action",
    side_effect=lambda *args, **kwargs: {"id": "audit-5"},
)
def test_diagnose_and_propose_checkpoints_streaming_response(
    mock_log, storage, tmp_path
):
    failure = _seed_drift_failure(storage)
    payload = json.dumps(
        {
            "candidates": [
                {
                    "candidate_code": "item = items[index] if items else None",
                    "confidence": 0.77,
                    "reason": "guard index",
                }
            ]
        }
    )

    def stream_fn(*_args):
        return iter([payload])

    result = FailureDiagnostician(storage=storage).diagnose_and_propose(
        failure,
        stream_fn=stream_fn,
        retry_handler=_handler(tmp_path),
    )
    assert result.generation_mode == "api"
    assert result.candidates[0].candidate_code.startswith("item = items[index]")


def test_fix_generator_falls_back_after_rate_limits():
    calls = {"n": 0}

    class BoomGenerator(FixGenerator):
        def _call_llm(self, request):
            calls["n"] += 1
            raise RetryableAPIError("rate limited", 429)

    diagnosis = DiagnosisResult(
        session_id=1,
        failed_call_id=9,
        root_cause="DRIFT",
        confidence=0.9,
        replay_verified=True,
        reason="drift",
        error_type="RecursionError",
        relevant_file="foo.py",
        signature_hash="sig",
    )
    request = build_fix_request(
        diagnosis,
        error_message="RecursionError: boom",
        source_contents={"foo.py": "def walk(n):\n    return walk(n)\n"},
    )
    generator = BoomGenerator(retry_handler=_handler())
    proposal = generator.generate(request)
    assert calls["n"] == 3
    assert isinstance(proposal, FixProposal)
    assert proposal.generation_mode == "fallback"
    assert "Generated via fallback heuristics" in proposal.diff
    assert "tail-call loop" in proposal.diff or "max_depth" in proposal.diff
