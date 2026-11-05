"""The author's own characters ("Your characters"), kept outside any story.

The author (Nov 4 2026): characters were kept only inside stories, and a
SillyTavern card imported became a story. They wanted dozens of characters
managed in one place, each with every field of a SillyTavern V3 card, to put
into any story or start a chat or a story with.

They are kept in the data folder's characters/ (`paths.COLLECTION_ID`), laid
out as a story's folder is: the characters in cast.json, their pictures under
images/refs, the pictures drawn for them in images.json, and what drawing
them cost in api_log.jsonl. So the window opens it as a session of its own
(gui/window_collection.py), and every picture tool a story's cards have works
on it unchanged. This module is what is particular to it: versions, copies,
search, and a SillyTavern card read in. It writes pictures only through the
store it is given.

A character's text keeps its `{{char}}` and `{{user}}` as a card wrote them:
the names go in when the character goes into a story or a chat.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePosixPath

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.character import Character
from sealedlore.models.collection import CollectionInfo
from sealedlore.models.image import ImageRef
from sealedlore.models.story import Story
from sealedlore.models.tavern import CardAsset, TavernFields
from sealedlore.storage.image_meta import MetadataError, picture_kind, raster_kind, strip_metadata
from sealedlore.storage.images import REFS_DIR
from sealedlore.storage.paths import COLLECTION_ID
from sealedlore.storage.picture_store import PictureStore
from sealedlore.storage.repository import StoryBundle, load_story_bundle
from sealedlore.storage.tavern_cards import (
    ICON_NAME,
    PORTRAIT_CAPTION,
    TavernCard,
    character_from_card,
    expression_refs,
)

COLLECTION_TITLE = "Your characters"
MAIN_CARD = "Main card"
COPY_SUFFIX = " (copy)"


def new_collection() -> StoryBundle:
    return StoryBundle(story=Story(id=COLLECTION_ID, title=COLLECTION_TITLE))


def load_collection(root=None) -> StoryBundle:
    """The author's characters as kept; an empty collection before the first
    is kept (nothing is written until then)."""
    try:
        bundle = load_story_bundle(COLLECTION_ID, root)
    except FileNotFoundError:
        return new_collection()
    for character in bundle.cast:
        # Every one of them is kept and has a card's fields, written by hand
        # or not.
        if character.collection is None:
            character.collection = CollectionInfo()
        if character.tavern is None:
            character.tavern = TavernFields()
    return bundle


def kept(name: str) -> Character:
    """A new character of the author's own, with nothing on it but a name."""
    return Character(
        name=name.strip() or "Unnamed", collection=CollectionInfo(), tavern=TavernFields()
    )


def info(character: Character) -> CollectionInfo:
    if character.collection is None:
        character.collection = CollectionInfo()
    return character.collection


def touch(character: Character) -> None:
    """Changed just now, as the list sorts and says it."""
    info(character).changed_at = utc_now_iso()


# --- versions ---------------------------------------------------------------------------


def is_main(character: Character, characters: Sequence[Character]) -> bool:
    """A main card, or a version whose main card is gone."""
    family = info(character).family
    return not family or not any(other.id == family for other in characters)


def main_of(character: Character, characters: Sequence[Character]) -> Character:
    if is_main(character, characters):
        return character
    family = info(character).family
    return next(other for other in characters if other.id == family)


def family_of(character: Character, characters: Sequence[Character]) -> list[Character]:
    """The main card, then its versions in the order they were made."""
    main = main_of(character, characters)
    versions = [
        other for other in characters if other is not main and info(other).family == main.id
    ]
    return [main, *sorted(versions, key=lambda other: info(other).added_at)]


def mains(characters: Sequence[Character]) -> list[Character]:
    return [character for character in characters if is_main(character, characters)]


def version_name(character: Character, characters: Sequence[Character]) -> str:
    if is_main(character, characters):
        return MAIN_CARD
    return info(character).version or "A version"


def copied(
    character: Character,
    store: PictureStore,
    *,
    name: str | None = None,
    family: str = "",
    version: str = "",
    came_from: str = "",
) -> Character:
    """A whole copy under a fresh id, its pictures copied into files of its
    own (so either can lose one without the other), kept as a main card or,
    with `family`, a version of that main card."""
    copy = character.model_copy(deep=True)
    copy.id = new_id()
    if name is not None:
        copy.name = name
    pictures = []
    for ref in copy.reference_images:
        data = store.read(ref.file)
        suffix = picture_kind(data) if data else None
        if data is None or suffix is None:
            continue  # its file was lost: nothing to copy
        ref.id = new_id()
        ref.file = f"{REFS_DIR}/{ref.id}{suffix}"
        store.write(ref.file, data)
        pictures.append(ref)
    copy.reference_images = pictures
    copy.collection = CollectionInfo(family=family, version=version, came_from=came_from)
    if copy.tavern is None:
        copy.tavern = TavernFields()
    copy_files(copy, store)
    return copy


