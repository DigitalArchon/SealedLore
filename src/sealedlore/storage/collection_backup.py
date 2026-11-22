"""A backup of all of Your characters, and putting one back.

A story backup (storage/archive.py) holds its pictures as base64 in one JSON
file. A character can carry far more (a card's files run to tens of
megabytes each), so this is a zip, written and read a file at a time:

- `manifest.json`: its format and version, when it was made, how many;
- `characters.json`: every character, versions too, as kept here, ids and
  all, so a story's links to them still hold once it is put back on
  another computer (one CHARX per card loses its versions and its id);
- `files/…`: every file they name (pictures, a card's files), and the
  pictures drawn for them (`images.json` beside, the records);
- `api_log.jsonl`: what drawing them cost, as their folder's log has it.

Putting one back adds every character the collection doesn't have, under
the same id; one it has already is left as it is, or replaced by the
backup's, as the author chooses. Pure of Qt: the window asks and shows.
"""

from __future__ import annotations

import json
import os
import zipfile
import zlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from sealedlore.ids import utc_now_iso
from sealedlore.models.character import Character
from sealedlore.models.image import GeneratedImage
from sealedlore.storage.atomic import nests_deeper, whole_characters
from sealedlore.storage.collection import files_of
from sealedlore.storage.formats import STORY_FORMAT
from sealedlore.storage.image_meta import (
    MetadataError,
    raster_kind,
    strip_metadata,
    stripped_or_same,
)
from sealedlore.storage.images import is_safe_relative
from sealedlore.storage.paths import is_plain_id
from sealedlore.storage.picture_store import PictureStore
from sealedlore.storage.repository import StoryBundle
from sealedlore.storage.zip_members import member_lines, read_member

BACKUP_FORMAT = "sealedlore-characters"
BACKUP_VERSION = 1
BACKUP_SUFFIX = ".sealedlore-characters.zip"
MANIFEST = "manifest.json"
CHARACTERS = "characters.json"
IMAGES = "images.json"
LOG = "api_log.jsonl"
FILES = "files/"
# Past any real one: a card's file is read to 32 MB, its JSON far less.
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 200_000
# What a backup's files may take once put back, by the size of the backup
# itself: `ROOM_FACTOR` times it and `ROOM_SPARE` more. A member is read to
# the size it claims, so the claims add up to the most a restore writes; with
# no limit on the sum, a backup of 786 KB (a thousand card files of zeros)
# wrote 805 MB. A backup made here stores its files as they are (pictures
# and videos are compressed already), so they come to about its own size
# however large the collection; zipped again by another program, pictures
# gain next to nothing, and the spare is for card files of text.
ROOM_FACTOR = 8
ROOM_SPARE = 256 * 1024 * 1024
# One entry of the log, past any a picture's request or reply writes: a line
# longer is passed over, as a torn one is.
MAX_LOG_LINE = 16 * 1024 * 1024
# What reading a damaged member raises (zipfile's, and its decompressors'),
# and a member past its limit (MemberTooLarge, a ValueError).
_DAMAGED: tuple[type[Exception], ...] = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    OSError,
    RuntimeError,
    ValueError,
)


class BackupError(ValueError):
    """A file that isn't a backup of characters this version can read, said in
    plain words."""


@dataclass
class Restored:
    """What putting a backup back did, for the window to say."""

    added: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    # Files the backup names but doesn't hold, or holds in a form nothing
    # could clean of its hidden data.
    missing: int = 0


def is_backup(path: Path) -> bool:
    """A backup of characters (by what it holds, not its name)."""
    try:
        with zipfile.ZipFile(path) as archive:
            return _manifest(archive) is not None
    except (OSError, zipfile.BadZipFile, ValueError, RecursionError):
        return False


