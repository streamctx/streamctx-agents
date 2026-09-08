# StreamCtx agents

Semi-autonomous agents that dogfood the StreamCtx SDK. Agents propose
actions; humans approve before anything touches a real repo.

## Run the pipeline

Polls `~/.streamctx/sessions.db` for failed calls and runs diagnose,
confidence gate, and the sandbox fix loop:

```
python -m agents.coding_agent.coding_agent run
```

Review `pending_approval` rows before approving a patch.

## Sandbox

The coding agent applies unified diffs in an isolated venv and runs
`python -m pytest`. `wrap()` tracks calls per-client instance.
