"""Claude Code local ingest.

Claude Code writes one JSONL per session under ~/.claude/projects/ and deletes
them on a rolling window (`cleanupPeriodDays`, default 30). We archive each file
before it can age out, then parse incrementally from the archive -- so history
survives even if the retention setting is ever reset by a reinstall.

Dedupe key is the message id plus request id, which makes re-parsing a file
harmless and lets the collector run as often as you like.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import db
from .fingerprint import HEAD_BYTES, head_hash

SOURCE = "claude_code_local"
PROVIDER = "anthropic"

# Placeholder model names Claude Code writes for local/no-op turns.
SKIP_MODELS = {"<synthetic>", "", None}


def _local_day(ts_iso: str) -> str:
    """Bucket by local calendar day -- 'what did I spend on Tuesday' means the
    Tuesday you lived, not the UTC one."""
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%Y-%m-%d")


def archive(projects_dir: Path, archive_dir: Path) -> tuple[int, int]:
    """Mirror new/changed JSONL files into the archive. Returns (copied, total)."""
    copied = total = 0
    if not projects_dir.is_dir():
        return (0, 0)

    for src in projects_dir.rglob("*.jsonl"):
        total += 1
        rel = src.relative_to(projects_dir)
        dst = archive_dir / rel
        try:
            s = src.stat()
        except OSError:
            continue

        if dst.exists():
            d = dst.stat()
            # `copy2` preserves mtime, so size and mtime together identify an
            # unchanged append-only file while still catching a same-size rewrite.
            if d.st_size == s.st_size and d.st_mtime_ns == s.st_mtime_ns:
                continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    return (copied, total)


def _usage_row(rec: dict, now: str) -> dict | None:
    msg = rec.get("message") or {}
    usage = msg.get("usage") or {}
    model = msg.get("model")

    if model in SKIP_MODELS or not usage:
        return None

    ts = rec.get("timestamp")
    if not ts:
        return None

    # Dedupe: message id is unique per API response; request id disambiguates
    # retries that reuse it. Fall back to the record uuid.
    key = f"{msg.get('id') or ''}:{rec.get('requestId') or ''}".strip(":")
    if not key:
        key = rec.get("uuid") or ""
    if not key:
        return None

    creation = usage.get("cache_creation") or {}
    write_5m = creation.get("ephemeral_5m_input_tokens")
    write_1h = creation.get("ephemeral_1h_input_tokens")

    if write_5m is None and write_1h is None:
        # Older records only carry the flat total. Attribute it to the 5m tier,
        # which is the default TTL, and accept the small pricing error.
        write_5m = usage.get("cache_creation_input_tokens", 0) or 0
        write_1h = 0

    cwd = rec.get("cwd") or ""

    return {
        "id": f"cc:{key}",
        "source": SOURCE,
        "provider": PROVIDER,
        "ts": ts,
        "day": _local_day(ts),
        "model": model,
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cache_write_5m_tokens": write_5m or 0,
        "cache_write_1h_tokens": write_1h or 0,
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
        "reasoning_tokens": 0,
        "requests": 1,
        "project": os.path.basename(cwd) or None,
        "project_path": cwd or None,
        "git_branch": rec.get("gitBranch") or None,
        "session_id": rec.get("sessionId") or rec.get("session_id"),
        "service_tier": usage.get("service_tier"),
        "ingested_at": now,
    }


def _read_state(conn: sqlite3.Connection, key: str) -> tuple[int, int | None, str | None, int]:
    """Read the incremental offset, archived-file mtime, and head fingerprint
    (hash, bytes covered).

    Older databases stored a plain integer offset. Keep accepting that format
    so an upgrade does not force a full re-ingest.
    """
    raw = db.get_state(conn, key)
    if not raw:
        return (0, None, None, 0)
    try:
        state = json.loads(raw)
        if isinstance(state, dict):
            mtime = state.get("mtime_ns")
            head = state.get("head")
            return (int(state.get("offset", 0)),
                    int(mtime) if mtime is not None else None,
                    str(head) if head else None, int(state.get("head_len", 0)))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        return (int(raw), None, None, 0)
    except ValueError:
        return (0, None, None, 0)


def ingest(conn: sqlite3.Connection, archive_dir: Path, now: str) -> dict:
    """Parse archived JSONL from the last read offset of each file."""
    stats = {"files": 0, "files_read": 0, "events": 0, "bad_lines": 0, "bad_records": 0,
             "rewritten": 0}
    if not archive_dir.is_dir():
        return stats

    batch: list[dict] = []

    for path in sorted(archive_dir.rglob("*.jsonl")):
        stats["files"] += 1
        rel = str(path.relative_to(archive_dir))
        state_key = f"cc_offset:{rel}"
        offset, saved_mtime, saved_head, saved_head_len = _read_state(conn, state_key)
        stat = path.stat()
        size = stat.st_size

        if size == offset and (saved_mtime is None or saved_mtime == stat.st_mtime_ns):
            continue
        rewritten = size < offset or (size == offset and saved_mtime is not None)
        if not rewritten and saved_head is not None:
            # Same number of bytes the last run hashed, so a plain append
            # (which leaves the prefix alone) never looks like a rewrite.
            rewritten = head_hash(path, saved_head_len)[0] != saved_head
        if rewritten:
            # Start over. Ids are deterministic, so the replay is a no-op for
            # what survived; unlike Codex there is nothing to delete first --
            # a message that vanished from the rewrite is history this tool
            # exists to keep, not stale usage.
            offset = 0
            stats["rewritten"] += 1

        stats["files_read"] += 1
        # Read bytes, not text. `offset` is a byte position, and decoding first
        # breaks that correspondence two ways: universal newlines collapse
        # "\r\n" to one character, and errors="replace" turns an undecodable
        # byte into a 3-byte U+FFFD. Either makes len(line.encode()) disagree
        # with the bytes consumed, so the stored offset drifts and every later
        # read of this file starts mid-line -- permanently, with no resync.
        with path.open("rb") as fh:
            fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    # Partial trailing line -- a session still being written.
                    # Leave the offset short of it so we re-read it next run.
                    break
                offset += len(raw)
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    stats["bad_lines"] += 1
                    continue
                if rec.get("type") != "assistant":
                    continue
                try:
                    row = _usage_row(rec, now)
                except (ValueError, TypeError, AttributeError):
                    # A structurally valid record with a malformed timestamp or
                    # token value. Skipping it costs one message; letting it
                    # raise would roll the whole source back and re-read the
                    # same byte forever, so the collector could never advance
                    # past it -- and every file sorted after this one would
                    # stay unread too.
                    stats["bad_records"] += 1
                    continue
                if row:
                    batch.append(row)

            if len(batch) >= 2000:
                stats["events"] += db.upsert_usage(conn, batch)
                batch = []

        head, head_len = head_hash(path, HEAD_BYTES)
        db.set_state(conn, state_key, json.dumps({
            "offset": offset, "mtime_ns": stat.st_mtime_ns,
            "head": head, "head_len": head_len,
        }), now)

    stats["events"] += db.upsert_usage(conn, batch)
    return stats


def backfill_stats_cache(conn: sqlite3.Connection, stats_cache: Path, now: str) -> int:
    """Recover coarse daily totals from Claude Code's own stats cache.

    This file keeps a per-day, per-model token count that outlives the JSONL
    retention window -- but it is a single scalar with no input/output/cache
    split, so it cannot be priced. Stored separately and used only to show that
    activity existed on days the detailed record no longer covers.
    """
    if not stats_cache.is_file():
        return 0
    try:
        data = json.loads(stats_cache.read_text())
    except (OSError, json.JSONDecodeError):
        return 0

    rows = []
    for entry in data.get("dailyModelTokens") or []:
        day = entry.get("date")
        if not day:
            continue
        for model, total in (entry.get("tokensByModel") or {}).items():
            try:
                total_tokens = int(total or 0)
            except (TypeError, ValueError):
                continue
            rows.append({
                "id": f"ccstats:{day}:{model}",
                "source": "claude_code_stats_cache",
                "provider": PROVIDER,
                "day": day,
                "model": model,
                "total_tokens": total_tokens,
                "ingested_at": now,
            })
    return db.upsert_coarse(conn, rows)


def run(conn: sqlite3.Connection, claude_dir: Path, archive_dir: Path) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    copied, total = archive(claude_dir / "projects", archive_dir)
    stats = ingest(conn, archive_dir, now)
    stats["archived"] = copied
    stats["source_files"] = total
    stats["coarse_days"] = backfill_stats_cache(conn, claude_dir / "stats-cache.json", now)
    return stats
