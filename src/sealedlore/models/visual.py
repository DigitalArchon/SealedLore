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
    "thing",
]

CreatureState = Literal["wander", "drawn", "chase", "down"]


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


class VisualDesign(BaseModel):
    locked: list[LockedDoor] = Field(default_factory=list)
    creatures: list[CreatureSpawn] = Field(default_factory=list)
    items: list[ItemSpawn] = Field(default_factory=list)
    # A line for the log the first time an area is entered, by place name.
    flavour: dict[str, str] = Field(default_factory=dict)
    # Combat ability by character name; the rest take `default_ability`.
    abilities: dict[str, int] = Field(default_factory=dict)
    default_ability: int = 5
    # Hits the player takes before they die.
    player_hits: int = 3
