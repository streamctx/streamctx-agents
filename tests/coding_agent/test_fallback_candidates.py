"""Unit tests for local fallback heuristic fix candidates."""

from __future__ import annotations

from agents.coding_agent.fallback_candidates import (
    FALLBACK_ATTESTATION,
    FALLBACK_CONFIDENCE,
    LocalFallbackGenerator,
)
from agents.coding_agent.models import FixCandidate

ERROR_TYPES = (
    "RecursionError",
    "AttributeError",
    "KeyError",
    "TypeError",
    "IndexError",
    "ImportError",
    "ValueError",  # default / generic handler
)


def test_generate_heuristic_fixes_for_each_error_type():
    gen = LocalFallbackGenerator()
    for error_type in ERROR_TYPES:
        candidates = gen.generate_heuristic_fixes(
            error_type=error_type,
            error_message=f"{error_type}: boom",
            code_context="def target():\n    pass\n",
            failed_call_id="call-42",
        )
        assert isinstance(candidates, list)
        assert candidates
        assert all(isinstance(item, FixCandidate) for item in candidates)
        for item in candidates:
            assert item.confidence == FALLBACK_CONFIDENCE
            assert item.confidence < 0.7
            assert "fallback" in item.reason
            assert item.attestation == FALLBACK_ATTESTATION
            assert item.generation_mode == "fallback"
            assert item.failed_call_id == "call-42"
            assert 2 <= len(item.candidate_code.splitlines()) <= 4 or len(
                item.candidate_code.splitlines()
            ) == 1


def test_recursion_error_heuristic_candidate():
    gen = LocalFallbackGenerator()
    candidates = gen.generate_heuristic_fixes(
        error_type="RecursionError",
        error_message="RecursionError: maximum recursion depth exceeded",
        code_context="def walk(node): return walk(node.child)",
        failed_call_id="1",
    )
    assert candidates[0].confidence == 0.55
    assert "tail-call loop" in candidates[0].candidate_code
    assert "while remaining" in candidates[0].candidate_code
