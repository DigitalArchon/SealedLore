"""Lorebook entries."""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sealedlore.ids import new_id
from sealedlore.models.image import ImageRef


def lore_fingerprint(text: str) -> str:
    """Of the text a condensed version is written from: a change to it makes
    the condensed version out of date (`LoreEntry.condensed_now`)."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class TavernEntry(BaseModel):
    """An entry as SillyTavern had it (its keys, order, groups, timing…), kept
    when a lorebook is brought in so a story that fires its lore as
    SillyTavern does (engine/tavern_firing.py) reads every setting. Its
    names filled in ({{char}}, {{user}}), its own field names as they came:
    a world file's (`numbered`) or a card's book's."""

    numbered: bool = False
    fields: dict[str, Any] = Field(default_factory=dict)
    # Its @@activate / @@dont_activate, read off the head of its text.
    decorators: list[str] = Field(default_factory=list)


class TavernWorldInfo(BaseModel):
    """SillyTavern's World Info settings, a story's own when its lore fires as
    SillyTavern's does (Lore page, Lore fires by). SillyTavern's defaults."""

    model_config = ConfigDict(validate_assignment=True)

    scan_depth: int = Field(default=2, ge=0, le=1000)
    budget_percent: int = Field(default=25, ge=1, le=100)
    budget_cap: int = Field(default=0, ge=0)
    min_activations: int = Field(default=0, ge=0)
    min_activations_depth_max: int = Field(default=0, ge=0)
    max_recursion_steps: int = Field(default=0, ge=0)
    recursive: bool = True
    case_sensitive: bool = False
    match_whole_words: bool = True
    use_group_scoring: bool = False
    include_names: bool = True


class LoreEntry(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str
    content: str
    keywords: list[str] = Field(default_factory=list)
    always_on: bool = False
    priority: int = 0
    enabled: bool = True
    # Kept from every model until the author's own turn or Direction names it
    # (its title or a keyword) on the path being played: for what hasn't
    # happened yet, which the storyteller otherwise treats as already true.
    until_mentioned: bool = False
    embedding_hash: str | None = None
    # Pictures of the thing (a ship, a place) sent with an image prompt.
    reference_images: list[ImageRef] = Field(default_factory=list)
    # In a story with a plot: kept from every model and the author's panels
    # until an event brings it in (models/plot.py).
    plot_hidden: bool = False
    # Where an entry brought in from outside came from, in words ("Elena
    # Ruiz's lorebook", "the lorebook Hellsville.json"), for the Lore page to
    # say. Never sent to a model; blank for an entry written here.
    came_from: str = ""
    # The entry as SillyTavern had it, when it came from a lorebook: what a
    # story firing its lore as SillyTavern does reads (`TavernEntry`).
    tavern: TavernEntry | None = None
    # A shorter version of the entry, written by the authoring model (or by
    # the author) beside it, never in its place: sent instead of the whole
    # when always-on lore is more than the lore budget holds, and the story
    # says to (engine/lore_condense.py). `condensed_from` is the fingerprint
    # of the text it was written from: an edited entry's old one is never sent.
    condensed: str | None = None
    condensed_from: str | None = None

    def condensed_now(self) -> str | None:
        """The condensed version, while it is of the entry as it stands."""
        if self.condensed and self.condensed_from == lore_fingerprint(self.content):
            return self.condensed
        return None