def write_backup(
    path: Path,
    characters: Sequence[Character],
    store: PictureStore,
    *,
    images: Sequence[GeneratedImage] = (),
    log: Sequence[dict[str, Any]] = (),
) -> int:
    """Every character into a backup at `path`, with every file they name
    and the pictures drawn for them; how many files went in. Written beside
    `path` first and moved over it once whole."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.part")
    names = sorted({file for character in characters for file in files_of(character)})
    names += sorted({image.file for image in images} - set(names))
    written = 0
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            manifest = {
                "format": BACKUP_FORMAT,
                "version": BACKUP_VERSION,
                # The characters' own layout, a story folder's
                # (storage/formats.py), which the backup's version doesn't cover.
                "story_format": STORY_FORMAT,
                "made_at": utc_now_iso(),
                "characters": len(characters),
            }
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
            archive.writestr(
                CHARACTERS,
                json.dumps([c.model_dump(mode="json") for c in characters], ensure_ascii=False),
            )
            archive.writestr(
                IMAGES,
                json.dumps([i.model_dump(mode="json") for i in images], ensure_ascii=False),
            )
            archive.writestr(LOG, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in log))
            for name in names:
                if not is_safe_relative(name):
                    continue
                data = store.read(name)
                if data is None:
                    continue
                # Pictures and videos are compressed already: stored as they are.
                archive.writestr(FILES + name, data, compress_type=zipfile.ZIP_STORED)
                written += 1
        # Its log holds the prompts their pictures were drawn from: the
        # author's alone, as the data folder's files are.
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return written


@dataclass
class Backup:
    """A backup read: its characters and drawn pictures, and its files and
    log, read one at a time while it is open (`open_backup`)."""

    characters: list[Character]
    images: list[GeneratedImage]
    _archive: zipfile.ZipFile

    def read(self, relative: str) -> bytes | None:
        """A file the backup holds, None when it holds none by that name."""
        if not is_safe_relative(relative):
            return None
        try:
            info = self._archive.getinfo(FILES + relative)
        except KeyError:
            return None
        try:
            return read_member(self._archive, info, MAX_MEMBER_BYTES)
        except _DAMAGED:
            return None  # too large, or damaged: said as missing

    def log(self) -> Iterator[dict[str, Any]]:
        """Its log's entries, a line at a time: a log is read only when the
        characters are put back, and never held whole."""
        return _log(self._archive)


@contextmanager
def open_backup(path: Path) -> Iterator[Backup]:
    """A backup of characters, open while it is put back. BackupError in
    words for anything else, a damaged one or a newer version's."""
    name = Path(path).name
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError(f"{name} isn't a backup of characters (it isn't a zip).") from exc
    with archive:
        if len(archive.infolist()) > MAX_MEMBERS:
            raise BackupError(f"{name} holds far more files than any backup of characters.")
        manifest = _manifest(archive)
        if manifest is None:
            raise BackupError(f"{name} isn't a backup of characters.")
        claimed = sum(i.file_size for i in archive.infolist() if i.filename.startswith(FILES))
        try:
            size = Path(path).stat().st_size
        except OSError as exc:
            raise BackupError(f"{name} couldn't be read ({exc}).") from exc
        if claimed > ROOM_SPARE + ROOM_FACTOR * size:
            raise BackupError(
                f"{name} says its files take {_megabytes(claimed)} once put back, far more than "
                f"a backup of its size ({_megabytes(size)}) holds: nothing was put back."
            )
        version = manifest.get("version")
        if not isinstance(version, int) or version > BACKUP_VERSION:
            raise BackupError(
                f"{name} was made by a newer SealedLore (version {version} of the backup): "
                "update SealedLore to put it back."
            )
        found = manifest.get("story_format")
        if isinstance(found, int) and found > STORY_FORMAT:
            raise BackupError(
                f"{name} was made by a newer SealedLore (story format {found}; this version "
                f"reads up to {STORY_FORMAT}): update SealedLore to put it back. Put back "
                "here, whatever this version doesn't know of would be lost."
            )
        try:
            characters = [Character.model_validate(c) for c in _json(archive, CHARACTERS, [])]
            images = [GeneratedImage.model_validate(i) for i in _json(archive, IMAGES, [])]
        except (ValidationError, TypeError) as exc:
            raise BackupError(f"{name} is damaged: its characters can't be read.") from exc
        if not all(is_plain_id(character.id) for character in characters):
            # A character's id names their folder of card files: one that
            # isn't a plain name ("../../x") is no id SealedLore made.
            raise BackupError(f"{name} is damaged: one of its characters has no id of its own.")
        yield Backup(characters, images, archive)


