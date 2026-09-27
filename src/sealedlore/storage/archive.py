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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.aside import Aside
from sealedlore.models.authoring import ReviewRecord
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node
from sealedlore.models.story import Story
from sealedlore.models.summary import Summary
from sealedlore.models.supporting import SupportingCast
from sealedlore.storage.atomic import atomic_write_json
from sealedlore.storage.image_meta import stripped_or_same
from sealedlore.storage.images import IMAGES_FILE, is_safe_relative
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
    except (OSError, json.JSONDecodeError):
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


def _decoded_files(raw: Any) -> dict[str, bytes]:
    """The archive's pictures, dropping any path that could leave the story,
    their hidden data removed where it can be."""
    if not isinstance(raw, dict):
        return {}
    files: dict[str, bytes] = {}
    for relative, text in raw.items():
        if not isinstance(relative, str) or not isinstance(text, str):
            continue
        if not is_safe_relative(relative):
            continue
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            continue
        files[relative] = data if relative == IMAGES_FILE else stripped_or_same(data)
    return files


def read_archive(path: Path) -> tuple[StoryBundle, list[dict[str, Any]]]:
    name = Path(path).name
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ArchiveError(f"Couldn't read {path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"{name} isn't valid JSON (line {exc.lineno}).") from exc

    if not isinstance(data, dict) or data.get("format") != ARCHIVE_FORMAT:
        raise ArchiveError(f"{name} isn't a SealedLore story archive.")
    version = data.get("version")
    if not isinstance(version, int) or version > ARCHIVE_VERSION:
        raise ArchiveError(
            f"{name} is archive format version {version}; this SealedLore reads up to "
            f"version {ARCHIVE_VERSION}."
        )
    try:
        bundle = StoryBundle(
            story=Story.model_validate(data["story"]),
            cast=[Character.model_validate(c) for c in data.get("cast", [])],
            supporting=SupportingCast.model_validate(data.get("supporting") or {}),
            lore=[LoreEntry.model_validate(e) for e in data.get("lore", [])],
            nodes=[Node.model_validate(n) for n in data.get("nodes", [])],
            summaries=[Summary.model_validate(s) for s in data.get("summaries", [])],
            asides=[Aside.model_validate(a) for a in data.get("asides", [])],
            reviews=[ReviewRecord.model_validate(r) for r in data.get("reviews", [])],
            pending_files=_decoded_files(data.get("files")),
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
    if not is_safe_story_id(bundle.story.id):
        # The id names a folder; one that could escape stories/ is replaced.
        bundle.story.id = new_id()
    elif story_dir(bundle.story.id, root).exists():
        bundle.story.id = new_id()
        bundle.story.title = f"{bundle.story.title} (imported)"
    # The vector cache isn't in the archive; entries re-embed on first use.
    for entry in bundle.lore:
        entry.embedding_hash = None
    save_story_bundle(bundle, root=root)
    for record in api_log:
        append_api_log(bundle.story.id, record, root=root)
    return bundle
