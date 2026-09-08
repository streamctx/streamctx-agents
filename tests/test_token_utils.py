"""
tests/test_token_utils.py
Expected behavior for sample_target/token_utils.py — currently FAILING
because of the intentional bug. The Coding Agent should reproduce this
failure first, then propose a fix.
"""

from agents.coding_agent.sample_target.token_utils import (
    compression_ratio,
    average_savings,
)


def test_compression_ratio_basic():
    # 100 tokens -> 40 tokens = 60% saved
    assert compression_ratio(100, 40) == 60.0


def test_compression_ratio_zero_original():
    # Should not crash; 0 original tokens means 0% saved (no data to save)
    assert compression_ratio(0, 0) == 0.0


def test_average_savings():
    sessions = [(100, 40), (200, 100)]  # 60% and 50% saved
    assert average_savings(sessions) == 55.0


def test_average_savings_empty():
    # Should not crash on empty input; 0 sessions = 0.0 average
    assert average_savings([]) == 0.0