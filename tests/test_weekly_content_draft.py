"""Weekly content-draft generator — writes the existing pipeline store only."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from io import BytesIO

import pytest

from content_pipeline import (
    FLAG_LEGAL,
    FLAG_NONE,
    FLAG_PRICING,
    STAGE_DRAFT,
    STAGE_NEEDS_REVIEW,
    STAGE_PUBLISHED,
    STAGE_READY_TO_POST,
    ContentPipelineStore,
)
from scripts.weekly_content_draft import (
    CHANNELS,
    DEFAULT_PREFERRED_MODEL,
    OPENROUTER_CHAT_URL,
    OPENROUTER_MODELS_URL,
    RateLimitError,
    WEEKLY_MARKER,
    WeekMaterial,
    clip_body,
    extract_free_model_ids,
    http_json,
    map_flag,
    map_stage,
    parse_draft_payload,
    resolve_model,
    run_weekly_drafts,
)


FREE_MODELS_PAYLOAD = {
    "data": [
        {
            "id": "paid/model",
            "pricing": {"prompt": "0.5", "completion": "0.5"},
        },
        {
            "id": "acme/alpha:free",
            "pricing": {"prompt": "0", "completion": "0"},
        },
        {
            "id": "acme/beta:free",
            "pricing": {"prompt": 0, "completion": 0},
        },
    ]
}

SHIPPED_CHANGELOG = """
# Changelog

## [0.4.7] - 2026-08-24

### Fixed
- `wrap()` no longer double-counts LLM calls when start() and wrap() are combined.

