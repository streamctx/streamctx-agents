"""Top-level orchestrator wiring all six coding-agent stages."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from streamctx.storage import SessionStorage, get_storage

from agents.coding_agent.confidence_gate import ConfidenceGate
from agents.coding_agent.diagnose import FailureDiagnostician
from agents.coding_agent.fix_generator import FixGenerator
from agents.coding_agent.fix_loop import FixValidationLoop
from agents.coding_agent.fix_patterns import FixPatternStore
from agents.coding_agent.models import (
    FixLoopResult,
    FixPatternMatch,
    GateResult,
    PipelineItemResult,
    PipelineRunResult,
)
from agents.coding_agent.pattern_memory import PatternMemory
from agents.coding_agent.pending_approval import PendingApprovalStore
from agents.coding_agent.revert_cli import revert
from shared.config import BASE_DIR


class CodingAgentPipeline:
    """
    End-to-end autonomous bug-fix pipeline:

    1. Detect failures in ``sessions.db``
    2. Diagnose root cause + replay-verify
    3. Confidence gate → ``needs_human_review`` or proceed
    4. Generate fix + validate in sandbox (with retries)
    5. Pattern memory on human approve/reject
    6. Webhook notification on every new ``pending_approval`` row
    """

    def __init__(
        self,
        *,
        storage: Optional[SessionStorage] = None,
        source_root: Optional[Path | str] = None,
        db_path: Optional[Path | str] = None,
        fix_generator: Optional[FixGenerator] = None,
        enable_notifications: bool = True,
    ) -> None:
        self.storage = storage or get_storage()
        self.source_root = Path(source_root or BASE_DIR)
        self.approval_store = PendingApprovalStore(
            db_path=db_path,
            enable_default_notifier=enable_notifications,
        )
        self.pattern_store = FixPatternStore(db_path=self.approval_store.db_path)
        self.pattern_memory = PatternMemory(
            pattern_store=self.pattern_store,
            approval_store=self.approval_store,
        )
        self.diagnostician = FailureDiagnostician(
            storage=self.storage,
            pattern_store=self.pattern_store,
        )
        self.confidence_gate = ConfidenceGate(
            diagnostician=self.diagnostician,
            approval_store=self.approval_store,
        )
        self.fix_loop = FixValidationLoop(
            fix_generator=fix_generator,
            approval_store=self.approval_store,
            storage=self.storage,
            source_root=self.source_root,
            pattern_memory=self.pattern_memory,
        )

    def close(self) -> None:
        self.approval_store.close()
        self.pattern_store.close()

    def __enter__(self) -> CodingAgentPipeline:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def run(self, *, limit: Optional[int] = None) -> PipelineRunResult:
        """Execute stages 2–4 for each failed call found in StreamCtx storage."""
        gate_results = self.confidence_gate.run(limit=limit)
        items: list[PipelineItemResult] = []

        for gate_result in gate_results:
            fix_result = self.fix_loop.run(gate_result)
            items.append(PipelineItemResult(gate=gate_result, fix=fix_result))

        return PipelineRunResult(items=items)

    def approve(
        self,
        entry_id: str,
        *,
        applied_commit: Optional[str] = None,
    ) -> FixPatternMatch:
        """Stage 5: record approval and optionally tie a git commit for revert."""
        return self.pattern_memory.approve(
            entry_id,
            applied_commit=applied_commit,
        )

    def reject(self, entry_id: str, reason: str) -> FixPatternMatch:
        """Stage 5: record rejection reason and update pattern trust scores."""
        return self.pattern_memory.reject(entry_id, reason)

    def revert_fix(
        self,
        entry_id: str,
        *,
        repo_root: Optional[Path | str] = None,
    ) -> str:
        """Stage 6: revert the git commit tied to an approved fix."""
        return revert(
            entry_id,
            repo_root=repo_root or self.source_root,
            approval_store=self.approval_store,
        )

    def summarize(self, result: PipelineRunResult) -> str:
        return (
            f"failures={result.failures_detected} "
            f"blocked={result.blocked_for_review} "
            f"ready={result.fixes_ready} "
            f"auto_fix_failed={result.fixes_failed}"
        )
