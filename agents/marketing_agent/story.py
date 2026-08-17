"""Platform-agnostic story generator for marketing_agent."""

from __future__ import annotations

import re
from typing import Any, Mapping, Union

from agents.marketing_agent.models import CHANGELOG_KIND, COMMIT_KIND, SourceData, Story

SourceInput = Union[SourceData, Mapping[str, Any]]

HEADLINE_MAX_CHARS = 90
PROOF_MAX_CHARS = 220

PROOF_PATTERNS = (
    re.compile(r"closes?\s+#\d+", re.IGNORECASE),
    re.compile(r"#\d+"),
    re.compile(r"\d+\s*/\s*\d+\s+passing", re.IGNORECASE),
    re.compile(r"\d+\s+passed", re.IGNORECASE),
    re.compile(r"under\s+\d+\s+seconds?", re.IGNORECASE),
    re.compile(r"0\.\d+\s+confidence", re.IGNORECASE),
    re.compile(r"\d+\s+concurrent\s+workers?", re.IGNORECASE),
)

KEYWORD_TONES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(ci|pytest|coverage|test suite)\b", re.I), "quality"),
    (re.compile(r"\b(sqlite|concurren|wal|race|leak)\b", re.I), "reliability"),
    (re.compile(r"\b(attribution|replay|checkpoint|compress|poison)\b", re.I), "observability"),
    (re.compile(r"\b(wrap|tracker|resume|get_stats)\b", re.I), "sdk"),
    (re.compile(r"\b(pypi|release|launch|public)\b", re.I), "announcement"),
    (re.compile(r"\b(security|audit|secret|token)\b", re.I), "security"),
    (re.compile(r"\b(docs|readme|mkdocs)\b", re.I), "docs"),
)

CATEGORY_TONES = {
    "fixed": ["technical", "reliability", "bugfix"],
    "added": ["technical", "feature", "shipping"],
    "changed": ["technical", "improvement"],
    "removed": ["technical", "breaking"],
    "deprecated": ["technical", "breaking"],
    "security": ["trust", "security"],
    "known issues": ["technical", "caveat"],
}

CONVENTIONAL_RE = re.compile(
    r"^[a-z]+(?:\([^)]+\))?!?:\s*(.+)$",
    re.IGNORECASE,
)
MARKDOWN_RE = re.compile(r"[`*_]+")


def generate_story(source_data: SourceInput) -> Story:
    """
    Turn one changelog entry or significant commit into a platform-agnostic Story.

    Does not call an LLM and does not post anywhere — later stages adapt
    this Story per platform and enqueue it for approval.
    """
    source = (
        source_data
        if isinstance(source_data, SourceData)
        else SourceData.from_mapping(source_data)
    )
    facts = _key_facts(source)
    if not facts and not source.title.strip():
        raise ValueError("source_data has no title, body, or items to turn into a story")

    return Story(
        headline=_headline(source, facts),
        key_facts=facts,
        proof_point=_proof_point(source, facts),
        tone_tags=_tone_tags(source, facts),
    )


def generate_stories(source_data: list[SourceInput]) -> list[Story]:
    """Map ``generate_story`` across a list of sources."""
    return [generate_story(item) for item in source_data]


def _headline(source: SourceData, facts: list[str]) -> str:
    if source.kind == COMMIT_KIND:
        subject = _strip_conventional_prefix(source.title)
        return _truncate(_strip_md(subject), HEADLINE_MAX_CHARS)

    first = facts[0] if facts else _strip_conventional_prefix(source.title)
    first = _first_sentence(_strip_md(first))
    if source.version:
        return _truncate(f"{source.version}: {first}", HEADLINE_MAX_CHARS)
    return _truncate(first, HEADLINE_MAX_CHARS)


def _key_facts(source: SourceData) -> list[str]:
    facts: list[str] = []
    for item in source.items:
        cleaned = _strip_md(_collapse_ws(item))
        if cleaned:
            facts.append(_truncate(cleaned, 280))
    if facts:
        return facts

    for line in source.body.splitlines():
        cleaned = _strip_md(_collapse_ws(line.lstrip("-*• ")))
        if cleaned:
            facts.append(_first_sentence(cleaned, max_chars=180))
    if facts:
        return facts

    title = _strip_md(_strip_conventional_prefix(source.title))
    return [title] if title else []


def _proof_point(source: SourceData, facts: list[str]) -> str:
    haystacks = [*facts, source.body, source.title]
    for text in haystacks:
        for pattern in PROOF_PATTERNS:
            match = pattern.search(text)
            if match:
                sentence = _sentence_containing(text, match.start()) or text
                return _truncate(_strip_md(_collapse_ws(sentence)), PROOF_MAX_CHARS)

    if source.kind == CHANGELOG_KIND and source.version:
        n = len(facts)
        category = (source.category or "updates").lower()
        return _truncate(
            f"Shipped in {source.version} ({n} {category} item{'s' if n != 1 else ''}"
            + (f", {source.date}" if source.date else "")
            + ").",
            PROOF_MAX_CHARS,
        )

    if facts:
        return _truncate(facts[0], PROOF_MAX_CHARS)
    return _truncate(_strip_md(source.title), PROOF_MAX_CHARS)


def _tone_tags(source: SourceData, facts: list[str]) -> list[str]:
    tags: list[str] = []
    categories = list(source.categories)
    if source.category and source.category not in categories:
        categories.insert(0, source.category)

    for category in categories:
        tags.extend(CATEGORY_TONES.get(category.lower(), ["technical"]))

    blob = " ".join([source.title, source.body, *facts])
    for pattern, tag in KEYWORD_TONES:
        if pattern.search(blob):
            tags.append(tag)

    if source.kind == COMMIT_KIND:
        tags.append("from_commit")
    else:
        tags.append("from_changelog")

    # Prefer technical contribution over launch-speak when both appear.
    if "announcement" in tags and ("bugfix" in tags or "observability" in tags):
        tags.append("community_safe")
    elif "bugfix" in tags or "observability" in tags or "sdk" in tags:
        tags.append("community_safe")
    elif "announcement" in tags:
        tags.append("promo_risk")

    return _unique(tags)


def _strip_conventional_prefix(subject: str) -> str:
    match = CONVENTIONAL_RE.match(subject.strip())
    if match:
        return match.group(1).strip()
    return subject.strip()


def _strip_md(value: str) -> str:
    return MARKDOWN_RE.sub("", value).strip()


def _first_sentence(value: str, max_chars: int = HEADLINE_MAX_CHARS) -> str:
    stripped = value.strip()
    match = re.search(r"(?<=[a-z0-9)])\.\s+[A-Z]", stripped)
    if match:
        stripped = stripped[: match.start() + 1]
    return _truncate(stripped.rstrip("."), max_chars)


def _sentence_containing(text: str, index: int) -> str:
    start = text.rfind(".", 0, index)
    start = 0 if start == -1 else start + 1
    end = text.find(".", index)
    chunk = text[start:] if end == -1 else text[start : end + 1]
    return _collapse_ws(chunk)


def _truncate(value: str, max_chars: int) -> str:
    value = _collapse_ws(value)
    if len(value) <= max_chars:
        return value
    clipped = value[: max_chars - 1].rsplit(" ", 1)[0]
    return (clipped or value[: max_chars - 1]).rstrip(",;:") + "…"


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen
