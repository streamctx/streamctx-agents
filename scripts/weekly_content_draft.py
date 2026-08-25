#!/usr/bin/env python3
"""Weekly marketing-pipeline drafts from shipped repo activity via OpenRouter.

Reads git/CHANGELOG activity from the StreamCtx SDK and this agents repo,
then writes Draft / Needs Review cards into the existing content-pipeline
SQLite store the dashboard already reads. Does not call the marketing agent,
does not post, and never sets Ready to Post or Published.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agents.marketing_agent.sources import (  # noqa: E402
    _commit_to_source,
    _parse_git_log,
    is_significant_commit,
    parse_changelog,
)
from content_pipeline import (  # noqa: E402
    CHANNELS,
    DEFAULT_PIPELINE_DB,
    FLAG_FOUNDER,
    FLAG_LEGAL,
    FLAG_NONE,
    FLAG_PRICING,
    STAGE_DRAFT,
    STAGE_NEEDS_REVIEW,
    ContentItem,
    ContentPipelineStore,
)

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_LOG_PATH = ROOT / "logs" / "weekly_draft_runs.log"
WEEKLY_MARKER = "[weekly-draft]"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
HTTP_REFERER = "https://github.com/streamctx/streamctx-agents"

# Preferred when OPENROUTER_MODEL is unset: a :free-suffixed id. Not a
# permanent pin — resolve_model() always checks the live free list.
DEFAULT_PREFERRED_MODEL = "meta-llama/llama-3.3-70b-instruct:free"

SleepFn = Callable[[float], None]
HttpFn = Callable[..., dict[str, Any]]

POSITIONING = """
StreamCtx is a Context Nervous System for AI agents: a lightweight Python SDK
that sits between an app and any LLM API and watches messages for poisoned
context, silent drift, runaway loops, and failures that trace back to earlier
steps. Positioning: most tools answer "how many tokens?"; StreamCtx answers
"why is my agent broken, what caused it, and how do I fix it?"

Pricing (do not invent other prices or plans):
- Core SDK (all features + local SQLite) is free forever, MIT-licensed, no
  feature gates, no credit card, no signup.
- A managed offering (hosted Supabase + dashboard + team access) is planned;
  features themselves will never move behind a paywall.
- Do not claim the managed offering is shipped unless this week's sources say so.

Tone: technically substantive, specific, calm. No hype ("game-changing",
"revolutionary"), no invented metrics, no unshipped-feature claims.
""".strip()

GUARDRAILS = """
Guardrails:
- Only use facts present in this week's shipped sources. If a number, benchmark,
  customer, or feature is not in the sources, omit it.
- Do not claim something shipped if it only appears as a plan, TODO, or roadmap.
- If the draft would touch legal, pricing, terms, privacy, licenses, support,
  refunds, or SLAs, set flag accordingly and stage to "review".
- Never set stage to ready or published. Automated copy is a starting point.
- HN and Reddit: value-first, answer a technical question, no product pitch,
  at most one incidental product mention.
