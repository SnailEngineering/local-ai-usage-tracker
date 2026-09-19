"""Notice that an archived JSONL was rewritten rather than appended to.

Size and mtime miss the case that matters most: a rewrite that also *grows* the
file looks exactly like an append, so the changed prefix is never re-read.
Hashing the file's first bytes catches it -- an append leaves them untouched.
Shared by both sources' incremental ingest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

HEAD_BYTES = 4096


def head_hash(path: Path, n: int) -> tuple[str, int]:
    """Hash the first `n` bytes. Returns (digest, bytes_actually_read), so a
    file shorter than `n` records how much it covered rather than a digest that
    would change on the next append."""
    with path.open("rb") as fh:
        head = fh.read(n)
    return (hashlib.sha256(head).hexdigest()[:16], len(head))
