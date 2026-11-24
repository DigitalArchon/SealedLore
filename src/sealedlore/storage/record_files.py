"""A story's JSON files, made again only as far as their records changed.

A session saves after nearly everything it does (some hundred places), and
each save used to make every file of the story whole: every record dumped,
written out indented and hashed, so that the atomic write could find the
bytes unchanged and leave the disk alone. On a 516-message story with 500
vectors that was 0.4 s a save, 0.35 s of it embeddings.json (14 MB) that
almost no save changes, the rest mostly nodes.json.

So each file remembers what it was last written from, record by record:

- a record that may be changed in place (a message, a card, an entry) by a
  fingerprint of its dump: `marshal` at version 2, which writes every value
  with its exact type and a float to its bits, and holds no references
  (version 3 on writes an object's second appearance as one, which depends
  on which objects are shared), hashed. Equal fingerprints are equal dumps,
  so equal JSON. A dump marshal won't take (a subclass of str, say) has no
  fingerprint and is made again every time;
- a record that can't change once made (`fixed`: a vector,
  `EmbeddingCacheEntry`, frozen and holding only a tuple and strings) by
  the object itself, through a weak reference: one gone is never mistaken
  for another made since in its place, and none is kept alive by this.

A file whose records are all as they were, and which still stands on disk
as it was written (`atomic.stands_as_written`: nothing has touched it since),
is left as it is, nothing dumped but the fingerprints. Otherwise only the
records that changed are made into JSON again, and the file is written whole,
atomically, exactly as `atomic_write_json` would write it, `.bak` and all.
What is kept to do so is the fingerprints and, for records that can change,
each record's JSON (as big as the file); a fixed file keeps only references
to its records, and is made whole when one comes or goes (embeddings.json
would hold 11 MB of text for 500 vectors, to save its 0.2 s now and then).
It is kept for the files of the last `KEPT_FOLDERS` story folders written:
the open story's, saved again and again, Your characters' (laid out as a
story's), and whatever was saved beside them (a duplicate, an import).
Anything not kept is made whole, as before.
"""

from __future__ import annotations

import hashlib
import json
import marshal
import os
import threading
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sealedlore.storage.atomic import atomic_write_encoded, stands_as_written, utf8

# Version 2: no references between objects (see above).
_MARSHAL_VERSION = 2


@dataclass(frozen=True)
class _Written:
    """What a file was last written from: one key per record (a fingerprint,
    None where there is none, or a weak reference to a fixed record), each
    record's JSON as it stands in the file (none for a fixed file), and the
    file's digest."""

    keys: tuple[Any, ...]
    chunks: tuple[str, ...] | None
    digest: bytes


KEPT_FOLDERS = 3

# The tests' alone (conftest): after every save of a file, written or left,
# it is read back and held to what writing it whole would have written, so
# that each save the suite makes, by whichever way its records changed,
# shows that nothing changed was left unwritten.
CHECKED = False

_LOCK = threading.Lock()
# Folder -> file -> what it was written from, the folder written last, last.
_WRITTEN: dict[str, dict[str, _Written]] = {}


def _was(where: str) -> _Written | None:
    with _LOCK:
        return _WRITTEN.get(os.path.dirname(where), {}).get(where)


def _remember(where: str, written: _Written) -> None:
    folder = os.path.dirname(where)
    with _LOCK:
        files = _WRITTEN.pop(folder, {})
        files[where] = written
        _WRITTEN[folder] = files
        while len(_WRITTEN) > KEPT_FOLDERS:
            del _WRITTEN[next(iter(_WRITTEN))]


def fingerprint(dumped: Any) -> bytes | None:
    """What a record's dump is, exactly, in 32 bytes; None when it can't be
    told (a value marshal doesn't write)."""
    try:
        return hashlib.sha256(marshal.dumps(dumped, _MARSHAL_VERSION)).digest()
    except ValueError:  # unmarshallable, or nested past marshal's depth
        return None


def _item(dumped: Any, compact: bool) -> str:
    """One record as it stands in its file's list: indented by a level, as
    `json.dumps(records, indent=2)` puts it, or compact. (JSON writes no
    newline inside a string, so every newline is the layout's.)"""
    if compact:
        return json.dumps(dumped, ensure_ascii=False, separators=(",", ":"))
    return "  " + json.dumps(dumped, indent=2, ensure_ascii=False).replace("\n", "\n  ")


def _joined(chunks: Sequence[str], compact: bool) -> str:
    if compact:
        return "[" + ",".join(chunks) + "]"
    return "[\n" + ",\n".join(chunks) + "\n]" if chunks else "[]"


def write_records(
    path: Path,
    records: Sequence[BaseModel],
    *,
    keep_backup: bool = False,
    compact: bool = False,
    fixed: bool = False,
) -> None:
    """`[r.model_dump() for r in records]` as JSON at `path`, indented (or
    `compact`), written only if it changed. `fixed`: records that can't
    change once made, told apart by which objects they are."""
    where = os.path.abspath(path)
    was = _was(where)
    dumps: list[Any] | None = None
    if fixed:
        same = (
            was is not None
            and len(was.keys) == len(records)
            and all(ref() is record for ref, record in zip(was.keys, records, strict=True))
        )
        keys: tuple[Any, ...] = tuple(weakref.ref(record) for record in records)
    else:
        dumps = [record.model_dump() for record in records]
        keys = tuple(fingerprint(dumped) for dumped in dumps)
        same = was is not None and None not in keys and was.keys == keys
    if same and was is not None and stands_as_written(path, was.digest):
        if CHECKED:
            _check(path, [record.model_dump() for record in records], compact)
        return
    if dumps is None:
        chunks = [_item(record.model_dump(), compact) for record in records]
    else:
        known: dict[bytes, str] = {}
        if was is not None and was.chunks is not None:
            known = {k: c for k, c in zip(was.keys, was.chunks, strict=True) if k is not None}
        chunks = [
            known.get(key) or _item(dumped, compact)
            for key, dumped in zip(keys, dumps, strict=True)
        ]
    digest = atomic_write_encoded(path, utf8(_joined(chunks, compact)), keep_backup=keep_backup)
    _remember(where, _Written(keys, None if fixed else tuple(chunks), digest))
    if CHECKED:
        _check(path, [record.model_dump() for record in records], compact)


def write_record(path: Path, dumped: dict[str, Any]) -> None:
    """One record's dump (story.json, supporting.json) as indented JSON at
    `path`, written only if it changed."""
    where = os.path.abspath(path)
    key = fingerprint(dumped)
    was = _was(where)
    if key is not None and was is not None and was.keys == (key,):
        if stands_as_written(path, was.digest):
            if CHECKED:
                _check(path, dumped, False)
            return
    data = utf8(json.dumps(dumped, indent=2, ensure_ascii=False))
    _remember(where, _Written((key,), None, atomic_write_encoded(path, data)))
    if CHECKED:
        _check(path, dumped, False)


def _check(path: Path, dumped: Any, compact: bool) -> None:
    """`CHECKED`: the file holds what writing it whole would write."""
    how: dict[str, Any] = {"separators": (",", ":")} if compact else {"indent": 2}
    whole = utf8(json.dumps(dumped, ensure_ascii=False, **how))
    if Path(path).read_bytes() != whole:
        raise AssertionError(f"{path} doesn't hold what was saved")
