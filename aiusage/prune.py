"""Archive pruning.

The archive only ever grows -- that is the point, and it is what makes
re-parsing and re-pricing history possible. But it is also full session
transcripts, so it gets large: a couple of GB a year at steady use, Codex
rollout files dominating.

Deleting old archived JSONL is safe *once it has been ingested*: the events
already live in `usage_event`, and cost is derived at render time from token
columns, so nothing about the dashboard changes. What it costs you is the
ability to re-parse those sessions if a future version learns to extract
something new from them.

So this module never deletes a file it cannot prove was read to the end. A
file is prunable only when all three hold:

  * it is older than the cutoff,
  * `ingest_state` has an offset for it, and
  * that offset equals the file's current size -- meaning the last run
    consumed every byte, with no partial trailing line outstanding.

Anything else is reported with the reason it was kept, rather than removed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import db

# state-key prefix -> the archive directory those keys are relative to
STATE_PREFIXES = {"claude_code_local": "cc_offset", "codex_local": "codex_offset"}


def _offset_of(raw: str | None) -> int | None:
    """Read the byte offset out of an ingest_state value, in either the current
    JSON form or the bare-integer one older databases used."""
    if not raw:
        return None
    try:
        state = json.loads(raw)
        if isinstance(state, dict):
            return int(state.get("offset", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class Candidate:
    """One archived file and what we decided about it."""

    __slots__ = ("path", "state_key", "size", "age_days", "reason")

    def __init__(self, path: Path, state_key: str, size: int, age_days: float,
                 reason: str | None) -> None:
        self.path = path
        self.state_key = state_key
        self.size = size
        self.age_days = age_days
        self.reason = reason  # None means prunable

    @property
    def prunable(self) -> bool:
        return self.reason is None


def survey(conn: sqlite3.Connection, archives: dict[str, Path],
           older_than_days: float, now: float | None = None) -> list[Candidate]:
    """Classify every archived file. `archives` maps source name -> directory."""
    now = time.time() if now is None else now
    cutoff_s = older_than_days * 86400.0
    out: list[Candidate] = []

    for source, directory in archives.items():
        prefix = STATE_PREFIXES[source]
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.jsonl")):
            try:
                stat = path.stat()
            except OSError:
                continue
            age_days = (now - stat.st_mtime) / 86400.0
            key = f"{prefix}:{path.relative_to(directory)}"
            offset = _offset_of(db.get_state(conn, key))

            if now - stat.st_mtime < cutoff_s:
                reason = "newer than the cutoff"
            elif offset is None:
                reason = "never ingested"
            elif offset != stat.st_size:
                # A partial trailing line, or a run that stopped early. Those
                # bytes have not reached usage_event yet.
                reason = f"only {offset:,} of {stat.st_size:,} bytes ingested"
            else:
                reason = None
            out.append(Candidate(path, key, stat.st_size, age_days, reason))

    return out


def prune(conn: sqlite3.Connection, candidates: list[Candidate]) -> tuple[int, int]:
    """Delete the prunable candidates and forget their ingest_state. Returns
    (files removed, bytes reclaimed).

    The state row goes with the file so a later re-archive of the same session
    is read from scratch rather than resumed against an offset whose file no
    longer exists. Re-reading is harmless -- every row's primary key is
    deterministic, so a replay is a no-op.
    """
    removed = reclaimed = 0
    directories = set()

    for candidate in candidates:
        if not candidate.prunable:
            continue
        try:
            candidate.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue  # permissions, or a mount that went away -- leave the state
        conn.execute("DELETE FROM ingest_state WHERE key = ?", (candidate.state_key,))
        directories.add(candidate.path.parent)
        removed += 1
        reclaimed += candidate.size

    conn.commit()

    # Codex nests sessions under YYYY/MM/DD, so pruning a day empties its
    # directory. Tidy those up, deepest first, never touching a non-empty one.
    for directory in sorted(directories, key=lambda d: -len(d.parts)):
        while True:
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent

    return (removed, reclaimed)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"
