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

from sealedlore.models.overmap import OvermapState
from sealedlore.models.plot import Chronicle
from sealedlore.models.visual import Creature, DoorState, NpcGoal, PersonSheet

ThingVerb = Literal["use", "take", "destroy", "eat"]
PersonVerb = Literal["talk", "look", "attack"]
MapVerb = Literal[
    "use",
    "take",
    "destroy",
    "eat",
    "talk",
    "look",
    "attack",
    "travel",
    "drop",
    "give",
    # Moving on the map (the author's ruling, Oct 23 2026: the map is a view
    # and a way to point; nothing moves until the turn is sent): to a spot in
    # the area, in through a building's door, out through it, by the stairs,
    # on past the area's edge.
    "walk",
    "approach",
    "enter",
    "leave",
    "stairs",
    "onward",
]
Density = Literal["sparse", "normal", "full"]
# What became of a thing (engine/session_area.py: a menu choice the author
# sent, or what the map read found the passage did; the author's rulings:
# "If something is taken, it should be recorded as with the character who
# took it. Something broken should visibly be broken.", and things the story
# adds show on the play view): taken = carried by `by`; broken = broken,
# where it stood or at `spot`; gone = eaten, used up or destroyed outright;
# dropped = lies at `spot` (put down, moved there, or put there by the story).
# seen = looked at closely (a visual story's Look): still where it was, with
# what the look found in or on it (`contents`) and the words it was seen in.
ThingState = Literal["taken", "broken", "gone", "dropped", "seen"]


class Found(BaseModel):
    """Something a look found (a visual story, engine/look.py): in or on a
    thing, or lying in the area. Real in the game from then on: the
    record's contents, or a thing of its own on the floor."""

    name: str
    count: int = 1
    # A word for what kind of thing it is (food, weapon, tool, clothing,
    # key, fuel, paper…), as the look said it.
    kind: str = ""
    note: str = ""


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
    # What a look found in or on it, and what it was seen to be (a visual
    # story's Look; engine/look.py). Kept whatever the state.
    contents: list[Found] = Field(default_factory=list)
    seen: str = ""
    # What was taken out of it or off it since (names), and, for a find
    # lying beside the thing it was found at, that thing's placement id:
    # a later look is told what is gone, so the words it was seen in before
    # don't bring it back.
    taken_out: list[str] = Field(default_factory=list)
    found_at: str = ""
    # A visual story's world kit decided what it holds (its item table was
    # rolled): a look may tell of it, never add to it. And whether the
    # player has looked inside (searched it, or looked at it closely): its
    # menu lists what it holds only then.
    stocked: bool = False
    searched: bool = False
    # A visual story's items (engine/visual_rules.py): how many of it, and
    # its kind as the rules read it ("" until asked).
    count: int = 1
    kind: str = ""
    # A visual story's keeper may make a thing into what it has become
    # (engine/keeper.py `transform`: a repaired truck): words for its state
    # ("works", "fuelled", "lit") and the verbs it now offers in its menu
    # ("drive", "start"), each of which goes back to the keeper as an
    # attempt when chosen. The author (Oct 24 2026): not a million small
    # mechanics, but ways for the model to manage any it is asked for.
    tags: list[str] = Field(default_factory=list)
    uses: list[str] = Field(default_factory=list)
    # A vehicle driven across a visual story's overmap: the km of fuel left
    # in its tank (None until first driven: a full tank).
    fuel_km: float | None = None


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
    # The tiles the author's character walks in the area they set out from
    # (a walk, a walk up to someone or something, the way to a door), for the
    # play view to show once the turn goes; [] for a journey.
    steps: list[tuple[int, int]] = Field(default_factory=list)
    # Give: who the thing is handed to (as the map names them). Onward: the
    # side of the area walked past ("north").
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
    # A visual story's own (models/visual.py, engine/visual_rules.py): the
    # doors as play has them, the dead, the player's wounds (hits taken),
    # the people and creatures reported seen, the areas whose flavour line
    # was said, and whether the design's creatures and items were laid.
    doors: dict[str, DoorState] = Field(default_factory=dict)
    creatures: dict[str, Creature] = Field(default_factory=dict)
    wounds: int = 0
    dead: bool = False
    seen_now: list[str] = Field(default_factory=list)
    flavour_said: list[str] = Field(default_factory=list)
    laid_out: bool = False
    # The design's places whose dead, things and locks have been laid, by
    # name: a place the overmap makes later is laid once it is on the map.
    laid_places: list[str] = Field(default_factory=list)
    # The areas whose containers and floor a world kit's item tables have
    # stocked (engine/kits.py), the first time the player entered each.
    stocked: list[str] = Field(default_factory=list)
    # The people (engine/npc.py): each conversation so far ([who, words]
    # pairs, the latest last), what each remembers (their own lines), what
    # each saw the player do, their mood, what they are doing of their own
    # accord, and who travels with the player.
    talks: dict[str, list[list[str]]] = Field(default_factory=dict)
    memories: dict[str, list[str]] = Field(default_factory=dict)
    witnessed: dict[str, list[str]] = Field(default_factory=dict)
    moods: dict[str, str] = Field(default_factory=dict)
    goals: dict[str, NpcGoal] = Field(default_factory=dict)
    companions: list[str] = Field(default_factory=list)
    # The people greeted (the encounter call made) in each area, by
    # "<name>@<area id>", so a greeting is once a visit.
    greeted: list[str] = Field(default_factory=list)
    # A person who attacked and lost: down.
    people_down: list[str] = Field(default_factory=list)
    # The design's people stood on the map so far (one whose place the plot
    # still hides is stood once it is on the map).
    placed: list[str] = Field(default_factory=list)
    # The plot's state (engine/keeper.py): a visual story has no passages
    # to snapshot it on, so it lives here; its minutes follow the clock.
    chronicle: Chronicle | None = None
    # The plot places the player has been told of as entered (a moment of
    # the plot is asked once per arrival), and the days the keeper has seen.
    plot_arrivals: list[str] = Field(default_factory=list)
    plot_days_seen: list[int] = Field(default_factory=list)
    # The story's end, when the keeper ended it: the words.
    ended: str = ""
    # A person's state in the keeper's words ("held by John", "bound",
    # "unconscious"): shown to the model that speaks for them, and one held
    # or out cold stays where they are (engine/session_visual.py).
    person_tags: dict[str, list[str]] = Field(default_factory=dict)
    # A place's state the keeper set ("burnt out"), by place id: a visual
    # story has no passages to carry a map delta, so the fold reads these
    # (engine/session_world.py `_fold`).
    place_states: dict[str, str] = Field(default_factory=dict)
    # People the keeper brought into the story (a traveller met on the road,
    # a bandit at a roadblock: engine/keeper.py "stranger"), with their sheet:
    # each has a supporting card too, so the game can stand and talk to them.
    strangers: dict[str, PersonSheet] = Field(default_factory=dict)
    # The wider world (models/overmap.py): places found, cells explored, and
    # what the traveller last ate, drank and slept.
    overmap: OvermapState = Field(default_factory=OvermapState)


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