# A card's other files (models/tavern.py CardAsset), beside the pictures:
# each character's in a folder of their own, named by their id (it stays
# right when they are renamed), each under the name the card gave it (the
# author, Nov 4 2026: one folder for every character's files gets messy).
FILES_DIR = "images/assets"
# Names Windows won't give a file, whatever its extension.
_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(10)}


def files_folder(character: Character) -> str:
    return f"{FILES_DIR}/{character.id}"


def file_path(character: Character, asset: CardAsset, taken: set[str]) -> str:
    """A new place for one of `character`'s files: in their folder, under the
    card's name for it made safe for any file system, numbered when `taken`
    (casefolded paths, added to) already has it."""
    stem = re.sub(r"[^\w\- ]+", "_", asset.name).strip(" _-")[:80].strip() or "file"
    if stem.casefold() in _RESERVED:
        stem = f"_{stem}"
    ext = re.sub(r"[^A-Za-z0-9]+", "", asset.ext)[:10] or "bin"
    folder = files_folder(character)
    path, number = f"{folder}/{stem}.{ext}", 2
    while path.casefold() in taken:
        path, number = f"{folder}/{stem}-{number}.{ext}", number + 1
    taken.add(path.casefold())
    return path


def copy_files(
    character: Character, store: PictureStore, source: PictureStore | None = None
) -> None:
    """The character's card files into files of their own in `store` (from
    `source`, by default the same store), in their own folder, so either copy
    can lose one without the other; one whose file was lost is dropped."""
    if character.tavern is None:
        return
    kept = []
    taken: set[str] = set()
    for asset in character.tavern.assets:
        if asset.held:
            data = (source or store).read(asset.file)
            if data is None:
                continue
            asset.file = file_path(character, asset, taken)
            store.write(asset.file, data)
        kept.append(asset)
    character.tavern.assets = kept


def _taken(character: Character, but: CardAsset | None = None) -> set[str]:
    assets = character.tavern.assets if character.tavern is not None else []
    return {a.file.casefold() for a in assets if a.held and a is not but}


def add_file(
    character: Character, store: PictureStore, name: str, ext: str, data: bytes, kind: str
) -> CardAsset:
    """A file onto the character's card, kept in their folder under `name`."""
    if character.tavern is None:
        character.tavern = TavernFields()
    asset = CardAsset(type=kind, name=name, ext=ext.lower().lstrip("."))
    asset.file = file_path(character, asset, _taken(character))
    store.write(asset.file, data)
    character.tavern.assets.append(asset)
    return asset


def add_link(character: Character, address: str, kind: str) -> CardAsset:
    """A file the card only links to: named by the address's last part,
    never fetched."""
    if character.tavern is None:
        character.tavern = TavernFields()
    last = address.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    stem, _dot, ext = last.rpartition(".")
    asset = CardAsset(type=kind, name=stem or last or "link", ext=ext.lower() if stem else "")
    asset.uri = address
    character.tavern.assets.append(asset)
    return asset


def rename_file(character: Character, asset: CardAsset, store: PictureStore) -> str | None:
    """A renamed file kept under its new name too: written there, its record
    pointed there, and the old place returned for the window to remove once
    the record is saved. None when nothing needed moving or it couldn't be
    read."""
    if not asset.held:
        return None
    wanted = file_path(character, asset, _taken(character, but=asset))
    if wanted.casefold() == asset.file.casefold():
        return None
    data = store.read(asset.file)
    if data is None:
        return None
    old, asset.file = asset.file, wanted
    store.write(asset.file, data)
    return old


