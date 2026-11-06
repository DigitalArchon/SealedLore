"""Characters.

The user never enters numeric stats: competence is either freeform prose or a
small set of user-named domains mapped to a coarse tier.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id
from sealedlore.models.collection import CardLink, CollectionInfo
from sealedlore.models.image import ImageRef
from sealedlore.models.tavern import TavernFields

CompetenceTier = Literal["none", "poor", "competent", "skilled", "expert", "legendary"]
# Who wrote the card: the author, or the model from what the story established.
CharacterOrigin = Literal["author", "model"]


class Competence(BaseModel):
    notes: str = ""
    tiers: dict[str, CompetenceTier] = Field(default_factory=dict)


class Character(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    aliases: list[str] = Field(default_factory=list)
    summary: str | None = None
    full_description: str | None = None
    # A SillyTavern card's own personality field: told with the description
    # (engine/rules.py), as "Personality: …" after it.
    personality: str | None = None
    voice_notes: str | None = None
    competence: Competence = Field(default_factory=Competence)
    # Who voices this character, and what the storyteller is told about them.
    # `is_player_available` off means the author never plays them, so the
    # storyteller voices them outright — being told so is the point: a hundred
    # turns of the author writing someone teaches the model they are the
    # author's, and nothing in the prompt used to say otherwise.
    # `author_only` is the opposite end: never voiced by the storyteller, even
    # on turns the author is holding someone else.
    is_player_available: bool = True
    author_only: bool = False
    portrait_path: str | None = None
    # Pictures sent with an image prompt so they look the same every time.
    reference_images: list[ImageRef] = Field(default_factory=list)
    # Their pictures as a sheet on the cast board (engine/look_sheet.py): the
    # looks the author has planned that no picture has yet, and the
    # expressions across (none: the usual few). The board's alone; no model
    # sees them.
    sheet_looks: list[str] = Field(default_factory=list)
    sheet_expressions: list[str] = Field(default_factory=list)
    origin: CharacterOrigin = "author"
    # The work a canonical character comes from ("Night of the Living Dead (1968)").
    # The model already knows them; the card only records what this story changed.
    canon: str | None = None
    # In a story with a plot: kept from every model and the author's panels
    # until an event brings it in (models/plot.py).
    plot_hidden: bool = False
    # A SillyTavern card's fields a story has no place for (models/tavern.py):
    # None for a character never on a card.
    tavern: TavernFields | None = None
    # Kept in the author's own characters (storage/collection.py), with which
    # version of its character it is; None in a story.
    collection: CollectionInfo | None = None
    # In a story, brought in from the author's own characters: which card,
    # and what the story was given from it (engine/card_link.py).
    linked: CardLink | None = None
    # The card in fewer words, sent in its place when the cast's cards don't
    # fit their share of the context (engine/card_condense.py), with the
    # fingerprint of the card it was written from: a card edited since has an
    # out-of-date one, which is never sent.
    condensed: str | None = None
    condensed_from: str | None = None
    # Never sent condensed, however large the cast (engine/cast_fit.py): the
    # author's own mark for a main character.
    keep_whole: bool = False
