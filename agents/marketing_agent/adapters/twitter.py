"""Twitter/X auto-tier adapter. Posts only via publish() after approval."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import quote

from agents.marketing_agent.adapters.base import (
    AutoAdapter,
    CredentialsError,
    PublishError,
    clip_text,
    strip_version_prefix,
)
from agents.marketing_agent.http import JsonHttpClient, JsonHttpError
from agents.marketing_agent.models import PendingApprovalEntry, Story
from agents.marketing_agent.pending_approval import PLATFORM_TWITTER
from agents.marketing_agent.settings import (
    TWITTER_ACCESS_TOKEN,
    TWITTER_ACCESS_TOKEN_SECRET,
    TWITTER_API_KEY,
    TWITTER_API_SECRET,
    TWITTER_BEARER_TOKEN,
)

TWITTER_MAX_CHARS = 280
TWITTER_TWEETS_URL = "https://api.twitter.com/2/tweets"


@dataclass(frozen=True)
class TwitterCredentials:
    bearer_token: str = ""
    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""
    access_token_secret: str = ""

    @classmethod
    def from_env(cls) -> TwitterCredentials:
        return cls(
            bearer_token=TWITTER_BEARER_TOKEN,
            api_key=TWITTER_API_KEY,
            api_secret=TWITTER_API_SECRET,
            access_token=TWITTER_ACCESS_TOKEN,
            access_token_secret=TWITTER_ACCESS_TOKEN_SECRET,
        )

    def uses_bearer(self) -> bool:
        return bool(self.bearer_token)

    def uses_oauth1(self) -> bool:
        return all(
            [self.api_key, self.api_secret, self.access_token, self.access_token_secret]
        )

    def assert_present(self) -> None:
        if not self.uses_bearer() and not self.uses_oauth1():
            raise CredentialsError(
                "Twitter credentials missing. Set TWITTER_BEARER_TOKEN "
                "(user token with tweet.write) or the OAuth 1.0a key set "
                "TWITTER_API_KEY / TWITTER_API_SECRET / TWITTER_ACCESS_TOKEN / "
                "TWITTER_ACCESS_TOKEN_SECRET."
            )


class TwitterAdapter(AutoAdapter):
    platform = PLATFORM_TWITTER

    def __init__(
        self,
        store,
        *,
        content_type: str = "post",
        http: Optional[JsonHttpClient] = None,
        credentials: Optional[TwitterCredentials] = None,
        safety=None,
    ) -> None:
        super().__init__(store, content_type=content_type, safety=safety)
        self.http = http or JsonHttpClient()
        self.credentials = credentials if credentials is not None else TwitterCredentials.from_env()

    def format(self, story: Story, target: Optional[str] = None) -> str:
        headline = strip_version_prefix(story.headline)
        proof = (story.proof_point or "").strip()
        if proof and proof.lower() not in headline.lower():
            candidate = f"{headline} {proof}"
            if len(candidate) <= TWITTER_MAX_CHARS:
                return candidate
        return clip_text(headline, TWITTER_MAX_CHARS)

    def _deliver(self, entry: PendingApprovalEntry) -> None:
        self.credentials.assert_present()
        headers = self._auth_headers()
        try:
            response = self.http.post_json(
                TWITTER_TWEETS_URL,
                json_body={"text": entry.content},
                headers=headers,
            )
        except JsonHttpError as exc:
            raise PublishError(f"Twitter API failed: {exc}") from exc
        tweet_id = (response.get("data") or {}).get("id")
        if not tweet_id:
            raise PublishError(f"Twitter API returned no tweet id: {response!r}")

    def _auth_headers(self) -> dict[str, str]:
        if self.credentials.uses_bearer():
            return {"Authorization": f"Bearer {self.credentials.bearer_token}"}
        return {
            "Authorization": oauth1_authorization_header(
                "POST",
                TWITTER_TWEETS_URL,
                api_key=self.credentials.api_key,
                api_secret=self.credentials.api_secret,
                access_token=self.credentials.access_token,
                access_token_secret=self.credentials.access_token_secret,
            )
        }


def oauth1_authorization_header(
    method: str,
    url: str,
    *,
    api_key: str,
    api_secret: str,
    access_token: str,
    access_token_secret: str,
    extra_params: Optional[Mapping[str, str]] = None,
) -> str:
    """Sign a Twitter OAuth 1.0a User Context request (JSON body is not signed)."""
    oauth_params = {
        "oauth_consumer_key": api_key,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }
    sign_params = dict(oauth_params)
    if extra_params:
        sign_params.update(extra_params)
    base = _signature_base(method, url, sign_params)
    key = f"{_pct(api_secret)}&{_pct(access_token_secret)}"
    digest = hmac.new(key.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    oauth_params["oauth_signature"] = base64.b64encode(digest).decode("ascii")
    parts = ", ".join(
        f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth_params.items())
    )
    return f"OAuth {parts}"


def _signature_base(method: str, url: str, params: Mapping[str, Any]) -> str:
    encoded = "&".join(
        f"{_pct(k)}={_pct(v)}" for k, v in sorted((str(k), str(v)) for k, v in params.items())
    )
    return f"{method.upper()}&{_pct(url)}&{_pct(encoded)}"


def _pct(value: str) -> str:
    return quote(str(value), safe="~")
