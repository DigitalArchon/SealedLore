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
MapVerb = Literal[
    "use", "take", "destroy", "eat", "talk", "look", "attack", "travel", "drop", "give"
]
Density = Literal["sparse", "normal", "full"]
# What became of a thing (engine/session_area.py: a menu choice the author
# sent, or what the map read found the passage did; the author's rulings:
# "If something is taken, it should be recorded as with the character who
# took it. Something broken should visibly be broken.", and things the story
# adds show on the play view): taken = carried by `by`; broken = broken,
# where it stood or at `spot`; gone = eaten, used up or destroyed outright;
# dropped = lies at `spot` (put down, moved there, or put there by the story).
ThingState = Literal["taken", "broken", "gone", "dropped"]

THING_VERBS: tuple[str, ...] = ("use", "take", "destroy", "eat")
PERSON_VERBS: tuple[str, ...] = ("talk", "look", "attack")
# The verbs that change a thing's record when the turn is sent (Use leaves
# it alone), and the two done to a carried thing (the Carrying ▾ menu).
RECORD_VERBS: tuple[str, ...] = ("take", "destroy", "eat", "drop", "give")
CARRY_VERBS: tuple[str, ...] = ("drop", "give")


class Spot(BaseModel):
    """A tile in an area: the area is the id of the place whose plan it is."""

    area: str
    x: int
    y: int
    # Stood on a window of open country for this place (by id) because it
    # had no plan yet: once it has one, they stand in it. Live, a new
    # world's opening was read before any plan was drawn; everyone stood on
    # the land, and when the plans came the author's character stayed out
    # in the trading post's yard while the story had her in the cabin. ""
    # for a spot they walked to or were put on as it is.
    for_place: str = ""


class ThingRecord(BaseModel):
    """What became of a thing on the play view, kept on each snapshot under
    its placement id (a plan's object, a piece of furniture, or one of the
    map's things by its id): the area shows it (gone from the floor, broken,
    lying where it was put) and the storyteller is told who carries what.
    Written by the menu's choices (code decides, no model call) and by what
    the map read found the passage did."""

    state: ThingState
    name: str
    # Taken: who carries it (as the map names them).
    by: str = ""
    # Dropped (and a broken thing that was moved): the tile it lies on, its
    # top-left corner when it is bigger than a tile.
    spot: Spot | None = None
    # Its footprint in tiles: a table moved is as big where it stands now.
    w: int = 1
    h: int = 1


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
    # Travel: where the author's character will stand (None: a place with no
    # area yet) and how long the way takes, applied when the turn is sent
    # (`stamp_turn`), never before: a turn rewritten travels nowhere.
    spot: Spot | None = None
    seconds: int = 0
    # Give: who the thing is handed to (as the map names them).
    to: str = ""
    # The thing's record before the turn's own was written
    # (`_record_action`; None for none), so Take back puts that back and
    # leaves every other record standing: the author's corrections made
    # before the turn (an object placed with Objects…, a thing dragged)
    # used to jump back to the plan's spots (the review, Oct 20 2026).
    was: ThingRecord | None = None


class Mark(BaseModel):
    """A spot the author marked on the map in their turn (engine/marks.py):
    the token as the turn writes it, the point in metres (east, north), the
    place it lies in, the tile where that place has an area, and what is
    there in words, as the storyteller is told it."""

    token: str
    x: float
    y: float
    place_id: str | None = None
    spot: Spot | None = None
    words: str = ""


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
    # The spots the turn marks on the map (an author's turn only).
    marks: list[Mark] = Field(default_factory=list)
    # What became of the things the menu's choices were done to, by
    # placement id: carried like the spots, settled with them.
    things: dict[str, ThingRecord] = Field(default_factory=dict)


class LiveArea(BaseModel):
    """Walking since the last turn, at the leaf it was done at. Kept on the
    story so closing the app loses nothing; a leaf moved meanwhile (another
    branch, a take swapped) makes it stale, and it is dropped."""

    leaf_id: str | None = None
    # Whose walking it is: taking up another character drops it.
    held: str | None = None
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
