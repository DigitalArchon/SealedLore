"""Atomic JSON file writes and append-only JSONL.

Every story file except api_log.jsonl is fully rewritten on save; the write
goes to a temp file first and is swapped in with os.replace so a crash never
leaves a half-written file. `keep_backup` additionally retains one `.bak` of
the previous version (used for nodes.json, rewritten on every turn).

Two threads write here: the worker after each turn, and the GUI thread for
config and pictures. Every write takes one lock and uses its own temp file,
so two saves of the same file can't race each other through one `.tmp`; a
JSONL append is one write of record-plus-newline, so two records can't land
on one line.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

# Deeper than anything SealedLore keeps nests: a card's own JSON is taken to
# 100 levels (tavern_cards.MAX_NESTING), and sits a few levels down in a
# character, a backup or a scenario. Past this, what was read couldn't be
# saved or copied (json.dumps recursed too far from about 990 levels, a deep
# copy from about 490), so a file someone else made that deep is refused as
# it is read (`nests_deeper`), not after half of it is written.
MAX_NESTING = 200


def nests_deeper(data: Any, limit: int = MAX_NESTING) -> bool:
    """Whether `data` (as json.loads made it) nests past `limit` levels.
    Walked, never recursed into."""
    stack: list[tuple[Any, int]] = [(data, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return True
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        stack.extend((child, depth + 1) for child in children if isinstance(child, dict | list))
    return False


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
            _remember(path, data, os.fstat(handle.fileno()))
    except (FileNotFoundError, NotADirectoryError):
        return default
    return json.loads(data.decode("utf-8"))


# What this process last wrote to each file, or read from it: a hash of its
# bytes, and the file as it stood then (`_standing`). A save of the same bytes
# to a file still standing as it was is skipped (`_write_atomic`): a story's
# save rewrites every file it has, and a turn changes one or two, and the
# config is saved after nearly everything the window does. Anything that has
# touched the file since (another program, a hand edit, a copy over it)
# changes how it stands, and the bytes are written again.
_KNOWN: dict[str, tuple[bytes, tuple[int, ...]]] = {}


def _standing(info: os.stat_result) -> tuple[int, ...]:
    """A file as it stands: which file it is, its size and when it last
    changed. (Windows' st_ctime is when it was made, so it is left out there.)"""
    standing = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return standing if sys.platform == "win32" else (*standing, info.st_ctime_ns)


def _remember(path: Path, data: bytes, info: os.stat_result) -> None:
    # Unlocked: a reader's own handle says how the file it read stood, so a
    # write swapping another file in meanwhile leaves this entry naming a
    # file no longer there, which only costs that save a write.
    _KNOWN[os.path.abspath(path)] = (hashlib.sha256(data).digest(), _standing(info))


def _unchanged(path: Path, digest: bytes, mode: int | None) -> bool:
    known = _KNOWN.get(os.path.abspath(path))
    if known is None or known[0] != digest:
        return False
    try:
        info = os.stat(path)
    except OSError:
        return False
    if mode is not None and sys.platform != "win32" and stat.S_IMODE(info.st_mode) != mode:
        return False  # the file's mode is part of what is asked for
    return known[1] == _standing(info)


def atomic_write_json(
    path: Path, data: Any, *, keep_backup: bool = False, mode: int | None = None
) -> None:
    """Write `data` as JSON to `path`, all or nothing.

    `mode` sets the file's permissions (the config file, which holds the API
    key, is written 0600); otherwise the process's umask decides.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False)
    _write_atomic(path, text.encode("utf-8"), keep_backup=keep_backup, mode=mode)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path`, all or nothing (a picture, a record from a
    backup), readable by its owner alone."""
    _write_atomic(path, data)


# Whether a write reaches the disk before it is swapped in (`_write_atomic`).
# Only the tests turn it off, for speed; what it does is tested with it on.
DURABLE = True


def _write_atomic(
    path: Path, data: bytes, *, keep_backup: bool = False, mode: int | None = None
) -> None:
    """`data` swapped in at `path` whole, through a temp file of its own.

    A rename alone isn't enough: on a file system that allocates late (ext4,
    XFS, F2FS, NTFS) a power cut soon after can leave the renamed file empty,
    its bytes never written. So the temp file is flushed to the disk before
    it replaces anything, and the folder after, so that the rename itself is
    kept and comes before any later file's.

    The same bytes as the file already holds are not written again
    (`_unchanged`), nor its backup moved on: the backup stays the version
    before the last change."""
    digest = hashlib.sha256(data).digest()
    with _LOCK:
        if _unchanged(path, digest, mode):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                if DURABLE:
                    handle.flush()
                    os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(tmp_path, mode)
            if keep_backup and path.exists():
                _keep_backup(path)
            replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            _KNOWN.pop(os.path.abspath(path), None)
            raise
        if DURABLE:
            sync_folder(path.parent)
        try:
            _KNOWN[os.path.abspath(path)] = (digest, _standing(os.stat(path)))
        except OSError:
            _KNOWN.pop(os.path.abspath(path), None)


def _keep_backup(path: Path) -> None:
    """The file as it stands, kept as `<name>.bak` before it is replaced.

    Linked, not moved: moving would leave no live file until the replace,
    and a crash in between would read as an empty story. A second name for
    the same bytes also costs no copy, and nothing more to flush: they were
    flushed when they were written. A copy where links can't be made (FAT,
    some network shares)."""
    backup_path = path.with_name(path.name + ".bak")
    backup_tmp = path.with_name(f"{path.name}.bak.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        os.link(path, backup_tmp)
    except OSError:
        shutil.copy2(path, backup_tmp)
        if DURABLE:
            with open(backup_tmp, "r+b") as handle:
                os.fsync(handle.fileno())
    try:
        replace(backup_tmp, backup_path)
    except BaseException:
        backup_tmp.unlink(missing_ok=True)
        raise


def sync_folder(folder: Path) -> None:
    """A folder's entries (a rename into it) flushed to the disk. POSIX
    only: Windows can't open a folder for this, and NTFS journals the rename
    itself."""
    if sys.platform == "win32":
        return
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # a file system that can't (some network ones): nothing more to do
    finally:
        os.close(fd)


# Windows: a virus scanner or the search indexer holds a file just written
# open for a moment, and replacing it then fails with PermissionError.
REPLACE_TRIES = 10
REPLACE_WAIT = 0.1


def replace(source: Path, target: Path) -> None:
    """`Path.replace`, waited out on Windows while another process has the
    target open. Elsewhere it is `Path.replace` and nothing more."""
    if sys.platform != "win32":
        source.replace(target)
        return
    for _ in range(REPLACE_TRIES - 1):
        try:
            source.replace(target)
            return
        except PermissionError:
            time.sleep(REPLACE_WAIT)
    source.replace(target)


# Every file here is the author's alone: a story's log holds every prompt
# sent for it. The temp file `mkstemp` makes is 0600 already; an append or a
# picture opened here gets the same, whatever the umask. (On Windows the mode
# only sets the read-only flag; the data folder's ACL does this job there,
# `storage/winacl.py`.)
FILE_MODE = 0o600

# Windows opens a raw descriptor in text mode, which turns every 0x0A byte
# written through it into 0x0D 0x0A: a picture's bytes, and a log's lines.
# The flag doesn't exist elsewhere.
_O_BINARY = getattr(os, "O_BINARY", 0)


def open_private(path: Path, flags: int) -> int:
    """`os.open` with the file created readable by its owner only, in binary."""
    return os.open(path, flags | os.O_CREAT | _O_BINARY, FILE_MODE)


def append_jsonl(path: Path, record: Any) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = open_private(path, os.O_WRONLY | os.O_APPEND)
        with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)


def read_jsonl(path: Path) -> list[Any]:
    """Every record in the file. A line that isn't JSON (a crash mid-write, a
    hand edit) is skipped rather than costing every reader the whole log."""
    if not path.exists():
        return []
    # A record ends at its newline and nowhere else: `splitlines` also breaks
    # at U+2028, U+2029 and U+0085, which JSON leaves unescaped inside a
    # string, and would cut such a record into pieces that parse as nothing.
    return parse_jsonl_lines(path.read_text(encoding="utf-8").split("\n"))


def parse_jsonl_lines(lines: list[str]) -> list[Any]:
    records: list[Any] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
