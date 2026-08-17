"""Dogfood: full pipeline on a fresh string_utils bug (not broken_math)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from streamctx.storage import SessionStorage

from agents.coding_agent.fix_generator import FixGenerator
from agents.coding_agent.models import FixProposal, FixRequest
from agents.coding_agent.pipeline import CodingAgentPipeline
from agents.coding_agent.pending_approval import (
    STATUS_AUTO_FIX_FAILED,
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_READY_FOR_APPROVAL,
)

FIXTURE_ROOT = Path(__file__).resolve().parent / "tests" / "fixtures" / "string_utils_project"

FIX_DIFF = """\
diff --git a/reverse_string.py b/reverse_string.py
--- a/reverse_string.py
+++ b/reverse_string.py
@@ -3,4 +3,4 @@
 
 def reverse_string(text: str) -> str:
     \"\"\"Return the reversed input string.\"\"\"
-    return text
+    return text[::-1]
"""

REGRESSION_TEST = """\
from reverse_string import reverse_string


def test_reverse_string_regression():
    assert reverse_string("pipeline") == "enilepip"
"""


def seed_string_utils_failure(storage: SessionStorage) -> dict:
    session_id = storage.start_session()
    error_message = (
        "AssertionError: assert 'hello' == 'olleh'\n"
        '  File "reverse_string.py", line 6, in reverse_string\n'
        '  File "tests/test_reverse_string.py", line 5, in test_reverse_string_basic'
    )
    messages_by_step = [
        [{"role": "user", "content": "Investigate reverse_string bug in string utils"}],
        [
            {"role": "user", "content": "Investigate reverse_string bug in string utils"},
            {"role": "assistant", "content": "reverse_string returns input unchanged"},
        ],
        [
            {"role": "user", "content": "Investigate reverse_string bug in string utils"},
            {"role": "assistant", "content": "reverse_string returns input unchanged"},
            {"role": "user", "content": "pytest failed: expected 'olleh' got 'hello'"},
        ],
    ]

    for step, messages in enumerate(messages_by_step):
        storage.record_call(
            session_id=session_id,
            provider="openrouter",
            model="test-model",
            input_tokens=120 + (step * 280),
            output_tokens=30,
            cost=0.0,
            reused_tokens=15,
            waste_category="drift" if step == 2 else None,
            messages=messages,
            failed=step == 2,
            error_message=error_message if step == 2 else None,
        )
        storage.save_checkpoint(
            session_id=session_id,
            step_number=step,
            messages=messages,
        )

    return {
        "session_id": session_id,
        "error_message": error_message,
        "bug": "reverse_string() returns input unchanged instead of reversed text",
    }


def mock_llm(request: FixRequest) -> FixProposal:
    return FixProposal(
        diff=FIX_DIFF,
        regression_test=REGRESSION_TEST,
        regression_test_path=f"tests/test_regression_session_{request.diagnosis.session_id}.py",
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dogfood_string_utils_"))
    streamctx_home = tmp / "streamctx"
    streamctx_home.mkdir()
    agent_db = streamctx_home / "coding_agent.db"
    sessions_db = streamctx_home / "sessions.db"
    os.environ["STREAMCTX_HOME"] = str(streamctx_home)

    print("=" * 72)
    print("STAGE 0: Seed fresh bug — reverse_string() does not reverse")
    print("=" * 72)
    storage = SessionStorage(db_path=sessions_db)
    seeded = seed_string_utils_failure(storage)
    storage.close()
    print(f"  Fixture       : {FIXTURE_ROOT}")
    print(f"  Bug           : {seeded['bug']}")
    print(f"  session_id    : {seeded['session_id']}")
    print(f"  error         : {seeded['error_message'].splitlines()[0]}")
    print()

    print("=" * 72)
    print("STAGES 1-6: Run CodingAgentPipeline.run()")
    print("=" * 72)

    with CodingAgentPipeline(
        source_root=FIXTURE_ROOT,
        db_path=agent_db,
        fix_generator=FixGenerator(llm_fn=mock_llm),
        enable_notifications=False,
    ) as pipeline:
        result = pipeline.run()

        print()
        print("-" * 72)
        print(f"SUMMARY: {pipeline.summarize(result)}")
        print("-" * 72)

        for index, item in enumerate(result.items, start=1):
            d = item.gate.diagnosis
            entry = item.fix.pending_entry or item.gate.pending_entry

            print()
            print(f"[Failure #{index}] STAGE 2 — DETECT + DIAGNOSE")
            print(f"  session_id       : {d.session_id}")
            print(f"  failed_call_id   : {d.failed_call_id}")
            print(f"  error_type       : {d.error_type}")
            print(f"  relevant_file    : {d.relevant_file}")
            print(f"  root_cause       : {d.root_cause}")
            print(f"  confidence       : {d.confidence:.4f}")
            print(f"  replay_verified  : {d.replay_verified}")
            print(f"  reason           : {d.reason}")

            print()
            print(f"[Failure #{index}] STAGE 3 — CONFIDENCE GATE")
            print(f"  proceed_to_fix   : {item.gate.proceed_to_fix}")
            if item.gate.pending_entry and not item.gate.proceed_to_fix:
                print(f"  blocked status   : {item.gate.pending_entry.status}")

            print()
            print(f"[Failure #{index}] STAGE 4 — FIX + SANDBOX TEST")
            print(f"  fix_success      : {item.fix.success}")
            print(f"  attempts         : {item.fix.attempts}")
            print(f"  skipped          : {item.fix.skipped}")

            if entry:
                print()
                print(f"[Failure #{index}] STAGE 4/6 — pending_approval ROW")
                print(f"  entry_id         : {entry.entry_id}")
                print(f"  status           : {entry.status}")
                print(f"  retries_used     : {entry.retries_used}")
                print(f"  diff preview     :")
                for line in (entry.diff or "").splitlines()[:6]:
                    print(f"    {line}")
                if entry.regression_test:
                    print(f"  regression_test  : {entry.regression_test.splitlines()[0]}...")
                if entry.test_results:
                    tr = json.loads(entry.test_results)
                    print(f"  test passed      : {tr.get('passed')}")
                    print(f"  coverage_delta   : {tr.get('coverage_delta')}")
                    output = (tr.get("output") or "").strip()
                    if output:
                        print(f"  pytest output    :")
                        for line in output.splitlines()[-12:]:
                            print(f"    {line}")

        ready = pipeline.approval_store.list_by_status(STATUS_READY_FOR_APPROVAL)
        blocked = pipeline.approval_store.list_by_status(STATUS_NEEDS_HUMAN_REVIEW)
        failed = pipeline.approval_store.list_by_status(STATUS_AUTO_FIX_FAILED)

        print()
        print("=" * 72)
        print("FINAL OUTCOME")
        print("=" * 72)
        checks = [
            ("Detect failure", result.failures_detected >= 1),
            ("Diagnose root cause", any(item.gate.diagnosis.replay_verified for item in result.items)),
            ("Pass confidence gate", any(item.gate.proceed_to_fix for item in result.items)),
            ("Fix + sandbox tests pass", any(item.fix.success for item in result.items)),
            ("pending_approval created", len(ready) + len(blocked) + len(failed) >= 1),
            ("ready_for_approval", len(ready) >= 1),
        ]
        for label, ok in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        print()
        print(
            f"  ready_for_approval={len(ready)} "
            f"needs_human_review={len(blocked)} "
            f"auto_fix_failed={len(failed)}"
        )

    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
