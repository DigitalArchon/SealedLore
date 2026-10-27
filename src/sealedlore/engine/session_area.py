"""Map-first play's side of the session (engine/area.py, models/area.py;
SealedLoreDocs design/map-first.md). A mixin for `StorySession`.

Every story that keeps a map is played on it (the author, Oct 16 2026: all
map stories move to map-first). Where people stand is a snapshot on the
nodes (`NodeMeta.area`); walking between turns is the story's
(`Story.area_live`) until the next turn is sent, which carries it: the
turn's snapshot, and a map delta putting the author's character where they
walked to, so the read and the art see it as any move.

The program keeps the clock (the author's ruling): walking within an area
takes little time, travelling between areas the way's distance, and with a
plot the walking moves the plot's clock (`walk_into_plot`), so travel
passes its days. The storyteller is never told the day; it is told how far
the character walked since the last passage, and where everyone stands.

Nobody moves on their own (the author's ruling, for now): the cast stand
where the story last put them until a passage moves them, and the read's
"at" may put them at one of the spots near the author's character.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from sealedlore.engine.area import (
    WALK_M_PER_S,
    Area,
    LaidThing,
    _front,
    anchor_of,
    apply_things,
    area_of,
    area_rect,
    bare,
    berth,
    beside_box,
    build_area,
    build_level,
    build_window,
    clamped_tile,
    exit_side,
    front_of,
    inside_door,
    lay_things,
    metres_per_tile,
    path,
    point_tile,
    stairs_landing,
    stand_near,
    thing_tile,
    tile_point,
    walk_metres,
    walk_seconds,
    walked_words,
)
from sealedlore.engine.levels import level_name, parse_level, place_of_area
from sealedlore.engine.marks import (
    place_at,
)
from sealedlore.engine.takeable import same_thing
from sealedlore.engine.tile_sets import (
    TileSetInfo,
    choose_set,
)
from sealedlore.engine.world import (
    key,
    unknown_for,
)
from sealedlore.engine.world_art import (
    ground_of,
    has_inside,
    is_window,
    outside_part,
    parse_window,
    window_id,
    window_rect,
)
from sealedlore.engine.world_layout import (
    position,
    size_of,
)
from sealedlore.models.area import (
    AreaState,
    LiveArea,
    Mark,
    Spot,
    ThingRecord,
)
from sealedlore.models.node import Node
from sealedlore.models.scene import SceneState
from sealedlore.models.world import (
    OwnTileSet,
    WorldArt,
    WorldMap,
    WorldPlace,
)
from sealedlore.providers.base import ChatProvider
from sealedlore.storage import tile_sets as shipped
from sealedlore.storage.area_cache import load_edge_cache, save_edge_cache

# Seconds taken going through a door.
DOOR_SECONDS = 3
# A way between areas winds: the straight line times this.
WINDING = 1.25
# Give to…: how far a thing can be handed over (beside them, or this far).
GIVE_REACH_M = 4.0
# Someone placed with the author's character stands this close to them.
BESIDE = 3
# How far from a building's door (or a place's spot) someone "at" it may
# stand before they are stood at it again: in metres, and never under a
# tile and a half (a tile's neighbours count whatever its width). It was 8
# tiles, which on a window of open country at 40 m a tile reached 320 m
# (the review, Oct 20 2026).
AT_A_PLACE_M = 12.0
AT_A_PLACE_TILES = 1.5
# Tiles from a door's front, outside, that count as at the door (shown at it
# from inside: `at_the_door`).
AT_THE_DOOR = 2.5
MAX_READ_MINUTES = 7 * 24 * 60
# Where the clock starts when the opening's read gives no time it can use:
# morning, not the midnight a count from nothing meant.
DEFAULT_START_MINUTE = 9 * 60
_CLOCK_TIME = re.compile(r"^\s*(\d{1,2})[:.h](\d{2})\s*$")


# Past an outdoor area's edge: how far straight on another area may start
# and still be walked into, the way a place must lie to be offered, and how
# many are.
EDGE_REACH = 120.0
EDGE_ARC = 60
EDGE_CHOICES = 8
# Landing past an edge: how far along the far edge, and how far in, a tile
# to stand on is looked for.
EDGE_LANDING_ALONG = 3
EDGE_LANDING_IN = 2
EDGE_STEPS = {"north": (0, 1), "south": (0, -1), "east": (1, 0), "west": (-1, 0)}
# Edge tiles tried, nearest first, for one the author's character can reach.
EDGE_TRIES = 12
# The menu choices that are a move and nothing else: the storyteller is told
# where they end ("from the house to the farm"); a walk up to someone keeps
# the area's own name.
MOVING_VERBS = ("travel", "walk", "enter", "leave", "stairs", "onward")


# The roles a gallery card shows thumbnails of: textures, then sprites.
CARD_ROLES = (
    "floor",
    "out-ground",
    "out-road",
    "out-roof",
    "rug",
    "decor0",
    "out-tree0",
    "out-door",
)


ASSET_LABELS = {
    "floor": "Floor",
    "cap": "Wall tops",
    "face": "Wall faces",
    "out-ground": "Ground",
    "out-road": "Roads",
    "out-water": "Water",
    "out-pave": "Paving",
    "out-canopy": "Tree canopy",
    "out-roof": "Roofs",
    "out-wall": "Outer walls",
    "rug": "Rug",
    "decor0": "Decoration 1",
    "decor1": "Decoration 2",
    "decor2": "Decoration 3",
    "out-tree0": "Tree 1",
    "out-tree1": "Tree 2",
    "out-bush": "Bush",
    "out-rock": "Rock",
    "out-rubble": "Rubble",
    "out-fence": "Fence",
    "out-door": "Door",
}


# How far along an outdoor area's edge a journey looks for the road it
# comes in by, in tiles.
ARRIVAL_ROAD_REACH = 16
# The AI's choice of tile set (`start_tile_set_pick`): how much of the
# world's text it is shown, and the room for its short answer (reasoning
# models think first).
PICK_SETTING_LIMIT = 6000
PICK_MAX_TOKENS = 4000
# What a thing's state says became of it (the map's own things, and what the
# read found the passage did to one on the play view): used up or gone
# (off the floor), or broken (still there, drawn broken).
GONE = re.compile(
    r"\b(?:gone|eaten|drunk|used up|consumed|burnt up|burned up|vanished|lost|"
    r"taken away|removed)\b"
)
BROKEN = re.compile(
    r"\b(?:broken|smashed|shattered|wrecked|destroyed|ruined|cracked|splintered|"
    r"snapped|collapsed|burnt|burned)\b"
)
# What says a thing stayed where it was, in the read's state or its quote
# ("bolted to the bench, did not move"): the read listed such things with a
# spot or a place-word, and the code moved them (the replay, Oct 17 2026).
STAYED = re.compile(
    r"\b(?:did not|didn't|does not|doesn't|would not|wouldn't|won't|will not|could not|"
    r"couldn't|can't|cannot)\s+(?:move|budge|shift|give|slide|stir|go)\b|"
    r"\b(?:unmoved|immovable|unmovable|motionless)\b|\bnot (?:moved|an inch|a hair)\b|"
    r"\b(?:bolted|riveted|nailed|fixed|screwed) (?:to|down|through|fast|into|onto)\b|"
    r"\b(?:held|holding|holds|stays?|stayed|stuck|wedged) (?:fast|put)\b"
)
# A thing is moved on the map only when it went this far: an inch or a
# scrape is not a move (the judges' "budged").
MOVE_MIN_M = 1.5
# The things laid on built areas, kept while what they are laid from is the
# same (`_laid_things`).
LAID_KEPT = 32
# The widest tile a thing is laid on: drawn about three tiles tall, a thing
# on land at 10-40 m a tile stood as tall as a church (AfterDark, Oct 18
# 2026: a bottle in the fields outside the farmhouse). Coarser than this, it
# shows once its place has a plan of its own.
THING_TILE_MAX_M = 5.0
# The play view's things the map read is told of, the nearest first.
THINGS_TOLD = 30
# A moored vehicle's length on the play view at most, in tiles (drawn to its
# footprint: a ship at a quay's walking scale is still a big boat, its
# sprite not stretched past what it was drawn at), and its beam a third of
# that. It lies afloat when its words say it floats.
MOORED_MAX_TILES = 16
AFLOAT = re.compile(
    r"\b(?:boat|ship|skiff|barge|canoe|raft|ferry|galleon|sloop|schooner|brig|yacht|"
    r"dinghy|punt|junk|dhow|vessel|steamer|launch|lighter|keelboat|cog|longship|trawler)s?\b",
    re.IGNORECASE,
)


def world_key(world: WorldMap) -> tuple:
    """What settling reads of the map, as a key: who is where (and outside),
    each place's spot, size, parent and state, each thing's whereabouts.
    Dumping the whole map as JSON took 25 ms a call, 133 calls an open."""
    return (
        tuple(sorted(world.where.items())),
        tuple(sorted(world.outside)),
        world.start,
        tuple((p.id, p.x, p.y, p.size, p.inside, p.kind, p.state) for p in world.places.values()),
        tuple((t.id, t.at, t.carried_by, t.state) for t in world.things.values()),
    )


def _doors_of(area: Area) -> set[tuple[int, int]]:
    """The tiles people must not be stood on: the area's exits, its
    buildings' doors and the doorways between its rooms."""
    doors = set(area.exits) | {(e.x, e.y) for e in area.entrances}
    for d in area.doorways:
        doors |= set(d.tiles())
    return doors


