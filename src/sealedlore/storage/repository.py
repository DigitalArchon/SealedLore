"""Load/save the on-disk representation of the app config and a story bundle.

A story is split across several JSON files; this
module is the only place that knows how those files map onto the domain
models, and the only place that reads or writes them.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.aside import Aside
from sealedlore.models.authoring import ReviewRecord
from sealedlore.models.character import Character
from sealedlore.models.config import Config, bind_unbound_keys, key_withheld, without_password
from sealedlore.models.embedding import EmbeddingCacheEntry
from sealedlore.models.lore import LoreEntry
from sealedlore.models.memory import Recollection
from sealedlore.models.node import Node
from sealedlore.models.prompt_edit import PromptEdit
from sealedlore.models.story import Story
from sealedlore.models.summary import Summary
from sealedlore.models.supporting import SupportingCast
from sealedlore.storage import folder_lock, keychain
from sealedlore.storage.atomic import (
    append_jsonl,
    append_jsonl_file,
    atomic_write_json,
    nests_deeper,
    open_private,
    parse_jsonl_lines,
    read_json,
    read_jsonl,
    sweep_temps,
)
from sealedlore.storage.formats import (
    CONFIG_FORMAT,
    STORY_FORMAT,
    NewerStory,
    format_of,
    newer_config_words,
    with_format,
)
from sealedlore.storage.images import (
    RECORD_FILES,
    copy_image_files,
    write_record_file,
    write_story_file,
)
from sealedlore.storage.paths import (
    config_file,
    ensure_data_home,
    ensure_story_dir,
    image_listing_file,
    is_safe_story_id,
    stories_dir,
    story_dir,
)
from sealedlore.storage.record_files import write_record, write_records
from sealedlore.tree import standing_leaf


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
    # What characters played by their own model remember (models/memory.py).
    memories: list[Recollection] = Field(default_factory=list)
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


# The config is one object shared by every thread: the window changes and
# saves it on the GUI thread, and what the engine learns (each model's token
# correction factor, its listed prices, its reasoning facts) is put in it and
# saved on the worker's thread or whichever thread a reply came back on. A
# save reads the whole config and writes the whole file, so two at once could
# land an older reading last, and a value put in meanwhile broke the copy
# ("dictionary changed size during iteration"). So a save holds this lock from
# its first look at the config to the file being in place, and whatever the
# engine puts in the config off the GUI thread is put in under it.
# Re-entrant, so a change under it may save.
CONFIG_LOCK = threading.RLock()


def save_config(config: Config, root: Path | None = None) -> None:
    """Written with each key the keychain holds left blank. A key the
    keychain refuses stays in the file this time, never lost; the next save
    tries again (`keychain.last_problem` says why)."""
    with CONFIG_LOCK:
        if config_newer(root) is not None:
            # A newer SealedLore's settings stand until the author says
            # otherwise (`save_over_newer`): written here, what only it knows
            # would be gone.
            return
        ensure_data_home(root)
        written = with_format(_as_written(config, root), CONFIG_FORMAT)
        atomic_write_json(config_file(root), written, mode=CONFIG_FILE_MODE)
        _keep_image_listing(config, root)


# config.json files saved by a newer SealedLore, by path, and their format:
# read, never written over this run unless the author says (`save_over_newer`).
_NEWER_CONFIG: dict[Path, int] = {}


def _config_key(root: Path | None) -> Path:
    return config_file(root).resolve()


def config_newer(root: Path | None = None) -> int | None:
    """The format of a config.json a newer SealedLore saved, read this run
    and so not saved over; None when this version may save it."""
    return _NEWER_CONFIG.get(_config_key(root))


def save_over_newer(root: Path | None = None) -> None:
    """The author's yes: from now on the settings are this version's to
    save, and whatever only the newer one knew goes with the next save."""
    _NEWER_CONFIG.pop(_config_key(root), None)


# The old default of `Config.lore_retrieval_k`, which every config.json saved
# before CONFIG_FORMAT 2 holds whether it was chosen or not.
_OLD_LORE_K = 3


def _settle_older(config: Config, data: Any) -> None:
    """What a config.json saved before this version's format means now: the
    lore's best-match count of 3 was the default every file wrote down, and
    is the new default (5) unless the file is of this format already."""
    if not isinstance(data, dict) or format_of(data) >= 2:
        return
    if data.get("lore_retrieval_k") == _OLD_LORE_K:
        config.lore_retrieval_k = Config.model_fields["lore_retrieval_k"].default


def _note_format(data: Any, root: Path | None) -> str | None:
    """Remembers a newer version's config.json, and says so."""
    found = format_of(data)
    if found <= CONFIG_FORMAT:
        _NEWER_CONFIG.pop(_config_key(root), None)
        return None
    _NEWER_CONFIG[_config_key(root)] = found
    return newer_config_words(found)


