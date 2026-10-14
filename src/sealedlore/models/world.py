"""The story's map: places, how they connect, who is where, and the things
that matter (engine/world.py keeps it, design notes in SealedLoreDocs'
design/map.md).

What a passage changed is stored on it (`NodeMeta.world`, a `WorldDelta`);
the map anywhere is every delta on the path to it, applied in order, so each
branch has its own and nothing needs undoing when a branch is left. Ids are
the program's; the model names places and people, code resolves the names.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id

PlaceKind = Literal["region", "settlement", "building", "room", "area", "route", "vehicle", "site"]


class WorldPlace(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    # Other names the story has used for it ("the station", "DS9"): a later
    # read that names it so finds this place rather than making a second.
    aliases: list[str] = Field(default_factory=list)
    kind: PlaceKind | None = None
    # The place it is part of (a room in a building), by id.
    inside: str | None = None
    what: str = ""


class WorldLink(BaseModel):
    """How two places sit: `direction` is the way from `a` to `b`, in the
    text's words ("north", "across the street"); `distance` likewise ("five
    miles", "two hours on foot"). Never a number the text didn't give."""

    a: str
    b: str
    direction: str | None = None
    distance: str | None = None


class WorldThing(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    # At a place (by id), or carried by someone (by name, as the map has them).
    at: str | None = None
    carried_by: str | None = None
    state: str = ""


class WorldDelta(BaseModel):
    """What one passage changed. Places and things are given whole (a changed
    one replaces the old by id); `where` maps a person's name to a place id."""

    places: list[WorldPlace] = Field(default_factory=list)
    links: list[WorldLink] = Field(default_factory=list)
    where: dict[str, str] = Field(default_factory=dict)
    things: list[WorldThing] = Field(default_factory=list)
    # A stated distance that disagrees with the one the map already had: the
    # map keeps the first, and says so here.
    conflicts: list[str] = Field(default_factory=list)

    def empty(self) -> bool:
        return not (self.places or self.links or self.where or self.things or self.conflicts)


class WorldMap(BaseModel):
    """The map at one point of the story: never stored, always folded from
    the deltas on the path (`engine.world.world_at`)."""

    places: dict[str, WorldPlace] = Field(default_factory=dict)
    links: list[WorldLink] = Field(default_factory=list)
    where: dict[str, str] = Field(default_factory=dict)
    things: dict[str, WorldThing] = Field(default_factory=dict)
    # Every place each person has been, by name: the author's character's is
    # what the fog lifts from.
    visited: dict[str, list[str]] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)
