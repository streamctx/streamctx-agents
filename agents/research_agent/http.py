"""stdlib HTTP fetch with retries/backoff. Same contract as competitor_agent.http."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_TIMEOUT = 20
DEFAULT_USER_AGENT = "streamctx-research-agent/0.1"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
SleepFn = Callable[[float], None]


class FetchError(RuntimeError):
    def __init__(self, status: int, body: str, url: str) -> None:
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")


def fetch_text(
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 3,
    backoff_base_seconds: float = 1.0,
    max_backoff_seconds: float = 30.0,
    sleep_fn: Optional[SleepFn] = None,
) -> str:
    return _request(
        url,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
        backoff_base_seconds=backoff_base_seconds,
        max_backoff_seconds=max_backoff_seconds,
        sleep_fn=sleep_fn,
    )


def fetch_json(
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 3,
    backoff_base_seconds: float = 1.0,
    max_backoff_seconds: float = 30.0,
    sleep_fn: Optional[SleepFn] = None,
) -> Any:
    raw = fetch_text(
        url,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
        backoff_base_seconds=backoff_base_seconds,
        max_backoff_seconds=max_backoff_seconds,
        sleep_fn=sleep_fn,
    )
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FetchError(200, f"non-JSON response: {raw[:300]}", url) from exc


def github_headers(user_agent: str, token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": user_agent,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(
    url: str,
    *,
    headers: Optional[Mapping[str, str]],
    timeout: float,
    max_retries: int,
    backoff_base_seconds: float,
    max_backoff_seconds: float,
    sleep_fn: Optional[SleepFn],
) -> str:
    sleeper = sleep_fn or time.sleep
    attempts = max(1, int(max_retries))
    delay = max(0.1, float(backoff_base_seconds))
    cap = max(delay, float(max_backoff_seconds))
    hdrs = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)

    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        request = Request(url, headers=hdrs, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            last_error = FetchError(int(exc.code), body, url)
            if int(exc.code) not in RETRY_STATUSES or attempt >= attempts - 1:
                raise last_error from exc
            sleeper(_retry_delay(exc, delay, cap))
            delay = min(cap, delay * 2)
        except URLError as exc:
            last_error = FetchError(0, str(exc.reason), url)
            if attempt >= attempts - 1:
                raise last_error from exc
            sleeper(min(cap, delay))
            delay = min(cap, delay * 2)

    raise last_error or FetchError(0, "request failed", url)


def _retry_delay(exc: HTTPError, delay: float, cap: float) -> float:
    raw = ""
    if exc.headers is not None:
        raw = str(exc.headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return min(cap, float(raw))
    return min(cap, delay)
