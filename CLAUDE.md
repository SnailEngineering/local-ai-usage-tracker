# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

No dependencies and no build step — stdlib Python only, a deliberate choice
(see below). Tests are stdlib `unittest`, so there is no runner to install:

```sh
python3 -m unittest discover -s tests   # the whole suite
```

```sh
./collect.py                         # run every configured source, rebuild dashboard.html
./collect.py --status                # print what's in the DB and the last few runs
./collect.py --only claude_code      # run a single source (claude_code|codex)
./collect.py --no-dashboard          # collect without rebuilding dashboard.html
./collect.py --open                  # ...and open dashboard.html afterward
./collect.py --serve                 # serve on localhost, re-collecting on dashboard refresh
./collect.py --prune 180 [--yes]     # list (or delete) fully-ingested archive older than N days
./setup.sh                           # one-time: .env, ~/.zshrc aliases, optional launchd install
```

`.env` (copied from `.env.example` by `setup.sh`) holds only path overrides
(`AIU_DB`, `AIU_ARCHIVE`, `AIU_ARCHIVE_CODEX`, `AIU_CLAUDE_DIR`, `AIU_CODEX_DIR`,
`AIU_DASHBOARD`) — no credentials are needed. Real environment variables always
win over `.env`. Relative values resolve against the repo root, not the caller's
cwd (`collect._expand`), so the installed aliases behave the same from anywhere.

## Architecture

### Pipeline

`collect.py` runs two independent local-ingest sources, each writing into one
shared SQLite table, then `aiusage/dashboard.py` queries that table and
renders a single static `dashboard.html`. Every stage is wrapped so one
source's exception (`aiusage/db.py` rollback + `run_log` entry) never blocks
the other. `aiusage/locking.py` holds a persistent `.aiusage.lock` in each
archive using `flock`. `run_sources()` locks each source from before archiving
through its database commit or rollback; pruning uses the same locks. Source
`run()`/`archive()`/`ingest()` helpers rely on this caller-owned lock.

Both live in `aiusage/sources/` and need no credentials — `claude_code_local.py`
reads Claude Code's `~/.claude/projects/*.jsonl`, `codex_local.py` reads Codex's
`~/.codex/sessions/**/*.jsonl`. Both follow the same two-step pattern:
`archive()` mirrors new/changed session files into `data/archive*/` before the
source tool can prune them (Claude Code deletes JSONL after `cleanupPeriodDays`,
default 30 — that's the whole reason this project exists), then `ingest()`
parses the *archive* copy incrementally, storing a per-file byte offset in
`ingest_state` so a run only reads what was appended since last time. A
partial trailing line (session still being written) is deliberately left for
the next run.

### Idempotency

Every row's primary key is deterministic (message id + request id for Claude
Code local, session id + sequence for Codex), and all writes go through
`db.upsert_usage`/`upsert_coarse`, which are `INSERT OR REPLACE`. Re-running
the collector or resuming a truncated file is always safe — never accumulates
duplicates.

### Schema (`aiusage/db.py`)

