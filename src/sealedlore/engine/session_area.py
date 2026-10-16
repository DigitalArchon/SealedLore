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
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sealedlore.engine.area import (
    WALK_M_PER_S,
    Area,
    _front,
    anchor_of,
    anchor_tile,
    anchors_near,
    area_of,
    bare,
    build_area,
    build_window,
    clamped_tile,
    exit_side,
    front_of,
    inside_door,
    metres_per_tile,
    path,
    point_tile,
    render_positions,
    stand_near,
    tile_point,
    walk_metres,
    walk_seconds,
    walked_words,
    where_words,
)
from sealedlore.engine.chronicle_read import stated_skip
from sealedlore.engine.furnish import FURNISH_MAX_TOKENS, furnish_messages, parse_furnishing
from sealedlore.engine.marks import (
    chain_words,
    compass,
    footprint,
    mark_token,
    marks_in,
    nearest_place,
    place_at,
)
from sealedlore.engine.session_world_art import _today
from sealedlore.engine.tile_draw import (
    RUN_LIMIT,
    SPRITE_SIZE,
    TEXTURE_SIZE,
    item_jobs,
    parse_tile_words,
    style_words,
    tile_jobs,
    tileset_messages,
    walker_jobs,
    world_words,
)
from sealedlore.engine.tile_sets import (
    OUTDOOR_SPRITES,
    SPRITES,
    TEXTURES,
    TileSetInfo,
    choose_set,
    item_slugs,
    slug,
)
from sealedlore.engine.validators import locate_quote
from sealedlore.engine.world import _NOTHING_SAID, applied, key
from sealedlore.engine.world_art import is_window, parse_window, patch_at, patch_under
from sealedlore.engine.world_layout import (
    CHILD_SHARE,
    KIND_SIZE,
    describe_metres,
    moves,
    position,
    size_of,
)
from sealedlore.engine.world_pictures import MAP_PICTURES_DIR, PictureJob, looks_of
from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.area import AreaState, LiveArea, MapAction, Mark, Spot
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID, Node, Usage
from sealedlore.models.world import WorldArt, WorldDelta, WorldMap
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError
from sealedlore.providers.images import ImageClient, build_image_payload, data_url, loggable_payload
from sealedlore.storage import tile_sets as shipped
from sealedlore.storage.image_meta import MetadataError, picture_kind, strip_metadata
from sealedlore.storage.images import MEDIA_TYPES

# Seconds taken going through a door.
DOOR_SECONDS = 3
# A way between areas winds: the straight line times this.
WINDING = 1.25
# Someone placed with the author's character stands this close to them.
BESIDE = 3
# How far from a building's door someone "at" it may stand before they are
# stood at it again.
AT_A_PLACE = 8
MAX_READ_MINUTES = 7 * 24 * 60
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


@dataclass
class TileAsset:
    """One asset of the tile set in use, as the editor lists it."""

    role: str
    label: str
    group: str
    # "the set's", "drawn for this story", "changed for this story".
    source: str
    # What it shows, in words: what a redraw is drawn from.
    words: str


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


