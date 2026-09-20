"""Codex local ingest -- the ChatGPT-subscription counterpart to Claude Code.

Codex (CLI and Desktop) writes a rollout JSONL per session under
~/.codex/sessions/YYYY/MM/DD/ and records full token accounting in
`token_count` events. That makes subscription usage trackable without any API
key, exactly the way the Claude Code source works.

Two details that matter:

  * `info.total_token_usage` is cumulative for the session and
    `info.last_token_usage` is the delta for that turn. Summing the deltas
    reconciles exactly with the final cumulative figure, and gives per-day
    attribution, so that's what we store.
  * `token_count` events carry no model name. The model comes from the most
    recent preceding `turn_context` event in the same file, so we track it as
    we stream.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import db
from .fingerprint import prefix_hash
from .validation import object_value, text_value, token_value

SOURCE = "codex_local"
PROVIDER = "openai"


def _local_day(ts_iso: str) -> str:
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%Y-%m-%d")


def archive(sessions_dir: Path, archive_dir: Path) -> tuple[int, int]:
    """Mirror rollout files before Codex can prune them."""
    copied = total = 0
    if not sessions_dir.is_dir():
        return (0, 0)

    for src in sessions_dir.rglob("*.jsonl"):
        total += 1
        dst = archive_dir / src.relative_to(sessions_dir)
        try:
            s = src.stat()
        except OSError:
            continue
        if dst.exists():
            d = dst.stat()
            # `copy2` preserves mtime; combine it with size so a same-size
            # rewrite is not mistaken for an already archived file.
            if d.st_size == s.st_size and d.st_mtime_ns == s.st_mtime_ns:
                continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    return (copied, total)


def _read_state(conn: sqlite3.Connection, key: str) -> tuple[int, int, int | None, str | None, int]:
    raw = db.get_state(conn, key)
    if not raw:
        return (0, 0, None, None, 0)
    try:
        d = json.loads(raw)
        mtime = d.get("mtime_ns")
        head = d.get("prefix_hash")
        return (int(d.get("offset", 0)), int(d.get("seq", 0)),
                int(mtime) if mtime is not None else None,
                str(head) if head else None, int(d.get("prefix_len", 0)))
    except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
        return (0, 0, None, None, 0)


def ingest(conn: sqlite3.Connection, archive_dir: Path, now: str) -> dict:
    stats = {"files": 0, "files_read": 0, "events": 0, "bad_lines": 0,
             "bad_records": 0, "rewritten": 0}
    if not archive_dir.is_dir():
        return stats

    batch: list[dict] = []

    for path in sorted(archive_dir.rglob("*.jsonl")):
        stats["files"] += 1
        rel = str(path.relative_to(archive_dir))
        state_key = f"codex_offset:{rel}"
        offset, seq, saved_mtime, saved_prefix, saved_prefix_len = _read_state(conn, state_key)
        stat = path.stat()
        size = stat.st_size

        if (size == offset and saved_prefix is not None
                and saved_prefix_len == offset and saved_mtime == stat.st_mtime_ns):
            continue

        session_id = path.stem
        # Verify every previously ingested byte. Legacy head-only fingerprints
        # cannot prove the prefix is unchanged, so replay those files once.
        rewritten = (size < offset or (size == offset and saved_mtime is not None)
                     or (offset > 0 and (saved_prefix is None or saved_prefix_len != offset)))
        if not rewritten and saved_prefix is not None:
            rewritten = prefix_hash(path, saved_prefix_len) != (saved_prefix, saved_prefix_len)

        if rewritten:
            # Codex ids are positional (`codex:<session>:<seq>`), so replaying a
            # rewritten file with INSERT OR REPLACE is not enough: any event that
            # disappeared keeps its old row forever, inventing usage that no
            # longer exists on disk. Clear the session first and rebuild it. The
            # delete and the replay share run_sources' transaction, so a failure
            # mid-file rolls both back rather than leaving a half-empty session.
            conn.execute("DELETE FROM usage_event WHERE source = ? AND session_id = ?",
                         (SOURCE, session_id))
            offset, seq = 0, 0
            stats["rewritten"] += 1

        stats["files_read"] += 1
        model = "unknown"
        cwd = None

        # Bytes, not text -- see the matching comment in claude_code_local.ingest:
        # decoding first lets `offset` drift away from the real byte position on
        # CRLF or undecodable input, which desyncs the file for good.
        with path.open("rb") as fh:
            # A resumed read starts mid-file, so re-scan the head cheaply to
            # recover the model/cwd context that precedes this offset.
            if offset:
                while fh.tell() < offset:
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        d = object_value(json.loads(line.decode("utf-8", "replace")))
                        p = object_value(d.get("payload"), optional=True)
                        if d.get("type") == "turn_context":
                            model = text_value(p.get("model")) or model
                        if d.get("type") == "session_meta":
                            cwd = text_value(p.get("cwd")) or cwd
                    except (ValueError, TypeError, AttributeError):
                        continue
                fh.seek(offset)

            for raw in fh:
                if not raw.endswith(b"\n"):
                    break  # session still being written
                offset += len(raw)
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    stats["bad_lines"] += 1
                    continue

                try:
                    rec = object_value(rec)
                    p = object_value(rec.get("payload"), optional=True)
                    rtype = rec.get("type")

                    if rtype == "session_meta":
                        cwd = text_value(p.get("cwd")) or cwd
                        continue
                    if rtype == "turn_context":
                        model = text_value(p.get("model")) or model
                        continue
                    if p.get("type") != "token_count":
                        continue

                    info = object_value(p.get("info"), optional=True)
                    usage = object_value(info.get("last_token_usage"), optional=True)
                    if not usage.get("total_tokens") or not rec.get("timestamp"):
                        continue

                    # Malformed usage still consumes its position, so replay
                    # and incremental reads assign the same ids to later rows.
                    seq += 1
                    row = _usage_row(rec, p, usage, session_id, seq, model, cwd, now)
                except (ValueError, TypeError, AttributeError):
                    # See claude_code_local.ingest: raising here would roll the
                    # whole source back and stall it on this byte permanently.
                    stats["bad_records"] += 1
                    continue
                batch.append(row)

        digest, prefix_len = prefix_hash(path, offset)
        db.set_state(conn, state_key, json.dumps({
            "offset": offset, "seq": seq, "mtime_ns": stat.st_mtime_ns,
            "prefix_hash": digest, "prefix_len": prefix_len,
        }), now)

        if len(batch) >= 2000:
            stats["events"] += db.upsert_usage(conn, batch)
            batch = []

    stats["events"] += db.upsert_usage(conn, batch)
    return stats


def _usage_row(rec: dict, p: dict, usage: dict, session_id: str, seq: int,
               model: str, cwd: str | None, now: str) -> dict:
    """Build one usage row from a `token_count` payload. Raises on malformed
    input; `ingest` counts that and moves on."""
    ts = text_value(rec["timestamp"])
    total_in = token_value(usage.get("input_tokens"))
    cached_in = token_value(usage.get("cached_input_tokens"))
    out = token_value(usage.get("output_tokens"))
    # Present since ~0.146 but always zero so far: OpenAI caching is
    # implicit, so there is no separate write to charge for. Read it
    # anyway so a future non-zero value is picked up automatically.
    cache_write = token_value(usage.get("cache_write_input_tokens"))

    # A small number of records carry a total with the whole
    # breakdown zeroed (observed on compaction turns). Dropping them
    # silently loses tokens; attribute the remainder to input, which
    # is where essentially all of it lives on these tools.
    declared = token_value(usage.get("total_tokens"))
    if declared and (total_in + out) == 0:
        total_in = declared

    return {
        "id": f"codex:{session_id}:{seq}",
        "source": SOURCE,
        "provider": PROVIDER,
        "ts": ts,
        "day": _local_day(ts),
        "model": model,
        # Codex reports cached tokens inside input_tokens; split them
        # so the column means the same thing as the Anthropic one.
        "input_tokens": max(total_in - cached_in, 0),
        "output_tokens": out,
        "cache_write_5m_tokens": cache_write,
        "cache_write_1h_tokens": 0,
        "cache_read_tokens": cached_in,
        "reasoning_tokens": token_value(usage.get("reasoning_output_tokens")),
        "requests": 1,
        "project": os.path.basename(cwd) if cwd else None,
        "project_path": cwd or None,
        "git_branch": None,
        "session_id": session_id,
        "service_tier": text_value(object_value(p.get("rate_limits"), optional=True).get("plan_type")),
        "ingested_at": now,
    }


def run(conn: sqlite3.Connection, codex_dir: Path, archive_dir: Path) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    copied, total = archive(codex_dir / "sessions", archive_dir)
    stats = ingest(conn, archive_dir, now)
    stats["archived"] = copied
    stats["source_files"] = total
    return stats
