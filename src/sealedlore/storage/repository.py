"""Load/save the on-disk representation of the app config and a story bundle.

A story is split across several JSON files; this
module is the only place that knows how those files map onto the domain
models, and the only place that reads or writes them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.aside import Aside
from sealedlore.models.authoring import ReviewRecord
from sealedlore.models.character import Character
from sealedlore.models.config import Config
from sealedlore.models.embedding import EmbeddingCacheEntry
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node
from sealedlore.models.prompt_edit import PromptEdit
from sealedlore.models.story import Story
from sealedlore.models.summary import Summary
from sealedlore.models.supporting import SupportingCast
from sealedlore.storage.atomic import (
    append_jsonl,
    atomic_write_json,
    parse_jsonl_lines,
    read_json,
    read_jsonl,
)
from sealedlore.storage.images import copy_image_files, write_story_file
from sealedlore.storage.paths import (
    config_file,
    ensure_data_home,
    ensure_story_dir,
    is_safe_story_id,
    stories_dir,
    story_dir,
)


class StoryBundle(BaseModel):
    story: Story
    cast: list[Character] = Field(default_factory=list)
    lore: list[LoreEntry] = Field(default_factory=list)
    nodes: list[Node] = Field(default_factory=list)
    summaries: list[Summary] = Field(default_factory=list)
    embeddings: list[EmbeddingCacheEntry] = Field(default_factory=list)
    asides: list[Aside] = Field(default_factory=list)
    supporting: SupportingCast = Field(default_factory=SupportingCast)
    reviews: list[ReviewRecord] = Field(default_factory=list)
    # Image files to write with the bundle (a path relative to the story
    # folder, to its bytes): the pictures an imported archive or scenario, or
    # a restart, brings with it. Written by `save_story_bundle`, then cleared.
    pending_files: dict[str, bytes] = Field(default_factory=dict, exclude=True)
    # What loading had to do to get here (a file read from its backup), for
    # the window to tell the author. Never saved.
    notices: list[str] = Field(default_factory=list, exclude=True)


# The API key lives in config.json in plain text (a hard constraint), so the
# file at least belongs to its owner alone.
CONFIG_FILE_MODE = 0o600


def save_config(config: Config, root: Path | None = None) -> None:
    ensure_data_home(root)
    atomic_write_json(config_file(root), config.model_dump(), mode=CONFIG_FILE_MODE)


def load_config(root: Path | None = None) -> Config:
    data = read_json(config_file(root), default=None)
    return Config() if data is None else Config.model_validate(data)


def load_config_or_recover(root: Path | None = None) -> tuple[Config, str | None]:
    """The config, or the defaults with a note when the file can't be read.

    A hand edit gone wrong, or a torn write, must not keep the app from
    starting: the broken file is set aside as `config.json.broken` (the key
    is still in it, to copy back by hand) and the settings start over.
    """
    try:
        return load_config(root=root), None
    except (ValueError, OSError) as exc:
        path = config_file(root)
        aside = path.with_name(path.name + ".broken")
        try:
            path.replace(aside)
        except OSError:
            pass
        reason = str(exc).splitlines()[0][:200]
        return Config(), (
            f"Settings couldn't be read ({reason}); starting with the defaults. "
            f"The old file is kept as {aside.name}."
        )


def save_story_bundle(bundle: StoryBundle, root: Path | None = None) -> None:
    story_id = bundle.story.id
    ensure_story_dir(story_id, root)
    directory = story_dir(story_id, root)

    atomic_write_json(directory / "story.json", bundle.story.model_dump())
    atomic_write_json(directory / "cast.json", [c.model_dump() for c in bundle.cast])
    atomic_write_json(directory / "lore.json", [entry.model_dump() for entry in bundle.lore])
    atomic_write_json(
        directory / "nodes.json", [node.model_dump() for node in bundle.nodes], keep_backup=True
    )
    atomic_write_json(directory / "summaries.json", [s.model_dump() for s in bundle.summaries])
    atomic_write_json(directory / "embeddings.json", [e.model_dump() for e in bundle.embeddings])
    atomic_write_json(directory / "asides.json", [a.model_dump() for a in bundle.asides])
    atomic_write_json(directory / "supporting.json", bundle.supporting.model_dump())
    atomic_write_json(directory / "reviews.json", [r.model_dump() for r in bundle.reviews])
    for relative, data in bundle.pending_files.items():
        write_story_file(story_id, relative, data, root)
    bundle.pending_files = {}


def load_story_bundle(story_id: str, root: Path | None = None) -> StoryBundle:
    directory = story_dir(story_id, root)

    story_data = read_json(directory / "story.json")
    if story_data is None:
        raise FileNotFoundError(f"no story.json found for story {story_id!r}")

    notices: list[str] = []
    try:
        nodes = [Node.model_validate(n) for n in read_json(directory / "nodes.json", [])]
    except ValueError as exc:
        # nodes.json is rewritten on every turn, and is the one file with a
        # backup: the previous save stands in, and the author is told.
        backup = directory / "nodes.json.bak"
        if not backup.exists():
            raise
        nodes = [Node.model_validate(n) for n in read_json(backup, [])]
        reason = str(exc).splitlines()[0][:120]
        notices.append(
            f"nodes.json couldn't be read ({reason}); the messages come from its backup, "
            "which may be a save behind."
        )

    return StoryBundle(
        story=Story.model_validate(story_data),
        cast=[Character.model_validate(c) for c in read_json(directory / "cast.json", [])],
        lore=[LoreEntry.model_validate(e) for e in read_json(directory / "lore.json", [])],
        nodes=nodes,
        notices=notices,
        summaries=[Summary.model_validate(s) for s in read_json(directory / "summaries.json", [])],
        embeddings=[
            EmbeddingCacheEntry.model_validate(e)
            for e in read_json(directory / "embeddings.json", [])
        ],
        asides=[Aside.model_validate(a) for a in read_json(directory / "asides.json", [])],
        supporting=SupportingCast.model_validate(read_json(directory / "supporting.json", {})),
        reviews=[ReviewRecord.model_validate(r) for r in read_json(directory / "reviews.json", [])],
    )


def append_api_log(story_id: str, record: dict, root: Path | None = None) -> None:
    append_jsonl(story_dir(story_id, root) / "api_log.jsonl", record)


def read_api_log(story_id: str, root: Path | None = None) -> list[dict]:
    return read_jsonl(story_dir(story_id, root) / "api_log.jsonl")


def read_api_log_since(
    story_id: str, offset: int, root: Path | None = None
) -> tuple[list[dict], int, int]:
    """Entries appended since byte `offset`: (entries, where they were read
    from, the offset to read from next).

    The log is append-only, so a caller keeping the entries it has read need
    only parse what came after them. A half-written last line is left for the
    next read. A file shorter than `offset` (replaced, or a different story)
    reads from the start again.
    """
    path = story_dir(story_id, root) / "api_log.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            if handle.tell() < offset:
                offset = 0
            handle.seek(offset)
            data = handle.read()
    except FileNotFoundError:
        return [], 0, 0
    end = data.rfind(b"\n") + 1
    entries = parse_jsonl_lines(data[:end].decode("utf-8", errors="replace").splitlines())
    return entries, offset, offset + end


def copy_story(story_id: str, *, title: str, root: Path | None = None) -> StoryBundle:
    """A complete copy under a fresh id: every branch, summary, aside and card,
    the vector cache (same machine, still valid), the API log and the images.

    Node, summary and aside ids are scoped to their story's folder, so they
    carry over unchanged; only the story id and title are new.
    """
    bundle = load_story_bundle(story_id, root=root)
    now = utc_now_iso()
    bundle.story = bundle.story.model_copy(
        update={"id": new_id(), "title": title, "created_at": now, "updated_at": now}
    )
    save_story_bundle(bundle, root=root)
    for record in read_api_log(story_id, root=root):
        append_api_log(bundle.story.id, record, root=root)
    copy_image_files(story_id, bundle.story.id, root)
    return bundle


def delete_story(story_id: str, root: Path | None = None) -> None:
    """Remove a story's folder for good. The app prefers the desktop trash;
    this is for when there is none, and for the terminal."""
    shutil.rmtree(story_dir(story_id, root))


def stories_with_prompt_edits(
    root: Path | None = None, *, excluding: str | None = None
) -> list[tuple[str, str, dict[str, PromptEdit]]]:
    """(id, title, edits) for every story that has edited prompt texts, read
    from `story.json` alone, for "Copy from another story". A story that
    can't be read is left out."""
    found: list[tuple[str, str, dict[str, PromptEdit]]] = []
    directory = stories_dir(root)
    if not directory.exists():
        return found
    for entry in sorted(directory.iterdir()):
        if entry.name == excluding or not entry.is_dir() or not is_safe_story_id(entry.name):
            continue
        try:
            story = read_json(entry / "story.json")
            raw = story.get("prompt_edits") if isinstance(story, dict) else None
            edits = {
                str(key): PromptEdit.model_validate(value) for key, value in (raw or {}).items()
            }
        except (OSError, ValueError):
            continue
        if edits:
            found.append((entry.name, str(story.get("title") or entry.name), edits))
    return found
