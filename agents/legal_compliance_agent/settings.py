"""Roots to scan and GitHub repo. Credentials come from env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agents.competitor_agent.settings import github_token
from agents.marketing_agent.sources import default_product_root
from shared.config import BASE_DIR

DEFAULT_GITHUB_REPO = "streamctx/streamctx-agents"
POLICY_FILENAMES = (
    "TERMS.md",
    "PRIVACY.md",
    "COMPLIANCE_VERIFICATION.md",
    "DEPLOYMENT.md",
)


@dataclass(frozen=True)
class LegalConfig:
    user_agent: str = "streamctx-legal-compliance-agent/0.1"
    github_repo: str = DEFAULT_GITHUB_REPO
    github_per_page: int = 20
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    agents_root: str = BASE_DIR
    product_root: str = ""


def github_repo() -> str:
    for name in ("LEGAL_GITHUB_REPO", "TECHSUPPORT_GITHUB_REPO", "GITHUB_REPOSITORY"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip().lstrip("/")
    return DEFAULT_GITHUB_REPO


def default_config(
    *,
    agents_root: Optional[Path | str] = None,
    product_root: Optional[Path | str] = None,
    github_repo_name: Optional[str] = None,
) -> LegalConfig:
    product = (
        Path(product_root)
        if product_root is not None
        else default_product_root()
    )
    return LegalConfig(
        github_repo=github_repo_name if github_repo_name is not None else github_repo(),
        agents_root=str(Path(agents_root) if agents_root is not None else Path(BASE_DIR)),
        product_root=str(product),
    )


__all__ = [
    "DEFAULT_GITHUB_REPO",
    "POLICY_FILENAMES",
    "LegalConfig",
    "default_config",
    "github_repo",
    "github_token",
]
