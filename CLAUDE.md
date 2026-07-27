# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

No dependencies, no build step, no test suite (stdlib Python only — that's a deliberate
choice, see below).

```sh
./collect.py                        # run every configured source, rebuild dashboard.html
./collect.py --status                # print what's in the DB and the last few runs
./collect.py --only claude_code      # run a single source (claude_code|codex|anthropic|openai)
./collect.py --no-dashboard          # collect without rebuilding dashboard.html
./collect.py --open                  # ...and open dashboard.html afterward
./setup.sh                           # one-time: .env, ~/.zshrc aliases, optional launchd install
```

`.env` (copied from `.env.example` by `setup.sh`) holds `ANTHROPIC_ADMIN_KEY` /
`OPENAI_ADMIN_KEY` and path overrides (`AIU_DB`, `AIU_ARCHIVE`, `AIU_CLAUDE_DIR`,
`AIU_CODEX_DIR`, `AIU_DASHBOARD`). Real environment variables always win over `.env`.

## Architecture

### Pipeline

`collect.py` runs four independent sources, each writing into one shared SQLite
table, then `aiusage/dashboard.py` queries that table and renders a single static
`dashboard.html`. Every stage is wrapped so one source's exception (`aiusage/db.py`
rollback + `run_log` entry) never blocks the others — a missing OpenAI key must
never stop the Claude Code archive from running.

Two source families, in `aiusage/sources/`:

- **Local ingest** (`claude_code_local.py`, `codex_local.py`) — no credentials.
  Reads Claude Code's `~/.claude/projects/*.jsonl` and Codex's
  `~/.codex/sessions/**/*.jsonl`. Both follow the same two-step pattern: `archive()`
  mirrors new/changed session files into `data/archive*/` before the source tool can
  prune them (Claude Code deletes JSONL after `cleanupPeriodDays`, default 30 —
  that's the whole reason this project exists), then `ingest()` parses the *archive*
  copy incrementally, storing a per-file byte offset in `ingest_state` so a run only
  reads what was appended since last time. A partial trailing line (session still
  being written) is deliberately left for the next run.
- **Admin API ingest** (`anthropic_admin.py`, `openai_admin.py`) — needs an org-level
  admin key (individual accounts can't create one). Re-reads a trailing 7-day window
  every run (30-day backfill on first run) because provider-side buckets can be
  restated after the fact; cheap because writes are idempotent.

### Idempotency

Every row's primary key is deterministic (message id + request id for Claude Code
local, session id + sequence for Codex, bucket + model + tier for the admin APIs),
and all writes go through `db.upsert_usage`/`upsert_cost`/`upsert_coarse`, which are
`INSERT OR REPLACE`. Re-running the collector, re-reading overlapping windows, or
resuming a truncated file is always safe — never accumulates duplicates.

### Schema (`aiusage/db.py`)

- `usage_event` — one row per billable unit (per-message for local sources,
  per-day-bucket for admin APIs). Token columns only; **no dollar amounts** except
  `reported_cost_usd`, which is set only when a provider hands us a real billed
  number (Anthropic's Claude Code endpoint) rather than something we'd compute.
- `provider_cost` — real invoice dollars that don't decompose by model (OpenAI's
  Costs endpoint, Anthropic's cost_report). Kept apart from `usage_event` so a
  computed estimate is never confused with a real bill.
- `coarse_daily_tokens` — recovered from Claude Code's `stats-cache.json`: one
  scalar per model per day, no input/output/cache split, so it's shown as a
  footnote and never enters cost math.
- `ingest_state` — offsets/cursors keyed by `source:relative_path`, the incremental-read bookkeeping.
- `run_log` — append-only, so a silently failing cron job is visible via `--status`.

### Pricing (`aiusage/pricing.py`)

Cost is computed at **render time**, not ingest time — a rate-table change
re-prices all history instead of freezing bad numbers into rows. `rates_for()`
returns `None` for a model with no known price; callers (`dashboard.py`) must
surface that as "unpriced" rather than inventing a zero. `normalize_model()` strips
the date suffix Claude Code sometimes appends (`claude-sonnet-4-5-20250929` →
`claude-sonnet-4-5`); `DATED_OVERRIDES` handles promotional/introductory pricing
windows. OpenAI has no rate table here at all — its real Costs figures are stored
verbatim in `provider_cost` instead of being re-derived.

### Dashboard (`aiusage/dashboard.py`)

Aggregates `usage_event` in SQL, prices it, then serializes one JSON payload
(`DATA = {...}`) directly into `dashboard.html` — no server, no build step, no
network at view time. Model series beyond `MAX_MODEL_SERIES` (8) fold into "Other";
provider→color slot is fixed (`PROVIDER_SLOT`) so adding a third provider never
repaints the first two.

### Adding a new source

Follow the shape in `aiusage/sources/`: a `run(conn, ...) -> dict` entry point
returning a stats dict for the run log, rows built with the exact `usage_event`
column set (`db.USAGE_COLUMNS`), and a stable, collision-proof `id`. Wire it into
`collect.py`'s `stage(...)` calls next to the existing four.
