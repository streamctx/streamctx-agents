"""
shared/approval_cli.py
Simple CLI to review pending_approval entries and apply approved
code patches. Approving here writes to the REAL source file — this
is the only place in the whole system where that happens, and it
only happens on explicit human command.
"""

import json
import re
import sys

from shared.audit_log import AUDIT_LOG_PATH, update_status
from shared.config import STATUS_APPROVED, STATUS_REJECTED


def list_pending():
    entries = []
    with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            if e["status"] == "pending_approval":
                entries.append(e)
    return entries


def extract_code_block(patch_text):
    """Pulls code out of a ```python ... ``` block."""
    match = re.search(r"```python\n(.*?)```", patch_text, re.DOTALL)
    return match.group(1) if match else patch_text


def review():
    pending = list_pending()
    if not pending:
        print("No pending approvals.")
        return

    for i, entry in enumerate(pending):
        print(f"\n{'='*60}")
        print(f"[{i}] id={entry['id']} agent={entry['agent']}")
        if entry["action_type"] == "code_patch":
            print(f"Bug: {entry['payload'].get('bug_description')}")
            print(f"Source file: {entry['payload'].get('source_file')}")
            print("\n--- Proposed patch ---")
            print(entry["payload"].get("proposed_patch", ""))
        print(f"{'='*60}")

        choice = input("Approve this? (y/n/skip): ").strip().lower()
        if choice == "y":
            if entry["action_type"] == "code_patch":
                source_file = entry["payload"]["source_file"]
                code = extract_code_block(entry["payload"]["proposed_patch"])
                with open(source_file, "w", encoding="utf-8") as f:
                    f.write(code)
                print(f"✅ Applied patch to {source_file}")
            update_status(entry["id"], STATUS_APPROVED, approved_by="sneh")
            print("✅ Marked approved in audit log.")
        elif choice == "n":
            update_status(entry["id"], STATUS_REJECTED, approved_by="sneh")
            print("❌ Marked rejected.")
        else:
            print("⏭️  Skipped.")


if __name__ == "__main__":
    review()