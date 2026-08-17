"""Load changelog entries and significant git commits as SourceData."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from shared.config import BASE_DIR

from agents.marketing_agent.models import CHANGELOG_KIND, COMMIT_KIND, SourceData

VERSION_RE = re.compile(
    r"^##\s*\[?v?(\d+\.\d+(?:\.\d+)?)\]?\s*(?:[-–—]\s*(.+))?$",
    re.IGNORECASE,
)
CATEGORY_RE = re.compile(
    r"^(?:###\s*)?(Added|Fixed|Changed|Removed|Deprecated|Security|Known Issues)\s*:?\s*$",
    re.IGNORECASE,
)
BULLET_RE = re.compile(r"^[-*•]\s+(.*)$")

# Conventional-commit types that are usually worth a story on their own.
SIGNIFICANT_COMMIT_TYPES = frozenset(
    {"feat", "fix", "perf", "security", "revert"}
)
# Types skipped unless the body is substantial.
TRIVIAL_COMMIT_TYPES = frozenset(
    {"chore", "docs", "style", "ci", "test", "build", "refactor"}
)
TRIVIAL_SUBJECT_RE = re.compile(
    r"^(wip|tmp|typo|oops|misc|nit)\b",
    re.IGNORECASE,
)
CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\([^)]+\))?(?P<breaking>!)?:\s*(?P<subject>.+)$",
    re.IGNORECASE,
)

MIN_SIGNIFICANT_BODY_CHARS = 40
DEFAULT_COMMIT_LIMIT = 20


def default_product_root() -> Path:
    """StreamCtx SDK checkout: STREAMCTX_PRODUCT_ROOT, else a sibling directory."""
    env = os.environ.get("STREAMCTX_PRODUCT_ROOT")
    if env:
        return Path(env)
    return Path(BASE_DIR).parent / "streamctx"


def default_changelog_path() -> Path:
    return default_product_root() / "CHANGELOG.md"


def parse_changelog(
    path: Optional[Path | str] = None,
    *,
    text: Optional[str] = None,
) -> list[SourceData]:
    """
    Parse Keep-a-Changelog text into one SourceData per version section.

    ``text`` wins when both are given. ``path`` defaults to the product CHANGELOG.
    """
    if text is None:
        changelog_path = Path(path) if path is not None else default_changelog_path()
        text = changelog_path.read_text(encoding="utf-8")
    return _parse_changelog_text(text)


def load_significant_commits(
    repo_path: Optional[Path | str] = None,
    *,
    limit: int = DEFAULT_COMMIT_LIMIT,
    raw_log: Optional[str] = None,
) -> list[SourceData]:
    """Return significant commits as SourceData, newest first."""
    if raw_log is None:
        repo = Path(repo_path) if repo_path is not None else default_product_root()
        raw_log = _run_git_log(repo, limit=limit)
    commits = [_commit_to_source(record) for record in _parse_git_log(raw_log)]
    return [commit for commit in commits if is_significant_commit(commit)]


def collect_sources(
    *,
    changelog_path: Optional[Path | str] = None,
    repo_path: Optional[Path | str] = None,
    commit_limit: int = DEFAULT_COMMIT_LIMIT,
    changelog_text: Optional[str] = None,
    raw_log: Optional[str] = None,
) -> list[SourceData]:
    """Changelog entries first, then significant commits."""
    sources: list[SourceData] = []
    sources.extend(parse_changelog(changelog_path, text=changelog_text))
    sources.extend(
        load_significant_commits(
            repo_path,
            limit=commit_limit,
            raw_log=raw_log,
        )
    )
    return sources


def is_significant_commit(source: SourceData) -> bool:
    """Filter merge/trivial noise; keep feat/fix and commits with a real body."""
    subject = source.title.strip()
    if not subject:
        return False
    if subject.lower().startswith("merge "):
        return False
    if TRIVIAL_SUBJECT_RE.match(subject):
        return False

    conventional = CONVENTIONAL_RE.match(subject)
    commit_type = conventional.group("type").lower() if conventional else None
    breaking = bool(conventional and conventional.group("breaking"))
    body = (source.body or "").strip()

    if breaking or (commit_type in SIGNIFICANT_COMMIT_TYPES):
        return True
    if commit_type in TRIVIAL_COMMIT_TYPES and len(body) < MIN_SIGNIFICANT_BODY_CHARS:
        return False
    if len(body) >= MIN_SIGNIFICANT_BODY_CHARS:
        return True
    # Unprefixed but descriptive subject (not a one-word stub).
    return len(subject) >= 24 and " " in subject


def _parse_changelog_text(text: str) -> list[SourceData]:
    releases: list[_ReleaseBuilder] = []
    current: Optional[_ReleaseBuilder] = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        version_match = VERSION_RE.match(stripped)
        if version_match:
            if current is not None:
                releases.append(current)
            current = _ReleaseBuilder(
                version=version_match.group(1),
                date=_clean_date(version_match.group(2)),
            )
            continue

        if current is None:
            continue

        category_match = CATEGORY_RE.match(stripped)
        if category_match:
            current.category = _normalize_category(category_match.group(1))
            continue

        bullet_match = BULLET_RE.match(stripped)
        if bullet_match:
            current.add_item(_collapse_ws(bullet_match.group(1)))
            continue

        if current.items:
            current.items[-1] = _collapse_ws(current.items[-1] + " " + stripped)
            continue

        current.preamble.append(_collapse_ws(stripped))

    if current is not None:
        releases.append(current)

    return [builder.to_source() for builder in releases if builder.has_content()]


class _ReleaseBuilder:
    def __init__(self, version: str, date: Optional[str]) -> None:
        self.version = version
        self.date = date
        self.category: Optional[str] = None
        self.items: list[str] = []
        self.item_categories: list[str] = []
        self.preamble: list[str] = []

    def add_item(self, text: str) -> None:
        self.items.append(text)
        self.item_categories.append(self.category or "Changed")

    def has_content(self) -> bool:
        return bool(self.items or self.preamble)

    def to_source(self) -> SourceData:
        categories = _unique(self.item_categories)
        items = list(self.items) or list(self.preamble)
        body_lines = []
        last_category = None
        for category, item in zip(self.item_categories, self.items):
            if category != last_category:
                body_lines.append(f"{category}:")
                last_category = category
            body_lines.append(f"- {item}")
        if not body_lines:
            body_lines = [f"- {line}" for line in self.preamble]
        sole_category = categories[0] if len(categories) == 1 else None
        return SourceData(
            kind=CHANGELOG_KIND,
            title=f"StreamCtx {self.version}",
            body="\n".join(body_lines),
            items=items,
            categories=categories,
            category=sole_category,
            version=self.version,
            date=self.date,
            identifier=self.version,
        )


def _parse_git_log(raw_log: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for chunk in raw_log.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split("\x1f", 3)
        if len(parts) < 2:
            continue
        sha = parts[0].strip()
        subject = parts[1].strip() if len(parts) > 1 else ""
        date = parts[2].strip() if len(parts) > 2 else ""
        body = parts[3].strip() if len(parts) > 3 else ""
        if not sha:
            continue
        records.append(
            {"sha": sha, "subject": subject, "date": date, "body": body}
        )
    return records


def _commit_to_source(record: dict[str, str]) -> SourceData:
    body = record["body"].strip()
    items = [_collapse_ws(line) for line in body.splitlines() if line.strip()]
    categories = _categories_from_commit(record["subject"], body)
    return SourceData(
        kind=COMMIT_KIND,
        title=record["subject"],
        body=body,
        items=items,
        categories=categories,
        category=categories[0] if categories else None,
        date=record["date"] or None,
        identifier=record["sha"],
    )


def _categories_from_commit(subject: str, body: str) -> list[str]:
    match = CONVENTIONAL_RE.match(subject.strip())
    if not match:
        text = f"{subject}\n{body}".lower()
        if "fix" in text or "bug" in text:
            return ["Fixed"]
        if "security" in text:
            return ["Security"]
        return ["Changed"]
    commit_type = match.group("type").lower()
    mapping = {
        "feat": "Added",
        "fix": "Fixed",
        "perf": "Changed",
        "security": "Security",
        "revert": "Fixed",
        "docs": "Changed",
        "refactor": "Changed",
    }
    return [mapping.get(commit_type, "Changed")]


def _run_git_log(repo_path: Path, *, limit: int) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_path),
            "log",
            "--no-merges",
            f"-n{int(limit)}",
            "--date=short",
            "--pretty=format:%H%x1f%s%x1f%ad%x1f%b%x1e",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"git log failed in {repo_path}")
    return result.stdout


def _normalize_category(value: str) -> str:
    lowered = value.strip().lower()
    if lowered == "known issues":
        return "Known Issues"
    return value.strip().title()


def _clean_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _collapse_ws(value)


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen
