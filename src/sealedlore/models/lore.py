"""Lorebook entries."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sealedlore.ids import new_id
from sealedlore.models.image import ImageRef


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
    SillyTavern's does (Lore page, Entries fire). SillyTavern's defaults."""

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
