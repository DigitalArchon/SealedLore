"""Lorebook entries."""

from __future__ import annotations

from pydantic import BaseModel, Field

from sealedlore.ids import new_id
from sealedlore.models.image import ImageRef


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
