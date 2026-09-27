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

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(
    path: Path, data: Any, *, keep_backup: bool = False, mode: int | None = None
) -> None:
    """Write `data` as JSON to `path`, all or nothing.

    `mode` sets the file's permissions (the config file, which holds the API
    key, is written 0600); otherwise the process's umask decides.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            if mode is not None:
                os.chmod(tmp_path, mode)
            if keep_backup and path.exists():
                # Copy rather than move: moving would leave no live file until the
                # replace below, and a crash in between would read as an empty story.
                backup_path = path.with_name(path.name + ".bak")
                backup_tmp = path.with_name(path.name + ".bak.tmp")
                shutil.copy2(path, backup_tmp)
                replace(backup_tmp, backup_path)
            replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


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
    return parse_jsonl_lines(path.read_text(encoding="utf-8").splitlines())


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