- X/Twitter: hard cap ~280 characters, one concrete shipped fact.
- LinkedIn: longer-form (a few short paragraphs), still no empty thought-leadership.
- Dev.to and GitHub: technical depth — what changed, why it mattered, how to use it.
- Product Hunt / Indie Hackers: only if there is a real user-facing ship or launch.
""".strip()

CHANNEL_SPECS: dict[str, dict[str, Any]] = {
    "Twitter / X": {
        "max_chars": 280,
        "shape": "One tweet. <=280 characters. One shipped fact. No thread unless sources need it.",
        "needs": "any_shipped",
    },
    "LinkedIn": {
        "max_chars": 1300,
        "shape": "2–4 short paragraphs. Concrete ship, then why it matters for agent builders.",
        "needs": "any_shipped",
    },
    "Hacker News": {
        "max_chars": 1200,
        "shape": "Value-first comment or Show HN note. Teach the technical change. No pitch.",
        "needs": "technical",
    },
    "Reddit": {
        "max_chars": 1200,
        "shape": "Helpful technical note for practitioners. No self-promo phrasing.",
        "needs": "technical",
    },
    "Dev.to": {
        "max_chars": 4000,
        "shape": "Short technical post: problem, what shipped, how it works. Include a title-worthy hook.",
        "needs": "depth",
    },
    "GitHub": {
        "max_chars": 2500,
        "shape": "Release/README-style notes: what changed, who it affects, upgrade notes if any.",
        "needs": "changelog_or_docs",
    },
    "Indie Hackers": {
        "max_chars": 1500,
        "shape": "Builder log: what shipped this week and why. No growth-hack tone.",
        "needs": "user_facing",
    },
    "Product Hunt": {
        "max_chars": 800,
        "shape": "Launch tagline + 2–3 bullets. Only if this week is an actual launch/release.",
        "needs": "launch",
    },
}

LEGAL_RE = re.compile(
    r"\b(legal|terms|privacy|gdpr|license|copyright|trademark)\b",
    re.I,
)
PRICING_RE = re.compile(
    r"\b(pric(?:e|ing)|paid plan|subscription|credit card|paywall)\b",
    re.I,
)
SUPPORT_RE = re.compile(
    r"\b(support|refund|sla|uptime guarantee|customer success)\b",
    re.I,
)
LAUNCH_RE = re.compile(
    r"\b(launch|released?|pypi|show hn|general availability)\b",
    re.I,
)
MINOR_RELEASE_RE = re.compile(r"\b\d+\.\d+\.0\b")
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.I)
TRUTHY_DISABLED = frozenset({"0", "false", "no", "off", "disabled"})


class RateLimitError(RuntimeError):
    """OpenRouter free-tier rate limit after retries."""


@dataclass
class RepoSlice:
    name: str
    path: str
    present: bool
    commits: list[str] = field(default_factory=list)
    merges: list[str] = field(default_factory=list)
    changelog_diff: str = ""
    changelog_text: str = ""
    error: str = ""


@dataclass
class WeekMaterial:
    repos: list[RepoSlice]
    changelog_entries: list[str]
    significant_commits: list[str]
    shipped: bool
    has_technical: bool
    has_depth: bool
    has_user_facing: bool
    has_launch: bool
    has_changelog_or_docs: bool
    briefing: str

    def channel_reason(self, channel: str) -> Optional[str]:
        needs = CHANNEL_SPECS[channel]["needs"]
        if not self.shipped:
            return "no shipped material this week"
        if needs == "any_shipped":
            return None
        if needs == "technical" and not self.has_technical:
            return "no technical ship worth a community comment"
        if needs == "depth" and not self.has_depth:
            return "not enough depth for a long-form technical post"
        if needs == "user_facing" and not self.has_user_facing:
            return "no user-facing ship for a builder log"
        if needs == "launch" and not self.has_launch:
            return "no launch or version release this week"
        if needs == "changelog_or_docs" and not self.has_changelog_or_docs:
            return "no changelog or docs ship for GitHub notes"
        return None


@dataclass
class ChannelOutcome:
    channel: str
    action: str
    detail: str = ""
    item_id: str = ""


@dataclass
class RunSummary:
    started_at: str
    model_used: str = ""
    preferred_model: str = ""
    model_fallback: bool = False
    free_model_count: int = 0
    checked: list[str] = field(default_factory=list)
    drafted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    outcomes: list[ChannelOutcome] = field(default_factory=list)
    disabled: bool = False
    error: str = ""

    def log_text(self) -> str:
        lines = [
            f"=== weekly-draft {self.started_at} ===",
        ]
        if self.disabled:
            lines.append("disabled: WEEKLY_DRAFTS_ENABLED is off")
            lines.append("")
            return "\n".join(lines)
        if self.error:
            lines.append(f"error: {self.error}")
        lines.append(
            f"model: {self.model_used or '(none)'} "
            f"(preferred={self.preferred_model or '(live :free first)'}"
            f"{'; FALLBACK to free list' if self.model_fallback else ''})"
        )
        lines.append(f"free_models_seen: {self.free_model_count}")
        lines.append("checked:")
        for row in self.checked or ["(nothing)"]:
            lines.append(f"  - {row}")
        lines.append("drafted: " + (", ".join(self.drafted) if self.drafted else "(none)"))
        lines.append("skipped:")
        for row in self.skipped or ["(none)"]:
            lines.append(f"  - {row}")
        if self.rate_limited:
            lines.append(
                "rate-limit fallbacks: " + ", ".join(self.rate_limited)
            )
        lines.append("")
        return "\n".join(lines)


def enabled() -> bool:
    raw = (os.environ.get("WEEKLY_DRAFTS_ENABLED") or "true").strip().lower()
    return raw not in TRUTHY_DISABLED


def pipeline_db_path(override: Optional[str] = None) -> Path:
    if override:
        return Path(override)
    env = (os.environ.get("CONTENT_PIPELINE_DB") or "").strip()
    if env:
        return Path(env)
    return Path(DEFAULT_PIPELINE_DB)


def log_path(override: Optional[str] = None) -> Path:
    if override:
        return Path(override)
    env = (os.environ.get("WEEKLY_DRAFT_LOG") or "").strip()
    if env:
        return Path(env)
    return DEFAULT_LOG_PATH


def agents_root() -> Path:
    env = (os.environ.get("STREAMCTX_AGENTS_ROOT") or "").strip()
    if env:
        return Path(env)
    return ROOT


def sdk_root() -> Path:
    env = (os.environ.get("STREAMCTX_PRODUCT_ROOT") or "").strip()
    if env:
        return Path(env)
    ci_checkout = agents_root() / ".deps" / "streamctx"
    if (ci_checkout / ".git").exists() or (ci_checkout / "CHANGELOG.md").exists():
        return ci_checkout
    sibling = agents_root().parent / "streamctx"
    return sibling


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_zero_price(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() in {"0", "0.0", "0.00", "0.000"}
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def is_free_model_entry(entry: Mapping[str, Any]) -> bool:
    pricing = entry.get("pricing") or {}
    if not isinstance(pricing, Mapping):
        return False
    return _is_zero_price(pricing.get("prompt")) and _is_zero_price(
        pricing.get("completion")
    )


def extract_free_model_ids(payload: Mapping[str, Any]) -> list[str]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if not is_free_model_entry(row):
            continue
        model_id = str(row.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        ids.append(model_id)
    return ids


def pick_default_free_model(free_ids: Sequence[str]) -> str:
    for model_id in free_ids:
        if model_id.endswith(":free"):
            return model_id
    return free_ids[0] if free_ids else ""


def resolve_model(
    preferred: str,
    free_ids: Sequence[str],
) -> tuple[str, bool]:
    """Return (model_id, used_fallback)."""
    wanted = (preferred or "").strip()
    if wanted and wanted in free_ids:
        return wanted, False
    fallback = pick_default_free_model(free_ids)
    if not fallback:
        raise RuntimeError("OpenRouter returned no free models (prompt=0, completion=0)")
    if not wanted:
        return fallback, False
    return fallback, True


def openrouter_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "HTTP-Referer": HTTP_REFERER,
        "X-Title": "StreamCtx Weekly Drafts",
        "User-Agent": "streamctx-weekly-content-draft/1.0",
    }


def http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    json_body: Optional[Mapping[str, Any]] = None,
    timeout: float = 45.0,
    max_retries: int = 4,
    backoff_base_seconds: float = 2.0,
    max_backoff_seconds: float = 45.0,
    sleep_fn: Optional[SleepFn] = None,
) -> dict[str, Any]:
    sleeper = sleep_fn or time.sleep
    delay = max(0.2, float(backoff_base_seconds))
    cap = max(delay, float(max_backoff_seconds))
    hdrs = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    last_error: Optional[Exception] = None
    attempts = max(1, int(max_retries))
    for attempt in range(attempts):
        request = Request(url, data=data, headers=hdrs, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            status = int(exc.code)
            last_error = exc
            if status == 429:
                if attempt >= attempts - 1:
                    raise RateLimitError(f"HTTP 429 for {url}: {body[:300]}") from exc
                sleeper(_retry_after(exc, delay, cap))
                delay = min(cap, delay * 2)
                continue
            if status in RETRY_STATUSES and attempt < attempts - 1:
                sleeper(_retry_after(exc, delay, cap))
                delay = min(cap, delay * 2)
                continue
            raise RuntimeError(f"HTTP {status} for {url}: {body[:300]}") from exc
        except URLError as exc:
            last_error = exc
            if attempt >= attempts - 1:
                raise RuntimeError(f"network error for {url}: {exc.reason}") from exc
            sleeper(min(cap, delay))
            delay = min(cap, delay * 2)
            continue

        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"non-JSON response from {url}: {raw[:300]}") from exc
        if isinstance(parsed, dict):
            return parsed
        return {"data": parsed}

    raise RuntimeError(f"request failed for {url}: {last_error}")


def _retry_after(exc: HTTPError, delay: float, cap: float) -> float:
    raw = ""
    if exc.headers is not None:
        raw = str(exc.headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return min(cap, float(raw))
    return min(cap, delay)


def list_free_models(
    api_key: str,
    *,
    http: Optional[HttpFn] = None,
    sleep_fn: Optional[SleepFn] = None,
) -> list[str]:
    fn = http or http_json
    payload = fn(
        "GET",
        OPENROUTER_MODELS_URL,
        headers=openrouter_headers(api_key),
        sleep_fn=sleep_fn,
    )
    return extract_free_model_ids(payload)


def chat_completion(
    api_key: str,
    model: str,
    messages: Sequence[Mapping[str, str]],
    *,
    http: Optional[HttpFn] = None,
    sleep_fn: Optional[SleepFn] = None,
) -> str:
    fn = http or http_json
    payload = fn(
        "POST",
        OPENROUTER_CHAT_URL,
        headers=openrouter_headers(api_key),
        json_body={
            "model": model,
            "messages": list(messages),
            "temperature": 0.2,
        },
        sleep_fn=sleep_fn,
    )
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {payload!r}"[:400])
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping)
        ]
        return "\n".join(p for p in parts if p).strip()
    return str(content or "").strip()


def run_git(repo: Path, args: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"git {' '.join(args)} failed in {repo}")
    return result.stdout


def _format_commit(source) -> str:
    date = f" ({source.date})" if source.date else ""
    ident = source.identifier[:10] if source.identifier else ""
    body = (source.body or "").strip()
    line = f"{ident} {source.title}{date}".strip()
    if body:
        compact = " ".join(body.split())
        line += f" — {compact[:240]}"
    return line


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        pass
    for fmt, size in (("%Y-%m-%d", 10), ("%Y-%m", 7)):
        try:
            parsed = datetime.strptime(text[:size], fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return None


def _changelog_in_window(date_str: Optional[str], cutoff: datetime, file_changed: bool) -> bool:
    parsed = _parse_date(date_str)
    if parsed is None:
        return file_changed
    return parsed >= cutoff.replace(hour=0, minute=0, second=0, microsecond=0)


def collect_repo(
    name: str,
    path: Path,
    *,
    since: str,
    git_fn: Optional[Callable[[Path, Sequence[str]], str]] = None,
) -> RepoSlice:
    slice_ = RepoSlice(name=name, path=str(path), present=path.exists())
    if not slice_.present:
        slice_.error = "path not found"
        return slice_
    git = git_fn or (lambda repo, args: run_git(repo, args))

    try:
        commit_raw = git(
            path,
            [
                "log",
                "--no-merges",
                f"--since={since}",
                "--date=short",
                "--pretty=format:%H%x1f%s%x1f%ad%x1f%b%x1e",
            ],
        )
        merge_raw = git(
            path,
            [
                "log",
                "--merges",
                f"--since={since}",
                "--date=short",
                "--pretty=format:%H%x1f%s%x1f%ad%x1f%b%x1e",
            ],
        )
        try:
            slice_.changelog_diff = git(
                path,
                ["log", f"--since={since}", "-p", "--", "CHANGELOG.md"],
            ).strip()
        except RuntimeError:
            slice_.changelog_diff = ""
        changelog_path = path / "CHANGELOG.md"
        if changelog_path.exists():
            slice_.changelog_text = changelog_path.read_text(encoding="utf-8")
    except RuntimeError as exc:
        slice_.error = str(exc)
        return slice_

    commits = [_commit_to_source(row) for row in _parse_git_log(commit_raw)]
    slice_.commits = [
        _format_commit(src) for src in commits if is_significant_commit(src)
    ]
    merges = [_commit_to_source(row) for row in _parse_git_log(merge_raw)]
    slice_.merges = [_format_commit(src) for src in merges if src.title.strip()]
    return slice_


def collect_week_material(
    *,
    since_days: int = 7,
    now: Optional[datetime] = None,
    git_fn: Optional[Callable[[Path, Sequence[str]], str]] = None,
) -> WeekMaterial:
    current = now or _now()
    cutoff = current - timedelta(days=since_days)
    since = f"{since_days} days ago"
    repos = [
        collect_repo("streamctx", sdk_root(), since=since, git_fn=git_fn),
        collect_repo(
            "streamctx-agents",
            agents_root(),
            since=since,
            git_fn=git_fn,
        ),
    ]

    changelog_lines: list[str] = []
    significant: list[str] = []
    categories: list[str] = []
    has_docs = False

    for repo in repos:
        significant.extend(f"[{repo.name}] {row}" for row in repo.commits)
        if repo.changelog_text:
            file_changed = bool(repo.changelog_diff.strip())
            for entry in parse_changelog(text=repo.changelog_text):
                if not _changelog_in_window(entry.date, cutoff, file_changed):
                    continue
                categories.extend(entry.categories)
                block = f"[{repo.name}] {entry.title}"
                if entry.date:
                    block += f" ({entry.date})"
                if entry.body:
                    block += f"\n{entry.body}"
                changelog_lines.append(block)
                docs_hit = any("doc" in (c or "").lower() for c in entry.categories)
                has_docs = has_docs or docs_hit
        if any("readme" in c.lower() or "docs" in c.lower() for c in repo.commits):
            has_docs = True

    shipped = bool(changelog_lines or significant)
    cats = {c.lower() for c in categories}
    commit_blob = " ".join(significant).lower()
    has_technical = shipped and (
        bool(cats & {"fixed", "added", "changed", "security"})
        or any(
            token in commit_blob
            for token in ("fix", "feat", "wrap", "checkpoint", "replay", "compress")
        )
    )
    has_user_facing = shipped and (
        "added" in cats
        or "feat" in commit_blob
        or "fixed" in cats
        or "fix:" in commit_blob
    )
    launch_blob = " ".join(changelog_lines + significant)
    has_launch = bool(LAUNCH_RE.search(launch_blob) or MINOR_RELEASE_RE.search(launch_blob))
    has_depth = len(changelog_lines) + len(significant) >= 2 or any(
        len(block) > 180 for block in changelog_lines
    )
    has_changelog_or_docs = bool(changelog_lines) or has_docs or bool(
        any(r.changelog_diff for r in repos)
    )

    briefing_parts = []
    for repo in repos:
        if not repo.present:
            briefing_parts.append(f"## {repo.name}\n(not found at {repo.path}: {repo.error})")
            continue
        if repo.error:
            briefing_parts.append(f"## {repo.name}\n(git error: {repo.error})")
            continue
        lines = [f"## {repo.name} ({repo.path})"]
        if repo.commits:
            lines.append("Significant commits:")
            lines.extend(f"- {row}" for row in repo.commits[:20])
        else:
            lines.append("Significant commits: none")
        if repo.merges:
            lines.append("Merged commits:")
            lines.extend(f"- {row}" for row in repo.merges[:10])
        if repo.changelog_diff:
            diff = repo.changelog_diff.strip()
            if len(diff) > 4000:
                diff = diff[:4000] + "\n...[truncated]"
            lines.append("CHANGELOG.md diff this week:")
            lines.append(diff)
        briefing_parts.append("\n".join(lines))

    if changelog_lines:
        briefing_parts.append("## Changelog entries in window\n" + "\n\n".join(changelog_lines))

    return WeekMaterial(
        repos=repos,
        changelog_entries=changelog_lines,
        significant_commits=significant,
        shipped=shipped,
        has_technical=has_technical,
        has_depth=has_depth,
        has_user_facing=has_user_facing,
        has_launch=has_launch,
        has_changelog_or_docs=has_changelog_or_docs,
        briefing="\n\n".join(briefing_parts).strip(),
    )


def existing_board_context(items: Sequence[ContentItem], *, since: datetime) -> str:
    recent = []
    for item in items:
        created = _parse_date(item.created_at) or _parse_date(item.created_at[:10])
        if created is not None and created < since:
            continue
        recent.append(f"- [{item.channel}/{item.stage}] {item.title}")
    if not recent:
        return "(no recent pipeline cards this week)"
    return "\n".join(recent[:30])


def already_drafted_this_week(
    items: Sequence[ContentItem],
    channel: str,
    *,
    cutoff: datetime,
) -> bool:
    for item in items:
        if item.channel != channel:
            continue
        if WEEKLY_MARKER not in (item.notes or "") and WEEKLY_MARKER not in item.title:
            continue
        created = _parse_date(item.created_at[:10] if item.created_at else None)
        if created is None:
            continue
        if created >= cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
            return True
    return False


def build_messages(
    channel: str,
    material: WeekMaterial,
    board: str,
) -> list[dict[str, str]]:
    spec = CHANNEL_SPECS[channel]
    user = f"""