- `usage_event` — one row per assistant message. Token columns only, no dollar
  amounts at all; cost is always derived at render time from `aiusage/pricing.py`.
  A project is identified by `project_path` (the session's full cwd); `project`
  is just its basename, kept for display and for rows that predate the path.
  `dashboard.project_labels()` shows the basename and widens only colliding
  ones to the shortest distinguishing trailing path (`acme/backend`).
- `coarse_daily_tokens` — recovered from Claude Code's `stats-cache.json`: one
  scalar per model per day, no input/output/cache split, so it's shown as a
  footnote and never enters cost math.
- `ingest_state` — incremental-read bookkeeping, one JSON value per key.
  - `cc_offset:<rel>` / `codex_offset:<rel>` (relative to the archive), both
    with `offset`, `mtime_ns`, and `prefix_hash`/`prefix_len`: a SHA-256 of
    every byte through `offset` (`sources/fingerprint.py`), which is how a
    growing rewrite is told apart from an append. Legacy offsets and head-only
    hashes are replayed once to establish the full fingerprint.
  - Offsets are **byte** positions, so both ingesters read the files in binary —
    decoding first would let CRLF or an undecodable byte desync them permanently.
  - Codex also saves (`codex_local._State`) `seq`, `context` (model and working
    directory, so a resume needn't rescan; older state without it scans once,
    and a rewrite discards it), `cumulative` (the last `total_token_usage`) and
    `accounting_version`. A repeated cumulative state is not new usage; a lower
    one, including a zero-usage reset event, starts a new epoch. An older
    accounting version rebuilds that session in one transaction; pruned
    sessions are left untouched.
  - `pruned_archive:<absolute path>` — the fingerprint of a pruned file, so an
    unchanged original is not archived again (see Retention).
- `run_log` — append-only, so a silently failing cron job is visible via
  `--status`. `triggered_by` separates `scheduled` runs from `serve` ones;
  both `--status` and the dashboard show scheduled runs plus anything that
  failed, because a `--serve` session would otherwise bury them.

Schema changes go through `PRAGMA user_version`: bump `db.SCHEMA_VERSION` and add
the step to `db.MIGRATIONS` (a callable, so it can inspect the DB and stay
idempotent — the same column arrives via `SCHEMA` on a new database and via
`ALTER` on an old one). `CREATE TABLE IF NOT EXISTS` alone cannot migrate.
A step may also make the next collection re-derive data: v3 adds `project_path`
and deletes the `cc_offset:`/`codex_offset:` state rows, so the archive replays
and rewrites each row in place (ids are deterministic) — the only place the
path still exists.

### Pricing (`aiusage/pricing.py`)

Cost is computed at **render time**, not ingest time — a rate-table change
re-prices all history instead of freezing bad numbers into rows. `rates_for()`
returns `None` for a model with no known price; callers (`dashboard.py`) must
surface that as "unpriced" rather than inventing a zero. `normalize_model()` strips
the date suffix Claude Code sometimes appends (`claude-sonnet-4-5-20250929` →
`claude-sonnet-4-5`); `DATED_OVERRIDES` handles promotional/introductory pricing
windows. `PROVIDER_RATES` holds one list-price table per provider (`ANTHROPIC_RATES`,
`OPENAI_RATES`) so both sources get priced by list price — every dollar figure
in this tool is a computed estimate, since neither subscription is billed per token.

### Dashboard (`aiusage/dashboard.py`)

Editable source assets live in `aiusage/templates/dashboard.{html,css,js}`.
`dashboard.TEMPLATE` assembles them at module load, preserving one self-contained
output file. A CSP hashes the final inline script and blocks external assets
and event-handler scripts. Escape imported values at every HTML sink as well.

Aggregates `usage_event` in SQL, prices it, then serializes one JSON payload
(`build_payload()`) directly into `dashboard.html` as `DATA = {...}` — no build
step, no network needed to view it. The page is built in three separable
pieces: `render_payload()` splices a payload into the template,
`render()` is that over a fresh `build_payload()`, and `write_page()` puts a
string on disk. `build()` is just `write_page(render(...))`. The split exists
so `--serve` can answer a request without a disk round-trip, and so its
`/api/data` can write the file through from the payload it already has.

`write_page()` writes a `tempfile.mkstemp` file in the target directory and
`os.replace`s it into position. Several writers target that path — the
launchd schedule, a manual `./collect.py`, and every `--serve` request that
writes through — and a plain write truncates first, so a reader (a browser on
`file://`, the next collector) could see a half-written page. The temp name
comes from `mkstemp`, not the pid: `--serve` writes through from request
threads, which share one.

Model series beyond `MAX_MODEL_SERIES` (8) fold into "Other"; provider→color
slot is fixed (`PROVIDER_SLOT`) so adding a third provider never repaints the
first two.

Clicking a row in the "By month" table drills into that month
(`#month=YYYY-MM` in the hash — bookmarkable, survives auto-refresh). The
drill-down is pure client-side re-render: `build_payload()` ships
`month_models` and `month_projects` rollups alongside the all-time ones, and
the charts just filter the existing daily series, so a month view needs no
server and no second query.

The page ships Refresh and Auto-refresh controls. HTTP refreshes keep existing
figures and show a persistent status message on server, network, or timeout
errors; requests cannot overlap. Only file:// refreshes reload the page (automatic
ones at most every 10 minutes, since the file changes only on collection), saving
view state in sessionStorage when available. Pausing is remembered locally.
Refresh preserves focus, disclosures and table scrolling; navigation focuses the
new heading. Resize/theme changes redraw only charts. Charts support keyboard
and pointer inspection and an exact daily table, populated only when opened.
Text colors have separate contrast-tested tokens from chart fill colors.