def restore(
    backup: Backup,
    bundle: StoryBundle,
    store: PictureStore,
    *,
    replace: bool,
    clean: Callable[[bytes], bytes] | None = None,
) -> Restored:
    """`backup`'s characters into the collection `bundle`, their files into
    `store`: each one it doesn't have, under the same id; each it has, left
    as it is, or with `replace` the backup's in its place. `clean`: what the
    window does to a picture whose hidden data can't be stripped (a
    re-encode); a picture nothing can clean isn't written, as no picture
    comes in with its hidden data. No file here is written over: a backup's
    file whose name one here has, with other bytes, goes under a name of its
    own (`_Files`). The bundle is the caller's to save.

    All or nothing: when a file can't be written (a full disk), BackupError
    says so, the files written so far are removed and the bundle is as it
    was, so nothing is left that no saved record names."""
    done = Restored()
    files = _Files(backup, store, clean)
    before = list(bundle.cast)
    replaced: list[Character] = []
    try:
        by_id = {character.id: index for index, character in enumerate(bundle.cast)}
        for character in backup.characters:
            index = by_id.get(character.id)
            if index is not None and not replace:
                done.kept.append(character.name)
                continue
            done.missing += files.put_for(character)
            if index is None:
                bundle.cast.append(character)
                done.added.append(character.name)
            else:
                replaced.append(bundle.cast[index])
                bundle.cast[index] = character
                done.replaced.append(character.name)
        known = {image.id for image in store.images()}
        drawn = [image for image in backup.images if image.id not in known]
        for image in drawn:
            placed = files.put(image.file)
            if placed is None:
                done.missing += 1
            else:
                image.file = placed
        if drawn:
            store.add_images(drawn)
    except OSError as exc:
        for file in files.written:
            store.remove(file)
        bundle.cast[:] = before
        raise BackupError(
            f"The backup's files couldn't be written here ({exc}). Nothing of it was put back: "
            "your characters are as they were."
        ) from exc
    # Only now, with everything of the backup's in place: a replaced
    # character's own files, where nobody names them any more.
    used = {file for other in bundle.cast for file in files_of(other)}
    for old in replaced:
        for file in files_of(old):
            if file not in used:
                store.remove(file)
    return done


