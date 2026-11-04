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

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.character import Character
from sealedlore.models.collection import CollectionInfo
from sealedlore.models.image import ImageRef
from sealedlore.models.story import Story
from sealedlore.models.tavern import TavernFields
from sealedlore.storage.image_meta import picture_kind
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
    return copy


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
    used = {ref.file for other in cast for ref in other.reference_images}
    return [ref.file for ref in character.reference_images if ref.file not in used]


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
    to a picture whose hidden data couldn't be stripped (a re-encode).
    And what didn't come along, in words."""
    character = character_from_card(card, lambda text: text, for_story=False)
    character.collection = CollectionInfo(came_from=came_from)
    notes: list[str] = []

    def keep(data: bytes, caption: str = "") -> ImageRef | None:
        data = clean(data) if clean is not None else data
        suffix = picture_kind(data)
        if suffix is None:
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
    assets = card.fields.get("assets")
    others = [
        asset
        for asset in (assets if isinstance(assets, list) else ())
        if isinstance(asset, Mapping) and asset.get("type") not in ("icon", "emotion")
    ]
    if others:
        notes.append(
            f"{len(others)} other picture{'s' if len(others) > 1 else ''} on the card "
            "(a background, a picture its greetings show) "
            f"{'were' if len(others) > 1 else 'was'} left out: SealedLore keeps a "
            "character's own pictures."
        )
    depth = card.extensions.get("depth_prompt")
    if isinstance(depth, Mapping) and str(depth.get("prompt") or "").strip():
        notes.append(
            "The card's note for SillyTavern to put deeper in a chat is now the end of its "
            "description."
        )
    return character, notes
