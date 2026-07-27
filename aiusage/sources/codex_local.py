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
        if dst.exists() and dst.stat().st_size == s.st_size:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    return (copied, total)


def _read_state(conn: sqlite3.Connection, key: str) -> tuple[int, int]:
    raw = db.get_state(conn, key)
    if not raw:
        return (0, 0)
    try:
        d = json.loads(raw)
        return (int(d.get("offset", 0)), int(d.get("seq", 0)))
    except (json.JSONDecodeError, ValueError):
        return (0, 0)


def ingest(conn: sqlite3.Connection, archive_dir: Path, now: str) -> dict:
    stats = {"files": 0, "files_read": 0, "events": 0, "bad_lines": 0}
    if not archive_dir.is_dir():
        return stats

    batch: list[dict] = []

    for path in sorted(archive_dir.rglob("*.jsonl")):
        stats["files"] += 1
        rel = str(path.relative_to(archive_dir))
        state_key = f"codex_offset:{rel}"
        offset, seq = _read_state(conn, state_key)
        size = path.stat().st_size

        if size == offset:
            continue
        if size < offset:
            offset, seq = 0, 0

        stats["files_read"] += 1
        session_id = path.stem
        model = "unknown"
        cwd = None

        with path.open("r", encoding="utf-8", errors="replace") as fh:
            # A resumed read starts mid-file, so re-scan the head cheaply to
            # recover the model/cwd context that precedes this offset.
            if offset:
                for line in fh:
                    if fh.tell() > offset:
                        break
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    p = d.get("payload") or {}
                    if d.get("type") == "turn_context" and p.get("model"):
                        model = p["model"]
                    if d.get("type") == "session_meta" and p.get("cwd"):
                        cwd = p["cwd"]
                fh.seek(offset)

            for line in fh:
                if not line.endswith("\n"):
                    break  # session still being written
                offset += len(line.encode("utf-8"))
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    stats["bad_lines"] += 1
                    continue

                p = rec.get("payload") or {}
                rtype = rec.get("type")

                if rtype == "session_meta":
                    cwd = p.get("cwd") or cwd
                    continue
                if rtype == "turn_context":
                    model = p.get("model") or model
                    continue
                if p.get("type") != "token_count":
                    continue

                usage = (p.get("info") or {}).get("last_token_usage") or {}
                if not usage.get("total_tokens"):
                    continue

                ts = rec.get("timestamp")
                if not ts:
                    continue

                seq += 1
                total_in = usage.get("input_tokens", 0) or 0
                cached_in = usage.get("cached_input_tokens", 0) or 0
                out = usage.get("output_tokens", 0) or 0
                # Present since ~0.146 but always zero so far: OpenAI caching is
                # implicit, so there is no separate write to charge for. Read it
                # anyway so a future non-zero value is picked up automatically.
                cache_write = usage.get("cache_write_input_tokens", 0) or 0

                # A small number of records carry a total with the whole
                # breakdown zeroed (observed on compaction turns). Dropping them
                # silently loses tokens; attribute the remainder to input, which
                # is where essentially all of it lives on these tools.
                declared = usage.get("total_tokens", 0) or 0
                if declared and (total_in + out) == 0:
                    total_in = declared

                batch.append({
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
                    "reasoning_tokens": usage.get("reasoning_output_tokens", 0) or 0,
                    "requests": 1,
                    "project": os.path.basename(cwd) if cwd else None,
                    "git_branch": None,
                    "session_id": session_id,
                    "service_tier": (p.get("rate_limits") or {}).get("plan_type"),
                    "ingested_at": now,
                })

        db.set_state(conn, state_key, json.dumps({"offset": offset, "seq": seq}), now)

        if len(batch) >= 2000:
            stats["events"] += db.upsert_usage(conn, batch)
            batch = []

    stats["events"] += db.upsert_usage(conn, batch)
    return stats


def run(conn: sqlite3.Connection, codex_dir: Path, archive_dir: Path) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    copied, total = archive(codex_dir / "sessions", archive_dir)
    stats = ingest(conn, archive_dir, now)
    stats["archived"] = copied
    stats["source_files"] = total
    return stats