def settle_files(characters: Sequence[Character], store: PictureStore) -> list[str]:
    """Card files kept before each character had a folder (3.0.0b1 kept them
    all in one) moved into their own character's: each written in its new
    place and its record pointed there. The old files no one names any more
    are returned, for the window to remove once the new records are saved,
    so nothing is lost if it stops in between. A file that can't be read
    stays where it is."""
    moved: list[str] = []
    for character in characters:
        if character.tavern is None:
            continue
        folder = files_folder(character) + "/"
        held = [asset for asset in character.tavern.assets if asset.held]
        taken = {asset.file.casefold() for asset in held if asset.file.startswith(folder)}
        for asset in held:
            if asset.file.startswith(folder):
                continue
            data = store.read(asset.file)
            if data is None:
                continue
            old, asset.file = asset.file, file_path(character, asset, taken)
            store.write(asset.file, data)
            moved.append(old)
    used = {file for character in characters for file in files_of(character)}
    return [file for file in dict.fromkeys(moved) if file not in used]


def files_of(character: Character) -> list[str]:
    """Every file the character keeps: their pictures and their card's files."""
    files = [ref.file for ref in character.reference_images]
    if character.tavern is not None:
        files += [asset.file for asset in character.tavern.assets if asset.held]
    return files


def forget(bundle: StoryBundle, character: Character) -> list[str]:
    """Take a character out of the collection. A main card's first version
    becomes the main card, the others its versions. The picture files that
    were only theirs, for the window to put away."""
    cast = bundle.cast
    if character not in cast:
        return []
    family = family_of(character, cast)
    cast.remove(character)
    if family[0] is character and len(family) > 1:
        heir = family[1]
        heir_info = info(heir)
        heir_info.family = heir_info.version = ""
        for other in family[2:]:
            info(other).family = heir.id
    used = {file for other in cast for file in files_of(other)}
    return [file for file in files_of(character) if file not in used]


# --- finding them -------------------------------------------------------------------------


def tags_of(characters: Sequence[Character]) -> list[str]:
    """Every tag, once, as first written, in alphabetical order."""
    found: dict[str, str] = {}
    for character in characters:
        for tag in character.tavern.tags if character.tavern else ():
            if tag.strip():
                found.setdefault(tag.strip().casefold(), tag.strip())
    return sorted(found.values(), key=str.casefold)


def has_tag(character: Character, tag: str) -> bool:
    tags = character.tavern.tags if character.tavern else ()
    return any(other.strip().casefold() == tag.strip().casefold() for other in tags)


def searchable(character: Character) -> str:
    tavern = character.tavern or TavernFields()
    parts = [
        character.name,
        *character.aliases,
        character.summary or "",
        character.full_description or "",
        character.personality or "",
        character.canon or "",
        info(character).version,
        tavern.scenario,
        tavern.creator,
        tavern.creator_notes,
        *tavern.tags,
    ]
    return "\n".join(part for part in parts if part).casefold()


def matches(character: Character, words: str) -> bool:
    """Every word of `words` somewhere on the card (its name, tags, what it
    says of them), any case."""
    text = searchable(character)
    return all(word in text for word in words.casefold().split())


def unique_name(name: str, characters: Sequence[Character]) -> str:
    """`name`, or with a number after it when a main card has it already."""
    taken = {other.name.casefold() for other in mains(characters)}
    if name.casefold() not in taken:
        return name
    base = re.sub(r"\s\(\d+\)$", "", name)
    number = 2
    while f"{base} ({number})".casefold() in taken:
        number += 1
    return f"{base} ({number})"


# --- a SillyTavern card read in -------------------------------------------------------------


def files_said(assets: Sequence[CardAsset]) -> str:
    """What a card's other files are, and what becomes of them, in a line."""
    held = [a for a in assets if a.held]
    links = len(assets) - len(held)
    backgrounds = sum(1 for a in held if a.type == "background")
    pictures = sum(1 for a in held if a.type != "background" and a.picture)
    others = len(held) - backgrounds - pictures
    parts = []
    if backgrounds:
        parts.append(_count(backgrounds, "background"))
    if pictures:
        parts.append(_count(pictures, "other picture"))
    if others:
        parts.append(_count(others, "other file") + " (sound, video, fonts…)")
    words = ""
    if parts:
        words = (
            f"Its {_listed(parts)} {'are' if len(held) > 1 else 'is'} kept with the card, and "
            "go out with it in a CHARX export. A background goes behind a chat or a story made "
            "from the card, and the pictures a greeting shows appear with that greeting."
        )
    if links:
        words += (" " if words else "") + (
            f"{_count(links, 'file')} {'are links' if links > 1 else 'is a link'}, not in the "
            "card: kept as links, and nothing is fetched."
        )
    return words


def _count(n: int, what: str) -> str:
    return f"{n} {what}{'' if n == 1 else 's'}"