# How often a copy is taken again when the GUI thread changed the config
# under it (`_copy_of`); each change is a single step, over in an instant.
_COPY_TRIES = 5


def _copy_of(config: Config) -> Config:
    """A deep copy of the config, taken again should the GUI thread change
    one of its dicts in the middle of it. The window makes its changes
    without the lock, on its own thread, and saves after each, so a copy
    taken a moment early is only ever followed by the window's own save.

    The picture listing is no part of config.json (`Config.image_models`):
    the copy shares it rather than copying it, which was most of the time
    every save took."""
    for _ in range(_COPY_TRIES - 1):
        try:
            return copy.deepcopy(config, {id(config.image_models): config.image_models})
        except RuntimeError as exc:
            if "changed size during iteration" not in str(exc):
                raise
    return copy.deepcopy(config, {id(config.image_models): config.image_models})


def _as_written(config: Config, root: Path | None) -> dict[str, Any]:
    # A key saved with no place recorded is for the address saved beside it.
    bind_unbound_keys(config)
    reachable = keychain.problem() is None
    use = config.key_storage == "keychain" and reachable
    listed = set(config.keychain_slots)
    present: set[str] = set()
    kept: list[str] = []
    keychain.last_problem = None if use or config.key_storage == "file" else keychain.problem()
    # Keys kept in the file by choice: one the keychain has said it hasn't got
    # has nothing to bring there, and is no longer looked for. One it couldn't
    # be asked about stays listed, to move to the file once it can be read.
    by_choice = config.key_storage == "file"
    for slot, holder in keychain.slots(config):
        present.add(slot)
        value = holder.api_key
        if slot in listed and not value and not keychain.known(slot, root):
            if by_choice and keychain.gone(config, root, slot):
                continue
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
    # A password written into an address goes as a key does: the keychain
    # has the address as typed, the file the address without it.
    for slot, holder in keychain.address_slots(config):
        present.add(slot)
        typed = holder.base_url
        if without_password(typed) == typed:
            if slot in listed and not keychain.known(slot, root):
                if not (by_choice and keychain.gone(config, root, slot)):
                    kept.append(slot)  # still there, out of reach: never lost
            elif slot in listed and reachable:
                keychain.remove(slot, root)  # the password taken out, or another address
        elif not use:
            if slot in listed and reachable:
                keychain.remove(slot, root)  # in the file as typed, by choice
        else:
            try:
                keychain.write(slot, root, typed)
            except keychain.KeychainError as exc:
                keychain.last_problem = f"the keychain refused an address's password ({exc})"
                continue  # in the file as typed this time, never lost
            kept.append(slot)
    if reachable:
        for slot in listed - present:
            keychain.remove(slot, root)  # a provider renamed or gone
    config.keychain_slots = kept
    written = _copy_of(config)
    for slot, holder in keychain.slots(written):
        if slot in kept:
            holder.api_key = ""
    for slot, holder in keychain.address_slots(written):
        if slot in kept:
            holder.base_url = without_password(holder.base_url)
    # Where a listing came from is only ever compared, never used to send:
    # it needs no password in any case (gui/video_jobs.py; the pictures'
    # is kept with its listing, `_keep_image_listing`).
    if written.video_models_source:
        written.video_models_source = without_password(written.video_models_source)
    return written.model_dump()


# The picture listing each config file's was last written with, or read
# with (`Config.image_models`): the list itself, which a fetch replaces whole
# and nothing changes in place, when it was fetched, and where from.
_LISTING_SAVED: dict[Path, tuple[list[dict], str | None, str | None]] = {}


def _listing_of(config: Config) -> tuple[list[dict], str | None, str | None]:
    return config.image_models, config.image_models_fetched_at, config.image_models_source


def _keep_image_listing(config: Config, root: Path | None) -> None:
    """The picture endpoint's listing the config holds, with when it was
    fetched and where from, written to a file of its own when it is not what
    was last written or read: a cache, fetched once a day, that config.json
    is saved without. Its 135 KB were most of every save of the config."""
    held = _listing_of(config)
    was = _LISTING_SAVED.get(_config_key(root))
    if was is not None and was[0] is held[0] and was[1:] == held[1:]:
        return
    path = image_listing_file(root)
    if held[0] or held[1] or path.exists():
        source = config.image_models_source
        atomic_write_json(
            path,
            {
                "models": config.image_models,
                "fetched_at": config.image_models_fetched_at,
                "source": without_password(source) if source else source,
            },
        )
    _LISTING_SAVED[_config_key(root)] = held


