"""Streamlit dashboard for reviewing SeniorEngineer-generated features.

Run from the repository root::

    streamlit run agents/coding_agent/dashboard_review.py

Expected layout under ``generated_features/``::

    generated_features/
      <feature_slug>/
        manifest.json
        INTEGRATION.md
        implementation/*.py
        tests/
      approved/<feature_slug>/
      rejected/<feature_slug>/
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agents.coding_agent.senior_engineer_mode import (
    IMPLEMENTATION_DIR,
    INTEGRATION_NAME,
    MANIFEST_NAME,
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_NEEDS_REVIEW,
    STATUS_READY,
    STATUS_REJECTED,
    SeniorEngineer,
    slug_feature_name,
    write_package,
)
from shared.audit_log import log_action
from shared.config import AGENT_IDS, BASE_DIR
from shared.config import STATUS_APPROVED as AUDIT_APPROVED
from shared.config import STATUS_PENDING as AUDIT_PENDING
from shared.config import STATUS_REJECTED as AUDIT_REJECTED

FEATURES_ENV = "SENIOR_ENGINEER_FEATURES_DIR"
RESERVED_DIRS = frozenset({"approved", "rejected"})
SKIP_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".git"})
TEST_FN_RE = re.compile(r"^(?:async\s+)?def test_[A-Za-z0-9_]+", re.MULTILINE)
STATUS_EMOJI = {
    STATUS_READY: "🟢",
    STATUS_NEEDS_REVIEW: "🟡",
    STATUS_BLOCKED: "🟠",
    STATUS_APPROVED: "✅",
    STATUS_REJECTED: "❌",
}

AuditFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class FeatureRecord:
    """On-disk SeniorEngineer package ready for human review."""

    slug: str
    path: Path
    status: str
    feature_name: str
    original_prompt: str
    created_at: str
    updated_at: str
    manifest: dict[str, Any]
    files: dict[str, str]
    tests: dict[str, str]
    integration_md: str
    rejection_reason: str = ""


def default_features_root() -> Path:
    env = os.environ.get(FEATURES_ENV)
    if env:
        return Path(env)
    return Path(BASE_DIR) / "generated_features"


def list_features(root: Path | str) -> list[FeatureRecord]:
    """Load every generated feature under pending, approved/, and rejected/."""
    base = Path(root)
    grouped: dict[str, list[FeatureRecord]] = {}
    for path in _iter_feature_dirs(base):
        rec = load_feature(path, root=base)
        grouped.setdefault(rec.status, []).append(rec)

    ordered: list[FeatureRecord] = []
    for status in (
        STATUS_NEEDS_REVIEW,
        STATUS_READY,
        STATUS_BLOCKED,
        STATUS_APPROVED,
        STATUS_REJECTED,
    ):
        bucket = grouped.pop(status, [])
        bucket.sort(
            key=lambda rec: (
                _timestamp_sort_key(rec.updated_at or rec.created_at),
                rec.feature_name.lower(),
            ),
            reverse=True,
        )
        ordered.extend(bucket)
    for leftover in grouped.values():
        leftover.sort(key=lambda rec: rec.feature_name.lower())
        ordered.extend(leftover)
    return ordered


def find_feature(slug: str, *, root: Path | str) -> FeatureRecord:
    """Locate a feature by slug. Pending wins over approved/rejected."""
    base = Path(root)
    candidates = [
        base / slug,
        base / STATUS_APPROVED / slug,
        base / STATUS_REJECTED / slug,
    ]
    for path in candidates:
        if path.is_dir() and _looks_like_feature(path):
            return load_feature(path, root=base)
    raise FileNotFoundError(f"No generated feature named {slug!r}")


def load_feature(path: Path | str, *, root: Optional[Path | str] = None) -> FeatureRecord:
    """Read a package directory, including a missing/legacy manifest."""
    folder = Path(path)
    if not folder.is_dir():
        raise FileNotFoundError(f"Feature directory not found: {folder}")
    base = Path(root) if root is not None else folder.parent
    if folder.parent.name in RESERVED_DIRS:
        base = folder.parent.parent
    manifest = _read_json(folder / MANIFEST_NAME)
    files, tests = _collect_code(folder)
    integration = ""
    guide_path = folder / INTEGRATION_NAME
    if guide_path.is_file():
        integration = guide_path.read_text(encoding="utf-8")
    slug = str(manifest.get("slug") or folder.name)
    status = _status_from_location(folder, base, str(manifest.get("status") or ""))
    return FeatureRecord(
        slug=slug,
        path=folder.resolve(),
        status=status,
        feature_name=str(manifest.get("feature_name") or folder.name),
        original_prompt=str(
            manifest.get("original_prompt") or manifest.get("prompt") or ""
        ),
        created_at=str(manifest.get("created_at") or ""),
        updated_at=str(manifest.get("updated_at") or ""),
        manifest=manifest,
        files=files,
        tests=tests,
        integration_md=integration,
        rejection_reason=str(manifest.get("rejection_reason") or ""),
    )


def approve_feature(
    slug: str,
    *,
    root: Path | str,
    log_fn: Optional[AuditFn] = None,
) -> FeatureRecord:
    """Move a package to ``approved/`` and append an audit entry."""
    record = find_feature(slug, root=root)
    dest = Path(root) / STATUS_APPROVED / record.slug
    moved = _relocate_feature(record.path, dest)
    updated = _write_review_status(
        moved,
        status=STATUS_APPROVED,
        rejection_reason="",
    )
    _audit(
        log_fn,
        action_type="senior_engineer_approve",
        record=updated,
        status=AUDIT_APPROVED,
        extra={},
    )
    return updated


def reject_feature(
    slug: str,
    reason: str,
    *,
    root: Path | str,
    log_fn: Optional[AuditFn] = None,
) -> FeatureRecord:
    """Move a package to ``rejected/`` with a required reason."""
    note = reason.strip()
    if not note:
        raise ValueError("Rejection reason is required.")
    record = find_feature(slug, root=root)
    dest = Path(root) / STATUS_REJECTED / record.slug
    moved = _relocate_feature(record.path, dest)
    updated = _write_review_status(
        moved,
        status=STATUS_REJECTED,
        rejection_reason=note,
    )
    _audit(
        log_fn,
        action_type="senior_engineer_reject",
        record=updated,
        status=AUDIT_REJECTED,
        extra={"reason": note},
    )
    return updated


def generate_feature(
    prompt: str,
    *,
    root: Path | str,
    engineer: Optional[SeniorEngineer] = None,
    log_fn: Optional[AuditFn] = None,
) -> FeatureRecord:
    """Run SeniorEngineer on a new feature request and write the package."""
    request = prompt.strip()
    if not request:
        raise ValueError("Feature description is required.")
    started = time.perf_counter()
    package = (engineer or SeniorEngineer()).package_feature(
        request,
        output_parent=root,
    )
    dest = Path(root) / slug_feature_name(package.feature_name)
    record = load_feature(dest, root=root)
    elapsed = time.perf_counter() - started
    _audit(
        log_fn,
        action_type="senior_engineer_generate",
        record=record,
        status=AUDIT_PENDING,
        extra={"elapsed_seconds": round(elapsed, 2)},
    )
    return record


def regenerate_feature(
    slug: str,
    *,
    root: Path | str,
    engineer: Optional[SeniorEngineer] = None,
) -> FeatureRecord:
    """Re-run SeniorEngineer on the original prompt and replace the package."""
    record = find_feature(slug, root=root)
    prompt = record.original_prompt.strip()
    if not prompt:
        raise ValueError(
            f"Feature {slug!r} has no original_prompt in manifest.json; cannot regenerate."
        )
    dest = Path(root) / record.slug
    package = (engineer or SeniorEngineer()).package_feature(prompt)
    package = replace(
        package,
        original_prompt=prompt,
        created_at=record.created_at or package.created_at,
    )
    old = record.path.resolve()
    new = dest.resolve()
    if old.exists() and old != new:
        shutil.rmtree(old)
    if new.exists():
        shutil.rmtree(new)
    write_package(package, dest)
    return load_feature(dest, root=root)


def copy_package_text(record: FeatureRecord) -> str:
    """Flatten the whole package into one clipboard-friendly string."""
    chunks: list[str] = []
    for name, content in record.files.items():
        chunks.append(f"# {name}\n{content.rstrip()}\n")
    for name, content in record.tests.items():
        chunks.append(f"# {name}\n{content.rstrip()}\n")
    if record.integration_md.strip():
        chunks.append(f"# {INTEGRATION_NAME}\n{record.integration_md.rstrip()}\n")
    chunks.append(
        f"# {MANIFEST_NAME}\n{json.dumps(record.manifest, indent=2, ensure_ascii=False)}\n"
    )
    return "\n".join(chunks).rstrip() + "\n"


def count_test_cases(tests: dict[str, str]) -> int:
    return sum(len(TEST_FN_RE.findall(code)) for code in tests.values())


def line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def feature_stats(features: list[FeatureRecord]) -> dict[str, int]:
    statuses = [rec.status for rec in features]
    return {
        "total": len(features),
        "approved": statuses.count(STATUS_APPROVED),
        "ready": statuses.count(STATUS_READY),
        "rejected": statuses.count(STATUS_REJECTED),
        "needs_review": statuses.count(STATUS_NEEDS_REVIEW),
        "blocked": statuses.count(STATUS_BLOCKED),
    }


def open_in_file_explorer(path: Path | str) -> None:
    """Open a folder in the OS file manager. Does not modify files."""
    folder = Path(path).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(folder)  # type: ignore[attr-defined]
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.run([opener, str(folder)], check=False)


def render(
    st: Any = None,
    *,
    features_root: Optional[Path | str] = None,
    engineer: Optional[SeniorEngineer] = None,
    log_fn: Optional[AuditFn] = None,
) -> None:
    """Render the review dashboard. ``st`` is injected so tests can skip Streamlit."""
    if st is None:
        import streamlit as st_mod

        st = st_mod

    root = Path(features_root) if features_root is not None else default_features_root()
    st.set_page_config(
        page_title="Coding Agent Feature Review",
        layout="wide",
        page_icon="🤖",
    )
    st.title("🤖 Coding Agent Feature Review Dashboard")
    st.caption(
        "Review generated packages before merging. "
        "Approve and reject move folders; they do not patch the live repo."
    )

    _render_generate_form(st, root=root, engineer=engineer, log_fn=log_fn)
    st.divider()

    features = list_features(root)
    selected = _render_sidebar(st, features)
    flash = st.session_state.pop("flash", None)
    if flash:
        st.success(flash)
    if selected is None:
        st.warning(
            "No features generated yet. Enter a request above and click **Generate**."
        )
        _render_stats(st, features)
        return

    record = find_feature(selected, root=root)
    _render_header(st, record)
    st.divider()
    _render_prompt(st, record)
    st.divider()
    _render_viewer(st, record)
    st.divider()
    _render_actions(st, record, root=root, engineer=engineer, log_fn=log_fn)
    st.divider()
    _render_stats(st, features)


def _render_sidebar(st: Any, features: list[FeatureRecord]) -> Optional[str]:
    with st.sidebar:
        st.title("Generated Features")
        if not features:
            st.caption("Nothing to review.")
            return None
        labels = {
            rec.slug: (
                f"{STATUS_EMOJI.get(rec.status, '•')} "
                f"{rec.feature_name} · {rec.status}"
            )
            for rec in features
        }
        current = st.session_state.get("selected_feature_slug")
        slugs = [rec.slug for rec in features]
        if current not in slugs:
            current = slugs[0]
        chosen = st.selectbox(
            "Select Feature",
            slugs,
            index=slugs.index(current),
            format_func=lambda slug: labels.get(slug, slug.replace("_", " ").title()),
        )
        st.session_state["selected_feature_slug"] = chosen
        return chosen


def _render_generate_form(
    st: Any,
    *,
    root: Path,
    engineer: Optional[SeniorEngineer],
    log_fn: Optional[AuditFn],
) -> None:
    st.subheader("🚀 Generate New Feature")
    col1, col2 = st.columns([4, 1])
    with col1:
        feature_request = st.text_input(
            "Feature request (describe what you need)",
            placeholder="e.g., Add retry budget to OpenRouter calls",
            key="new_feature_request",
        )
    with col2:
        st.markdown("<div style='height: 1.7rem'></div>", unsafe_allow_html=True)
        generate_button = st.button("Generate", type="primary", key="generate_new_feature")

    if not generate_button:
        return
    if not str(feature_request or "").strip():
        st.error("Please enter a feature request")
        return

    st.info("🔄 Generating feature with SeniorEngineer mode...")
    started = time.perf_counter()
    with st.spinner("SeniorEngineer is generating the package…"):
        try:
            record = generate_feature(
                feature_request,
                root=root,
                engineer=engineer,
                log_fn=log_fn,
            )
        except Exception as exc:
            st.error(f"❌ Failed: {exc}")
            return
    elapsed = time.perf_counter() - started
    st.session_state["selected_feature_slug"] = record.slug
    st.session_state["flash"] = (
        f"✅ Feature '{record.feature_name}' generated ({record.status}) in {elapsed:.1f}s"
    )
    st.rerun()


def _render_header(st: Any, record: FeatureRecord) -> None:
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        st.header(record.feature_name)
    with col2:
        emoji = STATUS_EMOJI.get(record.status, "•")
        st.metric("Status", f"{emoji} {record.status}")
    with col3:
        created = record.created_at or "N/A"
        st.caption(f"Created: {created[:10] if created != 'N/A' else 'N/A'}")
        st.caption(f"Slug: `{record.slug}`")
    if record.rejection_reason:
        st.error(f"Rejected: {record.rejection_reason}")


def _render_prompt(st: Any, record: FeatureRecord) -> None:
    st.subheader("📝 Original Prompt")
    if record.original_prompt:
        st.info(record.original_prompt)
    else:
        st.info("No prompt found")


def _render_actions(
    st: Any,
    record: FeatureRecord,
    *,
    root: Path,
    engineer: Optional[SeniorEngineer],
    log_fn: Optional[AuditFn],
) -> None:
    st.subheader("🎯 Review Actions")
    pending = record.status not in {STATUS_APPROVED, STATUS_REJECTED}
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        if st.button(
            "✅ Approve Feature",
            use_container_width=True,
            disabled=not pending,
            key=f"approve-{record.slug}",
        ):
            approve_feature(record.slug, root=root, log_fn=log_fn)
            st.success(f"Status updated to: {STATUS_APPROVED}")
            st.rerun()
    with col2:
        reason = st.text_input(
            "Rejection reason (if needed):",
            key=f"reject-reason-{record.slug}",
            disabled=not pending,
        )
        if st.button(
            "❌ Reject Feature",
            use_container_width=True,
            disabled=not pending,
            key=f"reject-{record.slug}",
        ):
            try:
                reject_feature(record.slug, reason, root=root, log_fn=log_fn)
            except ValueError:
                st.warning("Please provide rejection reason")
            else:
                st.success(f"Status updated to: {STATUS_REJECTED}")
                st.rerun()
    with col3:
        if st.button(
            "🔄 Regenerate",
            use_container_width=True,
            key=f"regen-{record.slug}",
        ):
            with st.spinner("Regenerating feature…"):
                try:
                    regenerate_feature(record.slug, root=root, engineer=engineer)
                except Exception as exc:
                    st.error(str(exc))
                else:
                    st.session_state["selected_feature_slug"] = record.slug
                    st.success("Regenerated package")
                    st.rerun()
    with col4:
        if st.button(
            "📁 Open in File Explorer",
            use_container_width=True,
            key=f"open-{record.slug}",
        ):
            try:
                open_in_file_explorer(record.path)
            except OSError as exc:
                st.error(str(exc))
            else:
                st.caption(f"Path: {record.path}")
        else:
            st.caption(f"Path: {record.path}")


def _render_viewer(st: Any, record: FeatureRecord) -> None:
    tab1, tab2, tab3, tab4 = st.tabs(
        ["Implementation", "Tests", "Integration", "Manifest"]
    )
    with tab1:
        st.subheader("Generated Implementation Code")
        impl_files = record.files
        impl_dir = record.path / IMPLEMENTATION_DIR
        if not impl_files and impl_dir.is_dir():
            impl_files = _read_python_tree(impl_dir, record.path)
        if not impl_files:
            st.warning(
                "No implementation files found. Expected "
                f"`{record.slug}/{IMPLEMENTATION_DIR}/*.py`."
            )
        for name, content in impl_files.items():
            with st.expander(f"📄 {Path(name).name}", expanded=True):
                st.caption(name)
                st.code(content or "# empty file", language="python")
                meta, copy_col = st.columns(2)
                with meta:
                    st.caption(f"Lines: {line_count(content)}")
                with copy_col:
                    if st.button("📋 Copy", key=f"copy-impl-{record.slug}-{name}"):
                        st.session_state["copy_payload"] = content
                        st.caption("Use the copy icon on the code block above.")
    with tab2:
        st.subheader("Generated Tests")
        total = count_test_cases(record.tests)
        st.caption(f"Test cases: {total}")
        if not record.tests:
            st.warning("No test files found")
        for name, content in record.tests.items():
            with st.expander(f"🧪 {Path(name).name}", expanded=True):
                st.caption(name)
                st.code(content, language="python")
                st.caption(f"Test cases: {len(TEST_FN_RE.findall(content))}")
    with tab3:
        st.subheader("Integration Guide")
        if record.integration_md.strip():
            st.markdown(record.integration_md)
        else:
            st.warning("No integration guide found")
    with tab4:
        st.subheader("Feature Manifest")
        st.json(record.manifest or {"warning": "manifest.json missing"})


def _render_stats(st: Any, features: list[FeatureRecord]) -> None:
    st.subheader("📊 Feature Statistics")
    stats = feature_stats(features)
    if not features:
        st.info("No features generated yet")
        return
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Features", stats["total"])
    col2.metric("Approved", stats["approved"])
    col3.metric("Ready", stats["ready"])
    col4.metric("Rejected", stats["rejected"])


def _iter_feature_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in SKIP_DIR_NAMES:
            continue
        if child.name in RESERVED_DIRS:
            for nested in sorted(child.iterdir()):
                if nested.is_dir() and nested.name not in SKIP_DIR_NAMES and _looks_like_feature(nested):
                    found.append(nested)
            continue
        if _looks_like_feature(child):
            found.append(child)
    return found


def _looks_like_feature(path: Path) -> bool:
    if (path / MANIFEST_NAME).is_file() or (path / INTEGRATION_NAME).is_file():
        return True
    return any(path.rglob("*.py"))


def _collect_code(folder: Path) -> tuple[dict[str, str], dict[str, str]]:
    files: dict[str, str] = {}
    tests: dict[str, str] = {}
    impl_dir = folder / IMPLEMENTATION_DIR
    if impl_dir.is_dir():
        files.update(_read_python_tree(impl_dir, folder))
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        rel = path.relative_to(folder).as_posix()
        if path.name in {MANIFEST_NAME, INTEGRATION_NAME}:
            continue
        if path.suffix.lower() not in {".py", ".pyi"}:
            continue
        if rel.startswith(f"{IMPLEMENTATION_DIR.as_posix()}/"):
            continue
        text = path.read_text(encoding="utf-8")
        if (
            rel.startswith("tests/")
            or "/tests/" in rel
            or path.name.startswith("test_")
        ):
            tests[rel] = text
        elif rel not in files:
            files[rel] = text
    return files, tests


def _read_python_tree(start: Path, package_root: Path) -> dict[str, str]:
    collected: dict[str, str] = {}
    for path in sorted(start.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix.lower() not in {".py", ".pyi"}:
            continue
        rel = path.relative_to(package_root).as_posix()
        collected[rel] = path.read_text(encoding="utf-8")
    return collected


def _status_from_location(path: Path, root: Path, manifest_status: str) -> str:
    try:
        relative = path.resolve().relative_to(Path(root).resolve())
    except ValueError:
        return manifest_status or STATUS_NEEDS_REVIEW
    parts = relative.parts
    if parts and parts[0] == STATUS_APPROVED:
        return STATUS_APPROVED
    if parts and parts[0] == STATUS_REJECTED:
        return STATUS_REJECTED
    return manifest_status or STATUS_NEEDS_REVIEW


def _relocate_feature(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = src.resolve()
    dest = dest.resolve()
    if src == dest:
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(src), str(dest))
    return dest


def _write_review_status(
    folder: Path,
    *,
    status: str,
    rejection_reason: str,
) -> FeatureRecord:
    manifest = _read_json(folder / MANIFEST_NAME)
    manifest["status"] = status
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["rejection_reason"] = rejection_reason or None
    if "slug" not in manifest:
        manifest["slug"] = slug_feature_name(str(manifest.get("feature_name") or folder.name))
    if "feature_name" not in manifest:
        manifest["feature_name"] = folder.name
    (folder / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    root = folder.parent.parent if folder.parent.name in RESERVED_DIRS else folder.parent
    return load_feature(folder, root=root)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _audit(
    log_fn: Optional[AuditFn],
    *,
    action_type: str,
    record: FeatureRecord,
    status: str,
    extra: dict[str, Any],
) -> None:
    writer = log_fn or log_action
    payload = {
        "feature_name": record.feature_name,
        "slug": record.slug,
        "path": str(record.path),
        "original_prompt": record.original_prompt,
        **extra,
    }
    writer(
        AGENT_IDS["coding"],
        record.slug,
        action_type,
        payload,
        status=status,
    )


def _timestamp_sort_key(value: str) -> str:
    return value or ""


def _in_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


if _in_streamlit():
    render()
