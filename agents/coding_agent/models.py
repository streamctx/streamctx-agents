"""Shared dataclasses for the coding agent pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

RootCauseType = str  # DRIFT | COMPRESSION | RECENCY | UNCLEAR


@dataclass(frozen=True)
class FailedCallRecord:
    call_id: int
    session_id: int
    error_message: Optional[str]
    timestamp: str
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class FixPatternMatch:
    signature_hash: str
    root_cause_type: str
    fix_diff_template: str
    success_count: int
    reject_count: int


@dataclass
class DiagnosisResult:
    session_id: int
    failed_call_id: int
    root_cause: RootCauseType
    confidence: float
    replay_verified: bool
    reason: str
    error_type: str
    relevant_file: str
    signature_hash: str
    matched_pattern: Optional[FixPatternMatch] = None
    skip_to_stage4: bool = False
    signal_breakdown: dict[str, float] = field(default_factory=dict)
