"""A visual story's own state and design (the author, Oct 24 2026; the
design in SealedLoreDocs/design/visual-stories.md): the rules the program
keeps, step 2. Pydantic only.

The design (`VisualDesign`) is the predesigned layer a world ships beside
its plot file (`<plot>.visual.json`, storage/visual_design.py): locked
doors and their keys, the dead and where they start, things laid in the
areas, a flavour line per area, and the cast's combat abilities. It is
copied onto the story (`Story.visual_design`) so a story is whole on its
own. The state (`AreaState.doors`, `.creatures`, `.wounds`…, models/area.py)
is what play changes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id
from sealedlore.models.overmap import Overmap

# The kinds of thing the rules know (engine/visual_rules.py `kind_of`).
Kind = Literal[
    "food",
    "drink",
    "weapon",
    "tool",
    "light",
    "key",
    "fuel",
    "clothing",
    "medicine",
    "ammunition",
    "paper",
    "container",
    "vehicle",
    "thing",
]

CreatureState = Literal["wander", "drawn", "chase", "down"]
# What a person may do of their own accord (engine/npc.py): the model
# chooses one from a sheet; the program carries it out on the map.
Intent = Literal[
    "stay", "follow", "lead_to", "attack", "retreat", "hide", "fetch_help", "leave", "give"
]
INTENTS: tuple[str, ...] = (
    "stay",
    "follow",
    "lead_to",
    "attack",
    "retreat",
    "hide",
    "fetch_help",
    "leave",
    "give",
)


class DoorState(BaseModel):
    """A door as play has it, keyed `"<area id>:<x>,<y>"` (the door tile in
    the outdoor area, or a building's exit tile indoors)."""

    locked: bool = False
    # What opens it (a key's name, matched by `kind_of`'s key words), or ""
    # for a lock that can only be forced.
    key: str = ""
    # Sealed by the plot until it opens it (a hidden place): no key, no force.
    sealed: bool = False
    # Forced open: broken for good.
    forced: bool = False


class Creature(BaseModel):
    """One of the dead (the author: programmatic, "more like animals")."""

    id: str = Field(default_factory=new_id)
    kind: str = "the dead"
    name: str = "one of the dead"
    area: str
    x: int
    y: int
    state: CreatureState = "wander"
    # Where it is drawn to (a noise) or chasing (the player's last seen tile).
    goal_x: int | None = None
    goal_y: int | None = None
    ability: int = 4
    # Hits it takes before it is down.
    hits: int = 1


class Shift(BaseModel):
    """Where a person is at these hours of the day when nothing else moves
    them (engine/session_visual.py `_keep_schedules`): "20:00" to "06:00"
    at The church. A shift may cross midnight."""

    start: str = "00:00"
    end: str = "24:00"
    place: str


class PersonSheet(BaseModel):
    """Who a person is, for the model that speaks for them (engine/npc.py):
    the design's words, over the character's card."""

    # A line for the card of a person the plot file has no card for (a
    # townsperson): the card is made from it when the story is made
    # (storage/visual_design.py `cards_for_design`).
    card: str = ""
    personality: str = ""
    goals: str = ""
    # How they take to a stranger, and what they want from one.
    wants: str = ""
    # What they know (and may tell), and what they never tell or do.
    knows: str = ""
    never: str = ""
    # How they speak.
    speech: str = ""
    # Where they are when the story begins (a place by name); "" for
    # someone the plot brings in later.
    place: str = ""
    # Their combat ability.
    ability: int = 5
    # What they carry, if anything is said of it.
    carries: list[str] = Field(default_factory=list)
    # Where they are through the day, when nothing else moves them.
    schedule: list[Shift] = Field(default_factory=list)


class NpcGoal(BaseModel):
    """What a person is doing of their own accord: their current intent
    and, for one that moves them, where to (a tile in an area)."""

    intent: str = "stay"
    area: str | None = None
    x: int | None = None
    y: int | None = None
    # Who the intent is toward (follow, attack, give): the player, mostly.
    toward: str = ""
    # What a give hands over.
    what: str = ""


class LockedDoor(BaseModel):
    """A door the design locks: of this place (by name), opened by `key`."""

    place: str
    key: str = ""
    sealed: bool = False


class CreatureSpawn(BaseModel):
    kind: str = "the dead"
    # The area (a place by name) they start in; a count, stood apart.
    place: str
    count: int = 1
    ability: int = 4


class ItemSpawn(BaseModel):
    """A thing the design lays in an area, with its kind and count."""

    name: str
    place: str
    count: int = 1
    kind: str = ""
    # In or on a thing of this name there (its contents), else on the floor.
    inside: str = ""


class ThingNote(BaseModel):
    """What the design says of a thing the plan already draws (by name, in
    the area of a place): its standing state in words (`tags`: "burning",
    "fed within the hour") and what is seen of it, so the keeper and the
    people know what the picture shows. Live (the author, Oct 24 2026), the
    creek crossing's campfire was painted burning and the model was told
    nothing of it."""

    name: str
    place: str
    tags: list[str] = Field(default_factory=list)
    seen: str = ""


class LogLine(BaseModel):
    """A line of a visual story's log (what happened, what was said), kept
    with the story so the game reopens on what has been (the author, Oct 25
    2026: the log came back as the opening alone). `kind` is the window's
    colour: report, note, you, speech, sound."""

    kind: str = "report"
    text: str


# The log lines a story keeps: the newest, once it runs past this.
LOG_KEPT = 400


class VisualDesign(BaseModel):
    locked: list[LockedDoor] = Field(default_factory=list)
    creatures: list[CreatureSpawn] = Field(default_factory=list)
    items: list[ItemSpawn] = Field(default_factory=list)
    # The state of things the plans already draw (a campfire that burns).
    things: list[ThingNote] = Field(default_factory=list)
    # A line for the log the first time an area is entered, by place name.
    flavour: dict[str, str] = Field(default_factory=dict)
    # Combat ability by character name; the rest take `default_ability`.
    abilities: dict[str, int] = Field(default_factory=dict)
    default_ability: int = 5
    # Hits the player takes before they die.
    player_hits: int = 3
    # How warm the player is dressed to begin with (a coat on): a journey's
    # warmth is this and each kind of warm thing carried (engine/overmap.py).
    dressed: int = 1
    # The people (by the character's name) and who they are.
    people: dict[str, PersonSheet] = Field(default_factory=dict)
    # The weather the play view draws outdoors ("snow", "rain", "fog"; ""
    # for clear), since a visual story has no passages to say it.
    weather: str = ""
    # The wider world the story's map stands in (models/overmap.py): its
    # regions, roads and places, for journeys and exploring.
    overmap: Overmap | None = None
