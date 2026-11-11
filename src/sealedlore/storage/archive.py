"""Story archives (§9): a whole story in one file, for backup or moving machines.

Everything the story is: settings, style, setup, cast, supporting cards, lore,
scene, the complete node tree (every branch), summaries, asides, the full
verbatim API log with usage and cost, and (from version 2) every picture:
reference and generated, as base64, with images.json. The only thing left out is the vector
cache, which is keyed to an embeddings endpoint and rebuilt on first use.

Importing never overwrites: a story whose id is already on disk comes back
under a fresh one. Only the story id needs changing — node, summary and
aside ids are scoped to their story's own folder.
"""

from __future__ import annotations

import base64
import binascii
import json
import shutil
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.aside import Aside
from sealedlore.models.authoring import ReviewRecord
from sealedlore.models.character import Character
from sealedlore.models.image import GeneratedImage
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node
from sealedlore.models.story import Story
from sealedlore.models.summary import Summary
from sealedlore.models.supporting import SupportingCast
from sealedlore.models.video import GeneratedVideo
from sealedlore.storage.atomic import atomic_write_json, nests_deeper
from sealedlore.storage.image_meta import stripped_or_same
from sealedlore.storage.images import IMAGES_FILE, RECORD_FILES, VIDEOS_FILE, is_story_file
from sealedlore.storage.paths import is_safe_story_id, story_dir
from sealedlore.storage.repository import StoryBundle, append_api_log, save_story_bundle

ARCHIVE_FORMAT = "sealedlore-archive"
ARCHIVE_VERSION = 2
ARCHIVE_SUFFIX = ".sealedlore-archive.json"


class ArchiveError(ValueError):
    """A file that isn't an archive this version can read, said in plain words."""


def file_format(path: Path) -> str | None:
    """The `format` a SealedLore file declares, or None if it isn't one.

    Lets either import command take either kind of file: a scenario and an
    archive are both `.json`, and nothing in a file dialog tells them apart.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        # Not text (a picture, a zip), or nested past any file of ours: not
        # one of ours.
        return None
    value = data.get("format") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def archive_data(
    bundle: StoryBundle,
    api_log: list[dict[str, Any]],
    files: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    """`files` are the story's pictures and images.json (`storage.images.all_image_files`)."""
    return {
        "format": ARCHIVE_FORMAT,
        "version": ARCHIVE_VERSION,
        "exported_at": utc_now_iso(),
        "story": bundle.story.model_dump(),
        "cast": [character.model_dump() for character in bundle.cast],
        "supporting": bundle.supporting.model_dump(),
        "lore": [entry.model_dump() for entry in bundle.lore],
        "nodes": [node.model_dump() for node in bundle.nodes],
        "summaries": [summary.model_dump() for summary in bundle.summaries],
        "asides": [aside.model_dump() for aside in bundle.asides],
        "reviews": [record.model_dump() for record in bundle.reviews],
        "api_log": api_log,
        "files": {
            relative: base64.b64encode(data).decode("ascii")
            for relative, data in (files or {}).items()
        },
    }


def write_archive(
    path: Path,
    bundle: StoryBundle,
    api_log: list[dict[str, Any]],
    files: Mapping[str, bytes] | None = None,
) -> None:
    atomic_write_json(Path(path), archive_data(bundle, api_log, files))


def _decoded_files(raw: Any, name: str) -> dict[str, bytes]:
    """The archive's pictures, dropping any path that could leave the story,
    their hidden data removed where it can be. ArchiveError when its records
    (images.json, videos.json) aren't records, or a file stands where
    another's folder must: either would be found only once half of it was
    written, or when the story was next opened."""
    if not isinstance(raw, dict):
        return {}
    files: dict[str, bytes] = {}
    for relative, text in raw.items():
        if not isinstance(relative, str) or not isinstance(text, str):
            continue
        if not is_story_file(relative):
            continue
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            continue
        files[relative] = data if relative in RECORD_FILES else stripped_or_same(data)
    for record, model in ((IMAGES_FILE, GeneratedImage), (VIDEOS_FILE, GeneratedVideo)):
        if record in files and not _records(files[record], model):
            raise ArchiveError(f"{name} is damaged: its {record} can't be read.")
    folders = {
        parent.as_posix() for relative in files for parent in PurePosixPath(relative).parents
    }
    if clash := sorted(folders.intersection(files)):
        raise ArchiveError(f"{name} is damaged: {clash[0]} is both a file and a folder in it.")
    return files


