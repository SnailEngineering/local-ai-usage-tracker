"""Private local files without changing a process-wide umask or shared parents."""

from __future__ import annotations

import os
import stat
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
    # Collection calls this for every archived file on every run, so skip the
    # open when the file is already private. Symlinks fall through and fail.
    info = path.lstat()
    if stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077:
        return
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def protect_tree(root: Path) -> None:
    """Make an application-owned directory tree private: `root` and every
    directory below it, without following symbolic links. Files are tightened
    as collection reads them. Installations from before private storage were
    created world-readable, and directory names are project paths."""
    if not root.is_dir() or root.is_symlink():
        return
    # os.walk does not descend into symlinked directories, so every
    # `directory` it yields is a real one inside the tree.
    for directory, _, _ in os.walk(root):
        if os.lstat(directory).st_mode & 0o077:
            os.chmod(directory, 0o700)


def open_private(path: Path):
    """Open/create a private file without truncating it (also used for locks)."""
    private_mkdir(path.parent)
    if path.exists():
        protect_file(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "a+b")
