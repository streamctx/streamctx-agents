"""RSS/Atom feed parsing. No extra packages — stdlib ElementTree only."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

FEED_ENTRY_LIMIT = 50
SNAPSHOT_SUMMARY_MAX = 500
SIGNAL_SNIPPET_MAX = 200


@dataclass(frozen=True)
class FeedEntry:
    entry_id: str
    title: str
    link: str
    published: str
    summary: str


def parse_feed(xml_text: str) -> list[FeedEntry]:
    """Parse RSS 2.0 or Atom XML into canonical feed entries."""
    text = (xml_text or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid RSS/Atom feed: {exc}") from exc

    local = _local(root.tag)
    if local == "rss":
        channel = next(iter(_children(root, "channel")), root)
        items = _children(channel, "item")
        entries = [_rss_item(item) for item in items]
    elif local == "feed":
        entries = [_atom_entry(item) for item in _children(root, "entry")]
    else:
        raise ValueError(f"unsupported feed root {_local(root.tag)!r}")

    return [entry for entry in entries if entry.entry_id][:FEED_ENTRY_LIMIT]


def serialize_feed_entries(entries: list[FeedEntry]) -> str:
    rows = [
        {
            "id": entry.entry_id,
            "title": entry.title,
            "link": entry.link,
            "published": entry.published,
            "summary": entry.summary[:SNAPSHOT_SUMMARY_MAX],
        }
        for entry in entries
    ]
    rows.sort(key=lambda row: (row["published"], row["id"]))
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def parse_serialized_entries(raw: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def summarize_entry(entry: FeedEntry) -> str:
    """Short human-readable line for a ``new_post`` signal."""
    title = (entry.title or "").strip() or "New post"
    body = (entry.summary or "").strip()
    if body.lower().startswith(title.lower()):
        body = body[len(title) :].lstrip(" :-—–")
    snippet = WS_RE.sub(" ", body).strip()
    if len(snippet) > SIGNAL_SNIPPET_MAX:
        snippet = snippet[: SIGNAL_SNIPPET_MAX - 3].rstrip() + "..."
    if not snippet:
        return title
    return f"{title} — {snippet}"


def summarize_serialized(row: dict[str, str]) -> str:
    return summarize_entry(
        FeedEntry(
            entry_id=str(row.get("id") or ""),
            title=str(row.get("title") or ""),
            link=str(row.get("link") or ""),
            published=str(row.get("published") or ""),
            summary=str(row.get("summary") or ""),
        )
    )


def _rss_item(item: ET.Element) -> FeedEntry:
    title = _child_text(item, "title")
    link = _child_text(item, "link")
    guid = _child_text(item, "guid")
    published = _normalize_date(_child_text(item, "pubDate") or _child_text(item, "date"))
    summary = _plain_text(
        _child_text(item, "encoded")
        or _child_text(item, "description")
        or _child_text(item, "summary")
    )
    entry_id = guid or link or title
    return FeedEntry(
        entry_id=entry_id.strip(),
        title=title,
        link=link or guid,
        published=published,
        summary=summary,
    )


def _atom_entry(entry: ET.Element) -> FeedEntry:
    title = _child_text(entry, "title")
    link = _atom_link(entry)
    entry_id = _child_text(entry, "id") or link or title
    published = _normalize_date(
        _child_text(entry, "published") or _child_text(entry, "updated")
    )
    summary = _plain_text(
        _child_text(entry, "summary") or _child_text(entry, "content")
    )
    return FeedEntry(
        entry_id=entry_id.strip(),
        title=title,
        link=link,
        published=published,
        summary=summary,
    )


def _atom_link(entry: ET.Element) -> str:
    links = _children(entry, "link")
    for link in links:
        rel = (link.attrib.get("rel") or "alternate").strip()
        href = (link.attrib.get("href") or "").strip()
        if href and rel in {"alternate", ""}:
            return href
    if links:
        href = (links[0].attrib.get("href") or "").strip()
        if href:
            return href
    return _child_text(entry, "link")


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(node) if _local(child.tag) == name]


def _child_text(node: ET.Element, name: str) -> str:
    for child in _children(node, name):
        text = "".join(child.itertext()).strip()
        if text:
            return text
    return ""


def _local(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _plain_text(value: str) -> str:
    return WS_RE.sub(" ", TAG_RE.sub(" ", value or "")).strip()


def _normalize_date(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return text
