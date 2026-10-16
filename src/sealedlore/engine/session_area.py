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
import threading
from collections.abc import Sequence
from typing import Any

from sealedlore.engine.area import (
    WALK_M_PER_S,
    Area,
    anchor_of,
    anchor_tile,
    anchors_near,
    area_of,
    bare,
    build_area,
    exit_side,
    front_of,
    inside_door,
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
from sealedlore.engine.tile_draw import (
    RUN_LIMIT,
    SPRITE_SIZE,
    TEXTURE_SIZE,
    item_jobs,
    parse_tile_words,
    tile_jobs,
    tileset_messages,
    walker_jobs,
    world_words,
)
from sealedlore.engine.tile_sets import (
    OUTDOOR_SPRITES,
    TEXTURES,
    TileSetInfo,
    choose_set,
    item_slugs,
)
from sealedlore.engine.validators import locate_quote
from sealedlore.engine.world import applied, key
from sealedlore.engine.world_layout import (
    CHILD_SHARE,
    KIND_SIZE,
    describe_metres,
    position,
    size_of,
)
from sealedlore.engine.world_pictures import MAP_PICTURES_DIR, PictureJob, looks_of
from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.area import AreaState, LiveArea, MapAction, Mark, Spot
from sealedlore.models.node import Node, Usage
from sealedlore.models.world import WorldArt, WorldDelta, WorldMap
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError
from sealedlore.providers.images import ImageClient, build_image_payload
from sealedlore.storage import tile_sets as shipped
from sealedlore.storage.image_meta import MetadataError, picture_kind, strip_metadata

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


class AreaRuntime:
    _area_cache: dict | None = None
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
        reader = self.pictures.read

        def read(role: str) -> bytes | None:
            if role.startswith("items/") and role[6:] in items:
                data = reader(items[role[6:]])
                if data:
                    return data
            if role in own:
                data = reader(own[role])
                if data:
                    return data
            return shipped.read_asset(folder, role) if folder is not None else None

        return info, read

    def generic_people(self) -> list[str]:
        """The tile set's generic people, by role."""
        name = self.tile_set_name()
        found = shipped.shipped_sets().get(name)
        return shipped.asset_names(found[1], "people") if found else []

    def walker_file(self, name: str, art: WorldArt | None = None) -> str | None:
        """The file of a person's walking sprite, if one is drawn."""
        art = art or self.map_art_copy()
        found = art.picture_for("walker", name)
        return found.file if found is not None else None

    def area(
        self, area_id: str | None, world: WorldMap | None = None, art: WorldArt | None = None
    ) -> Area | None:
        """The area of `area_id`, built from the map at the leaf (kept while
        nothing it is built from changes)."""
        if area_id is None:
            return None
        world = world if world is not None else self.world()
        art = art if art is not None else self.map_art_copy()
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

    # --- where everyone stands -------------------------------------------------

    def _snapshot_on(self, nodes: Sequence[Node]) -> AreaState | None:
        for node in reversed(nodes):
            if node.meta.area is not None:
                return node.meta.area
        return None

    def _live(self) -> AreaState | None:
        live = self.story.area_live
        if live is None or live.leaf_id != self.story.active_leaf_id:
            return None
        return live.state

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
        the way in. `walked`: the author's character's own walking stands as
        walked (the map catches up when the turn is sent)."""
        held = self.held_name()
        where = dict(world.where)
        if held is not None and held not in where and world.start is not None:
            where[held] = world.start
        spots: dict[str, Spot] = {}
        order = ([held] if held in where else []) + [n for n in where if n != held]
        travel = (
            state.action if state.action is not None and state.action.verb == "travel" else None
        )
        for name in order:
            place_id = where[name]
            spot = state.spots.get(name)
            if name == held and walked and travel is not None and travel.target_id in world.places:
                # Set off for somewhere: there, as far as the map has drawn it.
                place_id = travel.target_id
                if spot is None:
                    continue
            target = area_of(world, art, place_id)
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
            mine = spots.get(held or "")
            near_me = (
                name != held
                and mine is not None
                and mine.area == target
                and place_id == where.get(held or "")
            )
            new = self._stand(world, art, target, place_id, name, spots, mine if near_me else None)
            if new is not None:
                spots[name] = new
        state.spots = spots
        return state

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
    ) -> Spot | None:
        area = self.area(area_id, world, art)
        if area is None:
            return None
        taken = {(s.x, s.y) for s in spots.values() if s.area == area_id}
        if beside is not None:
            near = (beside.x, beside.y)
            x, y = stand_near(area, near, taken | {near}, seed=name, reach=BESIDE)
            return Spot(area=area_id, x=x, y=y)
        near = anchor_of(area, world, place_id)
        if near is None:
            near = self._way_in(area)
        x, y = stand_near(area, near, taken, seed=name, reach=2)
        return Spot(area=area_id, x=x, y=y)

    @staticmethod
    def _way_in(area: Area) -> tuple[int, int]:
        if area.indoor:
            inside = inside_door(area)
            if inside is not None:
                return inside
            if area.rooms:
                room = area.rooms[0]
                return int(room.cx), int(room.cy)
        if area.labels:
            return next(iter(area.labels.values()))
        return area.width // 2, area.height // 2

    def _set_live(self, state: AreaState) -> None:
        self.story.area_live = LiveArea(leaf_id=self.story.active_leaf_id, state=state)
        self.save()

    def place_of_spot(self, spot: Spot | None, world: WorldMap) -> str | None:
        """The place a spot is in, for the map's `where`: the area's place
        (a building's inside is that building)."""
        if spot is None:
            return None
        return spot.area if spot.area in world.places else None

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
        if beside:
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
        taken = {
            (s.x, s.y) for n, s in state.spots.items() if n != held and s.area == there.place_id
        }
        if not here.indoor:
            entrance = here.entrance_at(me.x, me.y)
            spot = inside_door(there, entrance.side if entrance else None) or self._way_in(there)
        else:
            side = exit_side(here, me.x, me.y)
            spot = front_of(there, here.place_id, side) or self._way_in(there)
        x, y = stand_near(there, spot, taken)
        state.spots[held] = Spot(area=there.place_id, x=x, y=y)
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
        start = position(world, here)
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
        me = state.spots.get(held or "")
        frm = self.area(me.area, world, art).name if me and self.area(me.area, world, art) else ""
        target = area_of(world, art, place_id)
        if target is not None:
            spot = self._stand(world, art, target, place_id, held or "", dict(state.spots), None)
            if spot is not None and held is not None:
                state.spots[held] = spot
        elif held is not None:
            state.spots.pop(held, None)
        self._add_time(state, seconds, frm, place.name)
        text = self.texts.fill("map.turn.travel", name=place.name)
        state.action = MapAction(verb="travel", target=place.name, target_id=place_id, text=text)
        if target is None:
            # Not drawn yet: where the turn takes them is said by the delta it
            # carries (`stamp_turn`), and the map draws it after the passage.
            state.action.target_id = place_id
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
        # The spots the turn marks, resolved on the map it was written on.
        state.marks = self.resolve_marks(user_node.content, world, self.map_art_copy(), state)
        to = None
        if state.action is not None and state.action.verb == "travel":
            to = state.action.target_id or None
        spot = state.spots.get(held or "")
        to = to or self.place_of_spot(spot, world)
        if held is not None and to is not None and world.where.get(held) != to:
            area = area_of(world, self.map_art_copy(), world.where.get(held))
            if area != to or to not in world.places:
                user_node.meta.world = WorldDelta(where={held: to})
        self.story.area_live = None

    def walk_into_plot(self, user_node: Node, chronicle) -> int:
        """The minutes this turn's walking took, added to the plot's clock
        (as the author's skip is: the read counts on from there)."""
        state = user_node.meta.area
        if state is None or state.walked <= 0:
            return 0
        minutes = int(round(state.walked / 60))
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
        me = state.spots.get(held or "")
        moved = (
            held is not None
            and me is not None
            and area_of(world, art, world.where.get(held)) not in (None, me.area)
        )
        if moved:
            state.spots.pop(held, None)  # the passage took them elsewhere
        state = self.settle(state, world, art, walked=False)
        me = state.spots.get(held or "")
        at = reply.get("at") if isinstance(reply.get("at"), dict) else {}
        by_point = {(m.x, m.y): m for m in marks}
        for name, spot_name in list(at.items()):
            found = marks_in(str(spot_name or ""))
            mark = by_point.get((found[0].x, found[0].y)) if found else None
            person = self._map_person(str(name))
            if mark is None or person is None:
                continue
            at.pop(name)
            spot = self._mark_spot(mark, world, art)
            current = state.spots.get(person)
            if spot is None or current is None or current.area != spot.area:
                continue  # not where the map has them: settled at their place
            marked = self.area(spot.area, world, art)
            taken = {(s.x, s.y) for n, s in state.spots.items() if n != person}
            x, y = stand_near(marked, (spot.x, spot.y), taken)  # type: ignore[arg-type]
            state.spots[person] = Spot(area=spot.area, x=x, y=y)
        area = self.area(me.area, world, art) if me is not None else None
        if area is not None:
            for name, spot_name in at.items():
                person = self._map_person(str(name))
                tile = anchor_tile(area, str(spot_name))
                if person is None or tile is None:
                    continue
                current = state.spots.get(person)
                if current is not None and current.area != area.place_id:
                    continue
                taken = {(s.x, s.y) for n, s in state.spots.items() if n != person}
                x, y = stand_near(area, tile, taken | {tile})
                state.spots[person] = Spot(area=area.place_id, x=x, y=y)
        if self.story.plot is None:
            minutes = reply.get("minutes")
            quote = str(reply.get("minutes_quote") or "")
            if (
                isinstance(minutes, (int, float))
                and not isinstance(minutes, bool)
                and 0 < minutes <= MAX_READ_MINUTES
                and quote
                and locate_quote(source, quote) is not None
            ):
                state.seconds += int(minutes) * 60
        node.meta.area = state

    def _map_person(self, name: str) -> str | None:
        from sealedlore.engine.scene_state import same_person

        for person in self.map_people():
            if same_person(name, person):
                return person.name
        return None

    def read_spots(self, path: Sequence[Node]) -> str:
        """The spots near the author's character, for the map read
        (`map_read.spots`); "" when the story isn't played on a map."""
        if not self.map_first:
            return ""
        before = self._snapshot_on(path)
        if before is None:
            return ""
        me = before.spots.get(self.held_name() or "")
        area = self.area(me.area if me else None)
        if me is None or area is None:
            return ""
        spots = anchors_near(area, me.x, me.y)
        return self.texts.fill("map_read.spots", spots=", ".join(spots)) if spots else ""

    def positions_context(self, nodes: Sequence[Node], held_id: str | None) -> str:
        """Where everyone stands, for the storyteller's map section (design
        §5): from the turn being sent (its snapshot) or, before it is, the
        walking since the last turn."""
        if not self.map_first or self.in_private:
            return ""
        held = self.map_held_name(held_id) if held_id is not None else self.held_name()
        if held is None:
            return ""
        turn = self._turn_after(nodes)
        if turn is not None and turn.meta.area is not None:
            state, action_node = turn.meta.area, turn
        else:
            live = self._live() if turn is None else None
            state = live if live is not None else self._snapshot_on(nodes)
            action_node = None
        if state is None:
            return ""
        world = self._world_on([*nodes, turn] if turn is not None else nodes)
        art = self.map_art_copy()
        me = state.spots.get(held)
        area = self.area(me.area if me else None, world, art)
        if me is None or area is None:
            return ""
        action_line = ""
        action = state.action
        if action is not None and (action_node is None or action.text in action_node.content):
            action_line = self.texts.fill(f"map.action.{action.verb}", name=action.target)
        text = render_positions(
            area,
            world,
            state.spots,
            held,
            walked=state.walked_words if state.walked >= 30 else "",
            action_line=action_line,
        )
        if not text:
            return ""
        heading = self.texts["map.positions"]
        return text.replace(text.split("\n", 1)[0], heading, 1)

    def _turn_after(self, nodes: Sequence[Node]) -> Node | None:
        """The author's turn being sent after `nodes`, once it is attached."""
        full = self.path()
        if len(full) != len(nodes) + 1 or full[-1].kind != "user":
            return None
        if nodes and full[-2].id != nodes[-1].id:
            return None
        return full[-1]

    # --- marked spots (engine/marks.py) ------------------------------------------

    def held_point(
        self, state: AreaState | None = None, world: WorldMap | None = None, art=None
    ) -> tuple[float, float] | None:
        """Where the author's character stands, in metres (east, north): their
        tile on the map, else their place's spot."""
        world = world if world is not None else self.world()
        art = art if art is not None else self.map_art_copy()
        held = self.held_name()
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
            area_id = area_of(world, art, place_id) if place_id else None
            area = self.area(area_id, world, art) if area_id else None
            tile = point_tile(world, area, mark.x, mark.y) if area is not None else None
            if area is not None and tile is not None:
                spot = Spot(area=area.place_id, x=tile[0], y=tile[1])
            if place_id:
                parts.append(chain_words(world, place_id))
            else:
                near = nearest_place(world, mark.x, mark.y)
                if near is not None:
                    other = world.places[near[0]]
                    way = compass(mark.x - other.x, mark.y - other.y)  # type: ignore[operator]
                    parts.append(f"open country, {describe_metres(near[1])} {way} of {other.name}")
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

    def marks_context(self, nodes: Sequence[Node]) -> str:
        """The marks of the turn being sent, for the storyteller's map
        section (`map.marks`); "" when it marks none."""
        if not self.map_on or self.in_private:
            return ""
        turn = self._turn_after(nodes)
        if turn is None or turn.meta.area is None or not turn.meta.area.marks:
            return ""
        lines = [f"- {m.token}: {m.words}." for m in turn.meta.area.marks]
        return self.texts.fill("map.marks", marks="\n".join(lines))

    def read_marks(self, author: Node | None) -> str:
        """The author's turn's marks, for the map read (`map_read.marks`)."""
        if author is None or author.meta.area is None or not author.meta.area.marks:
            return ""
        lines = [f"- {m.token}: {m.words}" for m in author.meta.area.marks]
        return self.texts.fill("map_read.marks", marks="\n".join(lines))

    def apply_marks(
        self, before: WorldMap, delta: WorldDelta, reply: dict[str, Any], author: Node | None
    ) -> None:
        """What the read put at a marked spot is put there exactly: a new
        place on the point (inside whatever there holds it), and anyone sent
        to one in the place it lies in."""
        marks = author.meta.area.marks if author is not None and author.meta.area else []
        if not marks:
            return
        by_point = {(m.x, m.y): m for m in marks}

        def mark_of(value: Any) -> Mark | None:
            found = marks_in(str(value or ""))
            return by_point.get((found[0].x, found[0].y)) if found else None

        new = {key(p.name): p for p in delta.places if p.x is None and p.id not in before.places}
        art = self.map_art_copy()
        for entry in reply.get("places") or []:
            if not isinstance(entry, dict):
                continue
            mark = mark_of(entry.get("mark"))
            place = new.get(key(str(entry.get("name") or "")))
            if mark is None or place is None:
                continue
            known = {**before.places, **{p.id: p for p in delta.places}}
            holder = known.get(place.inside or "")
            box = footprint(before, art, holder.id) if holder is not None else None
            if (
                holder is None
                or box is None
                or not (box[0] <= mark.x <= box[2] and box[1] <= mark.y <= box[3])
            ):
                place.inside = place_at(before, art, mark.x, mark.y)
                holder = known.get(place.inside or "")
            size = place.size if place.size is not None else KIND_SIZE.get(place.kind, 100.0)
            if holder is not None:
                size = min(size, size_of(holder) * CHILD_SHARE)
            place.size, place.x, place.y = size, mark.x, mark.y
        at = reply.get("at") if isinstance(reply.get("at"), dict) else {}
        if not at:
            return
        after = applied(before, delta)
        for name, value in at.items():
            mark = mark_of(value)
            person = self._map_person(str(name))
            if mark is None or person is None:
                continue
            place_id = place_at(after, art, mark.x, mark.y)
            if place_id is not None:
                delta.where[person] = place_id

    def _mark_spot(self, mark: Mark, world: WorldMap, art: WorldArt) -> Spot | None:
        """The tile a mark lies on, on the map as it is now."""
        place_id = place_at(world, art, mark.x, mark.y)
        area = self.area(area_of(world, art, place_id), world, art) if place_id else None
        tile = point_tile(world, area, mark.x, mark.y) if area is not None else None
        return Spot(area=area.place_id, x=tile[0], y=tile[1]) if tile else None

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
        if place_id is None or held is None:
            return ""
        state = self.area_state()
        me = state.spots.get(held)
        here = self.area(me.area, world, art) if me is not None else None
        spot = self._mark_spot(Mark(token="", x=x, y=y), world, art)
        if here is not None and spot is not None and spot.area == here.place_id:
            return ""  # in this area: walking is the way
        start = self.held_point(state, world, art)
        metres = math.hypot(x - start[0], y - start[1]) * WINDING if start else 0.0
        seconds = int(metres / WALK_M_PER_S)
        frm = here.name if here is not None else ""
        area = self.area(spot.area, world, art) if spot is not None else None
        if spot is not None and area is not None:
            taken = {
                (s.x, s.y) for n, s in state.spots.items() if n != held and s.area == spot.area
            }
            sx, sy = stand_near(area, (spot.x, spot.y), taken)
            state.spots[held] = Spot(area=spot.area, x=sx, y=sy)
        else:
            state.spots.pop(held, None)
        place = world.places[place_id]
        self._add_time(state, seconds, frm, place.name)
        token = mark_token(x, y)
        text = self.texts.fill("map.turn.go", name=place.name, mark=token)
        state.action = MapAction(verb="travel", target=place.name, target_id=place_id, text=text)
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
            and world.places[a.place_id].kind in ("building", "room")
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

        words = world_words({**info.words, **art.tile_words}, self.map_setting())
        jobs = item_jobs(areas, has_sprite, words, self.texts, failed=failed)
        here = {name for name, spot in self.area_state().spots.items()}
        people = []
        for card in (*self.visible_cast(), *self.visible_supporting()):
            portrait = card.reference_images[0].file if card.reference_images else None
            people.append((card.name, portrait, looks_of(card.summary, card.full_description)))
        people.sort(key=lambda p: p[0] not in here)
        jobs = (
            walker_jobs(
                people,
                lambda name: art.picture_for("walker", name) is not None,
                words,
                self.texts,
                failed=failed,
            )
            + jobs
        )
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
                    self._furnish_one(area, text)
                self._tile_done += 1
            if client is None:
                return
            for job in pictures:
                if self._art_stop:
                    return
                model = (
                    self.config.map_sprite_model if job.reference else self.config.map_tile_model
                )
                self._picture_one(
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
        with self._closing:
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
        price = model.price(TEXTURE_SIZE) if model is not None else None
        total = price * count if price is not None else None
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
            with self._closing:
                if self._art_stop or self._closed:
                    return
                with self._lock():
                    art.tile_assets = assets
                    art.tile_words = words
                    art.tile_set = "own"
                    self._save_art(art)
            callback = self.on_map_art
            if callback is not None:
                callback()
        finally:
            client.close()

    def _draw_tile(
        self,
        client: ImageClient,
        role: str,
        prompt: str,
        size: str,
        model: str,
        info,
    ) -> str | None:
        """One picture of the story's own tile set, logged, stripped and
        kept under images/map/tileset/; its file, or None."""
        payload = build_image_payload(model, prompt, size, 1, [], api=client.api)
        if client.api == "nanogpt":
            payload["enable_prompt_expansion"] = False
        log_ref = self._log("map_picture_request", {"job": f"tile:{role}", "payload": payload})
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
        relative = f"{MAP_PICTURES_DIR}/tileset/{role}-{new_id()[:8]}{picture_kind(data) or '.png'}"
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


def item_sprite_key(name: str) -> str:
    """The slug an item's own sprite is kept under (`MapPicture.subject_id`)."""
    return item_slugs(name)[0] if item_slugs(name) else ""
