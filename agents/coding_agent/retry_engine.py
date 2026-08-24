"""Retry + streaming checkpoint helpers for OpenRouter rate-limit resilience."""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = frozenset({429, 503})
NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 404, 422})
_AUTH_HINTS = (
    "auth",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "invalid_api_key",
    "authentication",
    "permission",
)
_MALFORMED_HINTS = (
    "malformed",
    "invalid request",
    "bad request",
    "validation error",
    "invalid_request",
)
_RATE_LIMIT_HINTS = ("rate limit", "too many requests", "429")
_UNAVAILABLE_HINTS = ("service unavailable", "503")

SleepFn = Callable[[float], None]
TokenCallback = Callable[[str], None]


class RetryableAPIError(Exception):
    """HTTP-like error with a status code, used by tests and callers."""

    def __init__(self, message: str, status_code: int = 429) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


class APIRetryHandler:
    """Exponential backoff, retry classification, and stream checkpoints."""

    def __init__(
        self,
        *,
        sleep_fn: Optional[SleepFn] = None,
        checkpoint_dir: Optional[Path | str] = None,
        max_attempts: int = MAX_ATTEMPTS,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._sleep = sleep_fn or time.sleep
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else Path(
            tempfile.gettempdir()
        )
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max(1, int(max_attempts))
        self._log = logger_ or logger
        self._attempt_count = 0

    def exponential_backoff(self, attempt_num: int) -> float:
        """Return 1s, 2s, 4s, 8s for attempts 1-4."""
        clamped = max(1, int(attempt_num))
        return float(2 ** (clamped - 1))

    def should_retry(
        self,
        error: Exception,
        attempt_num: Optional[int] = None,
    ) -> bool:
        """True only for transient 429/503 failures within the attempt budget."""
        attempts = self._attempt_count if attempt_num is None else int(attempt_num)
        if attempts > self.max_attempts:
            return False

        status = _extract_status_code(error)
        if status in RETRYABLE_STATUS_CODES:
            return True
        if status in NON_RETRYABLE_STATUS_CODES:
            return False

        text = _error_text(error)
        if _contains_any(text, _AUTH_HINTS) or _contains_any(text, _MALFORMED_HINTS):
            return False
        if _contains_any(text, _RATE_LIMIT_HINTS) or _contains_any(
            text, _UNAVAILABLE_HINTS
        ):
            return True
        return False

    def checkpoint_streaming_output(
        self,
        token_stream: Iterator[Any],
        *,
        checkpoint_id: str = "default",
        resume: bool = True,
        on_token: Optional[TokenCallback] = None,
    ) -> tuple[str, int]:
        """Consume a token stream, checkpointing each token as it arrives.

        Tokens are written to a temp file in real time via ``on_token`` (and
        the checkpoint file) so a paused call can resume from the last offset.

        Returns ``(full_accumulated_text, token_count_processed)``.
        """
        path = self._checkpoint_path(checkpoint_id)
        state = (
            self._load_checkpoint(path)
            if resume
            else {"text": "", "token_count": 0}
        )
        offset = int(state.get("token_count") or 0)
        text = str(state.get("text") or "")
        skip_remaining = offset if resume else 0
        count = offset

        for raw in token_stream:
            token = _coerce_token(raw)
            if skip_remaining > 0:
                skip_remaining -= 1
                continue
            text += token
            count += 1
            self._write_checkpoint(path, text, count)
            if on_token is not None:
                on_token(token)

        return text, count

    def wait_and_retry(self, current_attempt: int, error_message: str) -> None:
        """Log the failed attempt and sleep for the exponential backoff delay."""
        delay = self.exponential_backoff(current_attempt)
        self._attempt_count = max(self._attempt_count, int(current_attempt))
        self._log.info(
            "Retry attempt %s/%s after error: %s; sleeping %.1fs",
            current_attempt,
            self.max_attempts,
            error_message,
            delay,
        )
        self._sleep(delay)

    def _checkpoint_path(self, checkpoint_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", checkpoint_id) or "default"
        return self.checkpoint_dir / f"coding_agent_stream_{safe}.json"

    def _load_checkpoint(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {"text": "", "token_count": 0}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"text": "", "token_count": 0}
        if not isinstance(payload, dict):
            return {"text": "", "token_count": 0}
        return {
            "text": str(payload.get("text") or ""),
            "token_count": int(payload.get("token_count") or 0),
        }

    def _write_checkpoint(self, path: Path, text: str, token_count: int) -> None:
        payload = json.dumps(
            {"text": text, "token_count": token_count},
            ensure_ascii=False,
        )
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)


def _extract_status_code(error: Exception) -> Optional[int]:
    for attr in ("status_code", "status", "http_status"):
        value = getattr(error, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    response = getattr(error, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    code = getattr(error, "code", None)
    if isinstance(code, int) and 100 <= code <= 599:
        return code
    match = re.search(r"\b(429|503|401|403|400|422|404)\b", str(error))
    if match:
        return int(match.group(1))
    return None


def _error_text(error: Exception) -> str:
    parts = [type(error).__name__, str(error)]
    return " ".join(parts).lower()


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)


def _coerce_token(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    content = getattr(raw, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(raw, dict) and "content" in raw:
        return str(raw.get("content") or "")
    return str(raw)
