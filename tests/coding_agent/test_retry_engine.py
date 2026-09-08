"""Unit tests for OpenRouter retry + streaming checkpoints."""

from __future__ import annotations

import json

from agents.coding_agent.retry_engine import APIRetryHandler, RetryableAPIError


def test_exponential_backoff_matches_expected_delays():
    handler = APIRetryHandler(sleep_fn=lambda _: None)
    assert handler.exponential_backoff(1) == 1.0
    assert handler.exponential_backoff(2) == 2.0
    assert handler.exponential_backoff(3) == 4.0
    assert handler.exponential_backoff(4) == 8.0


def test_should_retry_rate_limit_true_auth_false():
    handler = APIRetryHandler(sleep_fn=lambda _: None)
    assert handler.should_retry(RetryableAPIError("rate limited", 429)) is True
    assert handler.should_retry(RetryableAPIError("service unavailable", 503)) is True
    assert handler.should_retry(RetryableAPIError("unauthorized", 401)) is False
    assert handler.should_retry(RetryableAPIError("bad request", 400)) is False
    assert handler.should_retry(RuntimeError("invalid api key")) is False
    assert handler.should_retry(RuntimeError("malformed request")) is False
    assert handler.should_retry(RetryableAPIError("still limited", 429), attempt_num=4) is False


def test_wait_and_retry_sleeps_exponential_backoff():
    slept: list[float] = []
    handler = APIRetryHandler(sleep_fn=slept.append)
    handler.wait_and_retry(1, "429 Too Many Requests")
    handler.wait_and_retry(2, "429 Too Many Requests")
    assert slept == [1.0, 2.0]


def test_checkpoint_streaming_with_mock_token_stream(tmp_path):
    handler = APIRetryHandler(sleep_fn=lambda _: None, checkpoint_dir=tmp_path)
    seen: list[str] = []
    text, count = handler.checkpoint_streaming_output(
        iter(["fix", " ", "ok"]),
        on_token=seen.append,
    )
    assert text == "fix ok"
    assert count == 3
    assert seen == ["fix", " ", "ok"]
    checkpoint = tmp_path / "coding_agent_stream_default.json"
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload["text"] == "fix ok"
    assert payload["token_count"] == 3


def test_checkpoint_streaming_resumes_from_token_offset(tmp_path):
    handler = APIRetryHandler(sleep_fn=lambda _: None, checkpoint_dir=tmp_path)
    first, first_count = handler.checkpoint_streaming_output(iter(["a", "b"]))
    assert first == "ab"
    assert first_count == 2

    resumed, resumed_count = handler.checkpoint_streaming_output(
        iter(["a", "b", "c", "d"])
    )
    assert resumed == "abcd"
    assert resumed_count == 4