class AreaRuntime:
    _area_cache: dict | None = None
    _asset_thread: threading.Thread | None = None
    _asset_client: ImageClient | None = None
    _asset_role: str | None = None
    # The roles queued after the one being drawn (several avatars at once).
    _asset_queue: list[tuple[str, str]] = []
    # The picture jobs of the tile run under way (`tile_art_jobs`'s keys).
    _tile_keys: set[str] = set()
    _tile_thread: threading.Thread | None = None
    _tile_client: ImageClient | None = None
    _tile_text: ChatProvider | None = None
    _tile_total = 0
    _tile_done = 0
    _own_set_thread: threading.Thread | None = None
    _own_set_client: ImageClient | None = None
    _own_set_total = 0
    _own_set_done = 0

    # --- what is played on ---------------------------------------------------

    @property
    def map_first(self) -> bool:
        """The story is played on its map: every story that keeps one."""
        return self.map_on

    def area_people(self) -> list[str]:
        return [c.name for c in self.map_people()]

    def tile_set_name(self, art: WorldArt | None = None) -> str:
        """The tile set the map is drawn in (`engine.tile_sets.choose_set`)."""
        art = art or self.map_art_copy()
        sets = {name: info for name, (info, _folder) in shipped.shipped_sets().items()}
        if art.tile_set == "own" and art.tile_assets:
            return "own"
        looks = [
            str((layout.spec or {}).get("ground") or "")
            for states in art.layouts.values()
            for layout in states.values()
        ]
        chosen = art.tile_set if art.tile_set != "own" else ""
        return choose_set(chosen, self.map_setting(), looks, sets)

    def tile_set(self, art: WorldArt | None = None) -> tuple[TileSetInfo, Any]:
        """(the set's manifest, a reader of its assets by role): the story's
        own drawings first (an item drawn for this story, its own set), then
        the shipped set's files."""
        art = art or self.map_art_copy()
        name = self.tile_set_name(art)
        sets = shipped.shipped_sets()
        info, folder = sets.get(name, (None, None))
        if name == "own":
            words = dict(art.tile_words)
            try:
                light = tuple(int(c) for c in words.get("light", "").split(","))
            except ValueError:
                light = ()
            info = TileSetInfo(
                name="own",
                title="This story's own",
                words=words,
                lava=words.get("lava") == "yes",
                **({"light": light} if len(light) == 3 else {}),
            )
        if info is None:
            info = TileSetInfo(name="")
        items = {p.subject_id: p.file for p in art.pictures if p.subject == "item"}
        own = dict(art.tile_assets)
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
            return role in own or (role.startswith("items/") and role[6:] in items)

        read.from_story = from_story  # type: ignore[attr-defined]
        return info, read

    def generic_people(self) -> list[str]:
        """The tile set's generic people, by role."""
        name = self.tile_set_name()
        found = shipped.shipped_sets().get(name)
        return shipped.asset_names(found[1], "people") if found else []

    def walker_file(self, name: str, art: WorldArt | None = None) -> str | None:
        """The file of a person's walking sprite, if one is drawn."""
        art = art or self.map_art_copy()
        changed = art.tile_overrides.get(f"walker/{name}")
        if changed is not None:
            return changed or None  # "": the set's generic people
        found = art.picture_for("walker", name)
        return found.file if found is not None else None

    def walker_asset(self, name: str) -> tuple[bytes | None, str]:
        """A person's avatar on the map as the Cast page shows it: its file,
        or None when a stand-in walks for them, and where it came from (as
        `TileAsset.source`; "" for none)."""
        art = self.map_art_copy()
        file = self.walker_file(name, art)
        data = self.pictures.read(file) if file else None
        if data is None:
            return None, ""
        changed = art.tile_overrides.get(f"walker/{name}")
        return data, "changed for this story" if changed else "drawn for this story"

    def tile_style(self, art: WorldArt | None = None) -> str:
        """The pixel art's style in a few words, for every picture drawn for
        the map: the tile set's own (`tile_draw.style_words`)."""
        art = art or self.map_art_copy()
        info, _read = self.tile_set(art)
        return style_words(self._set_words(art, info))

    def _set_words(self, art: WorldArt, info: TileSetInfo) -> dict[str, str]:
        """The words every prompt names the world and style by: the set in
        use's; a story's own words only while its own set is the one in use
        (they leaked into a shipped set's prompts; the second review)."""
        own = art.tile_words if info.name == "own" else {}
        return {**info.words, **own}

    def area(
        self, area_id: str | None, world: WorldMap | None = None, art: WorldArt | None = None
    ) -> Area | None:
        """The area of `area_id`, a place's plan or a window of open
        country (engine/area.py `build_window`), built from the map at the
        leaf (kept while nothing it is built from changes)."""
        if area_id is None:
            return None
        world = world if world is not None else self.world()
        art = art if art is not None else self.map_art_copy()
        if is_window(area_id):
            return self._window(area_id, world, art)
        place = world.places.get(area_id)
        if place is None:
            return None
        layout = art.layout_shown(place.id, place.state)
        if layout is None:
            return None
        plan = art.furnishing_for(place.id, place.state)
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
            plan.made_at if plan is not None else "",
            self.story.defaults.map_density,
            children,
            inner,
            tuple(self.area_people()),
        )
        if self._area_cache is None:
            self._area_cache = {}
        found = self._area_cache.get(cache_key)
        if found is None:
            found = build_area(
                world,
                art,
                place.id,
                density=self.story.defaults.map_density,
                people=self.area_people(),
            )
            if len(self._area_cache) > 24:
                self._area_cache.clear()
            self._area_cache[cache_key] = found
        return found

    def _window(self, area_id: str, world: WorldMap, art: WorldArt) -> Area | None:
        """A window of open country, kept while the land under it, the
        places in it and the people are the same."""
        parsed = parse_window(area_id)
        if parsed is None:
            return None
        west, north, side = parsed
        patch = patch_at(art, west, north, side) or patch_under(
            art, west + side / 2, north - side / 2
        )
        inside = tuple(
            sorted(
                (
                    p.id,
                    p.x,
                    p.y,
                    p.size,
                    p.kind,
                    p.state,
                    p.inside,
                    art.layout_shown(p.id, p.state) is not None,
                )
                for p in world.places.values()
                if p.x is not None
                and p.y is not None
                and abs(p.x - (west + side / 2)) <= side
                and abs(p.y - (north - side / 2)) <= side
            )
        )
        cache_key = (
            area_id,
            patch.id if patch is not None else "",
            patch.made_at if patch is not None else "",
            len(patch.rows) if patch is not None else 0,
            tuple(sorted((p.id, p.ground) for p in art.terrain)),
            inside,
            tuple(self.area_people()),
        )
        if self._area_cache is None:
            self._area_cache = {}
        found = self._area_cache.get(cache_key)
        if found is None:
            found = build_window(world, art, area_id, people=self.area_people())
            if len(self._area_cache) > 24:
                self._area_cache.clear()
            self._area_cache[cache_key] = found
        return found

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

    def restore_area(self, node: Node) -> None:
        """Take back this turn: where everyone stood as the turn carried it
        (the walking and travel before it, applied) is live again at the
        leaf the turn comes off, so the turn sent again carries it again.
        It used to be lost with the node, and the same turn moved nobody."""
        if not self.map_first or node.meta.area is None:
            return
        state = node.meta.area.model_copy(deep=True)
        if state.action is not None:
            # Applied already: its time never counted twice, its destination
            # (a place with no plan yet has no spot to stand on) kept.
            state.action = state.action.model_copy(update={"seconds": 0})
        state.marks = []
        self.story.area_live = LiveArea(
            leaf_id=node.parent_id,
            held=self.map_held_name(node.meta.controlled_character_id),
            state=state,
        )

    def area_state(self) -> AreaState:
        """Where everyone stands now: the walking since the last turn, else
        the snapshot at the leaf, settled onto the map as it stands."""
        live = self._live()
        state = live if live is not None else self._snapshot_on(self.path())
        state = state.model_copy(deep=True) if state is not None else AreaState()
        if live is None:
            state.walked, state.walked_from, state.walked_words, state.action = 0, "", "", None
        return self.settle(state, self.world(), self.map_art_copy())

    def held_name(self) -> str | None:
        return self.map_held_name(self.story.held_character_id)

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
            for name in self._present_names():
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
            if point is None and name != held and place_id == where.get(held or ""):
                point = self._spot_point(mine, world, art)
            target = area_of(world, art, place_id, point)
            if (
                name == held
                and walked
                and spot is not None
                and self.area(spot.area, world, art) is not None
            ):
                spots[name] = spot
                continue
            if target is None:
                continue
            area = self.area(target, world, art)
            if area is None:
                continue
            if (
                spot is not None
                and spot.area == target
                and (name == held or self._still_at(area, world, spot, place_id))
            ):
                spots[name] = spot
                continue
            near_me = (
                name != held
                and mine is not None
                and mine.area == target
                and place_id == where.get(held or "")
            )
            new = self._stand(
                world,
                art,
                target,
                place_id,
                name,
                spots,
                mine if near_me else None,
                point=point if area.window else None,
            )
            if new is not None:
                spots[name] = new
        # Nobody stands in fire or a wall a redrawn plan put under them
        # (kept there, every walk was refused; the second review).
        for name, spot in list(spots.items()):
            area = self.area(spot.area, world, art)
            if area is None or area.walkable(spot.x, spot.y):
                continue
            taken = {(s.x, s.y) for n, s in spots.items() if n != name and s.area == spot.area}
            x, y = stand_near(area, (spot.x, spot.y), taken, seed=name)
            spots[name] = Spot(area=spot.area, x=x, y=y)
        state.spots = spots
        return state

    def _present_names(self) -> list[str]:
        """The people the scene has here, by the names the map uses."""
        if self.story.chat:
            return []
        scene = self.story.scene
        names = [c.name for c in self.visible_cast() if c.id in scene.present_character_ids]
        for other in scene.present_others:
            person = self._map_person(other)
            names.append(person or other)
        return list(dict.fromkeys(names))

    def _still_at(self, area: Area, world: WorldMap, spot: Spot, place_id: str) -> bool:
        anchor = anchor_of(area, world, place_id)
        if anchor is None:
            return True
        return math.hypot(spot.x - anchor[0], spot.y - anchor[1]) <= AT_A_PLACE

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
        taken = {(s.x, s.y) for s in spots.values() if s.area == area_id}
        if beside is not None:
            near = (beside.x, beside.y)
            x, y = stand_near(area, near, taken | {near}, seed=name, reach=BESIDE)
            return Spot(area=area_id, x=x, y=y)
        near = anchor_of(area, world, place_id)
        if near is None and area.window:
            at = point if point is not None else position(world, place_id)
            near = clamped_tile(world, area, *at) if at is not None else None
        if near is None:
            near = self._way_in(area)
        x, y = stand_near(area, near, taken, seed=name, reach=0 if area.window else 2)
        return Spot(area=area_id, x=x, y=y)

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

    def here_place(self) -> str | None:
        """The place the author's character stands in now: the live spot's
        (walked, through a door, since the last turn), else the map's."""
        if not self.map_first or self.in_private:
            return None
        world = self.world()
        me = self.area_state().spots.get(self.held_name() or "")
        return self.place_of_spot(me, world) or world.where.get(self.held_name() or "")

    def place_of_spot(self, spot: Spot | None, world: WorldMap) -> str | None:
        """The place a spot is in, for the map's `where`: the area's place
        (a building's inside is that building); on a window of open country
        the place holding the window (a region), or None in no place (the
        map's `where` has no word for nowhere: it keeps the last place)."""
        if spot is None:
            return None
        if spot.area in world.places:
            return spot.area
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
        return path(area, (me.x, me.y), goal, blocked=others)

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

    def door_here(self) -> tuple[str, str] | None:
        """(where it leads, its name) when the author's character stands in a
        door that leads into or out of a building."""
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        area = self.area(me.area if me else None)
        if me is None or area is None:
            return None
        world = self.world()
        entrance = area.entrance_at(me.x, me.y)
        if entrance is not None:
            return entrance.place_id, world.places[entrance.place_id].name
        if area.indoor and (me.x, me.y) in area.exits and area.parent is not None:
            parent = world.places.get(area.parent)
            return area.parent, parent.name if parent else "outside"
        return None

    def go_through(self) -> AreaState | None:
        """Through the door the author's character stands in: into the
        building, or out of it to stand outside its door."""
        door = self.door_here()
        if door is None:
            return None
        state = self.area_state()
        held = self.held_name()
        me = state.spots[held]
        world, art = self.world(), self.map_art_copy()
        here = self.area(me.area, world, art)
        there = self.area(door[0], world, art)
        if here is None or there is None:
            return None
        taken = {(s.x, s.y) for n, s in state.spots.items() if n != held and s.area == there.id}
        if not here.indoor:
            entrance = here.entrance_at(me.x, me.y)
            spot = inside_door(there, entrance.side if entrance else None) or self._way_in(there)
        else:
            side = exit_side(here, me.x, me.y)
            spot = front_of(there, here.place_id, side)
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
        state.spots[held] = Spot(area=there.id, x=x, y=y)
        self._add_time(state, DOOR_SECONDS, here.name, there.name)
        self._set_live(state)
        return state

    def travel_choices(self) -> list[tuple[str, str, int]]:
        """(place id, name, seconds on foot) for each known place the
        author's character could set off for: the areas the map has drawn,
        and the known places with a spot of their own, nearest first."""
        world = self.world()
        art = self.map_art_copy()
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        here = me.area if me is not None else world.where.get(self.held_name() or "")
        if here is None:
            return []
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

    def travel(self, place_id: str) -> str:
        """Set off for a place: the author's character is stood there, at the
        way in, and the clock moved on by the way's length on foot. The
        turn's text for the composer (the author's ruling: the menu fills
        the composer)."""
        world, art = self.world(), self.map_art_copy()
        place = world.places.get(place_id)
        if place is None:
            return ""
        choices = {pid: seconds for pid, _n, seconds in self.travel_choices()}
        seconds = choices.get(place_id, 0)
        state = self.area_state()
        held = self.held_name()
        target = area_of(world, art, place_id)
        spot = None
        if target is not None:
            spot = self._stand(world, art, target, place_id, held or "", dict(state.spots), None)
        text = self.texts.fill("map.turn.travel", name=place.name)
        # Nothing moves until the turn is sent (`stamp_turn`): a turn
        # rewritten travels nowhere. A place not drawn yet: the turn's delta
        # says where they went, and the map draws it after the passage.
        state.action = MapAction(
            verb="travel",
            target=place.name,
            target_id=place_id,
            text=text,
            spot=spot,
            seconds=seconds,
        )
        self._set_live(state)
        return text

    def map_action(self, verb: str, target: str, target_id: str = "") -> str:
        """The turn a menu choice writes (`map.turn.<verb>`), kept with the
        live state so the storyteller is told what it means when it is sent
        as written (or added to)."""
        state = self.area_state()
        if verb in ("use", "take", "destroy", "eat"):
            target = bare(target)  # "the stove", not "the a stove"
        text = self.texts.fill(f"map.turn.{verb}", name=target)
        state.action = MapAction(verb=verb, target=target, target_id=target_id, text=text)  # type: ignore[arg-type]
        self._set_live(state)
        return text

    def people_here(self) -> list[tuple[str, Spot]]:
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        if me is None:
            return []
        return [(n, s) for n, s in state.spots.items() if s.area == me.area]

    # --- turns -----------------------------------------------------------------

    def stamp_turn(self, user_node: Node) -> None:
        """The walking done since the last turn goes with this one: its
        snapshot, and a move to where the author's character walked to (the
        map's `where`), so the read, the fog and the art follow."""
        if not self.map_first or self.in_private:
            return
        live = self._live()
        # Every turn carries where everyone stands: walked since the last
        # turn, or as they were.
        state = live.model_copy(deep=True) if live is not None else self.area_state()
        if state.action is not None and state.action.text not in user_node.content:
            state.action = None  # the author wrote a turn of their own
        user_node.meta.area = state
        held = self.held_name()
        world = self.world()
        to = None
        action = state.action
        if action is not None and action.verb == "travel" and held is not None:
            # Set off, now that the turn goes: there, as far as the map has
            # drawn it, the way's length on the clock.
            here = state.spots.get(held)
            frm = self.area(here.area, world) if here is not None else None
            if action.spot is not None:
                state.spots[held] = action.spot
            else:
                state.spots.pop(held, None)
            self._add_time(state, action.seconds, frm.name if frm else "", action.target)
            to = action.target_id or None
        if self.story.plot is None and user_node.speaker_id in (
            DIRECTOR_SPEAKER_ID,
            NARRATOR_SPEAKER_ID,
        ):
            # "Three weeks pass": the author's own statement moves the
            # map's clock, as `_author_skip` moves the plot's.
            now = state.seconds // 60
            skip = stated_skip(user_node.content, now)
            if skip is not None and skip[0] > now:
                state.seconds += (skip[0] - now) * 60
        # The spots the turn marks, resolved on the map it was written on.
        state.marks = self.resolve_marks(user_node.content, world, self.map_art_copy(), state)
        spot = state.spots.get(held or "")
        to = to or self.place_of_spot(spot, world)
        if held is not None and to is not None and world.where.get(held) != to:
            area = area_of(world, self.map_art_copy(), world.where.get(held))
            if area != to or to not in world.places:
                user_node.meta.world = WorldDelta(where={held: to})
        self.story.area_live = None

    @staticmethod
    def walk_minutes(user_node: Node) -> int:
        """The minutes a turn's walking and travel took, whole."""
        state = user_node.meta.area
        if state is None or state.walked <= 0:
            return 0
        return max(0, int(round(state.walked / 60)))

    def walk_into_plot(self, user_node: Node, chronicle) -> int:
        """The minutes this turn's walking took, added to the plot's clock
        (as the author's skip is: the read counts on from there)."""
        minutes = AreaRuntime.walk_minutes(user_node)
        if minutes <= 0:
            return 0
        chronicle.minutes += minutes
        chronicle.elapsed += minutes
        return minutes

    def area_after_read(self, node: Node, reply: dict[str, Any], source: str) -> None:
        """After the map read: everyone settled on the map the passage left,
        anyone it put at one of the spots near the author's character stood
        there, and the clock moved on by the time the passage told (without
        a plot; with one, the plot's clock read keeps time)."""
        if not self.map_first:
            return
        path = self._path_to(node.id)
        before = self._snapshot_on(path[:-1])
        state = before.model_copy(deep=True) if before is not None else AreaState()
        marks = list(state.marks)
        state.walked, state.walked_from, state.walked_words, state.action = 0, "", "", None
        state.marks = []
        world, art = self._world_on(path), self.map_art_copy()
        held = self.held_name()
        # The read placed the author's character (with a quote): the place
        # it names wins over where they stand, and they are stood at its
        # spot; otherwise they stay where they walked to (a window of open
        # country has no word in the map's `where`, so a stale place there
        # must not pull them back).
        moved = held is not None and held in (node.meta.world.where if node.meta.world else {})
        state = self.settle(state, world, art, walked=not moved)
        me = state.spots.get(held or "")
        at = reply.get("at") if isinstance(reply.get("at"), dict) else {}
        by_point = {(m.x, m.y): m for m in marks}
        # Only someone the read also placed (with a quote): `at` alone moved
        # people the passage left where they were (the second review).
        placed = {
            self._map_person(str(entry.get("who") or ""))
            for entry in (reply.get("where") or [])
            if isinstance(entry, dict)
        }
        for name, spot_name in list(at.items()):
            found = marks_in(str(spot_name or ""))
            mark = by_point.get((found[0].x, found[0].y)) if found else None
            person = self._map_person(str(name))
            if mark is None or person is None or person not in placed:
                continue
            at.pop(name)
            spot = self._mark_spot(mark, world, art)
            current = state.spots.get(person)
            if spot is None or current is None or current.area != spot.area:
                continue  # not where the map has them: settled at their place
            marked = self.area(spot.area, world, art)
            taken = {
                (s.x, s.y) for n, s in state.spots.items() if n != person and s.area == spot.area
            }
            x, y = stand_near(marked, (spot.x, spot.y), taken)  # type: ignore[arg-type]
            state.spots[person] = Spot(area=spot.area, x=x, y=y)
        area = self.area(me.area, world, art) if me is not None else None
        if area is not None:
            for name, spot_name in at.items():
                person = self._map_person(str(name))
                tile = anchor_tile(area, str(spot_name))
                if person is None or tile is None or person not in placed:
                    continue
                current = state.spots.get(person)
                if current is not None and current.area != area.id:
                    continue
                taken = {
                    (s.x, s.y) for n, s in state.spots.items() if n != person and s.area == area.id
                }
                x, y = stand_near(area, tile, taken | {tile})
                state.spots[person] = Spot(area=area.id, x=x, y=y)
        if self.story.plot is None:
            minutes = reply.get("minutes")
            quote = str(reply.get("minutes_quote") or "")
            if (
                isinstance(minutes, (int, float))
                and not isinstance(minutes, bool)
                and minutes > 0
                and quote
                and locate_quote(source, quote) is not None
            ):
                state.seconds += int(min(minutes, MAX_READ_MINUTES)) * 60
        node.meta.area = state

    def _map_person(self, name: str) -> str | None:
        from sealedlore.engine.scene_state import same_person

        for person in self.map_people():
            if same_person(name, person):
                return person.name
        return None

    def read_spots(self, path: Sequence[Node], held: str | None = None) -> str:
        """The spots near the author's character (`held`: the passage's, else
        the one held now), for the map read (`map_read.spots`); "" when the
        story isn't played on a map."""
        if not self.map_first:
            return ""
        before = self._snapshot_on(path)
        if before is None:
            return ""
        me = before.spots.get(held or self.held_name() or "")
        area = self.area(me.area if me else None)
        if me is None or area is None:
            return ""
        spots = anchors_near(area, me.x, me.y)
        return self.texts.fill("map_read.spots", spots=", ".join(spots)) if spots else ""

    def read_minutes(self, author: Node | None) -> str:
        """The story time the passage covers, asked of the map read where the
        map keeps the clock (no plot), with the walk already counted named
        (`map_read.minutes`, `map_read.walked`); "" otherwise."""
        if not self.map_first or self.story.plot is not None:
            return ""
        walked = self.walk_minutes(author) if author is not None else 0
        already = self.texts.fill("map_read.walked", minutes=str(walked)) if walked > 0 else ""
        return self.texts.fill("map_read.minutes", walked=already)

    def positions_context(
        self, nodes: Sequence[Node], held_id: str | None, *, turn_node_id: str | None = None
    ) -> str:
        """Where everyone stands, for the storyteller's map section (design
        §5): from the turn being sent (its snapshot, found by `turn_node_id`:
        on a retake the leaf is the previous take, so the leaf can't say) or,
        for a Question, where they stand now, with no turn's action or walk."""
        if not self.map_first or self.in_private:
            return ""
        held = self.map_held_name(held_id) if held_id is not None else self.held_name()
        if held is None:
            return ""
        turn = self._turn_node(nodes, turn_node_id)
        if turn is not None and turn.meta.area is not None:
            state = turn.meta.area
        else:
            turn = None
            live = self._live()
            state = live if live is not None else self._snapshot_on(nodes)
        if state is None:
            return ""
        world = self._world_on([*nodes, turn] if turn is not None else nodes)
        art = self.map_art_copy()
        me = state.spots.get(held)
        area = self.area(me.area if me else None, world, art)
        action_line = ""
        action = state.action
        if turn is not None and action is not None and action.text in turn.content:
            action_line = self.texts.fill(f"map.action.{action.verb}", name=action.target)
        if me is None or area is None:
            # Nowhere to stand yet (a place whose plan isn't drawn): the
            # journey and the choice still stand, or the storyteller wrote
            # the departure from the place left (the second review).
            if turn is None or (state.walked < 30 and not action_line):
                return ""
            place = world.places.get(world.where.get(held) or "")
            lines = [
                self.texts["map.positions"],
                f"- {held}: at {place.name} (just arrived; nothing of it is drawn yet)."
                if place is not None
                else f"- {held}: on the way.",
            ]
            if state.walked >= 30 and state.walked_words:
                lines.append(f"Since the last passage: {state.walked_words}.")
            if action_line:
                lines.append(action_line)
            return "\n".join(lines)
        text = render_positions(
            area,
            world,
            state.spots,
            held,
            walked=state.walked_words if turn is not None and state.walked >= 30 else "",
            action_line=action_line,
        )
        if not text:
            return ""
        heading = self.texts["map.positions"]
        return text.replace(text.split("\n", 1)[0], heading, 1)

    def _turn_node(self, nodes: Sequence[Node], turn_node_id: str | None) -> Node | None:
        """The author's turn being sent after `nodes`: the one named, else
        the leaf once it is attached there (a send; a retake names its turn,
        since its leaf is the previous take)."""
        if turn_node_id is not None:
            turn = next((n for n in self.nodes if n.id == turn_node_id), None)
            if turn is not None and turn.kind == "user":
                return turn
        full = self.path()
        if len(full) != len(nodes) + 1 or full[-1].kind != "user":
            return None
        if nodes and full[-2].id != nodes[-1].id:
            return None
        return full[-1]

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

    def _beyond(self, side: str) -> tuple[Spot, float] | None:
        """The first tile of another area straight on past this edge, and
        how far it is: a place's plan within EDGE_REACH metres (across a
        gap of open country, as before windows), else the window of open
        country just past the edge (the author: the world is limitless)."""
        world, art = self.world(), self.map_art_copy()
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
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
                    landing = self._landing(there, spot, side)
                    if there.window:
                        if window is None and landing is not None:
                            window = Spot(area=spot.area, x=landing[0], y=landing[1]), reach
                    elif landing is None:
                        return None  # water, trees or rock bar the way there
                    else:
                        return Spot(area=spot.area, x=landing[0], y=landing[1]), reach
            reach += step
        return window

    @staticmethod
    def _landing(area: Area, spot: Spot, side: str) -> tuple[int, int] | None:
        """A tile to stand on at the far area's edge near `spot`: along the
        edge a few tiles either way and a tile or two in, walkable, never
        past what bars it (`stand_near` would cross a river to the nearest
        dry tile; the review, Oct 16 2026)."""
        along = (1, 0) if side in ("north", "south") else (0, 1)
        inward = {"north": (0, 1), "south": (0, -1), "east": (-1, 0), "west": (1, 0)}[side]
        blocked = area.blocked()
        for depth in range(0, EDGE_LANDING_IN + 1):
            for offset in sorted(range(-EDGE_LANDING_ALONG, EDGE_LANDING_ALONG + 1), key=abs):
                x = spot.x + along[0] * offset + inward[0] * depth
                y = spot.y + along[1] * offset + inward[1] * depth
                if area.walkable(x, y, blocked):
                    return x, y
        return None

    def edge_choices(self, side: str) -> list[tuple[str, str, int]]:
        """Where the author's character can go on past this edge: ("area",
        its name, seconds) for an area straight on that adjoins this one,
        then ("place id", name, seconds on foot) for the known places that
        lie that way, nearest first."""
        out: list[tuple[str, str, int]] = []
        beyond = self._beyond(side)
        world = self.world()
        art = self.map_art_copy()
        if beyond is not None:
            spot, metres = beyond
            there = self.area(spot.area, world, art)
            name = there.name if there is not None else "open country"
            if there is not None and there.within:
                name = f"{name} ({there.within})"
            out.append(("area", name, int(metres / WALK_M_PER_S)))
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
        world, art = self.world(), self.map_art_copy()
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
        art = art if art is not None else self.map_art_copy()
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

    def mark_of_tile(self, area_id: str, x: int, y: int) -> str:
        """The token for a tile of an area, for the composer."""
        world = self.world()
        area = self.area(area_id, world)
        point = tile_point(world, area, x, y) if area is not None else None
        return mark_token(*point) if point is not None else ""

    def resolve_marks(
        self, text: str, world: WorldMap, art: WorldArt, state: AreaState | None
    ) -> list[Mark]:
        """The marks a turn's text holds, each with the place it lies in, its
        tile, and what is there in words for the storyteller."""
        found = marks_in(text)
        if not found:
            return []
        held = self.held_name()
        me = self.held_point(state, world, art)
        out = []
        for mark in found:
            place_id = place_at(world, art, mark.x, mark.y)
            spot = None
            parts: list[str] = []
            area_id = area_of(world, art, place_id, (mark.x, mark.y))
            area = self.area(area_id, world, art) if area_id else None
            tile = point_tile(world, area, mark.x, mark.y) if area is not None else None
            if area is not None and tile is not None:
                spot = Spot(area=area.id, x=tile[0], y=tile[1])
            if place_id:
                parts.append(chain_words(world, place_id))
            else:
                near = nearest_place(world, mark.x, mark.y)
                words = "open country"
                if near is not None:
                    other = world.places[near[0]]
                    way = compass(mark.x - other.x, mark.y - other.y)  # type: ignore[operator]
                    words += f", {describe_metres(near[1])} {way} of {other.name}"
                if area is not None and area.within:  # the region it lies in
                    words += f", in {area.within}"
                parts.append(words)
            if area is not None and tile is not None:
                where = where_words(area, *tile)
                if where:
                    parts.append(where)
            if me is not None and held:
                apart = math.hypot(mark.x - me[0], mark.y - me[1])
                if apart < 2:
                    parts.append(f"where {held} stands")
                else:
                    way = compass(mark.x - me[0], mark.y - me[1])
                    parts.append(f"{describe_metres(apart)} {way} of {held}")
            out.append(
                Mark(
                    token=mark.token,
                    x=mark.x,
                    y=mark.y,
                    place_id=place_id,
                    spot=spot,
                    words="; ".join(p for p in parts if p),
                )
            )
        return out

    def marks_context(self, nodes: Sequence[Node], *, turn_node_id: str | None = None) -> str:
        """The marks of the turn being sent, for the storyteller's map
        section (`map.marks`); "" when it marks none."""
        if not self.map_on or self.in_private:
            return ""
        turn = self._turn_node(nodes, turn_node_id)
        if turn is None or turn.meta.area is None or not turn.meta.area.marks:
            return ""
        lines = [f"- {m.token}: {m.words}." for m in turn.meta.area.marks]
        return self.texts.fill("map.marks", marks="\n".join(lines))

    def read_marks(self, author: Node | None) -> str:
        """The author's turn's marks, for the map read (`map_read.marks`)."""
        if author is None or author.meta.area is None or not author.meta.area.marks:
            return ""
        marks = author.meta.area.marks
        lines = [f"- {m.token}: {m.words}" for m in marks]
        return self.texts.fill("map_read.marks", marks="\n".join(lines), example=marks[0].token)

    def apply_marks(
        self, before: WorldMap, delta: WorldDelta, reply: dict[str, Any], author: Node | None
    ) -> None:
        """What the read put at a marked spot is put there exactly: a new
        place on the point (inside whatever there holds it), and anyone sent
        to one in the place it lies in."""
        marks = self._turn_marks(author)
        if not marks:
            return
        new = {key(p.name): p for p in delta.places if p.x is None and p.id not in before.places}
        art = self.map_art_copy()
        for entry in reply.get("places") or []:
            if not isinstance(entry, dict):
                continue
            mark = self._mark_of(marks, entry.get("mark"), words=True)
            place = new.get(key(str(entry.get("name") or "")))
            if mark is None or place is None:
                continue
            known = {**before.places, **{p.id: p for p in delta.places}}
            holder = known.get(place.inside or "")
            if holder is not None and holder.id not in before.places:
                # Inside a place this same read made: held to its own mark
                # when it has one, else as the read says.
                keep = holder.x is None or (
                    abs(mark.x - holder.x) <= size_of(holder) / 2
                    and abs(mark.y - holder.y) <= size_of(holder) / 2  # type: ignore[operator]
                )
            else:
                box = footprint(before, art, holder.id) if holder is not None else None
                keep = box is not None and box[0] <= mark.x <= box[2] and box[1] <= mark.y <= box[3]
            if not keep:
                place.inside = place_at(before, art, mark.x, mark.y)
                holder = known.get(place.inside or "")
            size = place.size if place.size is not None else KIND_SIZE.get(place.kind, 100.0)
            if holder is not None:
                size = min(size, size_of(holder) * CHILD_SHARE)
            place.size = size
            if moves(known, place):
                continue  # aboard a vehicle: it goes where the vehicle goes
            place.x, place.y = mark.x, mark.y
        # A thing the read puts at a marked spot is at the place there.
        after = applied(before, delta)
        for entry in reply.get("things") or []:
            if not isinstance(entry, dict):
                continue
            mark = self._mark_of(marks, entry.get("at"))
            if mark is None:
                continue
            name = key(str(entry.get("name") or ""))
            thing = next((t for t in delta.things if key(t.name) == name), None)
            if thing is not None and thing.at is None and not thing.carried_by:
                thing.at = place_at(after, art, mark.x, mark.y)

    @staticmethod
    def _turn_marks(author: Node | None) -> list[Mark]:
        return list(author.meta.area.marks) if author is not None and author.meta.area else []

    @staticmethod
    def _mark_of(marks: Sequence[Mark], value: Any, *, words: bool = False) -> Mark | None:
        """The mark a read's value names: by its coordinate; with `words`,
        also by the spot's own description when one spot is marked (live,
        the read copied "in the kitchen and eating area" for its coordinate),
        never for "none" or the like, nor for words the spot wasn't given."""
        text = str(value or "").strip()
        found = marks_in(text)
        by_point = {(m.x, m.y): m for m in marks}
        if found:
            return by_point.get((found[0].x, found[0].y))
        if not words or len(marks) != 1 or not text or _NOTHING_SAID.match(text.strip(" .")):
            return None
        given = {w for w in re.findall(r"[a-z]{3,}", marks[0].words.lower())}
        said = {w for w in re.findall(r"[a-z]{3,}", text.lower())}
        return marks[0] if len(given & said) >= 2 else None

    def apply_marks_at(
        self, before: WorldMap, delta: WorldDelta, reply: dict[str, Any], author: Node | None
    ) -> None:
        """Someone the read sends to a marked spot (its coordinate, never a
        named spot's words: those are `area_after_read`'s) is put in the
        place it lies in, once every new place has its spot."""
        marks = self._turn_marks(author)
        at = reply.get("at") if isinstance(reply.get("at"), dict) else {}
        if not marks or not at:
            return
        art = self.map_art_copy()
        after = applied(before, delta)
        for name, value in at.items():
            mark = self._mark_of(marks, value)
            person = self._map_person(str(name))
            if mark is None or person is None or person not in delta.where:
                continue  # placed by the read with a quote, or not at all
            place_id = place_at(after, art, mark.x, mark.y)
            if place_id is not None:
                delta.where[person] = place_id

    def _mark_spot(self, mark: Mark, world: WorldMap, art: WorldArt) -> Spot | None:
        """The tile a mark lies on, on the map as it is now: in the place
        there, or on the window of open country there."""
        place_id = place_at(world, art, mark.x, mark.y)
        area = self.area(area_of(world, art, place_id, (mark.x, mark.y)), world, art)
        tile = point_tile(world, area, mark.x, mark.y) if area is not None else None
        return Spot(area=area.id, x=tile[0], y=tile[1]) if tile else None

    def spot_of_point(self, x: float, y: float) -> Spot | None:
        """The tile a point on the map lies on: in the innermost place there
        with an area (a building's inside, not its roof)."""
        return self._mark_spot(Mark(token="", x=x, y=y), self.world(), self.map_art_copy())

    def go_to_point(self, x: float, y: float) -> str:
        """Set off for a spot on the map: the author's character stands on it
        (on the nearest tile they can, in the area it lies in), and the clock
        moves on by the way's length on foot. The turn's text."""
        world, art = self.world(), self.map_art_copy()
        place_id = place_at(world, art, x, y)
        held = self.held_name()
        if held is None:
            return ""
        state = self.area_state()
        me = state.spots.get(held)
        here = self.area(me.area, world, art) if me is not None else None
        spot = self._mark_spot(Mark(token="", x=x, y=y), world, art)
        if here is not None and spot is not None and spot.area == here.id:
            return ""  # in this area: walking is the way
        start = self.held_point(state, world, art)
        metres = math.hypot(x - start[0], y - start[1]) * WINDING if start else 0.0
        seconds = int(metres / WALK_M_PER_S)
        area = self.area(spot.area, world, art) if spot is not None else None
        landing = None
        if spot is not None and area is not None:
            taken = {
                (s.x, s.y) for n, s in state.spots.items() if n != held and s.area == spot.area
            }
            sx, sy = stand_near(area, (spot.x, spot.y), taken)
            landing = Spot(area=spot.area, x=sx, y=sy)
        place = world.places.get(place_id or "")
        # Open country: named by what holds it (a region), else as such.
        name = place.name if place is not None else (area.within if area else "") or "open country"
        token = mark_token(x, y)
        text = self.texts.fill("map.turn.go", name=name, mark=token)
        # Applied when the turn is sent (`stamp_turn`), as Travel… is.
        state.action = MapAction(
            verb="travel",
            target=name,
            target_id=place_id or "",
            text=text,
            spot=landing,
            seconds=seconds,
        )
        self._set_live(state)
        return text

    # --- the clock ---------------------------------------------------------------

    def map_clock(self) -> tuple[int, int] | None:
        """(day, minute of the day) on the map's clock: the plot's, when the
        story has a plot; else the walking and the time the passages told.
        Shown to the author; never to the storyteller."""
        if self.story.plot is not None:
            try:
                minutes = self.chronicle.minutes
            except AttributeError:
                return None
        else:
            minutes = self.area_state().seconds // 60
        return minutes // (24 * 60) + 1, minutes % (24 * 60)

    # --- drawing what play needs (engine/tile_draw.py) ---------------------------

    def _near_areas(self, world: WorldMap, art: WorldArt) -> list[Area]:
        """The areas the story is in or next to: the author's character's,
        the buildings in it with an inside, the one outside it."""
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        here = self.area(me.area if me else None, world, art)
        if here is None:
            return []
        found = [here]
        outer = self.area(here.parent, world, art) if here.indoor else None
        if outer is not None:
            found.append(outer)
        for area in list(found):
            for entrance in area.entrances:
                inner = self.area(entrance.place_id, world, art)
                if inner is not None and inner not in found:
                    found.append(inner)
        return found

    def tile_art_jobs(self) -> tuple[list[Area], list[PictureJob]]:
        """(buildings still to furnish, sprites still to draw), nearest the
        story first. Furnishing is a text model's (Draw the map); sprites are
        map pictures (the standing approval and its daily limit)."""
        if not self.map_first or not self.config.map_art or self.open_span is not None:
            return [], []
        world, art = self.world(), self.map_art_copy()
        areas = self._near_areas(world, art)
        furnish = [
            a
            for a in areas
            if a.indoor
            and a.rooms
            and art.furnishing_for(a.place_id, world.places[a.place_id].state) is None
            and art.failures.get(f"furnish:{a.place_id}", 0) < 2
        ]
        if not self.map_pictures_on:
            return furnish, []
        room = self.map_pictures_left_today(art)
        if room <= 0:
            return furnish, []
        info, read = self.tile_set(art)
        source_items = {p.subject_id for p in art.pictures if p.subject == "item"}

        def has_sprite(name: str) -> bool:
            return any(s in source_items or read(f"items/{s}") for s in item_slugs(name))

        def failed(job_key: str) -> bool:
            return art.failures.get(job_key, 0) >= 2

        words = world_words(self._set_words(art, info), self.map_setting())
        style = style_words(self._set_words(art, info))
        jobs = item_jobs(areas, has_sprite, words, self.texts, style=style, failed=failed)
        here = {name for name, spot in self.area_state().spots.items()}
        people = []
        for card in (*self.visible_cast(), *self.visible_supporting()):
            portrait = card.reference_images[0].file if card.reference_images else None
            people.append((card.name, portrait, looks_of(card.summary, card.full_description)))
        people.sort(key=lambda p: p[0] not in here)

        def has_walker(name: str) -> bool:
            # Drawn for the story, or the author's own choice for them (a
            # redraw, their own picture, or the set's figure: "").
            return (
                art.picture_for("walker", name) is not None
                or art.tile_overrides.get(f"walker/{name}") is not None
            )

        jobs = walker_jobs(people, has_walker, words, self.texts, style=style, failed=failed) + jobs
        drawing = self.tile_asset_drawing  # the author's own draw of the same figure
        if drawing is not None:
            jobs = [j for j in jobs if j.key != _asset_key(drawing)]
        return furnish, jobs[: min(RUN_LIMIT, room)]

    def _start_tile_art(self) -> bool:
        """Furnish and draw what the areas near the story need, on a thread
        of its own (called with the map's art, under its start lock)."""
        if self._map_closed:
            return False
        thread = self._tile_thread
        if thread is not None and thread.is_alive():
            return False
        furnish, pictures = self.tile_art_jobs()
        if not furnish and not pictures:
            return False
        self._tile_total, self._tile_done = len(furnish) + len(pictures), 0
        self._tile_keys = {job.key for job in pictures}
        detached = getattr(self.provider, "detached", None)
        self._tile_text = detached() if callable(detached) and furnish else None
        media = self.config.images()
        client = (
            ImageClient(media.base_url, media.api_key, api=media.api)
            if pictures and media is not None
            else None
        )
        self._tile_client = client
        self._art_stop = False
        if self._tile_text is None and furnish:
            # A provider without `detached` (the scripted mock): inline.
            self._tile_run(furnish, [], None, self.provider)
            return True
        thread = threading.Thread(
            target=self._tile_run,
            args=(furnish, pictures if client is not None else [], client, self._tile_text),
            name="map-tiles",
            daemon=True,
        )
        self._tile_thread = thread
        thread.start()
        return True

    def start_tile_art(self) -> bool:
        """What the play view needs now (a building walked into), drawn in
        the background."""
        with self._starting():
            return self._start_tile_art()

    def _tile_run(
        self,
        furnish: list[Area],
        pictures: list[PictureJob],
        client: ImageClient | None,
        text: ChatProvider | None,
    ) -> None:
        try:
            for area in furnish:
                if self._art_stop:
                    return
                if text is not None:
                    self._tile_job(f"furnish:{area.place_id}", self._furnish_one, area, text)
                self._tile_done += 1
            if client is None:
                return
            for job in pictures:
                if self._art_stop:
                    return
                model = (
                    self.config.map_sprite_model if job.reference else self.config.map_tile_model
                )
                self._tile_job(
                    job.key,
                    self._picture_one,
                    job,
                    client,
                    SPRITE_SIZE,
                    model=model,
                    info=self._listed_image_model(model),
                    extra={"enable_prompt_expansion": False},
                )
                self._tile_done += 1
        finally:
            if client is not None:
                client.close()
            _close_quietly(text)

    def _tile_job(self, key: str, work, *args, **kwargs) -> None:
        """One job of the tile run: an exception it doesn't expect (a disk
        full, a reply of the wrong shape) used to end the thread silently,
        uncounted, and the job was asked again every passage. Logged and
        counted as a failure instead."""
        try:
            work(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if self._art_stop or self._closed:
                return
            with self._lock():
                art = self.world_art
                art.failures[key] = art.failures.get(key, 0) + 1
                self._save_art(art)
            self._log("map_tile_error", {"job": key, "error": f"{type(exc).__name__}: {exc}"[:300]})

    def _furnish_one(self, area: Area, client: ChatProvider) -> None:
        """One building furnished: the plan asked of the map art model, held
        to the area's rooms, kept with the map's art."""
        world = self.world()
        place = world.places.get(area.place_id)
        if place is None:
            return
        model = self.map_art_model
        job_key = f"furnish:{area.place_id}"
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("map_art"),
            messages=furnish_messages(area, place, self.map_setting(), self.texts),
            params=self.side_params(model, max_tokens=FURNISH_MAX_TOKENS),
        )
        log_ref = self._log(
            "map_furnish_request",
            {"place_id": place.id, "payload": self.provider.build_payload(request)},
        )
        art = self.world_art
        try:
            reply, completed = client.complete(request)
            self._log(
                "map_furnish_response",
                {"request_log_ref": log_ref, "text": reply, "usage": completed.raw_usage},
            )
            self.unreported_usage.append(self.priced(completed.usage, model))
            plan = parse_furnishing(
                reply,
                area,
                place_id=place.id,
                state=place.state,
                model=model,
                made_at=utc_now_iso(),
            )
        except (ProviderError, ValueError) as exc:
            if self._art_stop:
                return
            with self._lock():
                art.failures[job_key] = art.failures.get(job_key, 0) + 1
                self._save_art(art)
            self._log("map_furnish_error", {"request_log_ref": log_ref, "error": str(exc)[:300]})
            return
        # `_lock()` first, never inside `_closing`: `_save_art` takes
        # `_closing` under `_lock()`, and the picture thread saves that way
        # at the same time (an ABBA deadlock, found in the second review).
        if self._art_stop or self._closed:
            return
        with self._lock():
            art.furnishings.setdefault(place.id, {})[place.state] = plan
            self._save_art(art)
        callback = self.on_map_art
        if callback is not None:
            callback()

    # --- the tile set (engine/tile_sets.py, engine/tile_draw.py) -----------------

    def tile_set_choices(self) -> list[tuple[str, str]]:
        """(name, title) of every set the map may be drawn in: the shipped
        ones, and this story's own once it has one."""
        out = [(name, info.title or name) for name, (info, _f) in shipped.shipped_sets().items()]
        art = self.map_art_copy()
        if art.tile_assets:
            out.append(("own", "This story's own"))
        return out

    def set_tile_set(self, name: str) -> None:
        """The author's choice of tile set ("" lets the program choose)."""
        art = self.world_art
        with self._lock():
            art.tile_set = name
            self._save_art(art)

    def own_tile_set_offer(self) -> tuple[int, float | None, str | None]:
        """(pictures, listed price for them, why it can't be drawn now)."""
        count = len(TEXTURES) + 1 + 3 + len(OUTDOOR_SPRITES)
        model = self._listed_image_model(self.config.map_tile_model)
        texture = model.price(TEXTURE_SIZE) if model is not None else None
        sprite = model.price(SPRITE_SIZE) if model is not None else None
        total = (
            texture * len(TEXTURES) + sprite * (count - len(TEXTURES))
            if texture is not None and sprite is not None
            else None
        )
        if self.in_private or self.tee_chat:
            return count, total, "Not in a private scene or a private chat."
        media = self.config.images()
        if media is None or not media.api_key:
            return count, total, "Pictures have no endpoint or key (Settings → Images)."
        thread = getattr(self, "_own_set_thread", None)
        if thread is not None and thread.is_alive():
            return count, total, "This story's tile set is being drawn now."
        return count, total, None

    def start_own_tile_set(self) -> bool:
        """Draw a tile set for this story (the author asked, having seen the
        price): the map art model writes what each picture shows from the
        world, then each is drawn, and the map is drawn in it once the set is
        whole."""
        if self.own_tile_set_offer()[2] is not None or self._map_closed:
            return False
        media = self.config.images()
        detached = getattr(self.provider, "detached", None)
        text = detached() if callable(detached) else self.provider
        client = ImageClient(media.base_url, media.api_key, api=media.api)
        self._own_set_client = client
        self._art_stop = False
        thread = threading.Thread(
            target=self._own_set_run, args=(text, client), name="map-tileset", daemon=True
        )
        self._own_set_thread = thread
        self._own_set_done, self._own_set_total = 0, 1
        thread.start()
        return True

    def _own_set_run(self, text: ChatProvider, client: ImageClient) -> None:
        try:
            model = self.map_art_model
            request = ChatRequest(
                model=model,
                extra_body=self.route_for("map_art"),
                messages=tileset_messages(self.map_setting(), self.texts),
                params=self.side_params(model, max_tokens=FURNISH_MAX_TOKENS),
            )
            log_ref = self._log(
                "map_tileset_request", {"payload": self.provider.build_payload(request)}
            )
            try:
                reply, completed = text.complete(request)
                self._log(
                    "map_tileset_response",
                    {"request_log_ref": log_ref, "text": reply, "usage": completed.raw_usage},
                )
                self.unreported_usage.append(self.priced(completed.usage, model))
                words = parse_tile_words(reply)
            except (ProviderError, ValueError) as exc:
                self._log(
                    "map_tileset_error", {"request_log_ref": log_ref, "error": str(exc)[:300]}
                )
                return
            jobs = tile_jobs(words, self.texts)
            self._own_set_total = len(jobs) + 1
            self._own_set_done = 1
            assets: dict[str, str] = {}
            model = self.config.map_tile_model
            info = self._listed_image_model(model)
            for role, prompt, size in jobs:
                if self._art_stop:
                    return
                file = self._draw_tile(client, role, prompt, size, model, info)
                if file is not None:
                    assets[role] = file
                self._own_set_done += 1
            if not all(role in assets for role in TEXTURES):
                return  # not whole: the set the map has stays
            art = self.world_art
            if self._art_stop or self._closed:
                return
            with self._lock():  # never inside `_closing` (see `_furnish_one`)
                art.tile_assets = assets
                art.tile_words = words
                art.tile_set = "own"
                self._save_art(art)
            callback = self.on_map_art
            if callback is not None:
                callback()
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            self._log("map_tileset_error", {"error": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            client.close()
            _close_quietly(text)

    def _draw_tile(
        self,
        client: ImageClient,
        role: str,
        prompt: str,
        size: str,
        model: str,
        info,
        reference: bytes | None = None,
    ) -> str | None:
        """One picture of the story's own tile set (or one asset redrawn),
        logged, stripped and kept under images/map/tileset/; its file, or
        None. `reference`: a picture it is drawn from (a portrait)."""
        references = []
        if reference is not None:
            references.append(data_url(reference, MEDIA_TYPES[picture_kind(reference)]))
        payload = build_image_payload(model, prompt, size, 1, references, api=client.api)
        if client.api == "nanogpt":
            payload["enable_prompt_expansion"] = False
        log_ref = self._log(
            "map_picture_request",
            {
                "job": f"tile:{role}",
                "payload": loggable_payload(payload, ["the portrait"] if reference else []),
            },
        )
        try:
            reply = client.generate(payload)
            data = strip_metadata(reply.images[0])
        except (ProviderError, MetadataError, IndexError) as exc:
            self._log("map_picture_error", {"request_log_ref": log_ref, "error": str(exc)[:300]})
            return None
        if reply.cost is not None:
            usage: dict = {"cost": reply.cost, "cost_reported": True}
        elif info is not None and info.price(size) is not None:
            usage = {"cost": info.price(size), "cost_estimated": True}
        else:
            usage = {"cost_reported": False}
        name = slug(role) or "asset"
        relative = f"{MAP_PICTURES_DIR}/tileset/{name}-{new_id()[:8]}{picture_kind(data) or '.png'}"
        with self._closing:
            if self._art_stop or self._closed or not self.pictures.alive():
                return None
            self._log("map_picture_response", {"request_log_ref": log_ref, "usage": usage})
            self.pictures.write(relative, data)
        self.unreported_usage.append(
            Usage(
                cost=float(usage.get("cost", 0.0)),
                cost_estimated=bool(usage.get("cost_estimated")),
                cost_reported=bool(usage.get("cost_reported")),
            )
        )
        return relative

    # --- one asset at a time (Look ▾ → Edit the tile set…) -------------------------

    def tile_set_assets(self) -> list[TileAsset]:
        """Every asset the map is drawn with, as the editor lists them: the
        set's ground, walls and decoration, every thing it or the story has
        a sprite for (and every thing in the areas near the story), and
        every character's walking sprite."""
        art = self.map_art_copy()
        info, read = self.tile_set(art)
        name = self.tile_set_name(art)
        found = shipped.shipped_sets().get(name)
        folder = found[1] if found else None
        words = {**info.words, **(art.tile_words if name == "own" else {})}
        story_items = {p.subject_id for p in art.pictures if p.subject == "item"}
        out: list[TileAsset] = []

        def source(role: str, drawn: bool) -> str:
            changed = art.tile_overrides.get(role)
            if changed:
                return "changed for this story"
            if changed is None and (drawn or (name == "own" and role in art.tile_assets)):
                return "drawn for this story"
            return "the set's"

        for role in (*TEXTURES, *SPRITES):
            out.append(
                TileAsset(
                    role=role,
                    label=ASSET_LABELS.get(role, role),
                    group="Ground and walls" if role in TEXTURES else "Decoration and nature",
                    source=source(role, False),
                    words=words.get(role, ""),
                )
            )
        slugs = set(shipped.asset_names(folder, "items")) if folder is not None else set()
        slugs = {s.removeprefix("items/") for s in slugs} | story_items
        for area in self._near_areas(self.world(), art):
            for placement in area.placements:
                keys = item_slugs(placement.name)
                if keys and not any(k in slugs for k in keys):
                    slugs.add(keys[0])
        for key_ in sorted(slugs):
            role = f"items/{key_}"
            out.append(
                TileAsset(
                    role=role,
                    label=key_.replace("-", " "),
                    group="Things",
                    source=source(role, key_ in story_items),
                    words=key_.replace("-", " "),
                )
            )
        for card in (*self.visible_cast(), *self.visible_supporting()):
            role = f"walker/{card.name}"
            out.append(
                TileAsset(
                    role=role,
                    label=card.name,
                    group="People",
                    source=source(role, art.picture_for("walker", card.name) is not None),
                    words=looks_of(card.summary, card.full_description),
                )
            )
        return out

    def tile_asset_bytes(self, role: str) -> bytes | None:
        """The picture an asset is drawn with now."""
        if role.startswith("walker/"):
            file = self.walker_file(role[7:])
            return self.pictures.read(file) if file else None
        _info, read = self.tile_set()
        return read(role)

    def _asset_job(self, role: str, words: str) -> tuple[str, str, str, bytes | None]:
        """(prompt, size, model, the picture it is drawn from) for redrawing
        one asset from these words."""
        art = self.map_art_copy()
        info, _read = self.tile_set(art)
        world = world_words(self._set_words(art, info), self.map_setting())
        style = style_words(self._set_words(art, info))
        what = " ".join(words.split()) or role.split("/")[-1].replace("-", " ")
        if role in TEXTURES:
            view = (
                "straight from the front"
                if role in ("face", "out-wall")
                else "from directly overhead"
            )
            prompt = self.texts.fill(
                "map.tile.texture", what=what, world=world, view=view, style=style
            )
            return prompt, TEXTURE_SIZE, self.config.map_tile_model, None
        if role == "rug":
            prompt = self.texts.fill("map.tile.rug", what=what, world=world, style=style)
        elif role in OUTDOOR_SPRITES:
            prompt = self.texts.fill("map.tile.nature", what=what, world=world, style=style)
        elif role.startswith("walker/"):
            name = role[7:]
            card = next(
                (c for c in (*self.visible_cast(), *self.visible_supporting()) if c.name == name),
                None,
            )
            portrait = card.reference_images[0].file if card and card.reference_images else None
            looks = looks_of(card.summary, card.full_description) if card else ""
            # No words of the author's own: the card's, and so the portrait.
            what = " ".join(words.split()) or looks
            data = self.pictures.read(portrait) if portrait and what == looks else None
            if data is not None:
                try:
                    data = strip_metadata(data)
                except MetadataError:
                    data = None  # never sent unstripped: drawn from the words
            if data is not None:
                prompt = self.texts.fill("map.tile.portrait", style=style)
                return prompt, SPRITE_SIZE, self.config.map_sprite_model, data
            who = f"{name}, {what}" if what else name
            prompt = self.texts.fill("map.tile.person", what=who, world=world, style=style)
        else:
            prompt = self.texts.fill("map.tile.sprite", what=what, world=world, style=style)
        return prompt, SPRITE_SIZE, self.config.map_tile_model, None

    def tile_asset_offer(self, role: str, words: str) -> tuple[str, float | None, str | None]:
        """(the model it would be drawn by, its listed price, why it can't be
        drawn now)."""
        _prompt, size, model, _ref = self._asset_job(role, words)
        listed = self._listed_image_model(model)
        price = listed.price(size) if listed is not None else None
        if self.in_private or self.tee_chat:
            return model, price, "Not in a private scene or a private chat."
        media = self.config.images()
        if media is None or not media.api_key:
            return model, price, "Pictures have no endpoint or key (Settings → Images)."
        if self.map_pictures_left_today(self.map_art_copy()) <= 0:
            return model, price, "Today's map pictures are all drawn (Settings → Images)."
        thread = self._asset_thread
        if thread is not None and thread.is_alive():
            return model, price, "Another asset is being drawn now."
        run = self._tile_thread
        if run is not None and run.is_alive() and _asset_key(role) in self._tile_keys:
            return model, price, "It is being drawn now, under the standing approval."
        return model, price, None

    def redraw_tile_asset(self, role: str, words: str) -> bool:
        """Draw one asset again from these words (the author asked, with the
        price shown), in the background; the map takes it when it is in."""
        return self.redraw_tile_assets([(role, words)])

    def redraw_tile_assets(self, wanted: Sequence[tuple[str, str]]) -> bool:
        """Several assets drawn again, one after another on one thread (the
        avatars of every card a new portrait landed on): each counted
        against the day as it is sent, the map taking each as it is in.
        False when the first can't be drawn now."""
        wanted = [(role, " ".join(words.split())) for role, words in wanted]
        if not wanted or self._map_closed:
            return False
        if self.tile_asset_offer(wanted[0][0], wanted[0][1])[2] is not None:
            return False
        media = self.config.images()
        client = ImageClient(media.base_url, media.api_key, api=media.api)
        self._asset_client = client
        self._art_stop = False
        art = self.world_art
        self._asset_queue = list(wanted[1:])

        def run() -> None:
            try:
                role, words = wanted[0]
                while True:
                    if self._art_stop or self._closed:
                        return
                    self._asset_role = role
                    self._draw_asset(client, art, role, words)
                    if not self._asset_queue:
                        return
                    role, words = self._asset_queue.pop(0)
                    with self._lock():
                        if self.map_pictures_left_today(art) <= 0:
                            return  # the day's pictures all drawn: the rest wait
                        day = _today()
                        art.pictures_by_day[day] = art.pictures_by_day.get(day, 0) + 1
                        self._save_art(art)
            finally:
                client.close()
                self._asset_queue = []

        with self._lock():
            day = _today()
            art.pictures_by_day[day] = art.pictures_by_day.get(day, 0) + 1
            self._save_art(art)
        thread = threading.Thread(target=run, name="map-tile-asset", daemon=True)
        self._asset_thread = thread
        self._asset_role = wanted[0][0]
        thread.start()
        return True

    def _draw_asset(self, client: ImageClient, art: WorldArt, role: str, words: str) -> None:
        """One asset of a redraw run: drawn, kept as the author's change,
        said to the window either way (the Cast page's avatar waits on it)."""
        try:
            prompt, size, model, reference = self._asset_job(role, words)
            file = self._draw_tile(
                client, role, prompt, size, model, self._listed_image_model(model), reference
            )
            if file is None or self._art_stop or self._closed:
                return
            with self._lock():  # never inside `_closing` (see `_furnish_one`)
                art.tile_overrides[role] = file
                self._save_art(art)
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            self._log(
                "map_picture_error",
                {"job": f"tile:{role}", "error": f"{type(exc).__name__}: {exc}"[:300]},
            )
        finally:
            callback = self.on_map_art
            if callback is not None and not self._closed:
                callback()

    @property
    def tile_asset_drawing(self) -> str | None:
        """The asset being drawn now, or None."""
        thread = self._asset_thread
        return self._asset_role if thread is not None and thread.is_alive() else None

    def use_own_tile_asset(self, role: str, data: bytes) -> None:
        """A picture of the author's own for one asset (already stripped by
        the crop dialog), kept with the story's pictures."""
        kind = picture_kind(data) or ".png"
        relative = f"{MAP_PICTURES_DIR}/tileset/{slug(role) or 'asset'}-{new_id()[:8]}{kind}"
        self.pictures.write(relative, strip_metadata(data))
        art = self.world_art
        with self._lock():
            art.tile_overrides[role] = relative
            self._save_art(art)

    def reset_tile_asset(self, role: str) -> None:
        """Back to the set's own picture for this asset."""
        art = self.world_art
        with self._lock():
            art.tile_overrides[role] = ""
            self._save_art(art)


def _asset_key(role: str) -> str:
    """The tile run's job key for an editor role ("walker/Jane" is
    "walker:Jane", "items/a-chest" is "item:a-chest")."""
    if role.startswith("walker/"):
        return f"walker:{role[7:]}"
    if role.startswith("items/"):
        return f"item:{role[6:]}"
    return role


def _close_quietly(client: ChatProvider | None) -> None:
    """A detached text client closed once its run is over (they were never)."""
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass


def item_sprite_key(name: str) -> str:
    """The slug an item's own sprite is kept under (`MapPicture.subject_id`)."""
    return item_slugs(name)[0] if item_slugs(name) else ""