def _take_image_listing(config: Config, root: Path | None) -> None:
    """The picture listing put in the config, just read from config.json,
    from its own file. One config.json holds (saved before the listing had a
    file, or by such a version since) and fetched later than the file's, or
    with no file, is moved there instead; the config's next save leaves it
    out."""
    try:
        kept = read_json(image_listing_file(root))
    except (OSError, ValueError):
        kept = None  # unreadable: fetched again, as one a day old is
    fetched = config.image_models_fetched_at or ""
    if config.image_models and (
        not isinstance(kept, dict) or fetched > str(kept.get("fetched_at") or "")
    ):
        try:
            _keep_image_listing(config, root)
        except OSError:
            pass  # moved with the next save; the config holds it meanwhile
        return
    models = kept.get("models") if isinstance(kept, dict) else None
    fetched_at = kept.get("fetched_at") if isinstance(kept, dict) else None
    source = kept.get("source") if isinstance(kept, dict) else None
    config.image_models = (
        [entry for entry in models if isinstance(entry, dict)] if isinstance(models, list) else []
    )
    config.image_models_fetched_at = fetched_at if isinstance(fetched_at, str) else None
    config.image_models_source = source if isinstance(source, str) else None
    _LISTING_SAVED[_config_key(root)] = _listing_of(config)


def _fill_keys(config: Config, root: Path | None) -> str | None:
    """Each key the keychain holds, put back, and each address's password;
    what went wrong, if anything. A key saved before the place it is for
    was recorded takes its address's now (`bind_unbound_keys`), and one whose
    address was since edited to another place is said, never sent there."""
    try:
        notes = [_fill_api_keys(config, root), _fill_addresses(config, root)]
    except keychain.KeychainError as exc:
        notes = [
            f"Your API keys are kept in {keychain.where()}, which couldn't be read "
            f"({exc}). Unlock it and restart, or enter the keys again in Settings."
        ]
    bind_unbound_keys(config)
    notes.append(_withheld_note(config))
    return " ".join(note for note in notes if note) or None


def _withheld_note(config: Config) -> str | None:
    """What is said of each key that isn't sent because its address names
    another place than it was saved for (config.json edited by hand, or No
    in Settings), or would go over plain http to another machine and hasn't
    been confirmed for it: once, at start, beside the refusal each request
    makes."""
    said = [
        why
        for slot, holder, address in config.key_holders()
        if (
            why := key_withheld(
                holder.api_key,
                holder.key_saved_for,
                address,
                _whose(slot),
                holder.key_in_clear_for,
            )
        )
    ]
    return " ".join(said) or None


_SLOTS_SAID = {"images": "pictures", "videos": "video", "private": "private scenes"}


def _whose(slot: str) -> str:
    """A key's slot as said: an endpoint by its name, else what it is for."""
    return _SLOTS_SAID.get(slot, slot.removeprefix("provider:"))


def _fill_api_keys(config: Config, root: Path | None) -> str | None:
    """Each key the keychain holds, put back; what is missing, if anything.
    Raises KeychainError."""
    missing: list[str] = []
    for slot, holder in keychain.slots(config):
        if slot not in config.keychain_slots or holder.api_key:
            continue
        value = keychain.read(slot, root)
        if value is None:
            missing.append(slot)
        else:
            holder.api_key = value
    notes = []
    keys = [slot for slot in missing if slot != "phone"]
    if keys:
        notes.append(
            f"{keychain.where(opening=True)} has no key for {', '.join(keys)}: "
            "enter it again in Settings (one that needs no key any more: save Settings with "
            "it left blank)."
        )
    if "phone" in missing:
        # Not a key the author typed: tor made it, and it is the phones'
        # address. A new one is made only when the author asks.
        notes.append(
            f"{keychain.where(opening=True)} has lost phone access's address key: phones "
            "can't reach this computer until it has a new address (File → Phone devices… → "
            "New address…), and then they pair again."
        )
    return " ".join(notes) or None


