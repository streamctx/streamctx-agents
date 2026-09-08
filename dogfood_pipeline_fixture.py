"""One-shot dogfood: run full pipeline against sandbox_project fixture."""

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
    STATUS_READY_FOR_APPROVAL,
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_AUTO_FIX_FAILED,
)

FIXTURE_ROOT = Path(__file__).resolve().parent / "tests" / "fixtures" / "sandbox_project"

FIX_DIFF = """\
diff --git a/broken_math.py b/broken_math.py
--- a/broken_math.py
+++ b/broken_math.py
@@ -4,3 +4,3 @@
 
 def add(a: int, b: int) -> int:
-    return a - b
+    return a + b
"""

REGRESSION_TEST = """\
from broken_math import add


def test_add_regression():
    assert add(2, 3) == 5
"""


def seed_broken_math_failure(storage: SessionStorage) -> dict:
    session_id = storage.start_session()
    error_message = (
        "AssertionError: assert -1 == 5\n"
        '  File "broken_math.py", line 5, in add\n'
        '  File "tests/test_broken_math.py", line 5, in test_add'
    )
    messages_by_step = [
        [{"role": "user", "content": "Investigate broken_math.add bug"}],
        [
            {"role": "user", "content": "Investigate broken_math.add bug"},
            {"role": "assistant", "content": "add() returns a - b instead of a + b"},
        ],
        [
            {"role": "user", "content": "Investigate broken_math.add bug"},
            {"role": "assistant", "content": "add() returns a - b instead of a + b"},
            {"role": "user", "content": "pytest failed on test_add"},
        ],
    ]

    for step, messages in enumerate(messages_by_step):
        storage.record_call(
            session_id=session_id,
            provider="openrouter",
            model="test-model",
            input_tokens=100 + (step * 250),
            output_tokens=25,
            cost=0.0,
            reused_tokens=10,
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

    return {"session_id": session_id, "error_message": error_message}


def mock_llm(request: FixRequest) -> FixProposal:
    return FixProposal(
        diff=FIX_DIFF,
        regression_test=REGRESSION_TEST,
        regression_test_path=f"tests/test_regression_session_{request.diagnosis.session_id}.py",
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dogfood_pipeline_"))
    streamctx_home = tmp / "streamctx"
    streamctx_home.mkdir()
    agent_db = streamctx_home / "coding_agent.db"
    sessions_db = streamctx_home / "sessions.db"

    os.environ["STREAMCTX_HOME"] = str(streamctx_home)

    storage = SessionStorage(db_path=sessions_db)
    seeded = seed_broken_math_failure(storage)
    storage.close()

    print("=" * 70)
    print("DOGFOOD: Full coding-agent pipeline on sandbox_project fixture")
    print("=" * 70)
    print(f"Fixture root : {FIXTURE_ROOT}")
    print(f"StreamCtx DB : {sessions_db}")
    print(f"Agent DB     : {agent_db}")
    print(f"Seeded session_id: {seeded['session_id']}")
    print(f"Seeded error     : {seeded['error_message'].splitlines()[0]}")
    print()

    with CodingAgentPipeline(
        source_root=FIXTURE_ROOT,
        db_path=agent_db,
        fix_generator=FixGenerator(llm_fn=mock_llm),
        enable_notifications=False,
    ) as pipeline:
        result = pipeline.run()

        print("PIPELINE SUMMARY")
        print("-" * 70)
        print(pipeline.summarize(result))
        print()

        for index, item in enumerate(result.items, start=1):
            d = item.gate.diagnosis
            entry = item.fix.pending_entry or item.gate.pending_entry

            print(f"Failure #{index}")
            print(f"  Detected       : session={d.session_id} call={d.failed_call_id}")
            print(f"  Diagnosed      : root_cause={d.root_cause} confidence={d.confidence:.4f}")
            print(f"  Replay verified: {d.replay_verified}")
            print(f"  Gate           : proceed_to_fix={item.gate.proceed_to_fix}")
            print(f"  Fix loop       : success={item.fix.success} attempts={item.fix.attempts}")

            if entry:
                print(f"  pending_approval:")
                print(f"    entry_id   : {entry.entry_id}")
                print(f"    status     : {entry.status}")
                print(f"    diff lines : {len((entry.diff or '').splitlines())}")
                print(f"    regression : {bool(entry.regression_test)}")
                if entry.test_results:
                    try:
                        tr = json.loads(entry.test_results)
                        print(f"    tests passed: {tr.get('passed')}")
                    except json.JSONDecodeError:
                        pass
            print()

        ready = pipeline.approval_store.list_by_status(STATUS_READY_FOR_APPROVAL)
        blocked = pipeline.approval_store.list_by_status(STATUS_NEEDS_HUMAN_REVIEW)
        failed = pipeline.approval_store.list_by_status(STATUS_AUTO_FIX_FAILED)

        print("OUTCOME")
        print("-" * 70)
        checks = {
            "Detected failure in sessions.db": result.failures_detected >= 1,
            "Diagnosed with clear root cause": any(
                item.gate.diagnosis.root_cause in {"DRIFT", "COMPRESSION", "RECENCY"}
                for item in result.items
            ),
            "Created pending_approval entry": any(
                (item.fix.pending_entry or item.gate.pending_entry) is not None
                for item in result.items
            ),
            "Fix validated (ready_for_approval)": len(ready) >= 1,
        }
        for label, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

        print()
        print(f"ready_for_approval={len(ready)} needs_human_review={len(blocked)} auto_fix_failed={len(failed)}")

    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
