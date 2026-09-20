# local-ai-usage-tracker

A permanent, local history of Claude Code and ChatGPT (Codex) usage, with a
dashboard of theoretical cost. Stdlib Python + SQLite, no dependencies. Runs
from launchd once or twice a day and writes a single self-contained
`dashboard.html`; an optional local server (`./collect.py --serve`) lets that
page refresh itself live instead.

```
launchd (2x/day)
  └─ collect.py
       ├─ ~/.claude/projects/*.jsonl   ─┐   (Claude Pro)
       └─ ~/.codex/sessions/*.jsonl    ─┴─> data/usage.db (SQLite)
                                                 │
                                                 v
                                          dashboard.html
```

Both sources read local files only — no API keys, no network calls, nothing
to configure. Just point it at `~/.claude` and `~/.codex` and run it.

## Example

![Dashboard showing daily cost by provider and daily tokens by model](docs/dashboard-example.png)

Real output from an actual `dashboard.html` — nothing here is staged. Every
number, chart, and note (including the "some tokens are unpriced" callout) is
generated straight from `usage_event`; there's no separate demo mode. The
screenshot shows the top of the all-time view; below it sit the by-month,
by-model and top-projects tables.

### Getting around it

- **Tiles** — lifetime totals, then this month / this week / today, so the
  number you usually want is the one you land on. Each card has Claude on the
  bottom left and ChatGPT on the bottom right, including in month drill-downs.
  Cache percentages use each provider's own tokens; active days can overlap.
- **Charts** — daily cost stacked by provider, daily tokens stacked by model.
  Hover for a per-day breakdown. Models past the top 8 fold into "Other".
- **Drill into a month** — click any row of the **By month** table. You get
  that month's own charts, a per-model table (where "Other" is itemised), and
  projects ranked within that month. The month lives in the URL as
  `#month=2026-07`, so it's bookmarkable and survives a refresh; "All time"
  in the breadcrumb goes back.
- **Costs marked `*`** used a model with no rate on file. Those tokens are
  counted but left out of the dollar figure, never priced at zero. Add the
  rate to `aiusage/pricing.py` and the whole history re-prices on the next run.

## Why this exists

`ccusage` reads `~/.claude/projects/*.jsonl`, and **Claude Code deletes those on
a 30-day rolling window** (`cleanupPeriodDays`). That is why a `ccusage monthly`
run in late July shows an implausibly small June — most of June was already
gone. This tool archives those files before they age out, so history accumulates
instead of scrolling off the back.

## Setup

Needs Python 3.9 or newer and nothing else. The `/usr/bin/python3` that macOS
ships is new enough, and is what the launchd agent runs, so there is nothing to
install — though on a Mac that has never seen Xcode, invoking it once may
prompt for the Command Line Tools.

```sh
git clone https://github.com/SnailEngineering/local-ai-usage-tracker.git
cd local-ai-usage-tracker
./setup.sh                # .env, shell aliases, optional launchd schedule
exec zsh                  # pick up the aliases setup.sh just added
aiusage                   # collect + build dashboard (alias for ./collect.py)
aiusage-dashboard         # open dashboard.html
```

`setup.sh` is idempotent — re-run it anytime. It will:

- create `data/` (the launchd agent writes its logs there and cannot create it itself)
- copy `.env.example` to `.env` if you don't have one (only needed to override default paths)
- add `aiusage`, `aiusage-status`, and `aiusage-dashboard` aliases to `~/.zshrc`
- offer to install the launchd agent that runs the collector twice a day

The aliases are zsh-only. On bash, either add the equivalents to `~/.bashrc`
yourself or just call `./collect.py` directly.

Prefer to do it by hand instead? `./collect.py` collects and builds the
dashboard; `./collect.py --status` prints what's in the database and the last
few runs.

### Configuration

Everything is optional and lives in `.env` (or the real environment, which wins):

| Variable | Default | What it moves |
|---|---|---|
| `AIU_DB` | `./data/usage.db` | the SQLite database |
| `AIU_ARCHIVE` | `./data/archive` | mirrored Claude Code JSONL |
| `AIU_ARCHIVE_CODEX` | `./data/archive-codex` | mirrored Codex rollouts (the larger half) |
| `AIU_CLAUDE_DIR` | `~/.claude` | where Claude Code keeps its sessions |
| `AIU_CODEX_DIR` | `~/.codex` | where Codex keeps its sessions |
| `AIU_DASHBOARD` | `./dashboard.html` | the rendered page |

Relative values are resolved against the repository, not your shell's working
directory, so they mean the same thing wherever you run the collector from —
`aiusage` from another directory reads your database rather than quietly
starting an empty one beside you. Use an absolute path to put data elsewhere.

### Live updates while the tab is open

`dashboard.html` always has a Refresh button and auto-refreshes on an
interval (60s by default). What that does depends on how you opened it:

