"""A picture the story made, or a part of it, kept as a card's own (the
author, Oct 24 2026): the place in a picture of the scene becomes the
"official" picture of that place, or the person in it their portrait, cropped
or highlighted first; and a new picture may be drawn from that part (a
portrait of the person highlighted, the place without the people in it).

Pure: where the kept picture goes on its card, and the direction the picture
prompt writer is given to draw from it. The window crops and writes the file
(gui/picture_use.py); `StorySession.keep_card_picture` puts it on the card.
"""

from __future__ import annotations

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.lore import LoreEntry

Card = Character | LoreEntry

# How a kept picture is placed on its card: as the picture that stands for
# it (a character's portrait, shown first; a place's current picture, the
# newest, shown behind the story), among its other pictures, or as a
# reference a new picture is then drawn from.
KEEP_MAIN = "main"
KEEP_OTHER = "other"
KEEP_DRAW = "draw"
KEEPS = (KEEP_MAIN, KEEP_OTHER, KEEP_DRAW)

# What a part kept as a character's portrait is captioned; a place's takes
# the place captions (engine/places.py).
PORTRAIT_CAPTION = "portrait"
# A part kept to draw from, so the writer's catalogue says what it is.
FROM_SCENE_CAPTION = "from a picture of the story"


def keep_picture(card: Card, ref: ImageRef, how: str) -> None:
    """Put `ref` on the card as `how` says (one of KEEPS). A character's
    main picture is their portrait, the first; a place's is its newest, the
    last (`places.newest_picture`), the one shown behind the story. Among
    the others: a character's goes after the portrait and the rest; a
    place's goes before its current picture, so that stays current. A
    reference to draw from goes last on either, and the drawing then takes
    the main place."""
    assert how in KEEPS, how
    pictures = card.reference_images
    if isinstance(card, Character):
        if how == KEEP_MAIN:
            pictures.insert(0, ref)
        else:
            pictures.append(ref)
        return
    if how == KEEP_OTHER and pictures:
        pictures.insert(len(pictures) - 1, ref)
    else:
        pictures.append(ref)


def from_picture_direction(card: Card, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The direction for a picture drawn from a part of a picture of the
    story (sent as image 1): a portrait of the person shown, or the place
    shown with nobody in it."""
    if isinstance(card, Character):
        return texts.fill("pictures.from_picture.person", name=card.name)
    return texts.fill("pictures.from_picture.place", place=card.title)


def main_words(card: Card) -> str:
    """What the card's main picture is called: its portrait, or the picture
    of the place."""
    if isinstance(card, Character):
        return f"{card.name}'s portrait"
    return f"the picture of {card.title}"