def _fill_addresses(config: Config, root: Path | None) -> str | None:
    """Each address as it was typed, from the keychain: only while the
    file's address is that one less its password, so the password goes to
    no address but the one it was typed for, however the file is edited.
    One the file has a password in already (kept there by choice, or a save
    the keychain refused) is used as it is. What is lost, if anything;
    raises KeychainError."""
    lost: list[str] = []
    for slot, holder in keychain.address_slots(config):
        written = holder.base_url
        if slot not in config.keychain_slots or without_password(written) != written:
            continue
        stored = keychain.read(slot, root)  # KeychainError: said as for a key
        if stored is None:
            lost.append(slot.removeprefix(keychain.ADDRESS))
        elif without_password(stored) == written:
            holder.base_url = stored
    if lost:
        return (
            f"{keychain.where(opening=True)} has lost the password written into the address "
            f"for {', '.join(lost)}: type it in again in Settings (one that needs none any "
            "more: save Settings with the address as it is)."
        )
    return None


def _load_config(root: Path | None) -> tuple[Config, str | None]:
    data = read_json(config_file(root), default=None)
    config = Config() if data is None else Config.model_validate(data)
    _settle_older(config, data)
    _take_image_listing(config, root)
    newer = _note_format(data, root)
    keys = _fill_keys(config, root)
    return config, " ".join(note for note in (newer, keys) if note) or None


def load_config(root: Path | None = None) -> Config:
    """With its keys, from the keychain where they are kept there. A key the
    keychain can't give back is left blank."""
    return _load_config(root)[0]


def load_config_or_recover(root: Path | None = None) -> tuple[Config, str | None]:
    """The config, as much of it as can be read, with a note for what
    couldn't.

    Nothing in the file must keep the app from starting. A value that
    doesn't validate (an older SealedLore reading a newer one's settings: a
    reasoning level or a limit it doesn't know yet, or a hand edit) goes
    back to its default alone, and everything else is kept: the prompt
    edits, saved prompts and paired phones with it. A file that isn't JSON
    at all (a torn write) starts over from the defaults. Either way the file
    as it was is kept beside it first (`_set_aside`), never over an earlier
    copy, so what was dropped can be copied back by hand. A keychain that
    couldn't give the keys back is noted too.
    """
    path = config_file(root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        config = Config()
        _take_image_listing(config, root)
        return config, _fill_keys(config, root)
    except OSError as exc:
        reason = _first_line(exc)
        return Config(), f"Settings couldn't be read ({reason}); starting with the defaults."
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        if nests_deeper(data):
            raise ValueError("nested far deeper than settings ever are")
    except (ValueError, RecursionError) as exc:  # RecursionError: nesting, on 3.11
        aside = _set_aside(path, raw)
        if aside is not None:
            path.unlink(missing_ok=True)
        return Config(), (
            f"Settings couldn't be read ({_first_line(exc)}); starting with the defaults."
            + (f" The old file is kept as {aside.name}." if aside is not None else "")
        )
    config, dropped = _what_validates(data)
    if config is not None:
        _settle_older(config, data)
    notes: list[str] = []
    if newer := _note_format(data, root):
        notes.append(newer)
    if dropped:
        aside = _set_aside(path, raw)
        kept = f" The file as it was is kept as {aside.name}." if aside is not None else ""
        if config is None:
            notes.append(
                "Settings couldn't be read (they may be from a newer SealedLore); starting "
                f"with the defaults.{kept}"
            )
        else:
            notes.append(
                "Some settings couldn't be read (they may be from a newer SealedLore) and are "
                f"back to their defaults: {', '.join(dropped)}. Everything else is as it was."
                f"{kept}"
            )
    config = config or Config()
    _take_image_listing(config, root)
    key_note = _fill_keys(config, root)
    return config, " ".join(note for note in [*notes, key_note] if note) or None


def _first_line(exc: BaseException) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:200]


# Each round drops one value; a config has nowhere near this many.
_MOST_DROPPED = 500


def _what_validates(data: dict[str, Any]) -> tuple[Config | None, list[str]]:
    """The config from `data`, dropping each value validation names until
    the rest validates, and where each was. None when even what is left
    won't (an error naming no value)."""
    data = copy.deepcopy(data)
    dropped: list[str] = []
    for _ in range(_MOST_DROPPED):
        try:
            return Config.model_validate(data), dropped
        except ValidationError as exc:
            location = exc.errors()[0]["loc"]
        where = _drop(data, location)
        if where is None:
            return None, dropped or ["all of them"]
        dropped.append(where)
    return None, dropped


