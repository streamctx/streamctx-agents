"""Reddit auto-tier adapter with karma gate and per-subreddit rate limit."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from agents.marketing_agent.adapters.base import (
    AutoAdapter,
    CredentialsError,
    PublishError,
    clip_text,
    facts_as_lines,
    strip_version_prefix,
)
from agents.marketing_agent.adapters.rate_limit import (
    RedditKarma,
    RedditRateLimiter,
    normalize_subreddit,
)
from agents.marketing_agent.http import JsonHttpClient, JsonHttpError
from agents.marketing_agent.models import PendingApprovalEntry, Story
from agents.marketing_agent.pending_approval import CONTENT_COMMENT, CONTENT_POST, PLATFORM_REDDIT
from agents.marketing_agent.settings import (
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
    REDDIT_PASSWORD,
    REDDIT_USER_AGENT,
    REDDIT_USERNAME,
)

REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_ME_URL = "https://oauth.reddit.com/api/v1/me"
REDDIT_SUBMIT_URL = "https://oauth.reddit.com/api/submit"
REDDIT_COMMENT_URL = "https://oauth.reddit.com/api/comment"
REDDIT_TITLE_MAX = 300
PROMO_RE = re.compile(
    r"\b(check out (our|my)|please upvote|follow us|we just launched)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RedditCredentials:
    client_id: str = ""
    client_secret: str = ""
    username: str = ""
    password: str = ""
    user_agent: str = REDDIT_USER_AGENT

    @classmethod
    def from_env(cls) -> RedditCredentials:
        return cls(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            username=REDDIT_USERNAME,
            password=REDDIT_PASSWORD,
            user_agent=REDDIT_USER_AGENT,
        )

    def assert_present(self) -> None:
        if not all([self.client_id, self.client_secret, self.username, self.password]):
            raise CredentialsError(
                "Reddit credentials missing. Set REDDIT_CLIENT_ID, "
                "REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD."
            )


class RedditAdapter(AutoAdapter):
    platform = PLATFORM_REDDIT

    def __init__(
        self,
        store,
        *,
        content_type: str = CONTENT_POST,
        http: Optional[JsonHttpClient] = None,
        credentials: Optional[RedditCredentials] = None,
        limiter: Optional[RedditRateLimiter] = None,
        access_token: Optional[str] = None,
    ) -> None:
        super().__init__(store, content_type=content_type)
        self.http = http or JsonHttpClient(
            user_agent=(credentials.user_agent if credentials else REDDIT_USER_AGENT)
        )
        self.credentials = credentials if credentials is not None else RedditCredentials.from_env()
        self.limiter = limiter or RedditRateLimiter(store.db_path)
        self._access_token = access_token

    def format(self, story: Story, target: Optional[str] = None) -> str:
        if self.content_type == CONTENT_COMMENT:
            return _format_reddit_comment(story)
        return _format_reddit_post(story)

    def submit(self, content: str, target: Optional[str] = None) -> str:
        if not target or not str(target).strip():
            raise ValueError("Reddit submit requires a target subreddit (or comment permalink)")
        return super().submit(content, target=target)

    def _deliver(self, entry: PendingApprovalEntry) -> None:
        subreddit, thing_id = parse_reddit_target(entry.target, entry.content_type)
        karma = self._fetch_karma()
        self.limiter.assert_can_post(subreddit, karma)
        if entry.content_type == CONTENT_COMMENT:
            self._post_comment(entry.content, thing_id)
        else:
            title, body = _split_title_body(entry.content)
            self._post_self(subreddit, title, body)
        self.limiter.record_post(subreddit)

    def _fetch_karma(self) -> RedditKarma:
        payload = self._oauth_get(REDDIT_ME_URL)
        try:
            return RedditKarma(
                link_karma=int(payload.get("link_karma") or 0),
                comment_karma=int(payload.get("comment_karma") or 0),
            )
        except (TypeError, ValueError) as exc:
            raise PublishError("Reddit /me did not return karma fields") from exc

    def _post_self(self, subreddit: str, title: str, body: str) -> None:
        payload = self._oauth_form(
            REDDIT_SUBMIT_URL,
            {
                "sr": subreddit,
                "kind": "self",
                "title": title,
                "text": body,
                "api_type": "json",
            },
        )
        _raise_if_reddit_errors(payload, action="submit")

    def _post_comment(self, text: str, thing_id: Optional[str]) -> None:
        if not thing_id:
            raise PublishError("Reddit comment publish requires a thing id (t3_… / t1_…)")
        payload = self._oauth_form(
            REDDIT_COMMENT_URL,
            {"thing_id": thing_id, "text": text, "api_type": "json"},
        )
        _raise_if_reddit_errors(payload, action="comment")

    def _oauth_get(self, url: str) -> dict:
        try:
            return self.http.get_json(url, headers=self._oauth_headers())
        except JsonHttpError as exc:
            raise PublishError(f"Reddit API GET failed: {exc}") from exc

    def _oauth_form(self, url: str, form: dict[str, str]) -> dict:
        try:
            return self.http.post_json(url, form=form, headers=self._oauth_headers())
        except JsonHttpError as exc:
            raise PublishError(f"Reddit API POST failed: {exc}") from exc

    def _oauth_headers(self) -> dict[str, str]:
        token = self._ensure_token()
        return {
            "Authorization": f"Bearer {token}",
            "User-Agent": self.credentials.user_agent,
        }

    def _ensure_token(self) -> str:
        if self._access_token:
            return self._access_token
        self.credentials.assert_present()
        try:
            payload = self.http.post_json(
                REDDIT_TOKEN_URL,
                form={
                    "grant_type": "password",
                    "username": self.credentials.username,
                    "password": self.credentials.password,
                },
                basic_auth=(self.credentials.client_id, self.credentials.client_secret),
                headers={"User-Agent": self.credentials.user_agent},
            )
        except JsonHttpError as exc:
            raise PublishError(f"Reddit token request failed: {exc}") from exc
        token = payload.get("access_token")
        if not token:
            raise CredentialsError(f"Reddit token response had no access_token: {payload!r}")
        self._access_token = str(token)
        return self._access_token


def parse_reddit_target(
    target: Optional[str], content_type: str
) -> tuple[str, Optional[str]]:
    if not target or not str(target).strip():
        raise PublishError("Reddit entry is missing target (subreddit or permalink)")
    raw = str(target).strip()

    thing = _extract_thing_id(raw)
    if content_type == CONTENT_COMMENT and not thing:
        raise PublishError(
            "Reddit comment target must include a thing id (t3_ / t1_) or a comments permalink."
        )

    if "reddit.com" in raw.lower() or raw.lower().startswith("http"):
        parsed = urlparse(raw)
        parts = [p for p in parsed.path.split("/") if p]
        # /r/<sub>/comments/<id>/...
        if len(parts) >= 2 and parts[0].lower() == "r":
            return normalize_subreddit(parts[1]), thing
        raise PublishError(f"Could not parse subreddit from Reddit URL {raw!r}")

    if raw.lower().startswith(("t3_", "t1_")):
        raise PublishError(
            "Bare thing ids need a subreddit; use a permalink like "
            "https://www.reddit.com/r/<sub>/comments/<id>/..."
        )
    return normalize_subreddit(raw), thing


def _extract_thing_id(raw: str) -> Optional[str]:
    match = re.search(r"\b(t[13]_[a-z0-9]+)\b", raw, re.IGNORECASE)
    if match:
        return match.group(1)
    parts = [p for p in urlparse(raw).path.split("/") if p]
    # /r/sub/comments/<id>/slug
    if "comments" in parts:
        idx = parts.index("comments")
        if idx + 1 < len(parts):
            return f"t3_{parts[idx + 1]}"
    return None


def _format_reddit_post(story: Story) -> str:
    title = clip_text(strip_version_prefix(story.headline), REDDIT_TITLE_MAX)
    bullets = "\n".join(facts_as_lines(story, limit=3, bullet="- "))
    proof = story.proof_point.strip()
    body = "\n\n".join(part for part in (bullets, proof) if part)
    text = f"{title}\n\n{body}".strip()
    if PROMO_RE.search(text):
        text = PROMO_RE.sub("", text)
        text = re.sub(r" {2,}", " ", text).strip()
    return text


def _format_reddit_comment(story: Story) -> str:
    lead = strip_version_prefix(story.headline)
    proof = story.proof_point.strip()
    parts = [lead]
    if proof and proof.lower() not in lead.lower():
        parts.append(proof)
    text = " ".join(parts)
    if PROMO_RE.search(text):
        text = PROMO_RE.sub("", text)
    return clip_text(text, 10000)


def _split_title_body(content: str) -> tuple[str, str]:
    lines = content.splitlines()
    title = next((line.strip() for line in lines if line.strip()), "Update")
    rest = "\n".join(lines[1:]).strip()
    return clip_text(title, REDDIT_TITLE_MAX), rest


def _raise_if_reddit_errors(payload: dict, *, action: str) -> None:
    errors = (payload.get("json") or {}).get("errors") if isinstance(payload, dict) else None
    if errors:
        raise PublishError(f"Reddit {action} errors: {errors!r}")
    # Some responses only set success=false
    if payload.get("success") is False:
        raise PublishError(f"Reddit {action} returned success=false: {payload!r}")
