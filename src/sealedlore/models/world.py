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
    # A change to the place itself the story showed ("blown up", "flooded"):
    # its art is redrawn for it, as an edit of the last (engine/world_art.py).
    state: str = ""
    # Where it is on the map, in metres east (x) and north (y) of the first
    # place, and how wide: set by the program when the place first appears
    # (engine/world_layout.py) and never moved after. Once the story has been
    # somewhere, that is where it is.
    x: float | None = None
    y: float | None = None
    size: float | None = None


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


# --- The map's art (engine/world_art.py), kept in world_art.json -------------
#
# Drawn once in the background by the map art model and never redrawn: once
# the story has been somewhere, that is what it looks like. Not branch state:
# art is keyed by the place or thing it shows, and outlives the passages.


class WorldSprite(BaseModel):
    """A small picture of a place or thing: a 64×64 SVG, cleaned, that the
    view turns into pixel art in the story's palette."""

    id: str = Field(default_factory=new_id)
    subject: Literal["place", "thing"]
    subject_id: str
    name: str
    # The state it shows ("burnt out"); a changed state is drawn as an edit
    # of the sprite before it (`from_sprite`), so it stays the same object.
    state: str = ""
    svg: str
    from_sprite: str | None = None
    model: str = ""
    made_at: str = ""


class PlaceLayout(BaseModel):
    """A place's ground plan, as tiles (engine/world_art.py's legend), drawn
    the first time the story is there, and again, as an edit, for each state
    the story puts it in."""

    place_id: str
    state: str = ""
    # The shapes it was drawn from: what a changed state is an edit of.
    spec: dict = Field(default_factory=dict)
    rows: list[str]
    objects: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, list[int]] = Field(default_factory=dict)
    model: str = ""
    made_at: str = ""


class TerrainPatch(BaseModel):
    """The land around a place the story reached, as tiles: a square `side`
    metres wide centred on (`x`, `y`). The map grows by these: the first time
    the story reaches unmapped country, its surroundings are drawn, and are
    part of the map from then on."""

    id: str = Field(default_factory=new_id)
    place_id: str
    x: float
    y: float
    side: float
    rows: list[str]
    # What its open ground is (snow, grass, dirt, sand, rock, space): how the
    # view colours it.
    ground: str = ""
    model: str = ""
    made_at: str = ""


class WorldArt(BaseModel):
    sprites: list[WorldSprite] = Field(default_factory=list)
    # By place id, then by state ("" before any change). Art is not branch
    # state: a barn blown up on one branch and standing on another has both.
    layouts: dict[str, dict[str, PlaceLayout]] = Field(default_factory=dict)
    terrain: list[TerrainPatch] = Field(default_factory=list)
    # Jobs that failed, by key, and how often: one that keeps failing is let be.
    failures: dict[str, int] = Field(default_factory=dict)

    def sprite_for(self, subject_id: str, state: str | None = None) -> WorldSprite | None:
        """The sprite of a place or thing in `state` (any state when None),
        the latest of them."""
        found = [s for s in self.sprites if s.subject_id == subject_id and state in (None, s.state)]
        return found[-1] if found else None

    def layout_for(self, place_id: str, state: str | None = None) -> PlaceLayout | None:
        """A place's layout in `state`, or, when None, the latest drawn."""
        drawn = self.layouts.get(place_id) or {}
        if state is not None:
            return drawn.get(state)
        return list(drawn.values())[-1] if drawn else None