def new_log_entries(backup: Backup, log: Sequence[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """The backup's log entries the folder's log doesn't have yet (by id), one
    at a time while the backup is open, to append as they come: what drawing
    the characters cost comes back with them."""
    known = {entry.get("id") for entry in log if entry.get("id")}
    return (entry for entry in backup.log() if entry.get("id") and entry["id"] not in known)


class _Files:
    """A backup's files on their way into the store, each read and written
    once however many records name it.

    A file's name is whatever the backup says it is, and Marcus's picture in
    someone's backup may have the name of Elena's here: written over hers, a
    character the author chose to keep had another's picture (and a card's
    file other bytes). So a file the store has is never written over: the
    same bytes are left as they are, other bytes go beside it under the
    name numbered, and the backup's records are pointed at that."""

    def __init__(
        self, backup: Backup, store: PictureStore, clean: Callable[[bytes], bytes] | None
    ) -> None:
        self.backup = backup
        self.store = store
        self.clean = clean
        # A backup's name for a file → where it is in the store; None for
        # one that couldn't be put there.
        self.placed: dict[str, str | None] = {}
        # Where the numbering of a name got to: names that differ only in
        # case clash on some file systems, and each would count up from 2.
        self._numbers: dict[str, int] = {}
        # Every file written, to take back if the restore can't be finished.
        self.written: list[str] = []

    def put(self, name: str) -> str | None:
        """The backup's file `name` into the store; the name it has there,
        None when the backup holds none or nothing could clean it."""
        if name not in self.placed:
            data = self.backup.read(name)
            if data is not None:
                data = _without_hidden_data(data, self.clean)
            self.placed[name] = None if data is None else self._write(name, data)
        return self.placed[name]

    def put_for(self, character: Character) -> int:
        """Every file `character` names, their records pointed at where each
        went; how many couldn't be put."""
        missing = 0
        held = character.tavern.assets if character.tavern is not None else []
        for record in (*character.reference_images, *(a for a in held if a.held)):
            placed = self.put(record.file)
            if placed is None:
                missing += 1
            else:
                record.file = placed
        return missing

    def _write(self, name: str, data: bytes) -> str:
        path = PurePosixPath(name)
        target, number = name, self._numbers.get(name.casefold(), 1)
        while (held := self.store.read(target)) is not None and held != data:
            number += 1
            target = path.with_name(f"{path.stem}-{number}{path.suffix}").as_posix()
        self._numbers[name.casefold()] = number
        if held is None:
            # Listed first: a write that fails may leave part of itself.
            self.written.append(target)
            self.store.write(target, data)
        return target


def _without_hidden_data(data: bytes, clean: Callable[[bytes], bytes] | None) -> bytes | None:
    """A picture stripped (after `clean` where it can't be), a video stripped,
    anything else as it is; None for a picture nothing could clean. A backup
    made here holds nothing hidden, but a backup may be anyone's."""
    if raster_kind(data) is None:
        return stripped_or_same(data)
    if clean is not None:
        data = clean(data)
    try:
        return strip_metadata(data)
    except MetadataError:
        return None


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):,.1f} MB"


def _manifest(archive: zipfile.ZipFile) -> dict[str, Any] | None:
    try:
        manifest = _json(archive, MANIFEST, None)
    except BackupError:
        return None
    if isinstance(manifest, dict) and manifest.get("format") == BACKUP_FORMAT:
        return manifest
    return None


def _json(archive: zipfile.ZipFile, member: str, default: Any) -> Any:
    try:
        info = archive.getinfo(member)
    except KeyError:
        return default
    if info.file_size > MAX_MEMBER_BYTES:
        raise BackupError(f"its {member} is far larger than any backup's.")
    try:
        data = whole_characters(
            json.loads(read_member(archive, info, MAX_MEMBER_BYTES).decode("utf-8"))
        )
    except RecursionError as exc:
        # Python 3.11's json gives up at about 1,000 levels, where 3.12 reads
        # them: either way it is the nesting, and says so. First: it is a
        # RuntimeError, which _DAMAGED holds.
        raise BackupError(f"its {member} is nested far deeper than any backup's.") from exc
    except (*_DAMAGED, json.JSONDecodeError) as exc:
        raise BackupError(f"its {member} is damaged.") from exc
    if nests_deeper(data):
        # Put back, it could never be saved again, and every edit after it
        # went unkept: refused before anything is put back.
        raise BackupError(f"its {member} is nested far deeper than any backup's.")
    return data


def _log(archive: zipfile.ZipFile) -> Iterator[dict[str, Any]]:
    try:
        info = archive.getinfo(LOG)
    except KeyError:
        return
    try:
        for line in member_lines(archive, info, MAX_MEMBER_BYTES * 4, MAX_LOG_LINE):
            try:
                entry = json.loads(line) if line.strip() else None
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                continue  # a torn line, as a log reader skips one
            if isinstance(entry, dict):
                yield entry
    except _DAMAGED:
        return  # too large, or damaged from here on: what came before stands