Channel: {channel}
Channel shape: {spec['shape']}
Hard length limit: {spec['max_chars']} characters for the body.

This week's shipped sources (SDK + agents repos):
{material.briefing}

Existing pipeline cards already on the board (do not duplicate):
{board}

Return JSON only, no markdown fences:
{{
  "skip": false,
  "reason": "",
  "title": "short board title",
  "body": "the draft copy",
  "flag": "",
  "stage": "draft"
}}

Rules for this response:
- skip=true if this channel has nothing honest to say from THIS week's ships.
- flag must be one of: "" (empty), "Legal review", "Founder/Eng review", "Pricing sign-off".
- stage must be "draft" or "review". Never "ready" or "published".
- body must obey the length limit.
- title is a tracking-board title, not a tweet.
""".strip()
    system = f"{POSITIONING}\n\n{GUARDRAILS}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_draft_payload(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty model response")
    fenced = JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"model did not return JSON: {text[:240]}")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model JSON was not an object")
    return data


def map_stage(raw: Any, flag: str) -> str:
    value = str(raw or "draft").strip().lower()
    if value in {"review", "needs review", "needs_review"}:
        stage = STAGE_NEEDS_REVIEW
    else:
        stage = STAGE_DRAFT
    if flag in {FLAG_LEGAL, FLAG_PRICING, FLAG_FOUNDER}:
        return STAGE_NEEDS_REVIEW
    if value in {"ready", "published", "ready to post"}:
        return STAGE_DRAFT if flag == FLAG_NONE else STAGE_NEEDS_REVIEW
    return stage


def map_flag(raw: Any, title: str, body: str) -> str:
    value = str(raw or "").strip()
    aliases = {
        "": FLAG_NONE,
        "none": FLAG_NONE,
        "legal review": FLAG_LEGAL,
        "legal": FLAG_LEGAL,
        "founder/eng review": FLAG_FOUNDER,
        "founder": FLAG_FOUNDER,
        "pricing sign-off": FLAG_PRICING,
        "pricing": FLAG_PRICING,
    }
    flag = aliases.get(value.lower(), FLAG_NONE)
    blob = f"{title}\n{body}"
    if PRICING_RE.search(blob):
        return FLAG_PRICING
    if LEGAL_RE.search(blob):
        return FLAG_LEGAL
    if SUPPORT_RE.search(blob):
        return FLAG_FOUNDER
    return flag


def clip_body(channel: str, body: str) -> str:
    limit = int(CHANNEL_SPECS[channel]["max_chars"])
    text = (body or "").strip()
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rsplit(" ", 1)[0]
    return clipped.rstrip(",;:") + "…"


def compose_notes(body: str, model: str, generated_at: str) -> str:
    return (
        f"{body.strip()}\n\n"
        f"{WEEKLY_MARKER} {generated_at} model={model} — starting point, not final copy."
    )


def write_item(
    store: ContentPipelineStore,
    *,
    title: str,
    channel: str,
    stage: str,
    flag: str,
    notes: str,
) -> ContentItem:
    if stage not in {STAGE_DRAFT, STAGE_NEEDS_REVIEW}:
        stage = STAGE_DRAFT
    return store.create_item(
        title=title,
        channel=channel,
        stage=stage,
        flag=flag,
        notes=notes,
    )


def append_log(path: Path, summary: RunSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(summary.log_text())


def draft_channel(
    channel: str,
    *,
    api_key: str,
    model: str,
    material: WeekMaterial,
    board: str,
    http: Optional[HttpFn],
    sleep_fn: Optional[SleepFn],
) -> dict[str, Any]:
    raw = chat_completion(
        api_key,
        model,
        build_messages(channel, material, board),
        http=http,
        sleep_fn=sleep_fn,
    )
    return parse_draft_payload(raw)


def run_weekly_drafts(
    *,
    api_key: Optional[str] = None,
    preferred_model: Optional[str] = None,
    since_days: int = 7,
    db_path: Optional[str] = None,
    log_file: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
    http: Optional[HttpFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    git_fn: Optional[Callable[[Path, Sequence[str]], str]] = None,
    now: Optional[datetime] = None,
    pause_seconds: float = 3.0,
) -> RunSummary:
    current = now or _now()
    summary = RunSummary(started_at=current.isoformat())
    log_target = log_path(log_file)

    if not enabled() and not force:
        summary.disabled = True
        append_log(log_target, summary)
        return summary

    key = (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        summary.error = "OPENROUTER_API_KEY is missing"
        append_log(log_target, summary)
        raise RuntimeError(summary.error)

    material = collect_week_material(
        since_days=since_days,
        now=current,
        git_fn=git_fn,
    )
    cutoff = current - timedelta(days=since_days)
    for repo in material.repos:
        status = "ok" if repo.present and not repo.error else f"missing/error: {repo.error or 'not found'}"
        summary.checked.append(
            f"{repo.name} @ {repo.path} ({status}); "
            f"commits={len(repo.commits)} merges={len(repo.merges)} "
            f"changelog_diff={'yes' if repo.changelog_diff else 'no'}"
        )
    summary.checked.append(
        f"shipped={material.shipped} technical={material.has_technical} "
        f"depth={material.has_depth} launch={material.has_launch}"
    )

    preferred = (
        preferred_model
        if preferred_model is not None
        else (os.environ.get("OPENROUTER_MODEL") or DEFAULT_PREFERRED_MODEL)
    ).strip()
    summary.preferred_model = preferred
    try:
        free_ids = list_free_models(key, http=http, sleep_fn=sleep_fn)
        summary.free_model_count = len(free_ids)
        model, fallback = resolve_model(preferred, free_ids)
        summary.model_used = model
        summary.model_fallback = fallback
    except RateLimitError as exc:
        summary.error = f"rate-limited while listing models: {exc}"
        summary.rate_limited.append("models.list")
        append_log(log_target, summary)
        return summary

    store = ContentPipelineStore(db_path=pipeline_db_path(db_path))
    try:
        items = store.list_items()
        board = existing_board_context(items, since=cutoff)

        if not material.shipped:
            for channel in CHANNELS:
                reason = "no shipped material this week"
                summary.skipped.append(f"{channel}: {reason}")
                summary.outcomes.append(ChannelOutcome(channel, "skipped", reason))
            append_log(log_target, summary)
            return summary

        sleeper = sleep_fn or time.sleep
        api_calls = 0
        for channel in CHANNELS:
            reason = material.channel_reason(channel)
            if reason:
                summary.skipped.append(f"{channel}: {reason}")
                summary.outcomes.append(ChannelOutcome(channel, "skipped", reason))
                continue
            if not force and already_drafted_this_week(items, channel, cutoff=cutoff):
                detail = "already has a weekly-draft card this week"
                summary.skipped.append(f"{channel}: {detail}")
                summary.outcomes.append(ChannelOutcome(channel, "skipped", detail))
                continue

            if api_calls and pause_seconds > 0:
                sleeper(pause_seconds)
            api_calls += 1
            try:
                payload = draft_channel(
                    channel,
                    api_key=key,
                    model=model,
                    material=material,
                    board=board,
                    http=http,
                    sleep_fn=sleep_fn,
                )
            except RateLimitError as exc:
                detail = f"rate-limited, skipped ({exc})"
                summary.skipped.append(f"{channel}: {detail}")
                summary.rate_limited.append(channel)
                summary.outcomes.append(ChannelOutcome(channel, "rate_limited", detail))
                continue
            except Exception as exc:
                detail = f"generation failed: {exc}"
                summary.skipped.append(f"{channel}: {detail}")
                summary.outcomes.append(ChannelOutcome(channel, "error", detail))
                continue

            if payload.get("skip") is True:
                detail = str(payload.get("reason") or "model skipped: nothing to say")
                summary.skipped.append(f"{channel}: {detail}")
                summary.outcomes.append(ChannelOutcome(channel, "skipped", detail))
                continue

            title = str(payload.get("title") or "").strip()
            body = clip_body(channel, str(payload.get("body") or ""))
            if not title or not body:
                detail = "model returned empty title/body"
                summary.skipped.append(f"{channel}: {detail}")
                summary.outcomes.append(ChannelOutcome(channel, "skipped", detail))
                continue

            flag = map_flag(payload.get("flag"), title, body)
            stage = map_stage(payload.get("stage"), flag)
            notes = compose_notes(body, model, current.isoformat())
            if dry_run:
                summary.drafted.append(f"{channel} (dry-run)")
                summary.outcomes.append(
                    ChannelOutcome(channel, "dry_run", title)
                )
                continue
            item = write_item(
                store,
                title=title,
                channel=channel,
                stage=stage,
                flag=flag,
                notes=notes,
            )
            items = [*items, item]
            summary.drafted.append(channel)
            summary.outcomes.append(
                ChannelOutcome(channel, "drafted", title, item.item_id)
            )
    finally:
        store.close()

    append_log(log_target, summary)
    return summary


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Draft weekly marketing-pipeline cards from shipped git/CHANGELOG activity.",
    )
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--db", dest="db_path", default=None, help="content_pipeline.db path")
    parser.add_argument("--log", dest="log_file", default=None)
    parser.add_argument("--model", dest="preferred_model", default=None)
    parser.add_argument("--force", action="store_true", help="Ignore disable flag and this-week duplicates")
    parser.add_argument("--dry-run", action="store_true", help="Call the model but do not write the store")
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=3.0,
        help="Delay between channel requests (free tier ~20 req/min)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        summary = run_weekly_drafts(
            preferred_model=args.preferred_model,
            since_days=args.since_days,
            db_path=args.db_path,
            log_file=args.log_file,
            force=args.force,
            dry_run=args.dry_run,
            pause_seconds=args.pause_seconds,
        )
    except RuntimeError as exc:
        print(f"[weekly-draft] {exc}", file=sys.stderr)
        return 1
    print(summary.log_text(), end="")
    return 1 if summary.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
