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
    last_rejection_reason: Optional[str] = None


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


@dataclass(frozen=True)
class PendingApprovalEntry:
    entry_id: str
    session_id: str
    root_cause: RootCauseType
    confidence: float
    matched_pattern_id: Optional[str]
    diff: Optional[str]
    regression_test: Optional[str]
    test_results: Optional[str]
    retries_used: int
    status: str
    created_at: str
    applied_commit: Optional[str] = None


@dataclass(frozen=True)
class GateResult:
    diagnosis: DiagnosisResult
    pending_entry: Optional[PendingApprovalEntry]
    proceed_to_fix: bool


@dataclass(frozen=True)
class FixProposal:
    diff: str
    regression_test: str
    regression_test_path: str


@dataclass
class FixRequest:
    diagnosis: DiagnosisResult
    error_message: str
    source_contents: dict[str, str]
    pattern_template: Optional[str] = None
    pattern_regression_test: Optional[str] = None
    attempt: int = 0
    previous_failure_output: Optional[str] = None
    regression_test_path: str = "tests/test_regression_auto.py"
    rejection_hint: Optional[str] = None


@dataclass(frozen=True)
class FixLoopResult:
    skipped: bool
    diagnosis: DiagnosisResult
    pending_entry: Optional[PendingApprovalEntry]
    success: bool
    attempts: int = 0
