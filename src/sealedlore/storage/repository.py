"""Load/save the on-disk representation of the app config and a story bundle.

A story is split across several JSON files; this
module is the only place that knows how those files map onto the domain
models, and the only place that reads or writes them.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from sealedlore.storage import keychain
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


# API keys may still be in config.json (no keychain, or kept there by
# choice: storage/keychain.py), so the file belongs to its owner alone.
CONFIG_FILE_MODE = 0o600


def save_config(config: Config, root: Path | None = None) -> None:
    """Written with each key the keychain holds left blank. A key the
    keychain refuses stays in the file this time, never lost; the next save
    tries again (`keychain.last_problem` says why)."""
    ensure_data_home(root)
    atomic_write_json(config_file(root), _as_written(config, root), mode=CONFIG_FILE_MODE)


def _as_written(config: Config, root: Path | None) -> dict[str, Any]:
    reachable = keychain.problem() is None
    use = config.key_storage == "keychain" and reachable
    listed = set(config.keychain_slots)
    present: set[str] = set()
    kept: list[str] = []
    keychain.last_problem = None if use or config.key_storage == "file" else keychain.problem()
    for slot, holder in keychain.slots(config):
        present.add(slot)
        value = holder.api_key
        if slot in listed and not value and not keychain.known(slot, root):
            kept.append(slot)  # still there, out of reach: never lost
        elif not use:
            if slot in listed and reachable:
                keychain.remove(slot, root)  # emptied, or moved to the file by choice
        elif value:
            try:
                keychain.write(slot, root, value)
            except keychain.KeychainError as exc:
                keychain.last_problem = f"the keychain refused the key ({exc})"
                continue
            kept.append(slot)
        elif slot in listed:
            keychain.remove(slot, root)  # the author emptied the field
    if reachable:
        for slot in listed - present:
            keychain.remove(slot, root)  # a provider renamed or gone
    config.keychain_slots = kept
    written = config.model_copy(deep=True)
    for slot, holder in keychain.slots(written):
        if slot in kept:
            holder.api_key = ""
    return written.model_dump()


def _fill_keys(config: Config, root: Path | None) -> str | None:
    """Each key the keychain holds, put back; what went wrong, if anything."""
    missing: list[str] = []
    for slot, holder in keychain.slots(config):
        if slot not in config.keychain_slots or holder.api_key:
            continue
        try:
            value = keychain.read(slot, root)
        except keychain.KeychainError as exc:
            return (
                f"Your API keys are kept in {keychain.where()}, which couldn't be read "
                f"({exc}). Unlock it and restart, or enter the keys again in Settings."
            )
        if value is None:
            missing.append(slot)
        else:
            holder.api_key = value
    if missing:
        return (
            f"{keychain.where().capitalize()} has no key for {', '.join(missing)}: "
            "enter it again in Settings."
        )
    return None


def _load_config(root: Path | None) -> tuple[Config, str | None]:
    data = read_json(config_file(root), default=None)
    config = Config() if data is None else Config.model_validate(data)
    return config, _fill_keys(config, root)


def load_config(root: Path | None = None) -> Config:
    """With its keys, from the keychain where they are kept there. A key the
    keychain can't give back is left blank."""
    return _load_config(root)[0]


def load_config_or_recover(root: Path | None = None) -> tuple[Config, str | None]:
    """The config, or the defaults with a note when the file can't be read.

    A hand edit gone wrong, or a torn write, must not keep the app from
    starting: the broken file is set aside as `config.json.broken` (any key
    still in it can be copied back by hand) and the settings start over.
    A keychain that couldn't give the keys back is noted too.
    """
    try:
        return _load_config(root)
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
    story_id: str,
    offset: int,
    root: Path | None = None,
    *,
    keep: Callable[[Any], Any] | None = None,
) -> tuple[list[Any], int, int]:
    """Entries appended since byte `offset`: (entries, where they were read
    from, the offset to read from next).

    The log is append-only, so a caller keeping the entries it has read need
    only parse what came after them. A half-written last line is left for the
    next read. A file shorter than `offset` (replaced, or a different story)
    reads from the start again.

    Read a line at a time, each entry passed through `keep` as it is parsed,
    so a caller that wants a few fields never holds the log. Cutting the
    entries down after reading it whole saved nothing: the process stayed as
    large as the whole log had made it (~190 MB for a 54 MB log).
    """
    path = story_dir(story_id, root) / "api_log.jsonl"
    entries: list[Any] = []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            if handle.tell() < offset:
                offset = 0
            handle.seek(offset)
            end = offset
            for line in handle:
                if not line.endswith(b"\n"):
                    break
                end += len(line)
                for record in parse_jsonl_lines([line.decode("utf-8", errors="replace")]):
                    entries.append(record if keep is None else keep(record))
    except FileNotFoundError:
        return [], 0, 0
    return entries, offset, end


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