`python3 -m unittest discover -s tests` also invokes the dependency-free Node
behavior suite when Node is installed (otherwise it reports a skip). Run it
alone with `node --test tests/dashboard.test.cjs`. Its DOM test double validates
behavior but is not a substitute for a real browser or screen-reader audit.

### Live serving (`aiusage/server.py`, `collect.py --serve`)

Optional, off by default. A stdlib `ThreadingHTTPServer` answers `GET /` with
`dashboard.render()` — the page built from the DB right then, in memory — and,
on every `GET /api/data`, re-runs `collect.run_sources()` and returns a fresh
`dashboard.build_payload()` as JSON. Rendering per request is the point: the
server used to send the `dashboard.html` written at startup, so a `--serve`
left running for days handed every new tab that snapshot until its first poll
replaced it. `GET /` deliberately does *not* collect (the poll owns
that) and renders in memory, so a page load cannot fail on an unwritable
directory.

`dashboard.html` on disk is then written through — after the response is
flushed, best-effort, exceptions logged and swallowed — from both `GET /` and
`GET /api/data`, so stopping the server does not leave the file back at
whatever the startup build wrote. The poll matters more than the page load
here: a tab left open for hours never reloads, so `/` alone would leave the
file as stale as the last navigation. Two rules keep this from becoming the
bug it replaced — it runs *after* the response (a page that rendered fine must
not turn into a 500 because a directory is read-only) and it never raises (by
then the response is on the wire, so do_GET's 500 handler would append a
second status line and headers to a page the browser is mid-parse of).

Requests run one per thread, so the shared `sqlite3.Connection` is opened with
`check_same_thread=False` and every access — collection and the
payload query alike — is serialized behind one `threading.Lock` in
`server.py`. This is the only path in the codebase where the DB connection is
touched from more than one thread.

Collection is throttled to at most once per `MIN_COLLECT_INTERVAL_S` (20s)
inside that lock, so N open tabs share one collection rather than each forcing
their own. `do_GET` answers 403 unless `Host` is `127.0.0.1:<port>` or
`localhost:<port>` (DNS rebinding). Every branch of `do_GET` must answer. Failures return a 500 that the page
reports in place; network failures also preserve the last usable figures.

### Retention (`aiusage/prune.py`, `collect.py --prune`)

The archive never shrinks on its own. `--prune DAYS` lists archived files older
than the cutoff; `--yes` deletes them. A file is only prunable when
`ingest_state` holds an offset for it *equal to its current size* — proof the
last run consumed every byte — and its complete prefix hash and mtime must
still match. Legacy offsets require collection before pruning. Deletion locks
the affected archives and rechecks the survey's file identity, size, timestamps,
ingest state, and fingerprint before unlinking. Directory cleanup stops at the
archive root and leaves the persistent lock file in place. The read-offset row is replaced with a `pruned_archive:<absolute archive path>`
fingerprint in `ingest_state`. `sources/archiving.py` leaves unchanged originals
pruned and re-archives changed ones. Mirroring uses a private temporary file and
atomic replacement. `storage.py` creates private files without changing the
process umask or permissions on existing shared parent directories. The
archive trees and the repo's own `data/` are the exceptions: they belong to this
tool, so `run_sources()` (`protect_tree`) and `main()` tighten them each run.

### Adding a new source

Follow the shape in `aiusage/sources/`: a `run(conn, ...) -> dict` entry point
returning a stats dict for the run log, rows built with the exact `usage_event`
column set (`db.USAGE_COLUMNS`), and a stable, collision-proof `id`. Wire it into
`collect.py`'s `run_sources()`, in the `stage(...)` calls next to the existing two.
