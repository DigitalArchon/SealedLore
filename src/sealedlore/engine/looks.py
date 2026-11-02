"""How each character is shown as the story stands: which of their pictures.

A character's pictures carry two labels (models/image.py `ImageRef`): a look
that lasts until the story changes it (normal clothes, combat armour,
injured) and the moment's expression or pose (angry, talking, in a combat
stance). Which one a character is shown with is kept on the nodes
(`NodeMeta.looks`), as the scene is: the nearest above the end of a path
counts, so it follows branches, takes and deletions with nothing to keep in
step. With none chosen, a character shows their first picture, the portrait.

Pure: the session and the window read and write through it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.node import Node

# Offered when labelling a picture, beside the labels the card already uses.
COMMON_LOOKS = ("normal clothes", "formal clothes", "armour", "sleepwear", "injured")
COMMON_EXPRESSIONS = (
    "neutral",
    "happy",
    "sad",
    "angry",
    "afraid",
    "surprised",
    "talking",
    "thinking",
    "in a fighting stance",
)


def looks_at(path: Sequence[Node]) -> dict[str, str]:
    """Who is shown with which picture at the end of `path` (character id →
    picture id): the nearest choice above it, or none."""
    for node in reversed(path):
        if node.meta.looks is not None:
            return dict(node.meta.looks)
    return {}


def shown_picture(character: Character, looks: Mapping[str, str]) -> ImageRef | None:
    """The picture a character is shown with: the chosen one while it is
    still on their card, else their first (their portrait), else none."""
    chosen = looks.get(character.id)
    if chosen:
        for ref in character.reference_images:
            if ref.id == chosen:
                return ref
    return character.reference_images[0] if character.reference_images else None


def look_words(ref: ImageRef, number: int | None = None) -> str:
    """A picture as a choice is named: its look and expression, else its
    caption, else its place among the pictures."""
    words = " · ".join(part for part in (ref.look.strip(), ref.expression.strip()) if part)
    return words or ref.caption.strip() or (f"Picture {number}" if number else "A picture")


def labels_in_use(refs: Iterable[ImageRef]) -> tuple[list[str], list[str]]:
    """The looks and expressions these pictures already carry, each once,
    in the order first used."""
    looks: dict[str, None] = {}
    expressions: dict[str, None] = {}
    for ref in refs:
        if ref.look.strip():
            looks.setdefault(ref.look.strip(), None)
        if ref.expression.strip():
            expressions.setdefault(ref.expression.strip(), None)
    return list(looks), list(expressions)


def look_change(*, look: str = "", expression: str = "", texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """What an edit of a character's picture is asked to change for a new
    look or expression (either may be blank: unchanged)."""
    parts = []
    if look.strip():
        parts.append(texts.fill("pictures.look.clothes", look=" ".join(look.split())))
    if expression.strip():
        parts.append(
            texts.fill("pictures.look.expression", expression=" ".join(expression.split()))
        )
    return " ".join(parts)


def faces_along(
    path: Sequence[Node],
    characters: Sequence[Character],
    *,
    start_present: Iterable[str] = (),
    held_id: str | None = None,
    open_span: str | None = None,
    same_person=None,
) -> dict[str, list[tuple[Character, ImageRef]]]:
    """For each passage on `path`, who was in the scene at it and the picture
    they were shown with then: (character, picture), the one the author
    played first, only those with a picture. One pass along the path, the
    scene and the looks carried as they are kept (the nearest above counts).
    A private scene's looks count only while it is open (`open_span`), as
    `LooksRuntime` reads them. `same_person(name, character)` matches the
    scene's named others to supporting cards (engine/scene_state.py)."""
    present: set[str] = set(start_present)
    others: list[str] = []
    looks: dict[str, str] = {}
    held = held_id
    faces: dict[str, list[tuple[Character, ImageRef]]] = {}
    for node in path:
        if node.meta.scene is not None:
            present = set(node.meta.scene.present_character_ids)
            others = list(node.meta.scene.present_others)
        if node.meta.looks is not None and node.meta.private_span in (None, open_span):
            looks = node.meta.looks
        if node.kind == "user" and node.meta.controlled_character_id:
            held = node.meta.controlled_character_id
        if node.kind != "assistant":
            continue
        here = []
        for character in characters:
            there = character.id in present or character.id == held
            if not there and same_person is not None:
                there = any(same_person(name, character) for name in others)
            picture = shown_picture(character, looks) if there else None
            if picture is not None:
                here.append((character, picture))
        here.sort(key=lambda item: item[0].id != held)
        faces[node.id] = here
    return faces
