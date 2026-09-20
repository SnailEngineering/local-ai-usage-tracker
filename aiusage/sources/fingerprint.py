"""Hash the complete ingested prefix to distinguish appends from rewrites."""

from __future__ import annotations

import hashlib
from pathlib import Path


def prefix_hash(path: Path, n: int) -> tuple[str, int]:
    """Return (SHA-256, bytes read), using bounded memory even for large files.

    Hash only committed, complete lines (the saved offset), so completing a
    partial trailing line never invalidates the previously ingested prefix.
    """
    digest = hashlib.sha256()
    remaining = n
    with path.open("rb") as fh:
        while remaining:
            chunk = fh.read(min(remaining, 1024 * 1024))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest(), n - remaining
