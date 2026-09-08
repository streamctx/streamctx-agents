"""Search README / CHANGELOG / docs for a ticket match. No network."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from agents.techsupport_agent.models import KbMatch, Ticket
from agents.techsupport_agent.settings import TechSupportConfig, default_config, repo_root

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-./]{1,}", re.I)
STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "for",
        "on",
        "is",
        "it",
        "this",
        "that",
        "with",
        "from",
        "be",
        "as",
        "at",
        "by",
        "we",
        "you",
        "our",
        "are",
        "was",
        "were",
        "not",
        "but",
        "if",
        "then",
        "so",
        "do",
        "does",
        "can",
        "i",
        "my",
        "me",
        "please",
        "thanks",
        "hi",
        "hello",
        "hey",
    }
)
MD_SUFFIXES = {".md", ".rst", ".txt"}


@dataclass(frozen=True)
class KbChunk:
    path: str
    heading: str
    text: str
    tokens: frozenset[str]


class KnowledgeBase:
    """In-memory index over local docs. Citation is ``path § heading``."""

    def __init__(
        self,
        *,
        root: Optional[Path | str] = None,
        config: Optional[TechSupportConfig] = None,
        chunks: Optional[Sequence[KbChunk]] = None,
    ) -> None:
        self.config = config or default_config()
        self.root = Path(root) if root is not None else Path(repo_root())
        self.chunks: tuple[KbChunk, ...] = (
            tuple(chunks) if chunks is not None else tuple(index_docs(self.root, self.config))
        )

    def search(self, ticket: Ticket) -> Optional[KbMatch]:
        query = tokenize(f"{ticket.title}\n{ticket.body}")
        if not query or not self.chunks:
            return None
        best: Optional[tuple[float, KbChunk]] = None
        for chunk in self.chunks:
            score = _score(query, chunk)
            if best is None or score > best[0]:
                best = (score, chunk)
        if best is None or best[0] <= 0:
            return None
        score, chunk = best
        excerpt = _excerpt(chunk.text, self.config.excerpt_max_chars)
        citation = f"{chunk.path} § {chunk.heading}" if chunk.heading else chunk.path
        return KbMatch(
            path=chunk.path,
            heading=chunk.heading,
            excerpt=excerpt,
            confidence=round(min(0.99, score), 3),
            citation=citation,
        )


def index_docs(root: Path, config: TechSupportConfig) -> list[KbChunk]:
    chunks: list[KbChunk] = []
    for rel in config.kb_roots:
        path = root / rel
        if path.is_file():
            chunks.extend(_chunks_for_file(root, path))
            continue
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix.lower() in MD_SUFFIXES:
                    chunks.extend(_chunks_for_file(root, child))
    return chunks


def tokenize(text: str) -> frozenset[str]:
    tokens = {match.group(0).lower() for match in TOKEN_RE.finditer(text or "")}
    return frozenset(token for token in tokens if token not in STOPWORDS and len(token) > 1)


def _chunks_for_file(root: Path, path: Path) -> list[KbChunk]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rel = _relpath(root, path)
    sections = _split_markdown(text, default_heading=path.stem)
    chunks: list[KbChunk] = []
    for heading, body in sections:
        body = body.strip()
        if not body:
            continue
        tokens = tokenize(f"{heading}\n{body}")
        if not tokens:
            continue
        chunks.append(KbChunk(path=rel, heading=heading, text=body, tokens=tokens))
    return chunks


def _split_markdown(text: str, *, default_heading: str) -> list[tuple[str, str]]:
    heading = default_heading
    lines: list[str] = []
    sections: list[tuple[str, str]] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if lines:
                sections.append((heading, "\n".join(lines)))
            heading = line.lstrip("#").strip() or default_heading
            lines = []
        else:
            lines.append(line)
    if lines:
        sections.append((heading, "\n".join(lines)))
    if not sections and text.strip():
        sections.append((default_heading, text))
    return sections


def _score(query: frozenset[str], chunk: KbChunk) -> float:
    if not query or not chunk.tokens:
        return 0.0
    overlap = query & chunk.tokens
    if not overlap:
        return 0.0
    recall = len(overlap) / len(query)
    # Four shared tokens is a strong match even when the ticket is long.
    saturating = min(1.0, len(overlap) / 4.0)
    heading_tokens = tokenize(chunk.heading)
    heading_bonus = 0.2 if heading_tokens and (query & heading_tokens) else 0.0
    return min(0.99, 0.5 * recall + 0.4 * saturating + heading_bonus)


def _excerpt(text: str, max_chars: int) -> str:
    blob = " ".join((text or "").split())
    if max_chars > 0 and len(blob) > max_chars:
        return blob[: max_chars - 3].rstrip() + "..."
    return blob


def _relpath(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def search_ticket(
    ticket: Ticket,
    *,
    root: Optional[Path | str] = None,
    config: Optional[TechSupportConfig] = None,
    kb: Optional[KnowledgeBase] = None,
) -> Optional[KbMatch]:
    index = kb or KnowledgeBase(root=root, config=config)
    return index.search(ticket)
