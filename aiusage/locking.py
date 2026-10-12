"""Serialize collection and pruning across CLI, launchd, and server processes."""

from __future__ import annotations

import fcntl
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .storage import open_private


@contextmanager
def archive_locks(*directories: Path):
    # Canonical ordering prevents deadlocks when multiple archives overlap.
    # Keep the lock files permanently: unlinking one would let another process
    # lock a different inode while an existing holder still owns the old one.
    with ExitStack() as stack:
        for directory in sorted({p.resolve() for p in directories}):
            fh = stack.enter_context(open_private(directory / ".aiusage.lock"))
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
