"""Discord support-channel poll — read-only. Never sends a message.

Uses ``DISCORD_BOT_TOKEN`` / ``DISCORD_TOKEN`` and
``DISCORD_SUPPORT_CHANNEL_ID``. HTTP is GET-only.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from agents.competitor_agent.http import fetch_json
from agents.techsupport_agent.models import SOURCE_DISCORD, IncomingItem
from agents.techsupport_agent.settings import TechSupportConfig, discord_token

DISCORD_MESSAGES_URL = "https://discord.com/api/v10/channels/{channel_id}/messages"

FetchJsonFn = Callable[[str], Any]


def messages_url(channel_id: str, *, limit: int) -> str:
    return f"{DISCORD_MESSAGES_URL.format(channel_id=channel_id)}?limit={int(limit)}"


def discord_headers(user_agent: str, token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    if token:
        headers["Authorization"] = f"Bot {token}"
    return headers


def fingerprint(channel_id: str, message_id: str) -> str:
    return f"discord:{channel_id}:{message_id}"


def parse_messages(
    payload: Any,
    *,
    channel_id: str,
) -> list[IncomingItem]:
    if not isinstance(payload, list):
        return []
    items: list[IncomingItem] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        if int(row.get("type") or 0) != 0:
            continue
        author = row.get("author") if isinstance(row.get("author"), dict) else {}
        if author.get("bot"):
            continue
        message_id = str(row.get("id") or "").strip()
        content = str(row.get("content") or "").strip()
        if not message_id or not content:
            continue
        username = str(author.get("username") or "").strip()
        title = content.splitlines()[0][:120]
        items.append(
            IncomingItem(
                source=SOURCE_DISCORD,
                source_ref=message_id,
                source_url=(
                    f"https://discord.com/channels/@me/{channel_id}/{message_id}"
                ),
                title=title,
                body=content,
                author=username,
                source_fingerprint=fingerprint(channel_id, message_id),
            )
        )
    return items


def fetch_channel_messages(
    config: TechSupportConfig,
    *,
    fetch_fn: Optional[FetchJsonFn] = None,
    token: Optional[str] = None,
) -> list[IncomingItem]:
    """GET recent channel messages. Read-only — no create-message endpoint."""
    channel_id = (config.discord_channel_id or "").strip()
    if not channel_id:
        return []
    auth = token if token is not None else discord_token()
    if fetch_fn is None and not auth:
        return []
    url = messages_url(channel_id, limit=config.discord_limit)
    if fetch_fn is not None:
        payload = fetch_fn(url)
    else:
        payload = fetch_json(
            url,
            headers=discord_headers(config.user_agent, auth),
            max_retries=config.max_retries,
            backoff_base_seconds=config.backoff_base_seconds,
            max_backoff_seconds=config.max_backoff_seconds,
        )
    return parse_messages(payload, channel_id=channel_id)
