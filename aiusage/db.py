"""SQLite storage. Tokens are the source of truth; dollars are derived at render
time so a pricing change re-prices all history instead of freezing bad numbers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

SCHEMA = """
PRAGMA journal_mode=WAL;

-- One row per assistant message. `requests` carries the count for sources
-- that ever aggregate (currently none do -- both sources are per-message).
CREATE TABLE IF NOT EXISTS usage_event (
  id                    TEXT PRIMARY KEY,   -- stable dedupe key, see sources/
  source                TEXT NOT NULL,      -- claude_code_local | codex_local
  provider              TEXT NOT NULL,      -- anthropic | openai | ollama
  ts                    TEXT NOT NULL,      -- ISO-8601 UTC
  day                   TEXT NOT NULL,      -- YYYY-MM-DD, local time
  model                 TEXT NOT NULL,
  input_tokens          INTEGER NOT NULL DEFAULT 0,  -- uncached input only
  output_tokens         INTEGER NOT NULL DEFAULT 0,
  cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens      INTEGER NOT NULL DEFAULT 0,
  requests              INTEGER NOT NULL DEFAULT 1,
  project               TEXT,
  git_branch            TEXT,
  session_id            TEXT,
  service_tier          TEXT,
  ingested_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_usage_day      ON usage_event(day);
CREATE INDEX IF NOT EXISTS ix_usage_provider ON usage_event(provider, day);
CREATE INDEX IF NOT EXISTS ix_usage_model    ON usage_event(model, day);

-- Coarse daily totals recovered from Claude Code's own stats cache. Only a
-- single token number per model per day -- no input/output/cache split -- so it
-- cannot feed cost math. Kept separate to fill gaps in the timeline honestly.
CREATE TABLE IF NOT EXISTS coarse_daily_tokens (
  id           TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  provider     TEXT NOT NULL,
  day          TEXT NOT NULL,
  model        TEXT NOT NULL,
  total_tokens INTEGER NOT NULL,
  ingested_at  TEXT NOT NULL
);

-- Incremental-ingest bookkeeping: file read offsets, API cursors, last-run marks.
CREATE TABLE IF NOT EXISTS ingest_state (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Append-only log of collector runs, so a silently failing cron is visible.
CREATE TABLE IF NOT EXISTS run_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  source      TEXT NOT NULL,
  status      TEXT NOT NULL,   -- ok | skipped | error
  detail      TEXT,
  triggered_by TEXT NOT NULL DEFAULT 'scheduled'  -- scheduled | serve
);
"""


# Three processes legitimately write this database: the launchd agent on its
# schedule, a `./collect.py` you run by hand, and a `--serve` instance
# re-collecting per request. WAL allows a single writer, so the losers of a
# race wait on the lock. sqlite3's 5s default is short next to how long a
# writer can legitimately hold it -- a first run, or any full re-ingest after
# the archive grows, stays in one transaction for seconds. Losing that race
# raises "database is locked", which run_sources logs as an error and skips,
# dropping that run's usage until the next one.
BUSY_TIMEOUT_S = 30.0


# Bump when the schema changes, and add the matching step to MIGRATIONS.
# `CREATE TABLE IF NOT EXISTS` builds a correct database from nothing but is a
# no-op against one that already exists, so without this a new column would
# silently never reach any installation that has already run -- and the failure
# would surface much later, as an OperationalError mid-collect.
SCHEMA_VERSION = 2


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """ALTER TABLE ... ADD COLUMN, but only if it is actually missing. The same
    column is created two ways -- by SCHEMA on a new database and by a
    migration on an old one -- so the step has to be safe either way."""
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# version -> the step taking the database from (version - 1) to it. Callables
# rather than raw SQL so a step can inspect the database first and stay
# idempotent; each runs at most once, in order, inside one transaction.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: lambda conn: _add_column(
        conn, "run_log", "triggered_by", "TEXT NOT NULL DEFAULT 'scheduled'"),
}


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any migrations this database has not seen. Returns the number
    applied. Safe to call on every connect: it is a no-op once current."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return 0

    applied = 0
    for version in range(current + 1, SCHEMA_VERSION + 1):
        step = MIGRATIONS.get(version)
        if step is not None:
            step(conn)
        applied += 1
    # PRAGMA does not take a bound parameter, and SCHEMA_VERSION is ours.
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return applied


def connect(path: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S,
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM ingest_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str, now: str) -> None:
    conn.execute(
        "INSERT INTO ingest_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now),
    )


USAGE_COLUMNS = (
    "id source provider ts day model input_tokens output_tokens cache_write_5m_tokens "
    "cache_write_1h_tokens cache_read_tokens reasoning_tokens requests project git_branch "
    "session_id service_tier ingested_at"
).split()


def upsert_usage(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """INSERT OR REPLACE: rows are immutable, but re-reading a truncated or
    rewritten file is safe -- the dedupe key makes repeats a no-op.

    One ordering dependency worth knowing about, because nothing enforces it.
    Claude Code writes one record per content block of a streamed message, all
    sharing (message id, requestId), and the earlier ones carry a *partial*
    output_tokens while the last carries the complete count -- roughly 5% of
    the author's records. "Last write wins" therefore lands on the right number
    only because `rows` is in file order and executemany preserves it. Feed
    these rows in out of order and output tokens silently under-count. See
    tests/test_claude_code_local.py for the case that pins this.
    """
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in USAGE_COLUMNS)
    sql = f"INSERT OR REPLACE INTO usage_event ({', '.join(USAGE_COLUMNS)}) VALUES ({placeholders})"
    conn.executemany(sql, [[r.get(c) for c in USAGE_COLUMNS] for r in rows])
    return len(rows)


def upsert_coarse(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = "id source provider day model total_tokens ingested_at".split()
    sql = (
        f"INSERT OR REPLACE INTO coarse_daily_tokens ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})"
    )
    conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])
    return len(rows)


def log_run(conn: sqlite3.Connection, source: str, status: str, detail: str,
            started_at: str, finished_at: str,
            triggered_by: str = "scheduled") -> None:
    conn.execute(
        "INSERT INTO run_log (started_at, finished_at, source, status, detail, "
        "triggered_by) VALUES (?,?,?,?,?,?)",
        (started_at, finished_at, source, status, detail, triggered_by),
    )
