"""Per-subreddit interval limiter + karma gate for Reddit auto-posts."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from agents.marketing_agent.adapters.base import PublishError
from agents.marketing_agent.settings import (
    REDDIT_MIN_COMMENT_KARMA,
    REDDIT_MIN_LINK_KARMA,
    REDDIT_SUBREDDIT_INTERVAL_SECONDS,
)
from shared.db import connect

NowFn = Callable[[], datetime]


class RedditRateLimitError(PublishError):
    """Too soon to post again in this subreddit."""


class RedditKarmaGateError(PublishError):
    """Account karma is below the configured auto-post threshold."""


@dataclass(frozen=True)
class RedditKarma:
    link_karma: int
    comment_karma: int


class RedditRateLimiter:
    """
    Persist last-post time per subreddit in the marketing SQLite file.

    Defaults are conservative (48h interval, 50/50 karma) because this
    account has spam-filter history. Override via constructor or env.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        interval_seconds: int = REDDIT_SUBREDDIT_INTERVAL_SECONDS,
        min_link_karma: int = REDDIT_MIN_LINK_KARMA,
        min_comment_karma: int = REDDIT_MIN_COMMENT_KARMA,
        now_fn: Optional[NowFn] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.interval = timedelta(seconds=max(1, int(interval_seconds)))
        self.min_link_karma = int(min_link_karma)
        self.min_comment_karma = int(min_comment_karma)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._conn = connect(
            schema="marketing",
            db_path=self.db_path,
            check_same_thread=False,
        )
        self._init_db()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reddit_subreddit_posts (
                    subreddit TEXT PRIMARY KEY,
                    last_posted_at TIMESTAMP NOT NULL
                )
                """
            )
            self._conn.commit()

    def assert_can_post(self, subreddit: str, karma: RedditKarma) -> None:
        self.assert_karma(karma)
        self.assert_interval(subreddit)

    def assert_karma(self, karma: RedditKarma) -> None:
        if karma.link_karma < self.min_link_karma:
            raise RedditKarmaGateError(
                f"link_karma {karma.link_karma} < minimum {self.min_link_karma}; "
                "refusing Reddit auto-post."
            )
        if karma.comment_karma < self.min_comment_karma:
            raise RedditKarmaGateError(
                f"comment_karma {karma.comment_karma} < minimum {self.min_comment_karma}; "
                "refusing Reddit auto-post."
            )

    def assert_interval(self, subreddit: str) -> None:
        name = normalize_subreddit(subreddit)
        last = self.last_posted_at(name)
        if last is None:
            return
        earliest = last + self.interval
        now = self.now_fn()
        if now < earliest:
            remaining = int((earliest - now).total_seconds())
            raise RedditRateLimitError(
                f"Already posted to r/{name} at {last.isoformat()}; "
                f"wait {remaining}s (interval {int(self.interval.total_seconds())}s)."
            )

    def last_posted_at(self, subreddit: str) -> Optional[datetime]:
        name = normalize_subreddit(subreddit)
        with self._lock:
            row = self._conn.execute(
                "SELECT last_posted_at FROM reddit_subreddit_posts WHERE subreddit = ?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return _parse_ts(row[0])

    def record_post(self, subreddit: str, *, posted_at: Optional[datetime] = None) -> None:
        name = normalize_subreddit(subreddit)
        stamp = (posted_at or self.now_fn()).astimezone(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO reddit_subreddit_posts (subreddit, last_posted_at)
                VALUES (?, ?)
                ON CONFLICT(subreddit) DO UPDATE SET last_posted_at = excluded.last_posted_at
                """,
                (name, stamp),
            )
            self._conn.commit()


def normalize_subreddit(value: str) -> str:
    name = (value or "").strip()
    if name.lower().startswith("r/"):
        name = name[2:]
    name = name.strip("/").split("/")[0]
    if not name:
        raise PublishError("Reddit target is missing a subreddit name")
    return name.lower()


def _parse_ts(raw: str) -> datetime:
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
