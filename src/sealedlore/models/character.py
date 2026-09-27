"""Characters.

The user never enters numeric stats: competence is either freeform prose or a
small set of user-named domains mapped to a coarse tier.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id
from sealedlore.models.image import ImageRef

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
    origin: CharacterOrigin = "author"
    # The work a canonical character comes from ("Night of the Living Dead (1968)").
    # The model already knows them; the card only records what this story changed.
    canon: str | None = None
    # In a story with a plot: kept from every model and the author's panels
    # until an event brings it in (models/plot.py).
    plot_hidden: bool = False
