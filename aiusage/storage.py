"""Private local files without changing a process-wide umask or shared parents."""

from __future__ import annotations

import os
from pathlib import Path


def private_mkdir(path: Path) -> None:
    """Create missing directories privately; existing parents may be shared."""
    if path.is_dir():
        return
    private_mkdir(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise


def protect_file(path: Path) -> None:
    """Tighten an existing application file, refusing symbolic links."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def open_private(path: Path):
    """Open/create a private file without truncating it (also used for locks)."""
    private_mkdir(path.parent)
    if path.exists():
        protect_file(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "a+b")
