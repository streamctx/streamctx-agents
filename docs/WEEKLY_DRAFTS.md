# Weekly content drafts

Automated writer that turns the last 7 days of shipped work into **Draft** /
**Needs Review** cards on the Marketing Agent Content Pipeline board.

It does **not** talk to the marketing agent, change guardrails, or post
anywhere. It only feeds the same SQLite store the dashboard already reads
(`content_items`: title, channel, stage, flag, notes, created_at).

Free-tier OpenRouter models are convenient and cheap, but they are often
weaker than a paid model. Treat every generated card as a **starting point
for review**, not final copy.

## How it works

1. Collect activity from both repos (git log, merge commits, `CHANGELOG.md`
   diffs) for the last 7 days:
   - StreamCtx SDK (`STREAMCTX_PRODUCT_ROOT`, default sibling `../streamctx`
     or CI checkout `.deps/streamctx`)
   - this agents repo (`STREAMCTX_AGENTS_ROOT`)
2. Read existing cards from the pipeline store so the prompt can avoid
   duplicates.
3. `GET https://openrouter.ai/api/v1/models`, keep models whose
   `pricing.prompt` and `pricing.completion` are `"0"` (or numeric 0), and
   confirm the configured model is still free. If it is not, fall back to the
   first `:free` model in that list.
4. For each dashboard channel, skip when there is nothing honest to say
   (no ship this week, not a launch week for Product Hunt, already drafted
   this week, and so on). Remaining channels get one
   `POST https://openrouter.ai/api/v1/chat/completions` call.
5. Write cards as `Draft` or `Needs Review` only — never `Ready to Post` or
   `Published`. Legal / pricing / support copy is flagged.
6. Append a run summary to `logs/weekly_draft_runs.log`.

Rate limits on the free tier are roughly 20 requests/minute and 50–200
requests/day. The script retries with backoff on HTTP 429. If one channel
still hits the limit, that channel is skipped and the rest of the run
continues.

## Run locally

```powershell
$env:PYTHONPATH = "C:\path\to\streamctx-agents"
$env:OPENROUTER_API_KEY = "sk-or-..."
python scripts/weekly_content_draft.py
```

Local runs write to `~/.streamctx/content_pipeline.db` (same file as
`streamlit run content_pipeline_app.py` / the dashboard pipeline tab), unless
you override the path.

Useful flags:

```powershell
python scripts/weekly_content_draft.py --dry-run
python scripts/weekly_content_draft.py --force
python scripts/weekly_content_draft.py --since-days 7 --db data/content_pipeline.db
```

## Change the model

Set `OPENROUTER_MODEL` to any OpenRouter model id. Prefer a `:free` suffix
while staying on the free tier — the lineup rotates, so this is a preference,
not a pin.

- Local: environment variable or `.env`
- GitHub Actions: repo **variable** `OPENROUTER_MODEL` (optional). If unset
  or no longer free, the job falls back to the first live free model and
  records that in the run log.

The default preference in code is a `:free`-suffixed id. It is always checked
against the live free list before the run.

## Disable it

- Local / any environment: `WEEKLY_DRAFTS_ENABLED=false`
- GitHub Actions: repo variable `WEEKLY_DRAFTS_ENABLED=false` (the workflow
  job is skipped). You can also disable the workflow in the Actions tab.
- CI still supports **Run workflow** (`workflow_dispatch`) while the cron is
  enabled.

## GitHub Actions + the data store

The dashboard reads a **local SQLite file**, not a hosted DB. The weekly
workflow therefore:

1. Checks out this repo and `streamctx/streamctx`
2. Runs the script with `CONTENT_PIPELINE_DB=data/content_pipeline.db`
3. Commits `data/content_pipeline.db` and `logs/weekly_draft_runs.log`

Add a repository secret named `OPENROUTER_API_KEY`. Never put the key in
code or workflow YAML.

To review CI-produced cards on the local board, point the store at the
committed file:

```powershell
$env:CONTENT_PIPELINE_DB = "C:\path\to\streamctx-agents\data\content_pipeline.db"
streamlit run content_pipeline_app.py
```

Or copy `data/content_pipeline.db` over `~/.streamctx/content_pipeline.db`.