def _listed(parts: Sequence[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _without_hidden_data(data: bytes, clean: Callable[[bytes], bytes] | None) -> bytes | None:
    """A picture with nothing hidden left in it: stripped, after `clean` (the
    window's re-encode) has redrawn what can't be (a GIF, a BMP). None when
    neither can (an iPhone's HEIC, which Qt can't read): such a picture
    isn't kept, as no picture coming in is kept with its hidden data."""
    if clean is not None:
        data = clean(data)
    try:
        return strip_metadata(data)
    except MetadataError:
        return None


def kept_from_card(
    card: TavernCard,
    store: PictureStore,
    *,
    came_from: str = "",
    clean: Callable[[bytes], bytes] | None = None,
) -> tuple[Character, list[str]]:
    """A card read into the collection: the character with every field the
    card has and its pictures (its own, and any expression pictures, labelled),
    `{{char}}` and `{{user}}` left as written. `clean`: what the window does
    to a picture whose hidden data couldn't be stripped (a re-encode); a
    picture neither can clean is left out. And what didn't come along, in
    words."""
    character = character_from_card(card, lambda text: text, for_story=False)
    character.collection = CollectionInfo(came_from=came_from)
    notes: list[str] = []

    def keep(data: bytes, caption: str = "") -> ImageRef | None:
        data = _without_hidden_data(data, clean)
        suffix = picture_kind(data) if data is not None else None
        if data is None or suffix is None:
            return None
        ref_id = new_id()
        ref = ImageRef(id=ref_id, file=f"{REFS_DIR}/{ref_id}{suffix}", caption=caption)
        store.write(ref.file, data)
        return ref

    if card.picture is not None:
        portrait = keep(card.picture, PORTRAIT_CAPTION)
        if portrait is None:
            notes.append("Its picture is in a format SealedLore doesn't keep.")
        else:
            look, expression, caption, framing = card.picture_labels.get(
                ICON_NAME, ("", "", "", "")
            )
            portrait.look, portrait.expression, portrait.framing = look, expression, framing
            portrait.caption = caption or portrait.caption
            character.reference_images.append(portrait)
    if card.expressions:

        def keep_expression(data: bytes, _suffix: str, label: str) -> ImageRef | None:
            ref = keep(data)
            if ref is not None:
                ref.expression = label
            return ref

        refs, skipped = expression_refs(card.expressions, keep_expression, card.picture_labels)
        character.reference_images += refs
        if skipped:
            notes.append(
                f"{skipped} expression picture{'s were' if skipped > 1 else ' was'} in a "
                "format SealedLore doesn't keep."
            )
    notes += card.expression_notes
    assert character.tavern is not None
    taken: set[str] = set()
    # A file the card names twice is the same bytes (tavern_cards reads each
    # once): cleaned once too.
    cleaned: dict[int, bytes | None] = {}
    unclean: list[str] = []
    for found in card.files:
        asset = CardAsset(type=found.type, name=found.name, ext=found.ext, path=found.path)
        if found.data is None:
            asset.uri = found.uri
        else:
            data: bytes | None = found.data
            # A picture, told by its bytes, goes in as any picture does,
            # nothing hidden left in it; one that can't be had so is left
            # out. Anything else (a sound, a font, a PDF or an SVG, which Qt
            # would have redrawn as a picture) is kept as it came, for the
            # card's sake.
            if raster_kind(found.data) is not None:
                if id(found.data) not in cleaned:
                    cleaned[id(found.data)] = _without_hidden_data(found.data, clean)
                data = cleaned[id(found.data)]
                if data is None:
                    unclean.append(found.name or PurePosixPath(found.path).name or found.type)
                    continue
                asset.ext = (picture_kind(data) or f".{asset.ext}").lstrip(".")
            asset.file = file_path(character, asset, taken)
            store.write(asset.file, data)
        character.tavern.assets.append(asset)
    if card.files and character.tavern.assets:
        notes.append(files_said(character.tavern.assets))
    if unclean:
        several = len(unclean) > 1
        notes.append(
            f"{_count(len(unclean), 'picture')} on the card ({', '.join(unclean)}) "
            f"{'are' if several else 'is'} in a format SealedLore can't remove hidden data "
            f"from, so {'they were' if several else 'it was'} left out and won't go out with "
            "the card again."
        )
    depth = card.extensions.get("depth_prompt")
    if isinstance(depth, Mapping) and str(depth.get("prompt") or "").strip():
        notes.append(
            "The card's note for SillyTavern to put deeper in a chat is now the end of its "
            "description."
        )
    return character, notes
