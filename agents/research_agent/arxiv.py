"""arXiv Atom polling for cs.AI / cs.MA papers matching research keywords.

Uses the public export API (no key). arXiv asks for ≥3 seconds between requests;
the poller sleeps ``config.poll_delay_seconds`` between category queries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from agents.research_agent.http import SleepFn, fetch_text
from agents.research_agent.models import SOURCE_TYPE_ARXIV, SourceItem
from agents.research_agent.settings import ResearchConfig

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_ACCEPT = "application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8"
WS_RE = re.compile(r"\s+")
VERSION_RE = re.compile(r"v\d+$")

FetchTextFn = Callable[[str], str]


@dataclass(frozen=True)
class ArxivPaper:
    arxiv_id: str
    title: str
    summary: str
    published: str
    html_url: str
    categories: tuple[str, ...]


def arxiv_search_url(
    category: str,
    keywords: tuple[str, ...],
    *,
    max_results: int,
) -> str:
    keyword_clause = " OR ".join(_arxiv_keyword_term(keyword) for keyword in keywords)
    query = f"cat:{category}"
    if keyword_clause:
        query = f"{query} AND ({keyword_clause})"
    params = urlencode(
        {
            "search_query": query,
            "start": "0",
            "max_results": str(max_results),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
    )
    return f"{ARXIV_API_URL}?{params}"


def parse_arxiv_atom(xml_text: str) -> list[ArxivPaper]:
    text = (xml_text or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid arXiv Atom feed: {exc}") from exc

    papers: list[ArxivPaper] = []
    for entry in _children(root, "entry"):
        paper = _parse_entry(entry)
        if paper is not None:
            papers.append(paper)
    return papers


def matches_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    haystack = (text or "").lower()
    return any(keyword.lower() in haystack for keyword in keywords if keyword.strip())


def fetch_arxiv_papers(
    config: ResearchConfig,
    *,
    fetch_fn: Optional[FetchTextFn] = None,
    sleep_fn: Optional[SleepFn] = None,
) -> list[ArxivPaper]:
    """Fetch recent papers for each configured category, de-duped by abs URL."""
    sleeper = sleep_fn or (lambda _seconds: None)
    seen: set[str] = set()
    papers: list[ArxivPaper] = []
    for index, category in enumerate(config.arxiv_categories):
        if index and config.poll_delay_seconds > 0:
            sleeper(config.poll_delay_seconds)
        url = arxiv_search_url(
            category,
            config.arxiv_keywords,
            max_results=config.arxiv_max_results,
        )
        xml = _fetch_feed(url, config, fetch_fn)
        for paper in parse_arxiv_atom(xml):
            if paper.html_url in seen:
                continue
            blob = f"{paper.title}\n{paper.summary}"
            if config.arxiv_keywords and not matches_keywords(blob, config.arxiv_keywords):
                continue
            seen.add(paper.html_url)
            papers.append(paper)
    return papers


def papers_to_source_items(
    papers: list[ArxivPaper],
    *,
    excerpt_max_chars: int,
) -> list[SourceItem]:
    items: list[SourceItem] = []
    for paper in papers:
        items.append(
            SourceItem(
                source_url=paper.html_url,
                source_type=SOURCE_TYPE_ARXIV,
                title=paper.title,
                content_excerpt=_clip(paper.summary, excerpt_max_chars),
            )
        )
    return items


def canonicalize_arxiv_url(url: str) -> str:
    text = (url or "").strip().replace("http://", "https://")
    text = text.replace("export.arxiv.org", "arxiv.org")
    abs_prefix = "https://arxiv.org/abs/"
    if abs_prefix in text:
        path = text.split("/abs/", 1)[1]
        path = VERSION_RE.sub("", path)
        return abs_prefix + path
    return text


def _arxiv_keyword_term(keyword: str) -> str:
    term = keyword.strip()
    if not term:
        return ""
    if " " in term:
        return f'all:"{term}"'
    return f"all:{term}"


def _fetch_feed(
    url: str,
    config: ResearchConfig,
    fetch_fn: Optional[FetchTextFn],
) -> str:
    if fetch_fn is not None:
        return fetch_fn(url)
    return fetch_text(
        url,
        headers={"User-Agent": config.user_agent, "Accept": ARXIV_ACCEPT},
        max_retries=config.max_retries,
        backoff_base_seconds=config.backoff_base_seconds,
        max_backoff_seconds=config.max_backoff_seconds,
    )


def _parse_entry(entry: ET.Element) -> Optional[ArxivPaper]:
    title = _plain(_child_text(entry, "title"))
    if not title:
        return None
    raw_id = _child_text(entry, "id")
    html_url = canonicalize_arxiv_url(_atom_html_link(entry) or raw_id)
    if not html_url:
        return None
    summary = _plain(_child_text(entry, "summary"))
    published = _normalize_date(
        _child_text(entry, "published") or _child_text(entry, "updated")
    )
    categories = tuple(
        (child.attrib.get("term") or "").strip()
        for child in _children(entry, "category")
        if (child.attrib.get("term") or "").strip()
    )
    arxiv_id = html_url.rsplit("/", 1)[-1]
    return ArxivPaper(
        arxiv_id=arxiv_id,
        title=title,
        summary=summary,
        published=published,
        html_url=html_url,
        categories=categories,
    )


def _atom_html_link(entry: ET.Element) -> str:
    for link in _children(entry, "link"):
        href = (link.attrib.get("href") or "").strip()
        rel = (link.attrib.get("rel") or "alternate").strip()
        typ = (link.attrib.get("type") or "").strip()
        if href and rel in {"alternate", ""} and ("html" in typ or not typ):
            return href
    for link in _children(entry, "link"):
        href = (link.attrib.get("href") or "").strip()
        if href:
            return href
    return ""


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


def _plain(value: str) -> str:
    return WS_RE.sub(" ", value or "").strip()


def _clip(value: str, max_chars: int) -> str:
    text = (value or "").strip()
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


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