def _records(data: bytes, model: type[GeneratedImage] | type[GeneratedVideo]) -> bool:
    """Whether a backup's images.json or videos.json is a list of records
    this version reads, as the story reads them each time."""
    try:
        records = json.loads(data.decode("utf-8")) if data else []
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return False
    if not isinstance(records, list) or nests_deeper(records):
        return False
    try:
        for record in records:
            model.model_validate(record)
    except ValidationError:
        return False
    return True


def _items(data: dict[str, Any], section: str, name: str) -> list[Any]:
    """A section that is a list, or none; ArchiveError for anything else."""
    items = data.get(section)
    if items is None:
        return []
    if not isinstance(items, list):
        raise ArchiveError(f"{name} has invalid content — {section}: not a list")
    return items


def read_archive(path: Path) -> tuple[StoryBundle, list[dict[str, Any]]]:
    name = Path(path).name
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ArchiveError(f"Couldn't read {path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"{name} isn't valid JSON (line {exc.lineno}).") from exc
    except UnicodeDecodeError as exc:
        raise ArchiveError(f"{name} isn't a SealedLore story archive (it isn't text).") from exc
    except RecursionError as exc:
        raise ArchiveError(
            f"{name} is nested far deeper than any story backup, so it can't be read."
        ) from exc

    if not isinstance(data, dict) or data.get("format") != ARCHIVE_FORMAT:
        raise ArchiveError(f"{name} isn't a SealedLore story archive.")
    if nests_deeper(data):
        # Read, it couldn't be written back: refused before anything is.
        raise ArchiveError(
            f"{name} is nested far deeper than any story backup, so it can't be read."
        )
    version = data.get("version")
    if not isinstance(version, int) or version > ARCHIVE_VERSION:
        raise ArchiveError(
            f"{name} is archive format version {version}; this SealedLore reads up to "
            f"version {ARCHIVE_VERSION}."
        )
    # Everything checked before anything is written (`restore_archive`).
    try:
        bundle = StoryBundle(
            story=Story.model_validate(data["story"]),
            cast=[Character.model_validate(c) for c in _items(data, "cast", name)],
            supporting=SupportingCast.model_validate(data.get("supporting") or {}),
            lore=[LoreEntry.model_validate(e) for e in _items(data, "lore", name)],
            nodes=[Node.model_validate(n) for n in _items(data, "nodes", name)],
            summaries=[Summary.model_validate(s) for s in _items(data, "summaries", name)],
            asides=[Aside.model_validate(a) for a in _items(data, "asides", name)],
            reviews=[ReviewRecord.model_validate(r) for r in _items(data, "reviews", name)],
            pending_files=_decoded_files(data.get("files"), name),
        )
    except KeyError as exc:
        raise ArchiveError(f"{name} has no {exc.args[0]!r} section.") from exc
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:3]
        )
        raise ArchiveError(f"{name} has invalid content — {problems}") from exc
    raw_log = data.get("api_log")
    api_log = [e for e in raw_log if isinstance(e, dict)] if isinstance(raw_log, list) else []
    return bundle, api_log


def import_archive(path: Path, root: Path | None = None) -> StoryBundle:
    """Restore an archive as a story on disk, under a fresh id if needed."""
    bundle, api_log = read_archive(path)
    return restore_archive(bundle, api_log, root)


def restore_archive(
    bundle: StoryBundle, api_log: list[dict[str, Any]], root: Path | None = None
) -> StoryBundle:
    """Write an archive already read (`read_archive`) as a story on disk.

    A chat exported from memory comes back as an ordinary saved chat: one
    loaded from disk and still marked "memory" could never be saved.

    All of it or none: a write that fails part way (a full disk, a name the
    file system won't take) takes away what was written, and is an
    ArchiveError. Left, a story without its pictures or its log was listed
    though the import was said to have failed."""
    bundle.story.chat_keep = "disk"
    if not is_safe_story_id(bundle.story.id):
        # The id names a folder; one that could escape stories/ is replaced.
        bundle.story.id = new_id()
    elif story_dir(bundle.story.id, root).exists():
        bundle.story.id = new_id()
        bundle.story.title = f"{bundle.story.title} (imported)"
    # The vector cache isn't in the archive; entries re-embed on first use.
    for entry in bundle.lore:
        entry.embedding_hash = None
    folder = story_dir(bundle.story.id, root)  # new: no story had it
    try:
        save_story_bundle(bundle, root=root)
        for record in api_log:
            append_api_log(bundle.story.id, record, root=root)
    except (OSError, ValueError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        raise ArchiveError(
            f"The story backup couldn't be written ({reason}), so nothing of it was kept."
        ) from exc
    return bundle
