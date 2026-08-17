"""Minimal JSON/form HTTP client using stdlib urllib. No extra packages."""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_TIMEOUT = 20
DEFAULT_USER_AGENT = "streamctx-marketing-agent/0.1"


class JsonHttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str) -> None:
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")


class JsonHttpClient:
    """POST/GET JSON (and form-encoded POST) via urllib."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def post_json(
        self,
        url: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        form: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        basic_auth: Optional[tuple[str, str]] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            url,
            json_body=json_body,
            form=form,
            headers=headers,
            basic_auth=basic_auth,
        )

    def get_json(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        basic_auth: Optional[tuple[str, str]] = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            url,
            headers=headers,
            basic_auth=basic_auth,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        form: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        basic_auth: Optional[tuple[str, str]] = None,
    ) -> dict[str, Any]:
        hdrs = {"Accept": "application/json", "User-Agent": self.user_agent}
        if headers:
            hdrs.update(headers)
        data: Optional[bytes] = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif form is not None:
            data = urlencode(form).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        if basic_auth is not None:
            token = base64.b64encode(
                f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")
            ).decode("ascii")
            hdrs["Authorization"] = f"Basic {token}"

        request = Request(url, data=data, headers=hdrs, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise JsonHttpError(int(exc.code), body, url) from exc
        except URLError as exc:
            raise JsonHttpError(0, str(exc.reason), url) from exc

        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JsonHttpError(200, f"non-JSON response: {raw[:300]}", url) from exc
        if not isinstance(parsed, dict):
            return {"data": parsed}
        return parsed
