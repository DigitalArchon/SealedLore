"""One SealedLore at a time for each data folder.

Every SealedLore process holds the whole config and each open story in
memory and rewrites whole files when it saves, so two at once on one data
folder (two windows, or the window and `sealedlore chat`) each write over
the other: the last to save drops the other's turns, settings and keychain
entries without a word. So the window, and each command that writes, holds
an exclusive lock on a file in the data folder while it runs: flock on
Linux and macOS, msvcrt's byte lock on Windows. The system lets go of it
when the process ends however it ends, so a crash leaves nothing to clear
away. The file itself stays, empty.

The lock is per folder: `--data-dir` names another, with a lock of its own.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
from pathlib import Path

from sealedlore.storage.atomic import open_private
from sealedlore.storage.paths import ensure_data_home

LOCK_FILE = "sealedlore.lock"

# What the system says when the lock is another process's: BSD and Linux
# flock, and msvcrt's LK_NBLCK.
_HELD_ELSEWHERE = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EDEADLK}


class FolderInUse(Exception):
    """Another SealedLore has this data folder."""

    def __init__(self, folder: Path) -> None:
        super().__init__(
            f"SealedLore is already running with the data folder {folder}. Close it first: "
            "two at once on one folder would each save over the other's work."
        )
        self.folder = folder


class FolderLock:
    def __init__(self, key: str, fd: int | None) -> None:
        self.key = key
        self.fd = fd
        self.holders = 1

    def release(self) -> None:
        """Let go, once every `hold` in this process has."""
        with _guard:
            self.holders -= 1
            if self.holders > 0:
                return
            _held.pop(self.key, None)
            if self.fd is not None:
                _unlock(self.fd)
                os.close(self.fd)
                self.fd = None


_guard = threading.Lock()
_held: dict[str, FolderLock] = {}


def hold(root: Path | None = None) -> FolderLock:
    """The data folder's lock, held until released (or the process ends).
    Held already in this process, it is the same lock, released when every
    holder has. Raises FolderInUse when another process holds it.

    A folder whose file system can't lock (some network shares), or where
    the file can't be made (a read-only folder), is used unlocked, as it
    always was: the lock is a guard, and must never be why the app can't
    start."""
    folder = ensure_data_home(root)
    key = os.path.realpath(folder)
    with _guard:
        held = _held.get(key)
        if held is not None:
            held.holders += 1
            return held
        try:
            fd: int | None = open_private(folder / LOCK_FILE, os.O_RDWR)
        except OSError:
            fd = None
        if fd is not None:
            try:
                _lock(fd)
            except OSError as exc:
                os.close(fd)
                if exc.errno in _HELD_ELSEWHERE:
                    raise FolderInUse(folder) from exc
                fd = None
        lock = FolderLock(key, fd)
        _held[key] = lock
        return lock


def _lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        # The first byte of the file, which needn't exist to be locked.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # closing the file lets go of it in any case
