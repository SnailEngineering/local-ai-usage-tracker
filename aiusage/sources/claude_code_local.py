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
            # Append-only files: size is the reliable change signal. mtime alone
            # is not, because copy2 replicates it.
            if d.st_size == s.st_size:
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
        "git_branch": rec.get("gitBranch") or None,
        "session_id": rec.get("sessionId") or rec.get("session_id"),
        "service_tier": usage.get("service_tier"),
        "reported_cost_usd": None,
        "ingested_at": now,
    }


def ingest(conn: sqlite3.Connection, archive_dir: Path, now: str) -> dict:
    """Parse archived JSONL from the last read offset of each file."""
    stats = {"files": 0, "files_read": 0, "events": 0, "bad_lines": 0}
    if not archive_dir.is_dir():
        return stats

    batch: list[dict] = []

    for path in sorted(archive_dir.rglob("*.jsonl")):
        stats["files"] += 1
        rel = str(path.relative_to(archive_dir))
        state_key = f"cc_offset:{rel}"
        offset = int(db.get_state(conn, state_key, "0") or 0)
        size = path.stat().st_size

        if size == offset:
            continue
        if size < offset:
            offset = 0  # file was rewritten; start over, PK dedupe absorbs it

        stats["files_read"] += 1
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            for line in fh:
                if not line.endswith("\n"):
                    # Partial trailing line -- a session still being written.
                    # Leave the offset short of it so we re-read it next run.
                    break
                offset += len(line.encode("utf-8"))
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    stats["bad_lines"] += 1
                    continue
                if rec.get("type") != "assistant":
                    continue
                row = _usage_row(rec, now)
                if row:
                    batch.append(row)

            if len(batch) >= 2000:
                stats["events"] += db.upsert_usage(conn, batch)
                batch = []

        db.set_state(conn, state_key, str(offset), now)

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
            rows.append({
                "id": f"ccstats:{day}:{model}",
                "source": "claude_code_stats_cache",
                "provider": PROVIDER,
                "day": day,
                "model": model,
                "total_tokens": int(total or 0),
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
