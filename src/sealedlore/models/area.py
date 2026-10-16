"""Map-first play (SealedLoreDocs design/map-first.md): where everyone stands
on the map's tiles, the map's clock, and how each room is furnished.

The map is the law of space. A place with a plan is an area, a grid of tiles
(engine/area.py builds it from the plan, the program's rules and the room's
furnishing); the author's character walks on it with no model call, and
everyone else stands where the story last put them. Where they stand is a
snapshot on the nodes, like the scene (`NodeMeta.area`): the state anywhere is
the nearest snapshot above it, so branches, retakes and deletions need no
bookkeeping. Walking between turns is kept on the story (`Story.area_live`)
until the next turn is sent, which carries it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ThingVerb = Literal["use", "take", "destroy", "eat"]
PersonVerb = Literal["talk", "look", "attack"]
MapVerb = Literal["use", "take", "destroy", "eat", "talk", "look", "attack", "travel"]
Density = Literal["sparse", "normal", "full"]

THING_VERBS: tuple[str, ...] = ("use", "take", "destroy", "eat")
PERSON_VERBS: tuple[str, ...] = ("talk", "look", "attack")


class Spot(BaseModel):
    """A tile in an area: the area is the id of the place whose plan it is."""

    area: str
    x: int
    y: int


class MapAction(BaseModel):
    """The menu choice a turn came from (the author's ruling: it fills the
    composer, so the turn can be read and changed before it is sent). The
    storyteller is told what the choice means (`map.action.*`)."""

    verb: MapVerb
    # Who or what it is done to, as the map names them.
    target: str
    # A placement's id, a person's name, or a place's id (travel).
    target_id: str = ""
    # The turn's text as the menu wrote it: an author who replaces it has
    # written a turn of their own, and the action no longer applies.
    text: str = ""


class AreaState(BaseModel):
    """Where everyone stands, and the map's clock, after a node."""

    spots: dict[str, Spot] = Field(default_factory=dict)
    # The map's clock in seconds since the story began: walking and
    # travelling, and the time the passages tell (the read). With a plot, the
    # plot's clock is the story's and this follows it.
    seconds: int = 0
    # Since the last passage: how long the author's character walked or
    # travelled, and how, for the storyteller ("about twenty minutes on
    # foot, from the farm to Hellsville").
    walked: int = 0
    walked_from: str = ""
    walked_words: str = ""
    # The menu choice this turn came from, if any (an author's turn only).
    action: MapAction | None = None


class LiveArea(BaseModel):
    """Walking since the last turn, at the leaf it was done at. Kept on the
    story so closing the app loses nothing; a leaf moved meanwhile (another
    branch, a take swapped) makes it stale, and it is dropped."""

    leaf_id: str | None = None
    state: AreaState = Field(default_factory=AreaState)


class FurnishItem(BaseModel):
    """One kind of furniture a room holds: its footprint in tiles, where in
    the room it stands, how many at normal density, and what can be done
    with it."""

    name: str
    w: int = 1
    h: int = 1
    where: Literal["wall", "middle", "corner"] = "wall"
    count: int = 1
    verbs: list[ThingVerb] = Field(default_factory=list)


class FurnishRoom(BaseModel):
    # The room's number in the plan's room list (engine/area.py `rooms`).
    room: int
    purpose: str = ""
    items: list[FurnishItem] = Field(default_factory=list)


class FurnishPlan(BaseModel):
    """How a place's rooms are furnished, as a model planned it. The program
    places the pieces (engine/area.py), so the same plan furnishes the same
    way every time, at the story's density."""

    place_id: str
    state: str = ""
    rooms: list[FurnishRoom] = Field(default_factory=list)
    model: str = ""
    made_at: str = ""
