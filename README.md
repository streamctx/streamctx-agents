# streamctx-agents

Semi-autonomous agents that dogfood the [StreamCtx](https://github.com/streamctx/streamctx) SDK. Agents **propose** actions; humans **approve** before anything touches a real repo.

## Coding agent — full workflow

The coding agent is a six-stage pipeline that detects LLM failures from StreamCtx telemetry, diagnoses root cause, generates diff-based fixes, validates them in an isolated sandbox, and queues results for human approval.

```
sessions.db (failed calls)
        │
        ▼
┌───────────────────┐
│ 2. Detect/Diagnose│  AttributionEngine + CounterfactualReplay verify
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ 3. Confidence gate│  confidence < 0.6 → needs_human_review (stop)
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ 4. Fix + validate │  LLM diff → sandbox → python -m pytest (≤3 retries)
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ pending_approval  │  ready_for_approval | auto_fix_failed | needs_human_review
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ 5. Human review   │  approve → pattern memory | reject → store reason
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ 6. Notify + revert│  webhook on new rows | git revert by entry_id
└───────────────────┘
```

### Prerequisites

- Python 3.9+
- Git (for sandbox diff apply and revert)
- StreamCtx installed (`pip install streamctx`)
- OpenRouter API key for fix generation (`OPENROUTER_API_KEY` in `.env`)
- Optional: webhook URL for Slack/Discord/etc. (`CODING_AGENT_WEBHOOK_URL`)

### Run the pipeline

Polls `~/.streamctx/sessions.db` for failed calls and runs stages 2–4 automatically:

```powershell
$env:PYTHONPATH = "C:\path\to\streamctx-agents"
python -m agents.coding_agent.coding_agent run
python -m agents.coding_agent.coding_agent run --limit 5
python -m agents.coding_agent.coding_agent run --no-notify   # skip webhook
```

The run itself is tracked by StreamCtx (`get_tracker("coding-agent")`).

Example output:

```
[coding-agent] failures=2 blocked=1 ready=1 auto_fix_failed=0
  session=519 status=needs_human_review id=abcd1234-...
  session=520 status=ready_for_approval id=ef567890-...
```

### Human review (approve / reject)

Every fix is diff-based — never a full-file rewrite. Review the `pending_approval` row in `~/.streamctx/coding_agent.db` (or your review UI) before approving.

**Approve** — records the fix in pattern memory and optionally ties a git commit for rollback:

```powershell
python -m agents.coding_agent.coding_agent approve <entry_id>
python -m agents.coding_agent.coding_agent approve <entry_id> --commit abc123def
```

**Reject** — increments reject count and stores the reason so future diagnoses avoid the same approach:

```powershell
python -m agents.coding_agent.coding_agent reject <entry_id> --reason "Fix breaks edge case in token_utils"
```

### Revert an approved fix

If you committed an approved patch and need to roll it back:

```powershell
python -m agents.coding_agent.coding_agent revert <entry_id>
python -m agents.coding_agent.coding_agent revert <entry_id> --repo-root C:\path\to\repo
```

Requires `--commit` (or `record_applied_commit`) on approve so the entry knows which SHA to revert.

### Webhook notifications

Set a generic incoming webhook URL (Slack-compatible `{"text": "..."}` payload):

```powershell
$env:CODING_AGENT_WEBHOOK_URL = "https://hooks.slack.com/services/..."
```

A one-line summary is POSTed on every new `pending_approval` row:

```
[coding-agent] ready_for_approval | session=520 | DRIFT conf=0.85 | id=abcd1234
```

### Programmatic usage

```python
from agents.coding_agent.pipeline import CodingAgentPipeline

with CodingAgentPipeline(source_root="C:/path/to/repo") as pipeline:
    result = pipeline.run(limit=10)
    print(pipeline.summarize(result))

    # After human review:
    pipeline.approve(entry_id, applied_commit="abc123")
    pipeline.reject(other_entry_id, reason="Insufficient test coverage")

    # Rollback:
    pipeline.revert_fix(entry_id)
```

### Key modules

| Module | Stage | Role |
|--------|-------|------|
| `sandbox.py` | 1 | Isolated venv clone; `apply_diff` + `python -m pytest` |
| `diagnose.py` | 2 | Poll failures, attribute root cause, replay-verify |
| `confidence_gate.py` | 3 | Block low-confidence fixes (`MIN_CONFIDENCE_FOR_AUTO_FIX = 0.6`) |
| `fix_loop.py` | 4 | LLM fix generation + sandbox validation loop |
| `pattern_memory.py` | 5 | Learn from approve/reject history |
| `notifications.py` | 6 | Webhook on new pending rows |
| `revert_cli.py` | 6 | Scoped `git revert` by entry |
| `pipeline.py` | — | Top-level orchestrator wiring all stages |

### Storage

| Path | Contents |
|------|----------|
| `~/.streamctx/sessions.db` | StreamCtx sessions, calls, checkpoints (read-only for agent) |
| `~/.streamctx/coding_agent.db` | `pending_approval`, `fix_patterns` tables |

### Running tests

```powershell
$env:PYTHONPATH = "C:\path\to\streamctx-agents"
python -m pytest tests/ -v
```

### Safety rules

- Agents never commit or push to the real repo on their own.
- All patches are unified diffs validated in a disposable sandbox first.
- Low-confidence or unclear diagnoses go straight to `needs_human_review`.
- Approved fixes can be reverted via the recorded git commit SHA.
