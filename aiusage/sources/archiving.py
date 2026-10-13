"""Shared atomic mirroring and persistent archive-pruning decisions.

Callers hold the archive lock through the associated database commit. Matching
size/mtime is the same unchanged-file contract used by normal mirroring; a
metadata-only change to a pruned source is checked against its full fingerprint.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .. import db
from ..storage import private_mkdir, protect_file
from .fingerprint import prefix_hash


def pruned_key(path: Path) -> str:
    # Include the configured archive root: relative names can collide across
    # archives sharing a database. No schema change is needed for these marks.
    return "pruned_archive:" + str(path.resolve())


def unchanged_pruned(conn: sqlite3.Connection, path: Path, src: Path, stat) -> bool:
    raw = db.get_state(conn, pruned_key(path))
    if not raw:
        return False
    try:
        state = json.loads(raw)
        if stat.st_size != state["offset"]:
            return False
        if stat.st_mtime_ns == state["mtime_ns"]:
            return True
        if prefix_hash(src, stat.st_size) != (state["prefix_hash"], stat.st_size):
            return False
        # A touch did not change the content. Remember the new metadata so
        # subsequent polls do not hash this old transcript again and again.
        state["mtime_ns"] = stat.st_mtime_ns
        db.set_state(conn, pruned_key(path), json.dumps(state), datetime.now(timezone.utc).isoformat())
        return True
    except (ValueError, KeyError, TypeError):
        return False


def atomic_copy(src: Path, dst: Path) -> None:
    private_mkdir(dst.parent)
    fd, name = tempfile.mkstemp(dir=dst.parent, prefix=".aiusage-", suffix=".tmp")
    tmp = Path(name)
    try:
        # copyfileobj retains mkstemp's 0600 mode; copy2 would inherit potentially
        # public source permissions. Snapshot metadata from the open source so
        # an append during copying cannot make an incomplete copy look current.
        with os.fdopen(fd, "wb") as out, src.open("rb") as source:
            before = os.fstat(source.fileno())
            shutil.copyfileobj(source, out, length=1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def mirror(source: Path, archive: Path,
           conn: sqlite3.Connection | None = None) -> tuple[int, int]:
    """Mirror changed JSONL atomically; leave intentionally pruned versions out."""
    copied = total = 0
    if not source.is_dir():
        return copied, total
    for src in source.rglob("*.jsonl"):
        total += 1
        dst = archive / src.relative_to(source)
        try:
            stat = src.stat()
        except OSError:
            continue
        if dst.exists():
            protect_file(dst)
            previous = dst.stat()
            if previous.st_size == stat.st_size and previous.st_mtime_ns == stat.st_mtime_ns:
                continue
        elif conn is not None and unchanged_pruned(conn, dst, src, stat):
            continue
        atomic_copy(src, dst)
        if conn is not None:
            conn.execute("DELETE FROM ingest_state WHERE key = ?", (pruned_key(dst),))
        copied += 1
    return copied, total
