"""SQLite storage. Tokens are the source of truth; dollars are derived at render
time so a pricing change re-prices all history instead of freezing bad numbers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
  detail      TEXT
);
"""


def connect(path: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
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
    rewritten file is safe -- the dedupe key makes repeats a no-op."""
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
            started_at: str, finished_at: str) -> None:
    conn.execute(
        "INSERT INTO run_log (started_at, finished_at, source, status, detail) VALUES (?,?,?,?,?)",
        (started_at, finished_at, source, status, detail),
    )