- **Plain file** (double-click, or `aiusage-dashboard`): there is no server to
  ask, so a refresh is a full page reload of whatever `./collect.py` last wrote
  to disk — useful to pick up a launchd run without reopening the tab. Note
  that the 60s timer reloads the tab whether or not anything changed; if you
  keep the dashboard open while reading it, build it with `./collect.py
  --interval 0` to leave only the Refresh button.
- **`./collect.py --serve`**: a small local `ThreadingHTTPServer`
  (`aiusage/server.py`) at `http://127.0.0.1:8787/`. Every refresh (auto or
  click) hits `GET /api/data`, which re-renders the charts in place — no full
  page reload, no separate terminal running `./collect.py` yourself. It
  re-collects at most once every 20s, so several open tabs share one
  collection instead of each triggering their own. If a collection fails, the
  page says so and keeps the figures it already has, rather than reloading.

Start it in the foreground; add `--open` to launch it in your browser:

```sh
./collect.py --serve --open
```

Or in the background, e.g. to leave running while you work:

```sh
mkdir -p data && ./collect.py --serve > data/serve.log 2>&1 &
```

`--port` (default `8787`) and `--interval` (seconds, default `60`; `0` disables
auto-refresh) override the defaults. `--interval` is baked into the page when
it is generated, so changing it means rebuilding `dashboard.html`.

The server binds `127.0.0.1` only, so nothing outside your Mac can reach it.
It does not authenticate requests, though, so treat it like any other localhost
dev server: run it while you're using it, not permanently.

To stop it: `Ctrl+C` if it's in the foreground; otherwise find and kill the
process by port —

```sh
lsof -ti:8787 | xargs kill
```

(swap `8787` for whatever `--port` you used). It holds no state beyond the
SQLite connection, so killing it any time is safe.

### Schedule it

`setup.sh` handles this (see above) by rendering
`local.ai-usage-tracker.plist.template` with your actual repo path and loading
it. To do it manually instead:

```sh
mkdir -p data                             # launchd won't create its own log dir
sed "s#__REPO_DIR__#$(pwd)#g" local.ai-usage-tracker.plist.template \
  > ~/Library/LaunchAgents/local.ai-usage-tracker.plist
launchctl load ~/Library/LaunchAgents/local.ai-usage-tracker.plist
launchctl start local.ai-usage-tracker    # run once now to verify
tail -f data/collect.log
```

Runs at 09:00 and 21:00. launchd re-runs a missed `StartCalendarInterval` when
the Mac wakes, so a closed lid at 09:00 does not lose that run; `RunAtLoad`
additionally collects once at login, which covers a machine that is only awake
outside those hours.

On newer macOS, `launchctl bootstrap gui/$UID <plist>` and
`launchctl kickstart -k gui/$UID/local.ai-usage-tracker` are the current
spellings of load/start. The deprecated `load`/`start` above still work.

### Also do this once

```jsonc
// ~/.claude/settings.json
"cleanupPeriodDays": 3650
```

Set that and Claude Code stops deleting its JSONL in the first place. The
archive is the belt to that suspenders: it keeps working if the setting is ever
reset by a reinstall — and it's what covers you for the days before you set it.

## Sources

| Source | Credential | Covers | History |
|---|---|---|---|
| `claude_code_local` | none | Claude Code, incl. Pro/Max subscription | forward from first run (+ the current 30-day window) |
| `codex_local` | none | Codex CLI & Desktop, incl. ChatGPT Plus | forward from first run (+ whatever Codex still holds) |

Both sources are local-only, by design — no admin key, no organization, no
network call. That also means they're the whole answer, not a fallback: they
record per-turn token counts with the working directory and git branch
attached, which is more granular than either vendor's usage API exposes even
to an org with an admin key.

What is **not** recoverable: usage from the chat web apps themselves
(claude.ai, chatgpt.com). Those run server-side and write nothing locally. Only
the coding agents — Claude Code and Codex — keep a local ledger.

Every source writes the same `usage_event` schema, so the dashboard and all
queries are provider-agnostic. Sources are independently skippable — a
problem in one never blocks the other.

## Cost accounting

**Tokens are stored; dollars are computed at render time.** A pricing change
re-prices all history instead of freezing bad numbers into rows. Rates live in
`aiusage/pricing.py`, one list-price table per provider:

- cache write, 5-minute TTL → 1.25× input rate
- cache write, 1-hour TTL → 2.00× input rate
- cache read → 0.10× input rate

A model with no rate on file is counted in tokens and **excluded from cost**,
never silently priced at zero; the dashboard says so.

> **These dollars are notional.** They are token counts × list API prices —
> the right measure of *what you consumed*, but not an invoice. ChatGPT Plus
> and Claude Pro/Max don't bill per token at all. Don't reconcile these
> numbers against a card statement.

## Tests

Stdlib `unittest`, no dependencies, no runner to install:

```sh
python3 -m unittest discover -s tests
```

They cover the parts where a silent regression would corrupt the numbers
rather than crash: incremental re-ingest and byte-offset integrity, archive
rewrites (shrinking, growing and same-size), malformed records, dated pricing
windows, per-day project costing, schema migration, archive pruning, and the
dashboard's unpriced-model and payload-escaping handling.

