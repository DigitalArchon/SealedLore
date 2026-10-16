"""The story's map: places, how they connect, who is where, and the things
that matter (engine/world.py keeps it, design notes in SealedLoreDocs'
design/map.md).

What a passage changed is stored on it (`NodeMeta.world`, a `WorldDelta`);
the map anywhere is every delta on the path to it, applied in order, so each
branch has its own and nothing needs undoing when a branch is left. Ids are
the program's; the model names places and people, code resolves the names.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from sealedlore.ids import new_id
from sealedlore.models.area import FurnishPlan

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
    # A place of an imported map named like a plot place or lore entry: kept
    # from every model while the plot keeps that entry hidden.
    lore_id: str | None = None
    # A change to the place itself the story showed ("blown up", "flooded"):
    # its art is redrawn for it, as an edit of the last (engine/world_art.py).
    state: str = ""
    # Where it is on the map, in metres east (x) and north (y) of the first
    # place, and how wide: set by the program when the place first appears
    # (engine/world_layout.py) and never moved after. Once the story has been
    # somewhere, that is where it is. The one exception is the author's
    # Direction or Narration (the author, Oct 16 2026: "The Director must be
    # able to make as broad and complete changes as possible"): what it says
    # about a place's spot, size, name or whereabouts is so (`WorldDelta`).
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
    # The ids among `places` this passage made (the rest are known places
    # it changed, carried whole). A delta read against another take's map
    # names that take's places: on a fold where they aren't, a changed copy
    # is skipped rather than brought back as a ghost (the second review).
    # None (a delta from before this): every place counts as made.
    added: list[str] | None = None
    links: list[WorldLink] = Field(default_factory=list)
    where: dict[str, str] = Field(default_factory=dict)
    things: list[WorldThing] = Field(default_factory=list)
    # A stated distance that disagrees with the one the map already had: the
    # map keeps the first, and says so here.
    conflicts: list[str] = Field(default_factory=list)
    # Places the story has now seen or told of. An imported map's places are
    # under fog until then (only its `known` ones are seen from the start);
    # a passage's own places, and any it names, are seen as it applies.
    seen: list[str] = Field(default_factory=list)
    # What the author's Direction or Narration did to places already on the
    # map (engine/world.py `resolve_reply`, on such a turn only): the ids
    # taken off the map (what was inside one goes to its parent, as does
    # anyone or anything there), the ids renamed with their new names (the
    # old name stays an alias on every fold), and the ids given a new spot
    # (what was inside one goes with it; its old ways are dropped).
    removed: list[str] = Field(default_factory=list)
    renamed: dict[str, str] = Field(default_factory=dict)
    moved: list[str] = Field(default_factory=list)

    def empty(self) -> bool:
        return not (
            self.places
            or self.links
            or self.where
            or self.things
            or self.conflicts
            or self.seen
            or self.removed
            or self.renamed
            or self.moved
        )


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
    # Places the story has seen or told of: what shows through the fog.
    seen: list[str] = Field(default_factory=list)
    # An imported map's starting place: where the author's character is
    # before any passage says otherwise.
    start: str | None = None


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


class MapPicture(BaseModel):
    """A picture an image model drew for the map (`Config.map_pictures`): a
    person's avatar, a place or a thing standing on the map (the view cuts it
    out of its plain background and shrinks it to the map's scale), or a
    place's postcard. The file is in the story's pictures, under images/map/."""

    id: str = Field(default_factory=new_id)
    # "walker": a person's sprite for map-first play, drawn from their
    # portrait; "item": a sprite of a kind of thing, by its name's slug
    # (engine/tile_sets.py `slug`), for a story whose tile set lacks it.
    subject: Literal["person", "place", "thing", "postcard", "isometric", "walker", "item"]
    # A place's or thing's id; a person's name; an item's slug.
    subject_id: str
    name: str
    # As for sprites: the state it shows, drawn from the picture before it.
    state: str = ""
    file: str
    from_picture: str | None = None
    prompt: str = ""
    model: str = ""
    made_at: str = ""
    # An isometric view: the plan it was painted from (that plan's
    # `made_at`). A plan drawn again makes the view out of date.
    plan: str = ""


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


class OwnTileSet(BaseModel):
    """A tile set drawn for one story (engine/tile_draw.py; the author
    describes it, a model designs it, an image model draws it): each asset's
    role ("floor", "out-road", "decor0") to its file under images/map/, and
    the words it was designed from, by role, with the prompt each picture was
    drawn from under "prompt:<role>". A story may keep several (the author,
    Oct 16 2026) and swap between them; `WorldArt.tile_set` names the one in
    use as "own:<key>"."""

    title: str = ""
    assets: dict[str, str] = Field(default_factory=dict)
    words: dict[str, str] = Field(default_factory=dict)
    made_at: str = ""


# The one own set a story could keep before several (`tile_assets`,
# `tile_words`, `tile_set == "own"`) is read into `tile_sets` under this key.
FIRST_OWN_SET = "own"
OWN_SET_PREFIX = "own:"


def own_set_key(tile_set: str) -> str | None:
    """The key of the story's own set a `WorldArt.tile_set` value names
    ("own:rain-slick-alleys" → "rain-slick-alleys"; the old "own" → the
    first), or None for a shipped set or no choice."""
    if tile_set == FIRST_OWN_SET:
        return FIRST_OWN_SET
    if tile_set.startswith(OWN_SET_PREFIX) and len(tile_set) > len(OWN_SET_PREFIX):
        return tile_set[len(OWN_SET_PREFIX) :]
    return None


def own_set_name(key: str) -> str:
    """The `WorldArt.tile_set` value that chooses the own set under `key`."""
    return OWN_SET_PREFIX + key


class WorldArt(BaseModel):
    sprites: list[WorldSprite] = Field(default_factory=list)
    # By place id, then by state ("" before any change). Art is not branch
    # state: a barn blown up on one branch and standing on another has both.
    layouts: dict[str, dict[str, PlaceLayout]] = Field(default_factory=dict)
    terrain: list[TerrainPatch] = Field(default_factory=list)
    # Jobs that failed, by key, and how often: one that keeps failing is let be.
    failures: dict[str, int] = Field(default_factory=dict)
    pictures: list[MapPicture] = Field(default_factory=list)
    # Pictures asked for, by UTC day ("2026-10-14"), against the daily limit.
    pictures_by_day: dict[str, int] = Field(default_factory=dict)
    # Places whose plan the author asked to be drawn again (Redraw plan…):
    # the plan they have stays until the new one is drawn.
    redraw: list[str] = Field(default_factory=list)
    # The isometric view the author chose to show over an area, by area id
    # and then by the map's state there (`world_pictures.isometric_state`):
    # an area may have several (Paint it again, pictures of one's own), and
    # the author swaps between them (the author, Oct 15 2026). Painting one
    # or bringing one in chooses it; a choice naming a picture that is gone
    # falls back to the latest.
    chosen_views: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Map-first play (models/area.py): each place's furnishing, by place id
    # and then by state, as a plan's are.
    furnishings: dict[str, dict[str, FurnishPlan]] = Field(default_factory=dict)
    # The tile set the map is drawn in (engine/tile_sets.py): a shipped set's
    # name, "own:<key>" for one drawn for this story (`tile_sets`), or "" for
    # the program to choose from the setting.
    tile_set: str = ""
    # The tile sets drawn for this story, by key (engine/tile_sets.py
    # `own_key`: from the title, "rain-slick-alleys").
    tile_sets: dict[str, OwnTileSet] = Field(default_factory=dict)
    # The author's changes to single assets, over whichever set is in use
    # (Look ▾ → Edit the tile set…): a role ("floor", "items/a-stone-well",
    # "walker/Jane Doe") to the file drawn or chosen for it; "" goes back to
    # the set's own, past anything drawn for the story.
    tile_overrides: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _one_own_set_becomes_the_first(cls, data: Any) -> Any:
        """A world_art.json from before several own sets (`tile_assets`,
        `tile_words`, `tile_set == "own"`): read into `tile_sets` under
        `FIRST_OWN_SET`, so a saved story opens on the set it had."""
        if not isinstance(data, dict) or not ("tile_assets" in data or "tile_words" in data):
            return data
        data = dict(data)
        assets = data.pop("tile_assets", None) or {}
        words = data.pop("tile_words", None) or {}
        sets = data.get("tile_sets")
        sets = dict(sets) if isinstance(sets, dict) else {}
        if assets and FIRST_OWN_SET not in sets:
            sets[FIRST_OWN_SET] = {
                "title": "This story's own",
                "assets": dict(assets),
                "words": dict(words),
            }
        data["tile_sets"] = sets
        if data.get("tile_set") == FIRST_OWN_SET:
            data["tile_set"] = f"own:{FIRST_OWN_SET}"
        return data

    def own_set_for(self, tile_set: str) -> tuple[str, OwnTileSet] | None:
        """(key, the set) when `tile_set` names one of this story's own sets
        that has assets ("own:<key>"; the old "own" is the first), else None."""
        key = own_set_key(tile_set)
        found = self.tile_sets.get(key) if key else None
        return (key, found) if key and found is not None and found.assets else None

    def furnishing_for(self, place_id: str, state: str) -> FurnishPlan | None:
        """A place's furnishing in `state`; else, when the state has no plan
        of its own (its rooms are the unchanged plan's), as the place was. A
        redrawn plan numbers its rooms afresh, so the old furnishing can't
        stand in for it (the second review): None until it is furnished."""
        drawn = self.furnishings.get(place_id) or {}
        if state and self.layout_for(place_id, state) is not None:
            return drawn.get(state)
        return drawn.get(state) or drawn.get("")

    def picture_for(
        self, subject: str, subject_id: str, state: str | None = None
    ) -> MapPicture | None:
        """The latest picture of this subject in `state` (any state when None)."""
        found = [
            p
            for p in self.pictures
            if p.subject == subject and p.subject_id == subject_id and state in (None, p.state)
        ]
        return found[-1] if found else None

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

    # What is shown for a place in a state: drawn for that state, else as
    # the place was (""), else, for a changed place only, the latest drawn.
    # A place as it was never borrows a changed state's drawing: a barn
    # blown up on one branch was drawn burning, inside and all, on the branch
    # where it stands (the review, Oct 15 2026).

    def layout_shown(self, place_id: str, state: str) -> PlaceLayout | None:
        drawn = self.layouts.get(place_id) or {}
        found = drawn.get(state) or drawn.get("")
        if found is None and state and drawn:
            found = list(drawn.values())[-1]
        return found

    def sprite_shown(self, subject_id: str, state: str) -> WorldSprite | None:
        found = self.sprite_for(subject_id, state) or self.sprite_for(subject_id, "")
        if found is None and state:
            found = self.sprite_for(subject_id)
        return found

    def picture_shown(self, subject: str, subject_id: str, state: str | None) -> MapPicture | None:
        if state is None:
            return self.picture_for(subject, subject_id)
        found = self.picture_for(subject, subject_id, state) or self.picture_for(
            subject, subject_id, ""
        )
        if found is None and state:
            found = self.picture_for(subject, subject_id)
        return found