### Added
- Checkpoint resume now restores healed failure markers.
"""

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _git_log_blob(subject: str, date: str = "2026-08-24") -> str:
    return (
        f"abc123def456\x1f{subject}\x1f{date}\x1f"
        "Restores checkpoint state after a crash. Closes #12.\x1e"
    )


class FakeGit:
    def __init__(self, *, commits: str = "", changelog: str = "") -> None:
        self.commits = commits
        self.changelog = changelog

    def __call__(self, repo: Path, args: list[str]) -> str:
        joined = " ".join(args)
        if "-p" in args and "CHANGELOG.md" in args:
            return self.changelog
        if "--merges" in args:
            return ""
        if "--no-merges" in args:
            return self.commits
        raise AssertionError(f"unexpected git args: {joined}")


class FakeHttp:
    def __init__(self, drafts: dict[str, dict] | None = None, rate_limit: str | None = None) -> None:
        self.drafts = drafts or {}
        self.rate_limit = rate_limit
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url))
        if url == OPENROUTER_MODELS_URL:
            return FREE_MODELS_PAYLOAD
        if url == OPENROUTER_CHAT_URL:
            body = kwargs.get("json_body") or {}
            messages = body.get("messages") or []
            user = messages[-1]["content"] if messages else ""
            channel = ""
            for name in CHANNELS:
                if f"Channel: {name}" in user:
                    channel = name
                    break
            if self.rate_limit and channel == self.rate_limit:
                raise RateLimitError("HTTP 429 for chat")
            payload = self.drafts.get(
                channel,
                {
                    "skip": False,
                    "title": f"{channel} weekly note",
                    "body": f"Shipped wrap() double-count fix this week for {channel}.",
                    "flag": "",
                    "stage": "draft",
                },
            )
            return {"choices": [{"message": {"content": json.dumps(payload)}}]}
        raise AssertionError(url)


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    db = tmp_path / "content_pipeline.db"
    log = tmp_path / "weekly_draft_runs.log"
    sdk = tmp_path / "streamctx"
    agents = tmp_path / "streamctx-agents"
    sdk.mkdir()
    agents.mkdir()
    (sdk / "CHANGELOG.md").write_text(SHIPPED_CHANGELOG, encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("CONTENT_PIPELINE_DB", str(db))
    monkeypatch.setenv("WEEKLY_DRAFT_LOG", str(log))
    monkeypatch.setenv("STREAMCTX_PRODUCT_ROOT", str(sdk))
    monkeypatch.setenv("STREAMCTX_AGENTS_ROOT", str(agents))
    monkeypatch.setenv("WEEKLY_DRAFTS_ENABLED", "true")
    return {"db": db, "log": log, "sdk": sdk, "agents": agents}


def test_extract_free_models_by_zero_pricing():
    ids = extract_free_model_ids(FREE_MODELS_PAYLOAD)
    assert ids == ["acme/alpha:free", "acme/beta:free"]
    assert "paid/model" not in ids


def test_resolve_model_keeps_configured_free_id():
    free = ["acme/alpha:free", "acme/beta:free"]
    used, fallback = resolve_model("acme/beta:free", free)
    assert used == "acme/beta:free"
    assert fallback is False


def test_resolve_model_falls_back_when_configured_id_is_not_free():
    free = ["acme/alpha:free", "acme/beta:free"]
    used, fallback = resolve_model("openai/gpt-4o", free)
    assert used == "acme/alpha:free"
    assert fallback is True


def test_resolve_model_defaults_to_first_free_suffix():
    used, fallback = resolve_model("", ["paid-looking", "vendor/model:free"])
    assert used == "vendor/model:free"
    assert fallback is False


def test_map_stage_never_ready_or_published():
    assert map_stage("draft", FLAG_NONE) == STAGE_DRAFT
    assert map_stage("review", FLAG_NONE) == STAGE_NEEDS_REVIEW
    assert map_stage("ready", FLAG_NONE) == STAGE_DRAFT
    assert map_stage("published", FLAG_NONE) == STAGE_DRAFT
    assert map_stage("ready", FLAG_LEGAL) == STAGE_NEEDS_REVIEW
    assert STAGE_READY_TO_POST not in {map_stage("ready", FLAG_NONE), map_stage("published", FLAG_NONE)}
    assert STAGE_PUBLISHED not in {map_stage("ready", FLAG_NONE), map_stage("published", FLAG_NONE)}


def test_map_flag_from_copy_and_model():
    assert map_flag("Legal review", "HN note", "technical fix") == FLAG_LEGAL
    assert map_flag("", "Launch", "Core SDK stays free; no new pricing.") == FLAG_PRICING
    assert map_flag("", "Fix", "wrap() double-count") == FLAG_NONE


def test_parse_draft_payload_strips_fences():
    raw = '```json\n{"skip": true, "reason": "nothing"}\n```'
    data = parse_draft_payload(raw)
    assert data["skip"] is True


def test_clip_twitter_body():
    body = "x" * 400
    clipped = clip_body("Twitter / X", body)
    assert len(clipped) <= 280


def test_channel_skip_when_nothing_shipped():
    material = WeekMaterial(
        repos=[],
        changelog_entries=[],
        significant_commits=[],
        shipped=False,
        has_technical=False,
        has_depth=False,
        has_user_facing=False,
        has_launch=False,
        has_changelog_or_docs=False,
        briefing="",
    )
    assert material.channel_reason("LinkedIn") == "no shipped material this week"
    assert material.channel_reason("Product Hunt") == "no shipped material this week"


def test_product_hunt_skipped_without_launch():
    material = WeekMaterial(
        repos=[],
        changelog_entries=[],
        significant_commits=["fix: wrap double count"],
        shipped=True,
        has_technical=True,
        has_depth=True,
        has_user_facing=True,
        has_launch=False,
        has_changelog_or_docs=False,
        briefing="fix only",
    )
    assert material.channel_reason("Product Hunt")
    assert material.channel_reason("LinkedIn") is None


def test_disabled_run_writes_log_without_store(tmp_env, monkeypatch):
    monkeypatch.setenv("WEEKLY_DRAFTS_ENABLED", "false")
    summary = run_weekly_drafts(
        now=NOW,
        pause_seconds=0,
        http=FakeHttp(),
        git_fn=FakeGit(),
        sleep_fn=lambda _s: None,
    )
    assert summary.disabled is True
    assert not tmp_env["db"].exists()
    assert "disabled" in tmp_env["log"].read_text(encoding="utf-8")


def test_run_skips_all_channels_without_shipped_material(tmp_env):
    (tmp_env["sdk"] / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [0.1.0] - 2020-01-01\n\n- ancient history\n",
        encoding="utf-8",
    )
    http = FakeHttp()
    summary = run_weekly_drafts(
        now=NOW,
        pause_seconds=0,
        http=http,
        git_fn=FakeGit(),
        sleep_fn=lambda _s: None,
        preferred_model="acme/alpha:free",
    )
    assert summary.model_used == "acme/alpha:free"
    assert summary.model_fallback is False
    assert not summary.drafted
    assert any("no shipped material" in row for row in summary.skipped)
    chat_calls = [url for _method, url in http.calls if url == OPENROUTER_CHAT_URL]
    assert chat_calls == []
    store = ContentPipelineStore(db_path=tmp_env["db"])
    try:
        assert store.list_items() == []
    finally:
        store.close()


def test_run_drafts_into_existing_store_schema(tmp_env):
    git = FakeGit(
        commits=_git_log_blob("fix: wrap() double-counts when start() and wrap() combine"),
        changelog="diff --git a/CHANGELOG.md\n+### Fixed\n",
    )
    http = FakeHttp(
        drafts={
            "Product Hunt": {"skip": True, "reason": "not a launch week"},
            "Dev.to": {
                "skip": False,
                "title": "How wrap() stopped double-counting",
                "body": "This week StreamCtx stopped double-counting LLM calls when start() and wrap() were used together.",
                "flag": "",
                "stage": "draft",
            },
        }
    )
    summary = run_weekly_drafts(
        now=NOW,
        pause_seconds=0,
        http=http,
        git_fn=git,
        sleep_fn=lambda _s: None,
        preferred_model="openai/gpt-4o",
    )
    assert summary.model_used == "acme/alpha:free"
    assert summary.model_fallback is True
    store = ContentPipelineStore(db_path=tmp_env["db"])
    try:
        items = store.list_items()
        assert items
        assert {item.channel for item in items} <= set(CHANNELS)
        for item in items:
            assert item.stage in {STAGE_DRAFT, STAGE_NEEDS_REVIEW}
            assert item.stage not in {STAGE_READY_TO_POST, STAGE_PUBLISHED}
            assert WEEKLY_MARKER in item.notes
            assert item.created_at
            assert item.title
    finally:
        store.close()
    log = tmp_env["log"].read_text(encoding="utf-8")
    assert "FALLBACK to free list" in log
    assert "Product Hunt" in log


def test_rate_limit_on_one_channel_does_not_abort_run(tmp_env):
    git = FakeGit(
        commits=_git_log_blob("feat: restore healed markers on checkpoint resume"),
        changelog="CHANGELOG updated",
    )
    http = FakeHttp(rate_limit="LinkedIn")
    summary = run_weekly_drafts(
        now=NOW,
        pause_seconds=0,
        http=http,
        git_fn=git,
        sleep_fn=lambda _s: None,
        preferred_model="acme/alpha:free",
    )
    assert "LinkedIn" in summary.rate_limited
    assert any(row.startswith("LinkedIn:") for row in summary.skipped)
    assert summary.drafted
    assert "LinkedIn" not in summary.drafted
    store = ContentPipelineStore(db_path=tmp_env["db"])
    try:
        channels = {item.channel for item in store.list_items()}
        assert "LinkedIn" not in channels
        assert channels
    finally:
        store.close()


def test_http_json_retries_then_raises_rate_limit(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout=0):
        calls["n"] += 1
        raise HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {"Retry-After": "0"},
            BytesIO(b'{"error":"rate"}'),
        )

    monkeypatch.setattr("scripts.weekly_content_draft.urlopen", fake_urlopen)
    with pytest.raises(RateLimitError):
        http_json(
            "POST",
            OPENROUTER_CHAT_URL,
            json_body={"model": "x"},
            max_retries=3,
            backoff_base_seconds=0.01,
            sleep_fn=lambda _s: None,
        )
    assert calls["n"] == 3


def test_default_preferred_model_is_free_suffix_not_a_paid_pin():
    assert DEFAULT_PREFERRED_MODEL.endswith(":free")