## Verification

Both local sources reconcile exactly against an independent scan of their own
raw files, and Claude Code additionally reconciles with `ccusage monthly`:

| | input | output | cache write | cache read | total | cost |
|---|---|---|---|---|---|---|
| ccusage | 19,938 | 86,084 | 415,146 | 7,860,779 | 8,381,947 | $10.33 |
| this | 19,938 | 86,084 | 415,146 | 7,860,779 | 8,381,947 | $10.33 |

Codex: 614 turns / 40,600,510 tokens in the database against 40,600,510 counted
by a direct scan of `~/.codex/sessions`.

## Design notes

**Idempotency.** Every row carries a deterministic primary key — message id +
request id for Claude Code, session id + sequence for Codex — and writes are
`INSERT OR REPLACE`. Running the collector twice in a row, or resuming a
truncated file, changes nothing.

**Codex token math.** `info.total_token_usage` is cumulative per session while
`info.last_token_usage` is the per-turn delta; the deltas sum exactly to the
cumulative figure, so the deltas are what get stored (they carry a date).
`token_count` events name no model, so the model is carried forward from the
preceding `turn_context`. A few compaction turns report a total with the whole
breakdown zeroed — those are attributed to input rather than dropped, which is
what makes the two totals match to the token.

**Incremental reads.** Each archived JSONL records a byte offset, so a run only
parses what was appended since last time. Changed files also have their entire
previously ingested prefix hashed in bounded chunks to detect rewrites anywhere
in the file. Upgrading from older offsets or 4 KB fingerprints replays each
archive once. Codex also saves the current model and working directory with
the offset, so resumed reads hash old bytes without reparsing their JSON.
A partial trailing line (a session
still being written) is left unread until it is complete. Full first run:
~8,300 messages in 0.3s.

**`coarse_daily_tokens`.** Claude Code's own `stats-cache.json` keeps a per-day,
per-model token count that outlives the JSONL window — but it is one scalar with
no input/output/cache split, so it cannot be priced. It is stored in a separate
table and shown as a footnote, never mixed into cost totals.

## Layout

```
collect.py                              entry point
setup.sh                                one-time setup: .env, shell aliases, launchd
local.ai-usage-tracker.plist.template   launchd agent template (setup.sh fills in the path)
aiusage/
  db.py                                 schema, upserts, run log
  pricing.py                            rate table, cost computation
  dashboard.py                          SQL -> JSON -> static HTML
  prune.py                              archive retention for --prune
  server.py                             optional localhost server for --serve
  sources/
    claude_code_local.py                archive + incremental JSONL parse
    codex_local.py                      same, for Codex rollout files
tests/                                  stdlib unittest, see Tests above
data/
  usage.db                              SQLite
  archive/                              mirrored Claude Code JSONL, the durable copy
  archive-codex/                        mirrored Codex rollout files
dashboard.html                          regenerated each run
```

## Privacy

Everything this tool produces is local and personal: `data/` (the SQLite DB
and the archived JSONL transcripts) and `dashboard.html` (which embeds your
project names, token counts, and dollar figures inline). Both are gitignored.
If you fork or clone this repo, nothing you generate by running the collector
gets committed — only the code does. Don't remove those `.gitignore` entries
without knowing what you're exposing.

Worth knowing: the archive holds **full session transcripts**, not just token
counts — everything you and the agent said. That is the price of being able to
re-price and re-parse history later, but it means `data/archive*/` deserves the
same care as the source directories it mirrors.

## Disk

The archive only ever grows — that's the point. Expect roughly a couple of GB
per year at steady daily use (Codex rollout files dominate; single sessions can
reach 100MB+). `du -sh data/` tells you where you are.

When you want the space back, `--prune` reclaims it safely:

```sh
./collect.py --prune 180          # list what's older than 180 days, delete nothing
./collect.py --prune 180 --yes    # actually delete it
```

Listing is the default, and there is no undo — these are full session
transcripts, not just token counts. A file is only ever removed when it is older than the cutoff, `ingest_state` has an offset equal to its
current size, and the complete ingested fingerprint still matches. Collection
and deletion share a process lock per archive; deletion rechecks the file and
ingest state under that lock before removing it. Legacy offsets without a full
fingerprint must be upgraded by running the collector before they can be pruned. Anything else is listed with the reason it
was kept, so nothing is dropped on the assumption it was ingested.

What you lose is the ability to re-parse those sessions later. What you keep is
every number: the events are already in `usage.db`, and cost is derived at
render time from the token columns, so no figure on the dashboard changes.

## Adding Ollama later

The schema already has a `provider` column and `is_free()` handles zero-cost
models. Ollama keeps no token accounting of its own — its logs record no
inference requests at all — so it needs a small reverse proxy on 11435 that
forwards to 11434 and records `prompt_eval_count` / `eval_count` from each
response. That captures every client rather than one tool's storage format.
Drop it in as `sources/ollama_proxy.py`; nothing else changes.