def _drop(data: dict[str, Any], location: tuple[Any, ...]) -> str | None:
    """The deepest value along `location` that is in `data`, taken out, so
    that its default stands in; where it was. A value inside a list (a
    provider, say) loses only its own field when the error names one.
    None when not even the first step is there."""
    path: list[tuple[Any, Any]] = []
    here: Any = data
    for step in location:
        if isinstance(here, dict) and isinstance(step, str) and step in here:
            path.append((here, step))
            here = here[step]
        elif isinstance(here, list) and isinstance(step, int) and 0 <= step < len(here):
            path.append((here, step))
            here = here[step]
        else:
            break  # a step of the validator's own (a union's member), or missing
    if not path:
        return None
    container, key = path[-1]
    del container[key]
    where = ""
    for _container, step in path:
        where += f"[{step}]" if isinstance(step, int) else (f".{step}" if where else step)
    return where


def _set_aside(path: Path, raw: bytes) -> Path | None:
    """The file's bytes as they were, copied beside it as `config.json.broken`,
    or `.broken-2` and on when that is taken: never over an earlier copy,
    which may hold what another start dropped. A copy already holding these
    bytes is used again. None when nothing could be written."""
    for number in range(1, 1000):
        aside = path.with_name(path.name + (".broken" if number == 1 else f".broken-{number}"))
        try:
            if aside.read_bytes() == raw:
                return aside
            continue
        except FileNotFoundError:
            pass
        except OSError:
            continue
        try:
            fd = open_private(aside, os.O_WRONLY | os.O_EXCL)
        except FileExistsError:
            continue
        except OSError:
            return None
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        return aside
    return None


def save_story_bundle(bundle: StoryBundle, root: Path | None = None) -> None:
    """Every file of the story, one after another, in an order that leaves
    each file naming only what is already saved, whichever file a crash
    stops at: pictures before the records and cards that name them, the
    cards before the messages, the messages before what is anchored to
    them, and story.json, which names the current message, last. A turn
    adds messages and moves to the newest, so cut short anywhere it opens
    where it was, the new messages kept. (A deletion the other way round,
    cut short, leaves story.json naming a message that is gone: loading
    finds the nearest, `_repair_leaf`.)"""
    story_id = bundle.story.id
    ensure_story_dir(story_id, root)
    directory = story_dir(story_id, root)

    story_json = _story_json(bundle)
    if not (directory / "story.json").exists():
        # A folder without it is no story at all: a first save cut short
        # still leaves one, and the second write below is skipped unless
        # something changed.
        write_record(directory / "story.json", story_json)
    for relative, data in bundle.pending_files.items():
        if relative not in RECORD_FILES:
            write_story_file(story_id, relative, data, root)
    for relative, data in bundle.pending_files.items():
        if relative in RECORD_FILES:
            write_record_file(story_id, relative, data, root)  # a story backup's
    # Each written only as far as it changed (storage/record_files.py): a
    # save comes after nearly everything a session does, and most change
    # one file or none.
    write_records(directory / "cast.json", bundle.cast)
    write_record(directory / "supporting.json", bundle.supporting.model_dump())
    write_records(directory / "lore.json", bundle.lore)
    write_records(directory / "nodes.json", bundle.nodes, keep_backup=True)
    write_records(directory / "summaries.json", bundle.summaries)
    # Machine data nobody reads by hand, and the story's biggest file (14 MB
    # indented for 500 vectors, 11 compact): written compact. Either reads.
    write_records(directory / "embeddings.json", bundle.embeddings, compact=True, fixed=True)
    write_records(directory / "asides.json", bundle.asides)
    write_records(directory / "reviews.json", bundle.reviews)
    write_records(directory / "memories.json", bundle.memories)
    write_record(directory / "story.json", story_json)
    bundle.pending_files = {}


def _story_json(bundle: StoryBundle) -> dict[str, Any]:
    return with_format(bundle.story.model_dump(), STORY_FORMAT)


# Why a Visual Story is listed and not opened: one played here before the
# visual mode became a game of its own.
VISUAL_ELSEWHERE = "A Visual Story: it is played in its own app now, not in SealedLore."