def _box_tiles(area: Area) -> set[tuple[int, int]]:
    """Every tile of the boxes an outdoor area draws for the places in it
    (a building, or on a window of open country a place with a plan, as a
    fenced yard with its gate): not open country, whatever is walkable in
    them."""
    return {
        (x, y)
        for x0, y0, x1, y1 in area.buildings.values()
        for y in range(y0, y1 + 1)
        for x in range(x0, x1 + 1)
    }


class AreaRuntime:
    _area_cache: dict | None = None
    _people_kept: tuple[tuple, list[str]] | None = None
    _edge_memo: dict[str, tuple[list[str], list[str]]] | None = None
    # The settled state last computed, with what it was computed from
    # (`area_state`): the window asks where everyone stands on every refresh
    # (133 times opening Brasshaven, Oct 19 2026, 44 s of a 63 s open), and
    # the answer only changes when the leaf, the walking, the map or its art
    # does.
    _settled: tuple[tuple, AreaState] | None = None

    # --- what is played on ---------------------------------------------------

    @property
    def map_first(self) -> bool:
        """The story is played on its map: every story that keeps one."""
        return self.map_on

    def area_people(self) -> list[str]:
        """Who may stand on the map, by name: kept while the leaf, the cards
        and what the plot hides are the same (asked 387 times an open,
        2 ms each, Oct 19 2026)."""
        key = (
            self.story.active_leaf_id,
            self.story.chat,
            tuple((c.id, c.name) for c in self.cast),
            tuple((c.id, c.name) for c in self.supporting),
            tuple(sorted(self.hidden_ids())) if self.story.plot is not None else (),
        )
        kept = self._people_kept
        if kept is not None and kept[0] == key:
            return list(kept[1])
        names = [c.name for c in self.map_people()]
        self._people_kept = (key, names)
        return list(names)

    def tile_set_name(self, art: WorldArt | None = None) -> str:
        """The tile set the map is drawn in (`engine.tile_sets.choose_set`):
        a shipped set's name or "own:<key>"."""
        art = art or self._art_read()
        sets = {name: info for name, (info, _folder) in shipped.shipped_sets().items()}
        looks = [
            ground_of(layout.spec) for states in art.layouts.values() for layout in states.values()
        ]
        own = [key for key, found in art.tile_sets.items() if found.assets]
        return choose_set(art.tile_set, self._setting_words(), looks, sets, own)

    def _setting_words(self) -> str:
        """What a tile set is chosen by: the whole of the world's text and
        where the story starts (a chat's system prompt), not the art's short
        setting. Live, a snowbound fur-trading post's first lines named none
        of the sets' words, and the burnt-out wasteland's set (first by name)
        drew it; its world text named winter and cabins further on."""
        if self.story.chat:
            return self.story.chat_prompt or ""
        start = self.story.setup.starting_scene
        parts = [self.story.world_bible or "", start.location or "", start.situation or ""]
        return "\n".join(p for p in parts if p)

    def tile_set(self, art: WorldArt | None = None) -> tuple[TileSetInfo, Any]:
        """(the set's manifest, a reader of its assets by role): the story's
        own drawings first (an item drawn for this story, its own set), then
        the shipped set's files."""
        art = art or self._art_read()
        name = self.tile_set_name(art)
        sets = shipped.shipped_sets()
        info, folder = sets.get(name, (None, None))
        own: dict[str, str] = {}
        found = art.own_set_for(name)
        if found is not None:
            info = _own_info(name, found[1])
            own = dict(found[1].assets)
        if info is None:
            info = TileSetInfo(name="")
        items = {p.subject_id: p.file for p in art.pictures if p.subject == "item"}
        # Textures drawn for the story on the fly: a roof of a material the
        # set has none of ("out-roof-stone", `roof_jobs`).
        textures = {p.subject_id: p.file for p in art.pictures if p.subject == "texture"}
        changed = dict(art.tile_overrides)
        reader = self.pictures.read

        def read(role: str) -> bytes | None:
            if role in changed:
                # The author's change to this one asset: theirs, or ("") the
                # set's own, past anything drawn for the story: the shipped
                # set's where it has one, else what the story drew (an own
                # set's floor, an item of the story's: once lost for good).
                if changed[role]:
                    data = reader(changed[role])
                    if data:
                        return data
                elif folder is not None:
                    data = shipped.read_asset(folder, role)
                    if data:
                        return data
            if role.startswith("items/") and role[6:] in items:
                data = reader(items[role[6:]])
                if data:
                    return data
            if role in textures:
                data = reader(textures[role])
                if data:
                    return data
            if role in own:
                data = reader(own[role])
                if data:
                    return data
            return shipped.read_asset(folder, role) if folder is not None else None

        def from_story(role: str) -> bool:
            """Whether the asset read is one drawn or chosen for the story
            (not pre-made to wrap, as the shipped sets' are)."""
            if changed.get(role):
                return True
            if changed.get(role) == "" and folder is not None and shipped.read_asset(folder, role):
                return False
            return (
                role in own or role in textures or (role.startswith("items/") and role[6:] in items)
            )

        found_names: list[str] = []

        def listed() -> list[str]:
            """Every thing the set and the story have a picture of, by slug:
            what a thing with none borrows from (`closest_item`)."""
            if found_names:
                return found_names
            names = set(items)
            names.update(r[6:] for r in own if r.startswith("items/"))
            if folder is not None:
                names.update(r[6:] for r in shipped.asset_names(folder, "items"))
            found_names.extend(sorted(names))
            return found_names

        read.from_story = from_story  # type: ignore[attr-defined]
        read.items = listed  # type: ignore[attr-defined]
        return info, read

    def generic_people(self) -> list[str]:
        """The tile set's generic people, by role."""
        name = self.tile_set_name()
        found = shipped.shipped_sets().get(name)
        return shipped.asset_names(found[1], "people") if found else []

    def walker_file(self, name: str, art: WorldArt | None = None) -> str | None:
        """The file of a person's walking sprite, if one is drawn."""
        art = art or self._art_read()
        changed = art.tile_overrides.get(f"walker/{name}")
        if changed is not None:
            return changed or None  # "": the set's generic people
        found = art.picture_for("walker", name)
        return found.file if found is not None else None

    def walker_frame_files(self, name: str, art: WorldArt | None = None) -> list[str]:
        """The files of a person's stride frames, when their standing sprite
        is the one they were drawn from (an avatar the author chose or drew
        instead has none that fit)."""
        art = art or self._art_read()
        standing = art.picture_for("walker", name)
        if standing is None or self.walker_file(name, art) != standing.file:
            return []
        return [p.file for p in art.walker_frames(name)]

    def area(
        self,
        area_id: str | None,
        world: WorldMap | None = None,
        art: WorldArt | None = None,
        *,
        things: Mapping[str, ThingRecord] | None = None,
    ) -> Area | None:
        """The area of `area_id`, a place's plan or a window of open
        country (engine/area.py `build_window`), built from the map at the
        leaf (kept while nothing it is built from changes), with what became
        of its things laid over it (`apply_things`: `things`, the records of
        the state in hand, else those live or at the leaf)."""
        world = world if world is not None else self._world_read()
        art = art if art is not None else self._art_read()
        built = self._built_area(area_id, world, art)
        if built is None:
            return None
        records = things if things is not None else self._things_now()
        laid = lay_things(built, self._laid_things(built, world, art, records))
        return apply_things(laid, records) if records else laid

    def _laid_things(
        self, area: Area, world: WorldMap, art: WorldArt, records: Mapping[str, ThingRecord]
    ) -> list[LaidThing]:
        """The map's things in this area that no record puts anywhere (what a
        read or a plot file put at a place): each laid near where its place
        is in the area (a building's door, a room's middle, a label), else
        near the way in, on a tile of its own (the author, Oct 17 2026:
        things the story adds must show on the play view; they were on the
        map's list only, and never drawn). Kept while the same."""
        found = []
        for t in sorted(world.things.values(), key=lambda t: t.id):
            if area.tile_m > THING_TILE_MAX_M:
                break  # a vehicle is drawn to its size; a thing would tower
            if t.carried_by or not t.at or GONE.search(t.state.lower()):
                continue
            record = records.get(t.id)
            if record is not None and (record.state in ("taken", "gone") or record.spot):
                continue  # where its record says (`apply_things`)
            found.append(t)
        # The vehicles docked here, laid first, whatever else there is.
        docked = [] if area.indoor else self._docked(area, world, art)
        if not found and not docked:
            return []
        cache_key = (
            area.id,
            id(area),
            tuple((t.id, t.name, t.at, t.state) for t in found),
            tuple((v.id, v.name, v.inside, v.size, has_inside(art, v)) for v in docked),
        )
        cache = self.__dict__.setdefault("_laid_cache", {})
        kept = cache.get(cache_key)
        if kept is not None:
            return kept
        taken: set[tuple[int, int]] = set()
        out: list[LaidThing] = self._moored(area, world, art, docked, taken)
        for t in found:
            if area_of(world, art, t.at) != area.id or _twin(area, t.name, world) is not None:
                continue  # elsewhere, or drawn here already (a plan's object of its name)
            near = anchor_of(area, world, t.at) or self._way_in(area)
            x, y = thing_tile(area, near, taken)
            taken.add((x, y))
            broken = BROKEN.search(t.state.lower()) is not None
            out.append(LaidThing(id=t.id, name=t.name, x=x, y=y, broken=broken))
        if len(cache) > LAID_KEPT:
            cache.clear()
        cache[cache_key] = out
        return out

    def _docked(self, area: Area, world: WorldMap, art: WorldArt) -> list[WorldPlace]:
        """The vehicles the map has docked at a place in this area."""
        return [
            v
            for v in sorted(world.places.values(), key=lambda p: p.id)
            if v.kind == "vehicle"
            and v.inside in world.places
            and area_of(world, art, v.inside) == area.id
        ]

    def _moored(
        self,
        area: Area,
        world: WorldMap,
        art: WorldArt,
        docked: list[WorldPlace],
        taken: set[tuple[int, int]],
    ) -> list[LaidThing]:
        """The vehicles docked in this area (a barge made fast at the quay, a
        cart in the yard), laid where they dock: a boat afloat by the shore
        (`berth`), else on the ground; as long as they are, up to
        `MOORED_MAX_TILES`, a third as wide; their names on the tile beside
        them nearest where they dock, so whoever is aboard stands there, and
        one with an inside of its own boarded there (the author, Oct 18 2026:
        a moored vehicle is drawn where it docks; the bargeman and his barge
        were invisible on the quay)."""
        out: list[LaidThing] = []
        for v in docked:
            near = anchor_of(area, world, v.inside) or self._spot_in(world, area, v.inside)
            if near is None:
                near = self._way_in(area)
            tiles = round(size_of(v) / max(area.tile_m, 0.1))
            w = max(1, min(tiles, MOORED_MAX_TILES, area.width // 2))
            h = max(1, min(round(w / 3), area.height // 3))
            afloat = AFLOAT.search(f"{v.name} {v.what}") is not None
            spot = berth(area, near, w, h, taken) if afloat else None
            if spot is None:
                spot = thing_tile(area, near, taken, w=w, h=h)
            x, y = spot
            taken |= {(x + dx, y + dy) for dx in range(w) for dy in range(h)}
            beside = beside_box(area, (x, y, w, h), near, taken | area.blocked())
            out.append(
                LaidThing(
                    id=v.id,
                    name=v.name,
                    x=x,
                    y=y,
                    w=w,
                    h=h,
                    vehicle=True,
                    label_at=beside,
                    boards=has_inside(art, v),
                )
            )
        return out

    def _things_now(self) -> Mapping[str, ThingRecord]:
        """The records live, else at the leaf (never through `settle`, which
        asks for areas itself)."""
        live = self._live()
        state = live if live is not None else self._snapshot_on(self.path())
        return state.things if state is not None else {}

    def built_area(self, area_id: str | None) -> Area | None:
        """The area as its plan and the author's own tiles make it, before
        anything became of its things: their places as the plan put them."""
        return self._built_area(area_id)

    def _built_area(
        self, area_id: str | None, world: WorldMap | None = None, art: WorldArt | None = None
    ) -> Area | None:
        """The area as built from its plan, kept while the same."""
        return self._base_area(area_id, world, art if art is not None else self._art_read())

    def _base_area(
        self, area_id: str | None, world: WorldMap | None = None, art: WorldArt | None = None
    ) -> Area | None:
        """The area as its plan builds it, before the author's own tiles."""
        if area_id is None:
            return None
        world = world if world is not None else self._world_read()
        art = art if art is not None else self._art_read()
        if is_window(area_id):
            return self._window(area_id, world, art)
        parsed = parse_level(area_id)
        level = parsed[1] if parsed is not None else 0
        place = world.places.get(parsed[0] if parsed is not None else area_id)
        if place is None:
            return None
        layout = art.layout_shown(place.id, place.state)
        if layout is None:
            return None
        if level and layout.level(level) is None:
            return None
        plan = art.furnishing_for(area_id, place.state)
        children = tuple(
            sorted(
                (c.id, c.name, c.x, c.y, c.size, c.kind)
                for c in world.places.values()
                if c.inside == place.id
            )
        )
        inner = tuple(
            sorted(
                c.id
                for c in world.places.values()
                if c.inside == place.id and art.layout_shown(c.id, c.state) is not None
            )
        )
        cache_key = (
            place.id,
            place.state,
            layout.made_at,
            len(layout.rows),
            # A level arriving puts stairs on the plan (engine/levels.py).
            tuple((lv.index, lv.made_at) for lv in layout.levels),
            level,
            plan.made_at if plan is not None else "",
            self.story.defaults.map_density,
            children,
            inner,
            tuple(self.area_people()),
        )
        # One reference to the cache for the whole lookup: a correction
        # (`_done`, on the worker) sets `_area_cache` to None while the GUI
        # thread may be in here (the review, Oct 20 2026).
        cache = self._area_cache
        if cache is None:
            cache = self._area_cache = {}
        found = cache.get(cache_key)
        if found is None:
            if level:
                found = build_level(
                    world,
                    art,
                    place.id,
                    level,
                    density=self.story.defaults.map_density,
                    people=self.area_people(),
                )
            else:
                memo = self._edge_cache()
                before = len(memo) if memo is not None else 0
                found = build_area(
                    world,
                    art,
                    place.id,
                    density=self.story.defaults.map_density,
                    people=self.area_people(),
                    edge_memo=memo,
                )
                if memo is not None and len(memo) > before:
                    self._save_edge_cache(memo)
            if len(cache) > 160:  # one per window of open country round a long walk
                cache.clear()
            cache[cache_key] = found
        return found

    def _window(self, area_id: str, world: WorldMap, art: WorldArt) -> Area | None:
        """A window of open country, kept while the land under it, the
        places in it and the people are the same."""
        parsed = parse_window(area_id)
        if parsed is None:
            return None
        # What a window is built from, as a key: the land (every patch, as
        # drawn), the map's places and the people. Looking the patch up and
        # scanning the places for every lookup cost 8 ms, 217 lookups a
        # settle with 39 people on 27 regions (Oct 19 2026).
        cache_key = (
            area_id,
            tuple((p.id, p.made_at, len(p.rows), p.ground) for p in art.terrain),
            self._art_version,
            self._world_key_of(world),
            tuple(sorted(art.layouts)),
            tuple(self.area_people()),
        )
        cache = self._area_cache  # one reference, as in `_base_area`
        if cache is None:
            cache = self._area_cache = {}
        found = cache.get(cache_key)
        if found is None:
            memo = self._edge_cache()
            before = len(memo) if memo is not None else 0
            found = build_window(world, art, area_id, people=self.area_people(), edge_memo=memo)
            if memo is not None and len(memo) > before:
                self._save_edge_cache(memo)
            if len(cache) > 160:  # one per window of open country round a long walk
                cache.clear()
            cache[cache_key] = found
        return found

    def _edge_cache(self) -> dict[str, tuple[list[str], list[str]]] | None:
        """The story's kept edge paths (storage/area_cache.py), read once;
        kept in memory only for a memory-only chat."""
        if self._edge_memo is None:
            self._edge_memo = {} if self.memory_only else load_edge_cache(self.story.id, self.root)
        return self._edge_memo

    def _save_edge_cache(self, memo: dict[str, tuple[list[str], list[str]]]) -> None:
        if self.memory_only or not self.pictures.alive():
            return
        try:
            save_edge_cache(self.story.id, memo, self.root)
        except OSError:
            pass  # a cache: the map is the same without it

    # --- where everyone stands -------------------------------------------------

    def _snapshot_on(self, nodes: Sequence[Node]) -> AreaState | None:
        for node in reversed(nodes):
            if node.meta.area is not None:
                return node.meta.area
        return None

    def _live(self) -> AreaState | None:
        live = self.story.area_live
        if live is None:
            return None
        if live.leaf_id != self.story.active_leaf_id or (
            live.held is not None and live.held != self.held_name()
        ):
            # A moved leaf (a retake, a branch) or another character's
            # walking: dropped, not kept to come back on a switch back.
            self.story.area_live = None
            return None
        return live.state

    def area_state(self) -> AreaState:
        """Where everyone stands now: the walking since the last turn, else
        the snapshot at the leaf, settled onto the map as it stands. Kept
        between calls while nothing it is settled from has changed: the
        leaf, who is held, the live walking or the snapshot, the map as
        folded, and the art (its version, bumped on every save)."""
        live = self._live()
        path = self.path()
        state = live if live is not None else self._snapshot_on(path)
        # The kept fold, read only (settling reads the map, never changes it).
        world = self._world_read(path)
        art = self.world_art
        key = (
            self.story.active_leaf_id,
            self.held_name(),
            live is None,
            state.model_dump_json() if state is not None else "",
            self._world_key_of(world),
            self._art_version,
            len(art.layouts),
            len(art.terrain),
            len(art.regions),
            # Whoever the scene has here stands with the author's character.
            tuple(self._present_on_map()),
            tuple(self.area_people()),
            self.story.defaults.map_density,
        )
        kept = self._settled
        if kept is not None and kept[0] == key:
            return kept[1].model_copy(deep=True)
        state = state.model_copy(deep=True) if state is not None else AreaState()
        if live is None:
            state.walked, state.walked_from, state.walked_words, state.action = 0, "", "", None
        settled = self.settle(state, world, self._art_read())
        self._settled = (key, settled)
        return settled.model_copy(deep=True)

    def held_name(self) -> str | None:
        return self.map_held_name(self.story.held_character_id)

    def _world_key_of(self, world: WorldMap) -> tuple:
        """`world_key` of a kept fold (`_world_read`), worked out once per
        fold: with 500 places it took a millisecond, asked on every lookup
        of a window and every `area_state`. Any other map (a copy, its
        holder's to change) is keyed afresh."""
        if not self._is_kept_fold(world):
            return world_key(world)
        memo = self.__dict__.setdefault("_world_keys", {})
        found = memo.get(id(world))
        if found is not None and found[0] is world:
            return found[1]
        made = world_key(world)
        if len(memo) > 8:
            memo.clear()
        memo[id(world)] = (world, made)  # the fold held, so its id is never given again
        return made

    def settle(
        self, state: AreaState, world: WorldMap, art: WorldArt, *, walked: bool = True
    ) -> AreaState:
        """Everyone on a tile of the area the map has them in: kept where
        they stand while that is still where the map puts them, else stood
        at their place's spot (a building's door, a room), beside the
        author's character when the map has them in the same place, or near
        the way in; in open country (a window of the land, engine/area.py)
        at their own point on the map. `walked`: the author's character's
        own walking stands as walked (the map catches up when the turn is
        sent); off, the place the map names wins (a read moved them)."""
        held = self.held_name()
        where = dict(world.where)
        if held is not None and held not in where and world.start is not None:
            where[held] = world.start
        if held in where:
            # Whoever the scene has here and the map hasn't placed is with the
            # author's character (live, Brasshaven's Silas stood in the
            # workshop's doorway and the map read never named him: he was
            # nowhere on the map, told to nobody, and Talk couldn't find him).
            for name in self._present_on_map():
                if name != held and name not in where:
                    where[name] = where[held]
        spots: dict[str, Spot] = {}
        order = ([held] if held in where else []) + [n for n in where if n != held]
        for name in order:
            place_id = where[name]
            spot = state.spots.get(name)
            # Where they stand on the map, for the window of open country
            # there: their tile's point; with the author's character, theirs.
            point = self._spot_point(spot, world, art)
            mine = spots.get(held or "")
            wide = False
            if point is None and name != held and place_id == where.get(held or "") and not wide:
                point = self._spot_point(mine, world, art)
            elif (
                name != held
                and mine is not None
                and place_id == where.get(held or "")
                and not wide
                and spot is not None
                and spot.area != mine.area
                and (here := self.area(mine.area, world, art)) is not None
                and here.window
            ):
                # With the author's character at a place in open country: on
                # their window, beside them, not by their own last spot (live,
                # Oct 18 2026: Tobias, walking the river road with Wren, stood
                # on the next window over, the road's spot on the line between).
                point = self._spot_point(mine, world, art)
            target = area_of(world, art, place_id, point)
            # Where they are stood: their place, or, outside a building, the
            # building (its door's front on the ground round it).
            stand_at = place_id
            outside = False
            if name in world.outside or outside_part(world, world.places.get(place_id)):
                # At a building but outside it (its door, porch, yard): on the
                # ground round it, at its door (`_stand` finds its front).
                around = self._outside_of(world, art, place_id, target, spot, point)
                if around is not None:
                    target, stand_at = around
                    outside = True
            # Stood on the land only until their place had a plan: there now.
            arrived = (
                spot is not None
                and spot.for_place == place_id
                and target is not None
                and target != spot.area
            )
            if (
                name == held
                and walked
                and spot is not None
                and not arrived
                and self.area(spot.area, world, art, things=state.things) is not None
            ):
                spots[name] = self._on_the_grid(spot, world, art) or spot
                continue
            if target is None:
                continue
            area = self.area(target, world, art, things=state.things)
            if area is None:
                continue
            # On the land for a place that is no area of its own yet (no
            # plan): they go into it once it has one (`Spot.for_place`); and
            # someone outside a building stood on the land because the place
            # round it has no plan yet goes onto that plan once it has (the
            # lab test, Oct 18 2026: Maren, at an inn's stable yard before the
            # village was drawn, stayed on the open country after).
            waiting = (
                place_id
                if (area.window or (_has_an_inside(world, place_id) and not outside))
                and place_id not in (area.place_id, area.id)
                else ""
            )
            if (
                spot is not None
                and spot.area == target
                # The author's character stays where they walked; a read that
                # moved them (`walked` off) moves them like anyone (live, Oct
                # 18 2026: read at the north gate, Wren stayed at the counting
                # house's door across the town).
                and ((name == held and walked) or self._still_at(area, world, spot, stand_at))
            ):
                spots[name] = (
                    spot
                    if spot.for_place == waiting
                    else spot.model_copy(update={"for_place": waiting})
                )
                continue
            if (
                spot is not None
                and spot.area != target
                and place_of_area(spot.area) == target
                and self.area(spot.area, world, art) is not None
            ):
                spots[name] = spot  # on another level of the place: still in it
                continue
            near_me = (
                name != held
                and mine is not None
                and mine.area == target
                and place_id == where.get(held or "")
                and not wide
            )
            # On a window, where they stand on the map; but someone moved to
            # a place there with a spot of its own (not a road or a region)
            # goes to that spot: kept at their own point, Wren read outside
            # the map shop stayed 700 m off on the land (Oct 18 2026).
            moved_here = (
                spot is not None
                and spot.area == target
                and world.places.get(stand_at) is not None
                and world.places[stand_at].kind not in ("route", "region")
            )
            new = self._stand(
                world,
                art,
                target,
                stand_at,
                name,
                spots,
                mine if near_me else None,
                point=point if area.window and not moved_here else None,
            )
            if new is not None:
                if outside and new.for_place != waiting:
                    new = new.model_copy(update={"for_place": waiting})
                spots[name] = new
        if self.story.visual:
            # A visual story's people (engine/session_visual.py) stand where
            # the design put them and where their own intents took them,
            # whatever the map's `where` says of them (it says nothing).
            for name in self.visual_people():
                spot = state.spots.get(name)
                if spot is not None and name not in spots:
                    spots[name] = spot
        # Nobody stands in fire or a wall a redrawn plan put under them
        # (kept there, every walk was refused; the second review).
        # Nor in a doorway: a walk into a door ends short of whoever stands
        # in it (live, Oct 18 2026: the reeve's clerk in the door, Ines
        # could not leave the hall).
        for name, spot in list(spots.items()):
            area = self.area(spot.area, world, art, things=state.things)
            if area is None:
                continue
            doors = _doors_of(area) if name != held else set()
            if area.walkable(spot.x, spot.y) and (spot.x, spot.y) not in doors:
                continue
            taken = {
                (s.x, s.y) for n, s in spots.items() if n != name and s.area == spot.area
            } | doors
            x, y = stand_near(area, (spot.x, spot.y), taken, seed=name)
            spots[name] = Spot(area=spot.area, x=x, y=y, for_place=spot.for_place)
        state.spots = spots
        return state

    def _on_the_grid(self, spot: Spot, world: WorldMap, art: WorldArt) -> Spot | None:
        """Someone on a window of the land laid out before the world
        designer's grid was (`window_rect`: a plot file's map, whose designer
        waits until the author's character leaves the file's places), on the
        grid's window at the point they stand at; None when they are on the
        grid, or on no window. Kept where they walked, the author's character
        stood on the old window while everyone else went onto the grid's."""
        if not art.region_origin or not is_window(spot.area):
            return None
        point = self._spot_point(spot, world, art)
        if point is None:
            return None
        grid = window_id(*window_rect(art, *point))
        if grid == spot.area:
            return None
        area = self.area(grid, world, art)
        tile = point_tile(world, area, *point) if area is not None else None
        if tile is None:
            return None
        return Spot(area=grid, x=tile[0], y=tile[1], for_place=spot.for_place)

    def _outside_of(
        self,
        world: WorldMap,
        art: WorldArt,
        place_id: str,
        target: str | None,
        spot: Spot | None,
        point: tuple[float, float] | None,
    ) -> tuple[str, str] | None:
        """For someone the map has outside `place_id`: the area round the
        building they are outside of, and that building, when `target` is
        the building's own inside (the place itself, or a porch or doorway
        the read put inside it). Its parent's ground when it stands in one,
        else the window of open country it stands in (AfterDark, Oct 18
        2026: Suzuka knocking at a farmhouse alone on the land was stood
        inside it, and at its "porch overhang", which the read had put
        inside the farmhouse, in the far hall out of John's sight). None
        when they are not at a building's inside."""
        building_id = place_of_area(target)
        if building_id is None or building_id not in world.places:
            return None
        chain, at = [], world.places.get(place_id)
        while at is not None and at.id not in chain:
            chain.append(at.id)
            at = world.places.get(at.inside) if at.inside else None
        if building_id not in chain:
            return None
        building = world.places[building_id]
        if building.kind not in ("building", "room", "vehicle"):
            # The area is the ground round it (a toll bridge's, for a toll
            # house with no plan of its own yet): they stand on it, at the
            # house. Live (Oct 18 2026) Wren, at the toll house door, was
            # stood outside the whole bridge, out in open country.
            return None
        # Their own point, if they stand on the ground already; the
        # building's spot when they stood inside it.
        if spot is None or place_of_area(spot.area) == building_id:
            point = position(world, building_id)
        if building.inside:
            around = area_of(world, art, building.inside, point)
        elif point is not None:
            around = window_id(*window_rect(art, *point))
        else:
            around = None
        if around is None or place_of_area(around) == building_id:
            return None
        return around, building_id

    def at_the_door(self) -> list[tuple[str, int, int, int, int]]:
        """Who stands at a door of the building the author's character is
        inside, from outside: (name, the tile just beyond the door's wall,
        the door tile), in that building's area, so the play view shows them
        at the door as seen through it (AfterDark, Oct 18 2026: Suzuka
        knocking at the farmhouse door, John inside, was on no view at all:
        the play view shows one area, and she stood in the one round it)."""
        if not self.map_first or self.in_private:
            return []
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        area = self.area(me.area) if me is not None else None
        if area is None:
            return []
        return self._at_the_door(state, self.world(), self._art_read(), held, area)

    def _at_the_door(
        self, state: AreaState, world: WorldMap, art: WorldArt, held: str | None, area: Area
    ) -> list[tuple[str, int, int, int, int]]:
        """`at_the_door` for a state and the area the author's character is
        in on it (the turn's snapshot for the storyteller, the live state for
        the play view)."""
        if not area.indoor or area.level or not area.exits:
            return []
        building = place_of_area(area.id)
        steps = {"north": (0, -1), "south": (0, 1), "west": (-1, 0), "east": (1, 0)}
        out: list[tuple[str, int, int, int, int]] = []
        used: dict[tuple[int, int], int] = {}
        for name, spot in state.spots.items():
            if name == held or spot.area == area.id:
                continue
            around = self.area(spot.area, world, art)
            if around is None or building not in around.buildings:
                continue
            box = around.buildings[building]
            doors = [e for e in around.entrances if e.place_id == building]
            near = [
                (math.hypot(spot.x - fx, spot.y - fy), door)
                for door in doors
                for fx, fy in (_front(door, box),)
            ]
            if not near:
                continue
            distance, door = min(near, key=lambda pair: pair[0])
            if distance > AT_THE_DOOR:
                continue
            side = door.side if door.side in steps else "south"
            exits = [e for e in area.exits if exit_side(area, *e) == side] or list(area.exits)
            ex, ey = exits[0]
            dx, dy = steps[side]
            k = used.get((ex, ey), 0)
            used[(ex, ey)] = k + 1
            # Side by side along the wall, the second to the right of the first.
            shift = (k + 1) // 2 * (1 if k % 2 else -1)
            bx, by = ex + dx + (shift if dy else 0), ey + dy + (shift if dx else 0)
            out.append((name, bx, by, ex, ey))
        return out

    def _present_on_map(self, scene: SceneState | None = None) -> list[str]:
        """The people the scene (the story's now, unless given) has here, by
        the names the map uses."""
        if self.story.chat:
            return []
        scene = scene if scene is not None else self.story.scene
        names = [c.name for c in self.visible_cast() if c.id in scene.present_character_ids]
        for other in scene.present_others:
            person = self._map_person(other)
            names.append(person or other)
        return list(dict.fromkeys(names))

    def _still_at(self, area: Area, world: WorldMap, spot: Spot, place_id: str) -> bool:
        anchor = anchor_of(area, world, place_id)
        if anchor is None and not area.indoor and place_id not in (area.place_id, area.id):
            anchor = self._spot_in(world, area, place_id)
        if anchor is None:
            return True
        reach = max(AT_A_PLACE_TILES, AT_A_PLACE_M / max(area.tile_m, 0.1))
        return math.hypot(spot.x - anchor[0], spot.y - anchor[1]) <= reach

    def _stand(
        self,
        world: WorldMap,
        art: WorldArt,
        area_id: str,
        place_id: str,
        name: str,
        spots: dict[str, Spot],
        beside: Spot | None,
        *,
        point: tuple[float, float] | None = None,
    ) -> Spot | None:
        """A tile for `name` in the area: beside `beside`, else at their
        place's spot in it (a door's front, a label), else, on a window of
        open country, at `point` (where they stand on the map), else near
        the way in."""
        area = self.area(area_id, world, art)
        if area is None:
            return None
        taken = {(s.x, s.y) for s in spots.values() if s.area == area_id} | _doors_of(area)
        if beside is not None:
            near = (beside.x, beside.y)
            x, y = stand_near(area, near, taken | {near}, seed=name, reach=BESIDE)
            return Spot(area=area_id, x=x, y=y)
        near = anchor_of(area, world, place_id)
        if near is None and area.window:
            at = point if point is not None else position(world, place_id)
            near = clamped_tile(world, area, *at) if at is not None else None
        if near is None and not area.indoor and place_id not in (area.place_id, area.id):
            # A place the plan doesn't mark but whose spot it covers (a cairn
            # standing on the plan drawn round a boundary stone): there.
            near = self._spot_in(world, area, place_id)
        if near is None:
            near = self._way_in(area)
        x, y = stand_near(area, near, taken, seed=name, reach=0 if area.window else 2)
        waiting = (area.window or _has_an_inside(world, place_id)) and place_id not in (
            area.place_id,
            area.id,
        )
        return Spot(area=area_id, x=x, y=y, for_place=place_id if waiting else "")

    @staticmethod
    def _spot_in(world: WorldMap, area: Area, place_id: str) -> tuple[int, int] | None:
        """The tile at a place's spot on the map, when the area covers it."""
        at = position(world, place_id)
        rect = area_rect(world, area)
        if at is None or rect is None:
            return None
        west, north, width, height = rect
        if not (west <= at[0] <= west + width and north - height <= at[1] <= north):
            return None
        return clamped_tile(world, area, *at)

    def _spot_point(
        self, spot: Spot | None, world: WorldMap, art: WorldArt
    ) -> tuple[float, float] | None:
        """Where a spot is on the map, in metres; None for none."""
        if spot is None:
            return None
        area = self.area(spot.area, world, art)
        return tile_point(world, area, spot.x, spot.y) if area is not None else None

    @staticmethod
    def _way_in(area: Area) -> tuple[int, int]:
        if area.level:
            # A level is come into by its stairs (engine/levels.py): a step
            # off the stairs nearest the plan's own level.
            stairs = sorted(area.stairs_entrances(), key=lambda e: e.side != "down")
            if stairs:
                return stairs_landing(area, (stairs[0].x, stairs[0].y))
        if area.indoor:
            inside = inside_door(area)
            if inside is not None:
                return inside
            if area.rooms:
                room = area.rooms[0]
                return int(room.cx), int(room.cy)
        # Outdoors: in front of a building's door, else at a label one can
        # stand on (the first label was a building's centre, under its roof;
        # the second review), else the middle.
        for entrance in area.entrances:
            box = area.buildings.get(entrance.place_id)
            if box is not None:
                fx, fy = _front(entrance, box)
                if area.walkable(fx, fy):
                    return fx, fy
        for x, y in area.labels.values():
            if area.walkable(x, y):
                return x, y
        return area.width // 2, area.height // 2

    def _set_live(self, state: AreaState) -> None:
        self.story.area_live = LiveArea(
            leaf_id=self.story.active_leaf_id, held=self.held_name(), state=state
        )
        self.save()

    def place_of_spot(self, spot: Spot | None, world: WorldMap) -> str | None:
        """The place a spot is in, for the map's `where`: the area's place
        (a building's inside is that building); on a window of open country
        the place holding the window (a region), or None in no place (the
        map's `where` has no word for nowhere: it keeps the last place)."""
        if spot is None:
            return None
        if spot.area in world.places:
            return spot.area
        parsed = parse_level(spot.area)
        if parsed is not None and parsed[0] in world.places:
            return parsed[0]
        if is_window(spot.area):
            area = self.area(spot.area, world)
            if area is not None and area.place_id in world.places:
                return area.place_id
        return None

    # --- moving ----------------------------------------------------------------

    def walk_route(self, x: int, y: int, *, beside: bool = False) -> list[tuple[int, int]]:
        """The tiles the author's character walks to reach (x, y) in their
        area, or to the nearest free tile beside it (`beside`: a person or a
        thing). [] when there is no way."""
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        area = self.area(me.area if me else None)
        if me is None or area is None:
            return []
        others = {(s.x, s.y) for n, s in state.spots.items() if n != held and s.area == me.area}
        goal = (x, y)
        if beside or goal in others:  # never onto someone's tile
            goal = stand_near(area, goal, others | area.blocked() | {goal})
        route = path(area, (me.x, me.y), goal, blocked=others)
        if not route and others and goal not in others:
            # Someone stands in the only way (a gate's gap, a doorway): they
            # are squeezed past, never walked onto (live, Marcus in the
            # south gate's gap kept John from reaching the doctor).
            route = path(area, (me.x, me.y), goal, blocked=set())
        return route

    def walked(self, steps: Sequence[tuple[int, int]]) -> AreaState:
        """The author's character has walked these tiles: where they stand,
        and the clock, moved on (live until the next turn is sent)."""
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        area = self.area(me.area if me else None)
        if me is None or area is None or not steps:
            return state
        # A walk is this character's, in this area, from where they stand:
        # one finishing after another story or character was taken up, or
        # after a read moved them, is not (the review, Oct 16 2026).
        others = {(s.x, s.y) for n, s in state.spots.items() if n != held and s.area == me.area}
        if (
            tuple(steps[0]) != (me.x, me.y)
            or not all(area.walkable(x, y) for x, y in steps)
            or (len(steps) > 1 and tuple(steps[-1]) in others)
        ):
            return state
        seconds = walk_seconds(walk_metres(area, steps))
        if self.story.visual:
            # A broken leg walks slow (engine/health.py `walk_factor`).
            seconds = int(seconds / max(0.2, self.visual_walk_factor(state)))
        x, y = steps[-1]
        state.spots[held] = Spot(area=me.area, x=x, y=y)
        self._add_time(state, seconds, area.name, area.name)
        self._set_live(state)
        return state

    def _add_time(self, state: AreaState, seconds: int, frm: str, to: str) -> None:
        state.seconds += seconds
        state.walked += seconds
        state.walked_from = state.walked_from or frm
        state.walked_words = walked_words(state.walked, state.walked_from, to)

    def _door_at(self, area: Area, x: int, y: int, world: WorldMap) -> tuple[str, str] | None:
        """(where it leads, its name) for a tile of `area` that is a door
        into a building, a door out of one, or the stairs; None otherwise."""
        entrance = area.entrance_at(x, y)
        if entrance is not None:
            return entrance.place_id, self.area_title(entrance.place_id, world)
        if area.indoor and (x, y) in area.exits and area.parent is not None:
            # Open country is named for the region it lies in, when it does.
            around = self.area(area.parent, world)
            holder = world.places.get(around.place_id) if around and around.window else None
            return area.parent, holder.name if holder else self.area_title(area.parent, world)
        return None

    def area_title(self, area_id: str, world: WorldMap | None = None) -> str:
        """An area's name for a message: a place's, a level's with its
        level ("The farmhouse, upper floor"), open country's."""
        world = world if world is not None else self.world()
        parsed = parse_level(area_id)
        place = world.places.get(parsed[0] if parsed is not None else area_id)
        if place is None:
            return "open country" if is_window(area_id) else area_id
        if parsed is None:
            return place.name
        layout = self._art_read().layout_shown(place.id, place.state)
        return f"{place.name}, {level_name(layout, parsed[1]).lower()}"

    def door_here(self) -> tuple[str, str] | None:
        """(where it leads, its name) when the author's character stands in a
        door that leads into or out of a building."""
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        area = self.area(me.area if me else None)
        if me is None or area is None:
            return None
        return self._door_at(area, me.x, me.y, self.world())

    def go_through(self) -> AreaState | None:
        """Through the door the author's character stands in: into the
        building, or out of it to stand outside its door; up or down the
        stairs they stand on, to a step off the stairs on the other level."""
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        if me is None:
            return None
        world, art = self.world(), self._art_read()
        through = self._through(state, held, me, world, art)
        if through is None:
            return None
        spot, frm, to = through
        state.spots[held] = spot
        self._add_time(state, DOOR_SECONDS, frm, to)
        self._set_live(state)
        return state

    def _through(
        self, state: AreaState, held: str, at: Spot, world: WorldMap, art: WorldArt
    ) -> tuple[Spot, str, str] | None:
        """Where the author's character stands once through the door at
        `at` (a door into a building, out of one, or the stairs), with the
        names of where they set out from and where they arrive; None when
        `at` is no door."""
        here = self.area(at.area, world, art)
        if here is None:
            return None
        door = self._door_at(here, at.x, at.y, world)
        if door is None:
            return None
        there = self.area(door[0], world, art)
        if there is None:
            return None
        taken = {(s.x, s.y) for n, s in state.spots.items() if n != held and s.area == there.id}
        stairs = here.entrance_at(at.x, at.y)
        if stairs is not None and stairs.stairs:
            x, y = stairs_landing(there, (at.x, at.y), taken)
            return (
                Spot(area=there.id, x=x, y=y),
                self.area_title(here.id, world),
                self.area_title(there.id, world),
            )
        if not here.indoor:
            entrance = here.entrance_at(at.x, at.y)
            spot = inside_door(there, entrance.side if entrance else None) or self._way_in(there)
        else:
            side = exit_side(here, at.x, at.y)
            spot = front_of(there, here.place_id, side)  # a moored vehicle's gangway too
            if spot is None:
                # The outside plan has no box for this building (a plan kept
                # as rows alone): outside it where the map has it, not at
                # the area's middle.
                building = world.places.get(here.place_id)
                point = (
                    point_tile(world, there, building.x, building.y)
                    if building is not None and building.x is not None and building.y is not None
                    else None
                )
                spot = point or self._way_in(there)
        x, y = stand_near(there, spot, taken)
        return Spot(area=there.id, x=x, y=y), here.name, there.name

    def travel_choices(self) -> list[tuple[str, str, int]]:
        """(place id, name, seconds on foot) for each known place the
        author's character could set off for: the areas the map has drawn,
        and the known places with a spot of their own, nearest first."""
        world = self.world()
        art = self._art_read()
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        here = me.area if me is not None else world.where.get(self.held_name() or "")
        if here is None:
            return []
        here = place_of_area(here) or here  # a level of a building is the building
        # From where they stand, not from the area's middle: the way's length
        # is what the clock moves by and the storyteller is told.
        start = self.held_point(state, world, art) or position(world, here)
        out = []
        # Not where they are already: this area, what is in it, and (inside a
        # building) the area outside it.
        outer = world.places[here].inside if here in world.places else None
        local = area_of(world, art, outer) if outer else None
        roots = {here} | ({local} if local else set())
        current = {
            p.id
            for p in world.places.values()
            if p.id in roots or roots & {c.id for c in self._containers_of(world, p.id)}
        }
        for place in world.places.values():
            if place.id in current or place.id not in world.seen:
                continue
            if place.kind in ("room", "route", "region"):
                continue
            target = area_of(world, art, place.id)
            if target is not None and target in current:
                continue
            spot = position(world, place.id)
            if start is None or spot is None:
                continue
            metres = math.hypot(spot[0] - start[0], spot[1] - start[1]) * WINDING
            if metres < 50:
                continue
            out.append((place.id, place.name, int(metres / WALK_M_PER_S)))
        out.sort(key=lambda item: item[2])
        return out

    @staticmethod
    def _containers_of(world: WorldMap, place_id: str) -> list:
        out = []
        seen: set[str] = set()
        current = world.places.get(place_id)
        while current is not None and current.id not in seen:
            seen.add(current.id)
            out.append(current)
            current = world.places.get(current.inside or "")
        return out

    def _arrival(
        self, world: WorldMap, art: WorldArt, area_id: str, held: str
    ) -> tuple[int, int] | None:
        """Where a journey to an outdoor area lands: at its edge toward where
        the traveller set out from, on the road or through the gate there
        when one comes in near it. Live (Oct 17 2026), a walk up the north
        road was told it ended "in Hellsville, outside The diner", and the
        storyteller wrote the barricade at the south gate. None for a
        building's inside, or a journey from within the area."""
        area = self.area(area_id, world, art)
        me = self.held_point()
        if area is None or area.indoor or me is None or point_tile(world, area, *me) is not None:
            return None
        edge = clamped_tile(world, area, *me)
        if edge is None:
            return None
        blocked = area.blocked()
        roads = [
            (math.hypot(x - edge[0], y - edge[1]), x, y)
            for y in range(
                max(0, edge[1] - ARRIVAL_ROAD_REACH),
                min(area.height, edge[1] + ARRIVAL_ROAD_REACH + 1),
            )
            for x in range(
                max(0, edge[0] - ARRIVAL_ROAD_REACH),
                min(area.width, edge[0] + ARRIVAL_ROAD_REACH + 1),
            )
            if area.rows[y][x] in "=+" and area.walkable(x, y, blocked)
        ]
        if roads:
            _d, x, y = min(roads)
            return x, y
        return stand_near(area, edge, set(), seed=held)

    # --- what became of the things ------------------------------------------------

    # --- turns -----------------------------------------------------------------

    # --- what the passage did to the things on the play view ----------------------

    def _map_person(self, name: str, world: WorldMap | None = None) -> str | None:
        """Who `name` is on the map: someone in the story, else (with the
        map) someone not named yet, by their label or description, else
        someone the map keeps by a name with no card."""
        from sealedlore.engine.scene_state import same_person
        from sealedlore.models.character import Character

        for person in self.map_people():
            if same_person(name, person):
                return person.name
        if world is None or not name:
            return None
        found = unknown_for(name, world)
        if found is not None:
            return found
        # Named by a passage with no card ("Unknown 1" named Ruth, live): the
        # map keeps them by that name, and without this the read could never
        # stand them anywhere again.
        return next(
            (
                known
                for known in world.where
                if known not in world.unknowns and same_person(name, Character(id="", name=known))
            ),
            None,
        )

    # --- an area's edges -----------------------------------------------------------

    def edge_here(self) -> str | None:
        """The side ("north"…) of an outdoor area whose edge the author's
        character stands on, or None."""
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        area = self.area(me.area if me else None)
        if me is None or area is None or area.indoor:
            return None
        if me.y == 0:
            return "north"
        if me.y == area.height - 1:
            return "south"
        if me.x == 0:
            return "west"
        if me.x == area.width - 1:
            return "east"
        return None

    def _beyond(self, side: str, at: Spot | None = None) -> tuple[Spot, float] | None:
        """The first tile of another area straight on past this edge, and
        how far it is: a place's plan within EDGE_REACH metres (across a
        gap of open country, as before windows), else the window of open
        country just past the edge (the author: the world is limitless).
        From `at` (a tile on the edge), else from where the author's
        character stands."""
        world, art = self.world(), self._art_read()
        state = self.area_state()
        me = at if at is not None else state.spots.get(self.held_name() or "")
        area = self.area(me.area if me else None, world, art)
        start = tile_point(world, area, me.x, me.y) if me and area else None
        if start is None or area is None:
            return None
        dx, dy = EDGE_STEPS[side]
        step = max(metres_per_tile(world, area), 1.0)
        reach = step
        window: tuple[Spot, float] | None = None
        while reach <= EDGE_REACH:
            x, y = start[0] + dx * reach, start[1] + dy * reach
            spot = self._mark_spot(Mark(token="", x=x, y=y), world, art)
            if spot is not None and spot.area != area.id:
                there = self.area(spot.area, world, art)
                if there is not None and not there.indoor:
                    if there.window:
                        # Never inside a place's own box on the land (the
                        # window boxes a place with a plan as a fenced yard,
                        # rounded out to whole land tiles): "Walk on into open
                        # country" from The Pilgrims' Rest landed on the Rest's
                        # own gate, and `door_here` led straight back in (the
                        # review, Oct 20 2026). On past the box, up to the reach.
                        landing = self._landing(there, spot, side, avoid=_box_tiles(there))
                        if window is None and landing is not None:
                            window = Spot(area=spot.area, x=landing[0], y=landing[1]), reach
                    else:
                        landing = self._landing(there, spot, side)
                        if landing is None:
                            return None  # water, trees or rock bar the way there
                        return Spot(area=spot.area, x=landing[0], y=landing[1]), reach
            reach += step
        return window

    @staticmethod
    def _landing(
        area: Area, spot: Spot, side: str, *, avoid: Collection[tuple[int, int]] = ()
    ) -> tuple[int, int] | None:
        """A tile to stand on at the far area's edge near `spot`: along the
        edge a few tiles either way and a tile or two in, walkable, never
        past what bars it (`stand_near` would cross a river to the nearest
        dry tile; the review, Oct 16 2026). "In" is on into the far area,
        away from the one being left: tile y grows south, so for `side`
        north (the far area lies north) in is y - 1. It stepped the other
        way, back off the far area's edge, so a line of trees just past the
        edge read as barred though the ground beyond it was open (the
        review, Oct 20 2026). `avoid`: tiles never to land on."""
        along = (1, 0) if side in ("north", "south") else (0, 1)
        inward = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}[side]
        blocked = area.blocked() | set(avoid)
        for depth in range(0, EDGE_LANDING_IN + 1):
            for offset in sorted(range(-EDGE_LANDING_ALONG, EDGE_LANDING_ALONG + 1), key=abs):
                x = spot.x + along[0] * offset + inward[0] * depth
                y = spot.y + along[1] * offset + inward[1] * depth
                if area.walkable(x, y, blocked):
                    return x, y
        return None

    def edge_tile(self, side: str) -> tuple[Spot, list[tuple[int, int]]] | None:
        """The tile on this side's edge of the author's character's outdoor
        area that they can walk to, nearest them, with the way there (one
        tile when they stand on it); None indoors or with no way to that
        edge. Leaving is offered from anywhere in the area (the author, Oct
        23 2026: with moves as turns, walking to the edge first cost a turn
        of its own)."""
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        area = self.area(me.area if me else None)
        if me is None or area is None or area.indoor:
            return None
        if side == "north":
            tiles = [(x, 0) for x in range(area.width)]
        elif side == "south":
            tiles = [(x, area.height - 1) for x in range(area.width)]
        elif side == "west":
            tiles = [(0, y) for y in range(area.height)]
        elif side == "east":
            tiles = [(area.width - 1, y) for y in range(area.height)]
        else:
            return None
        blocked = area.blocked()
        tiles = [t for t in tiles if area.walkable(t[0], t[1], blocked)]
        tiles.sort(key=lambda t: abs(t[0] - me.x) + abs(t[1] - me.y))
        for x, y in tiles[:EDGE_TRIES]:
            steps = self.walk_route(x, y)
            if steps:
                return Spot(area=me.area, x=x, y=y), [tuple(step) for step in steps]
        return None

    def edge_choices(self, side: str) -> list[tuple[str, str, int]]:
        """Where the author's character can go on past this edge: ("area",
        its name, seconds) for an area straight on that adjoins this one,
        then ("place id", name, seconds on foot) for the known places that
        lie that way, nearest first. From anywhere in the area: the walk to
        the edge is counted in."""
        out: list[tuple[str, str, int]] = []
        world = self.world()
        art = self._art_read()
        edge = self.edge_tile(side)
        beyond = self._beyond(side, edge[0]) if edge is not None else None
        if beyond is not None and edge is not None:
            spot, metres = beyond
            there = self.area(spot.area, world, art)
            name = there.name if there is not None else "open country"
            if there is not None and there.within:
                name = f"{name} ({there.within})"
            here = self.area(edge[0].area, world, art)
            walk = walk_metres(here, edge[1]) if here is not None else 0.0
            out.append(("area", name, int((metres + walk) / WALK_M_PER_S)))
        me = self.held_point()
        if me is None:
            return out
        want = {"north": 0, "east": 90, "south": 180, "west": 270}[side]
        adjoining = beyond[0].area if beyond is not None else None
        for place_id, name, seconds in self.travel_choices():
            spot = position(world, place_id)
            if spot is None:
                continue
            if adjoining is not None and (
                place_id == adjoining
                or (not is_window(adjoining) and area_of(world, art, place_id) == adjoining)
            ):
                continue  # offered once, as the walk on (a plan's; a window is wide)
            angle = (math.degrees(math.atan2(spot[0] - me[0], spot[1] - me[1])) + 360) % 360
            if min(abs(angle - want), 360 - abs(angle - want)) <= EDGE_ARC:
                out.append((place_id, name, seconds))
        return out[:EDGE_CHOICES]

    def cross_edge(self, side: str) -> AreaState | None:
        """On past the edge into the area that adjoins it, as through a door:
        no turn written, the clock moved on by the gap."""
        beyond = self._beyond(side)
        if beyond is None:
            return None
        spot, metres = beyond
        world, art = self.world(), self._art_read()
        state = self.area_state()
        held = self.held_name()
        here = self.area(state.spots[held].area, world, art)
        there = self.area(spot.area, world, art)
        if here is None or there is None:
            return None
        taken = {(s.x, s.y) for n, s in state.spots.items() if n != held and s.area == spot.area}
        x, y = (spot.x, spot.y)
        if (x, y) in taken:
            x, y = stand_near(there, (x, y), taken)
        state.spots[held] = Spot(area=spot.area, x=x, y=y)
        self._add_time(state, walk_seconds(metres), here.name, there.name)
        self._set_live(state)
        return state

    # --- marked spots (engine/marks.py) ------------------------------------------

    def held_point(
        self,
        state: AreaState | None = None,
        world: WorldMap | None = None,
        art=None,
        *,
        held: str | None = None,
    ) -> tuple[float, float] | None:
        """Where the author's character (`held`: the turn's, else the one
        held now) stands, in metres (east, north): their tile on the map,
        else their place's spot."""
        world = world if world is not None else self.world()
        art = art if art is not None else self._art_read()
        held = held or self.held_name()
        if state is None:
            state = self.area_state() if self.map_first else AreaState()
        me = state.spots.get(held or "")
        area = self.area(me.area, world, art) if me is not None else None
        if me is not None and area is not None:
            point = tile_point(world, area, me.x, me.y)
            if point is not None:
                return point
        here = world.where.get(held or "") or world.start
        return position(world, here) if here else None

    def _mark_spot(self, mark: Mark, world: WorldMap, art: WorldArt) -> Spot | None:
        """The tile a mark lies on, on the map as it is now: in the place
        there, or on the window of open country there."""
        place_id = place_at(world, art, mark.x, mark.y)
        area = self.area(area_of(world, art, place_id, (mark.x, mark.y)), world, art)
        tile = point_tile(world, area, mark.x, mark.y) if area is not None else None
        return Spot(area=area.id, x=tile[0], y=tile[1]) if tile else None

    # --- the clock ---------------------------------------------------------------

    def map_clock(self) -> tuple[int, int] | None:
        """(day, minute of the day) on the map's clock: the plot's, when the
        story has a plot; else the walking and the time the passages told.
        Shown to the author; never to the storyteller."""
        if self.story.visual:
            # No passages move the plot's clock in a visual story: the walking,
            # the doors and the journeys are all of it, from the plot's start.
            minutes = self.visual_start_minute() + self.area_state().seconds // 60
        elif self.story.plot is not None:
            try:
                minutes = self.chronicle.minutes
            except AttributeError:
                return None
        else:
            minutes = self.area_state().seconds // 60
        return minutes // (24 * 60) + 1, minutes % (24 * 60)

    # --- drawing what play needs (engine/tile_draw.py) ---------------------------

    # --- the tile set (engine/tile_sets.py, engine/tile_draw.py) -----------------

    # --- the AI's choice of the closest set ---------------------------------------

    # --- the offer when a story's map begins ---------------------------------------

    # --- a new own set: designed, approved, drawn ----------------------------------

    # --- one asset at a time (Look ▾ → Edit the tile set…) -------------------------


def _twin(area: Area, name: str, world: WorldMap):
    """The area's own drawn thing of this name (a plan's object or a piece
    of furniture, not one of the map's things laid on it), or None: a map's
    thing that is one of those is shown as it, not laid beside it."""
    wanted = key(bare(name))
    own = [p for p in area.placements if p.id not in world.things]
    exact = next((p for p in own if key(bare(p.name)) == wanted), None)
    if exact is not None:
        return exact
    # The map's "Pickup truck" is the barn plan's "rusted pickup truck on
    # flat tyres" (live, both were drawn).
    return next((p for p in own if same_thing(name, p.name)), None)


def _own_info(name: str, found: OwnTileSet) -> TileSetInfo:
    """A story's own set as a manifest, from its words: its title, the light
    it gave, whether its water is molten."""
    words = dict(found.words)
    try:
        light = tuple(int(c) for c in words.get("light", "").split(","))
    except ValueError:
        light = ()
    return TileSetInfo(
        name=name,
        title=found.title or "This story's own",
        words=words,
        lava=words.get("lava") == "yes",
        **({"light": light} if len(light) == 3 else {}),
    )


# An in-character turn that takes the author's character in through the
# door they stand at (`stamp_turn`).
_GOES_IN = re.compile(
    # "I", then a few words of the same clause (never another subject: a
    # name, "him", "to" before the verb), then the way in.
    r"\b(?:I|[Ww]e)\b(?:\s+(?!to\b|him\b|her\b|them\b)[a-z']+){0,6}?\s+"
    r"(?:(?:step|go|walk|come|duck|slip|head|move|get|climb|edge)(?:s|ed)?\s+(?:back\s+)?"
    r"(?:in|inside|indoors)\b(?!\s+front)|enter(?:s|ed)?\b|went\s+in\b)",
)


def _has_an_inside(world: WorldMap, place_id: str) -> bool:
    """A building, room or vehicle: stood on the ground round it until its
    inside is drawn, then inside (`Spot.for_place`). Live (Oct 18 2026)
    Wren went into Cade's shop before its inside was drawn and stayed on the
    town's ground at its door, with Cade inside, once it was."""
    place = world.places.get(place_id)
    return place is not None and place.kind in ("building", "room", "vehicle")


def _close_quietly(client: ChatProvider | None) -> None:
    """A detached text client closed once its run is over (they were never)."""
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