def load_story_bundle(
    story_id: str, root: Path | None = None, *, newer_ok: bool = False
) -> StoryBundle:
    """The story folder as this version reads it. One a newer SealedLore saved
    is NewerStory unless `newer_ok` (the author asked, knowing that what this
    version doesn't know of goes with its first save)."""
    directory = story_dir(story_id, root)

    story_data = read_json(directory / "story.json")
    if story_data is None:
        raise FileNotFoundError(f"no story.json found for story {story_id!r}")
    if isinstance(story_data, dict) and story_data.get("mode") == "visual":
        raise ValueError(VISUAL_ELSEWHERE)
    if (found := format_of(story_data)) > STORY_FORMAT and not newer_ok:
        raise NewerStory(found)
    if folder_lock.held(root):
        # No other process writes here: what writes cut short left can go.
        sweep_temps(directory)

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

    story = Story.model_validate(story_data)
    if story.id != story_id:
        # The folder is the story: one copied or renamed by hand keeps the
        # id of the story it was copied from in its story.json, and saved by
        # that id every change went on to the original's folder.
        if is_safe_story_id(story_id):
            notices.append(
                f"This story's folder ({story_id}) was copied or renamed outside SealedLore: "
                "it is now a story of its own, saved in its own folder, and the story it "
                "came from is left as it was."
            )
        story.id = story_id
    _repair_leaf(story, nodes, directory, notices)

    return StoryBundle(
        story=story,
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
        memories=[
            Recollection.model_validate(m) for m in read_json(directory / "memories.json", [])
        ],
    )


def _repair_leaf(story: Story, nodes: list[Node], directory: Path, notices: list[str]) -> None:
    """A story whose current message isn't among its messages opens at the
    nearest one that is (`tree.standing_leaf`), and says so: never refused.
    It comes of a save cut short between story.json and nodes.json, or of
    nodes.json read from its backup, a save behind."""
    leaf = story.active_leaf_id
    if standing_leaf(nodes, leaf) == leaf:
        return
    earlier: list[Node] = []
    backup = directory / "nodes.json.bak"
    try:
        earlier = [Node.model_validate(n) for n in read_json(backup, [])]
    except (OSError, ValueError):
        pass  # the newest message stands in instead
    story.active_leaf_id = standing_leaf(nodes, leaf, earlier=earlier)
    notices.append(
        "The message this story was last at wasn't saved (SealedLore may have been closed, "
        "or the computer stopped, in the middle of a save): it opens at the nearest message "
        "that was."
        if story.active_leaf_id is not None
        else "The message this story was last at wasn't saved, nor any it follows from: "
        "it opens at its start."
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
    _a_story(story_id)
    bundle = load_story_bundle(story_id, root=root)
    now = utc_now_iso()
    bundle.story = bundle.story.model_copy(
        update={"id": new_id(), "title": title, "created_at": now, "updated_at": now}
    )
    save_story_bundle(bundle, root=root)
    # Its records as they stand: the copy's cost history is the original's.
    append_jsonl_file(
        story_dir(bundle.story.id, root) / "api_log.jsonl",
        story_dir(story_id, root) / "api_log.jsonl",
    )
    copy_image_files(story_id, bundle.story.id, root)
    return bundle


def delete_story(story_id: str, root: Path | None = None) -> None:
    """Remove a story's folder for good. The app prefers the desktop trash;
    this is for when there is none, and for the terminal."""
    _a_story(story_id)
    shutil.rmtree(story_dir(story_id, root))


def _a_story(story_id: str) -> None:
    """A story's id, never the author's own characters' (whose folder is laid
    out as a story's: storage/collection.py) or anything that could leave
    the stories folder."""
    if not is_safe_story_id(story_id):
        raise ValueError(f"not a story's id: {story_id!r}")


def stories_linking(card_ids: set[str], root: Path | None = None) -> list[str]:
    """The titles of the stories with a character linked to one of
    `card_ids` (Your characters' cards), read from their card files alone,
    so deleting a card can say whose link it breaks. A story that can't be
    read is left out."""
    found: list[str] = []
    directory = stories_dir(root)
    if not directory.exists() or not card_ids:
        return found
    for entry in sorted(directory.iterdir()):
        if not entry.is_dir() or not is_safe_story_id(entry.name):
            continue
        try:
            cast = read_json(entry / "cast.json", [])
            supporting = read_json(entry / "supporting.json", {})
            story = read_json(entry / "story.json", {})
        except (OSError, ValueError):
            continue
        cards = [*(cast if isinstance(cast, list) else [])]
        if isinstance(supporting, dict) and isinstance(supporting.get("characters"), list):
            cards += supporting["characters"]
        if any(
            isinstance(card, dict)
            and isinstance(card.get("linked"), dict)
            and card["linked"].get("card_id") in card_ids
            for card in cards
        ):
            title = story.get("title") if isinstance(story, dict) else None
            found.append(str(title or entry.name))
    return found


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
