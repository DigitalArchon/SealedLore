"""A visual story's wider world in play (models/overmap.py,
engine/overmap.py, engine/cells.py; the author, Oct 26 2026). A mixin for
`StorySession`, over the visual runtime (engine/session_visual.py).

- Where the player is on the overmap: their tile on the map, in the cell
  it lies in (a made cell's area shows the whole cell at the farm's size, so
  its tiles are scaled back out to the cell's true size).
- Which area a cell is played on: a place of the plot file's map whose
  ground covers it, else the cell's own, made the first time it is
  entered (engine/cells.py) and kept with the map's art, its buildings'
  insides from the world kit, its dead from the region.
- Journeys to a place found or a cell anywhere: the way, the clock, the
  camps, food, water and warmth, the roll at each stretch; a roll that
  comes up stops the journey in that stretch's cell, and the keeper is
  asked what it is (engine/keeper.py `journey_messages`).
- Walking off an area's edge goes on into the next cell, with a roll the
  first time a cell is entered.
- Places found appear on the overmap: passed on a journey, come to, or
  told of by the keeper ("tell_of").
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field

from sealedlore.engine.area import (
    WALK_M_PER_S,
    Area,
    area_of,
    area_rect,
    bare,
    clamped_tile,
    outdoor_scale,
    stand_near,
    tile_point,
    walk_metres,
)
from sealedlore.engine.cells import (
    AREA_M,
    COLUMNS,
    ROWS,
    TERRAIN_NAMES,
    cell_of_place,
    cell_place_id,
    choose_template,
    is_cell,
    lay_pieces,
    make_cell,
)
from sealedlore.engine.kits import yard_pieces
from sealedlore.engine.levels import place_of_area
from sealedlore.engine.overmap import (
    CAMP_SHARE,
    KM,
    STARVE_AFTER,
    TIRED_AFTER,
    Estimate,
    Journey,
    Stop,
    Traveller,
    cell_centre,
    cell_key,
    cell_of,
    clamped_cell,
    estimate,
    is_night,
    journey,
    neighbour,
    on_map,
    region_at,
    region_named,
    region_of,
    roll_chance,
    route,
    times_from,
    walking_seconds,
    weights,
)
from sealedlore.engine.overmap import (
    PARCHED_AFTER as _PARCHED,
)
from sealedlore.engine.overmap import (
    pick_kind as _pick_kind,
)
from sealedlore.engine.session_area import ARRIVAL_ROAD_REACH
from sealedlore.engine.session_visual import SLEEP_UNTIL
from sealedlore.engine.visual_rules import (
    FUEL_CAN_KM,
    FULL_TANK_KM,
    drivable,
    kind_of,
    spawn_creatures,
    warmth,
)
from sealedlore.engine.world_art import plan_rect
from sealedlore.engine.world_layout import position, size_of
from sealedlore.models.area import Spot
from sealedlore.models.overmap import Overmap, OverPlace, OverRegion
from sealedlore.models.world import OverCell, WorldDelta, WorldMap

# Places this near a journey's way are seen on it, and found.
FOUND_M = 1500.0
# Places this near a cell entered are found.
FOUND_NEAR_M = 700.0
# Walking into a cell not entered before rolls as for this long in it: half
# an hour looking it over (a quarter gave about 1 % a cell in the country
# round Hellsville, and the author walked north through four with nothing).
EXPLORE_SECONDS = 1800
# Hungry, thirsty or weary from this long without: weaker in a fight.
HUNGRY_AFTER = 24 * 3600
THIRSTY_AFTER = 24 * 3600
# The kinds of happening, as the keeper is told them.
KIND_WORDS = {
    "danger": "something dangerous",
    "opportunity": "a chance: something worth having, or someone who could help",
    "interest": "something of interest, no threat in itself",
    "dwellers": "the people who are here: whoever lives in this place, holds it or has "
    "stopped here",
}


@dataclass(frozen=True)
class OverChoice:
    """A place on the overmap to set out for: its name, where (metres), the
    walk there (seconds, rested, no camps), and whether it is where the
    player is now."""

    name: str
    x: float
    y: float
    walking: int
    here: bool = False


@dataclass
class Travelled:
    """What came of setting out (or walking on into the next cell): the
    lines for the log, whether they arrived, died, or were stopped (and the
    brief for the keeper), and the time it took."""

    lines: list[str] = field(default_factory=list)
    arrived: bool = False
    dead: bool = False
    stop: Stop | None = None
    brief: str = ""
    seconds: int = 0
    to: str = ""


def _edge_tiles(area: Area, depth: int = 3) -> list[tuple[int, int]]:
    """The tiles within `depth` of any edge of the area: where a road comes
    in, whichever side it crosses."""
    return [
        (x, y)
        for y in range(area.height)
        for x in range(area.width)
        if x < depth or y < depth or x >= area.width - depth or y >= area.height - depth
    ]


class OvermapRuntime:
    # --- the overmap and its cells ---------------------------------------------------

    def overmap(self) -> Overmap | None:
        """The story's wider world, when it has one (a visual story only)."""
        if not self.story.visual:
            return None
        design = self.story.visual_design
        return design.overmap if design is not None else None

    def _overmap_delta(self) -> WorldDelta | None:
        """The made cells' places, folded under the map as the plot file's
        are (engine/session_world.py `_fold`); the same object while the
        cells are the same, since the fold is kept by its identity."""
        art = self._art_read()
        made = tuple((k, c.made_at, len(c.places)) for k, c in sorted(art.cells.items()))
        kept = self.__dict__.get("_overmap_delta_kept")
        if kept is not None and kept[0] == made:
            return kept[1]
        delta = (
            WorldDelta(
                places=[p.model_copy() for _k, c in sorted(art.cells.items()) for p in c.places]
            )
            if made
            else None
        )
        self.__dict__["_overmap_delta_kept"] = (made, delta)
        return delta

    def _plot_cells(self, world: WorldMap) -> dict[tuple[int, int], str]:
        """The cells the plot file's own places hold, by place id: the cell
        of each one's middle, and every cell whose middle its ground covers;
        where two do, the smaller. A place the plot keeps hidden is not on
        the map, and holds none."""
        om = self.overmap()
        if om is None:
            return {}
        art = self._art_read()
        places = [
            p
            for p in world.places.values()
            if not is_cell(p.id)
            and p.kind in ("settlement", "site", "area")
            and (
                p.inside is None
                or getattr(world.places.get(p.inside), "kind", None) in ("route", "region")
            )
            and art.layout_shown(p.id, p.state) is not None
            and p.x is not None
        ]
        memo_key = tuple(sorted((p.id, p.x, p.y, size_of(p)) for p in places))
        kept = self.__dict__.get("_plot_cells_kept")
        if kept is not None and kept[0] == memo_key:
            return kept[1]
        out: dict[tuple[int, int], tuple[float, str]] = {}
        for place in places:
            spot = position(world, place.id)
            rect = plan_rect(world, place)
            if spot is None or rect is None:
                continue
            size = size_of(place)
            held = set()
            middle = cell_of(om, *spot)
            if middle is not None:
                held.add(middle)
            west, north, width, height = rect
            a, b = cell_of(om, west, north), cell_of(om, west + width, north - height)
            if a is not None and b is not None:
                for i in range(a[0], b[0] + 1):
                    for j in range(a[1], b[1] + 1):
                        cx, cy = cell_centre(om, (i, j))
                        if west <= cx <= west + width and north - height <= cy <= north:
                            held.add((i, j))
            for cell in held:
                if cell not in out or size < out[cell][0]:
                    out[cell] = (size, place.id)
        cells = {cell: pid for cell, (_s, pid) in out.items()}
        self.__dict__["_plot_cells_kept"] = (memo_key, cells)
        return cells

    def cell_area_id(self, cell: tuple[int, int]) -> str | None:
        """The area a cell is played on, without making it: a plot place's,
        the made cell's own, or None for a cell not made yet."""
        world = self._world_read()
        plot = self._plot_cells(world).get(cell)
        if plot is not None:
            return area_of(world, self._art_read(), plot)
        pid = cell_place_id(cell)
        return pid if pid in world.places else None

    def _overmap_place_in(self, cell: tuple[int, int]) -> OverPlace | None:
        """The overmap's own place (one with a template) whose spot is in this cell."""
        om = self.overmap()
        if om is None:
            return None
        return next(
            (p for p in om.places if p.template and cell_of(om, p.x * KM, p.y * KM) == cell),
            None,
        )

    def cell_name(self, cell: tuple[int, int]) -> str:
        """What a cell is called, made or not: its area's name, else the
        overmap's place there, else what exploring would find there."""
        area_id = self.cell_area_id(cell)
        world = self._world_read()
        if area_id is not None:
            place = world.places.get(place_of_area(area_id) or "")
            if place is not None:
                return place.name
        om = self.overmap()
        own = self._overmap_place_in(cell)
        if own is not None:
            return own.name
        region = region_of(om, cell) if om is not None else None
        kit = self.visual_kit()
        key = choose_template(region, self.visual_seed(), cell)
        template = kit.outdoor.get(key) if kit is not None else None
        if template is not None:
            return template.name
        return TERRAIN_NAMES.get(region.terrain if region else "fields", "Open country")

    def ensure_cell(self, cell: tuple[int, int], state=None) -> str | None:
        """The area a cell is played on, made now if it isn't yet: its
        place, buildings and plan, its things and loot laid on it, its
        buildings' insides from the world kit, and (into `state`) the dead
        that wander it. None off the overmap."""
        om = self.overmap()
        if om is None or not on_map(om, cell):
            return None
        found = self.cell_area_id(cell)
        if found is not None:
            return found
        seed = self.visual_seed()
        region = region_of(om, cell)
        own = self._overmap_place_in(cell)
        kit = self.visual_kit()
        if own is not None:
            key, name, what = own.template, own.name, own.what
        else:
            key, name, what = choose_template(region, seed, cell), "", ""
        template = kit.outdoor.get(key) if kit is not None else None
        made = make_cell(om, cell, seed, template_key=key, template=template, name=name, what=what)
        art = self.world_art  # takes the lock itself: read before it is held
        with self._lock():
            art.cells[made.key] = OverCell(
                key=made.key,
                template=key,
                places=[made.place, *made.children],
                made_at=made.layout.made_at,
                lived_in=made.lived_in,
            )
            art.layouts.setdefault(made.place.id, {})[""] = made.layout
        self._save_art(art)
        world = self._world_read()
        area = self._built_area(made.place.id, world, self._art_read())
        if area is not None and (made.objects or made.loot or area.buildings):
            keep = set()
            for entrance in area.entrances:
                keep |= {
                    (entrance.x + dx, entrance.y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                }
            # The kit's yard pieces round the buildings first (a woodpile, a
            # kennel), as Doomsville's own were stamped; then the template's
            # things, clear of them.
            yard = (
                yard_pieces(
                    kit,
                    area.rows,
                    area.buildings,
                    area.names,
                    area.tile_m,
                    f"{seed}:{made.key}",
                    keep=keep,
                    blocked=area.blocked(),
                    reach_from=[(e.x, e.y + 1) for e in area.entrances]
                    + [(e.x, e.y - 1) for e in area.entrances],
                )
                if kit is not None and area.buildings
                else []
            )
            used = {
                (p["x"] + dx, p["y"] + dy)
                for p in yard
                for dy in range(int(p.get("h", 1)))
                for dx in range(int(p.get("w", 1)))
            }
            pieces, loot = lay_pieces(
                area.rows,
                made.objects,
                made.loot,
                area.tile_m,
                seed,
                made.key,
                buildings=list(area.buildings.values()),
                keep=keep | used,
            )
            art = self.world_art
            with self._lock():
                layout = art.layouts[made.place.id][""]
                layout.spec = {
                    **layout.spec,
                    "pieces": yard + pieces,
                    "loot": loot,
                    "pieces_scale": outdoor_scale(made.place, COLUMNS),
                }
                layout.made_at = f"{layout.made_at}:laid"
                art.cells[made.key].made_at = layout.made_at
            self._save_art(art)
        self.visual_kit_plans()  # the buildings' insides
        if state is not None and made.dead:
            there = self.area(made.place.id, things=state.things)
            if there is not None:
                taken = there.blocked() | {
                    (sp.x, sp.y) for sp in state.spots.values() if sp.area == there.id
                }
                for creature in spawn_creatures(
                    there, "the dead", made.dead, 4, taken, f"{there.id}:dead"
                ):
                    state.creatures[creature.id] = creature
        return made.place.id

    # --- where the player is ---------------------------------------------------------

    def _root_place(self, world: WorldMap, area_id: str | None) -> str | None:
        """The outermost place an area stands in (a building's yard, a
        cell), short of a route or a region."""
        place = world.places.get(place_of_area(area_id) or "")
        seen: set[str] = set()
        while place is not None and place.inside and place.id not in seen:
            seen.add(place.id)
            parent = world.places.get(place.inside)
            if parent is None or parent.kind in ("route", "region"):
                break
            place = parent
        return place.id if place is not None else None

    def _cell_scale(self, om: Overmap) -> tuple[float, float]:
        """How much larger a cell is than its area, across and down."""
        return om.cell_w * KM / AREA_M, om.cell_h * KM / (AREA_M * ROWS / COLUMNS)

    def overmap_point(self, state=None) -> tuple[float, float] | None:
        """Where the player is on the overmap, in metres: their tile on the
        map, scaled out to the true cell in a made cell."""
        om = self.overmap()
        if om is None:
            return None
        state = state if state is not None else self.area_state()
        world, art = self._world_read(), self._art_read()
        point = self.held_point(state, world, art)
        me = state.spots.get(self.held_name() or "")
        root = self._root_place(world, me.area) if me is not None else None
        place = world.places.get(root or "")
        if point is not None and place is not None and is_cell(place.id):
            sx, sy = self._cell_scale(om)
            point = (place.x + (point[0] - place.x) * sx, place.y + (point[1] - place.y) * sy)
        return point

    def overmap_cell(self, state=None) -> tuple[int, int] | None:
        om = self.overmap()
        point = self.overmap_point(state)
        return clamped_cell(om, *point) if om is not None and point is not None else None

    def overmap_region(self, state=None) -> OverRegion | None:
        om = self.overmap()
        point = self.overmap_point(state)
        return region_at(om, *point) if om is not None and point is not None else None

    def _in_area(self, area: Area, point: tuple[float, float]) -> tuple[float, float]:
        """A true point as the area draws it (a made cell's area is the
        cell drawn smaller)."""
        om = self.overmap()
        world = self._world_read()
        root = self._root_place(world, area.id)
        place = world.places.get(root or "")
        if om is None or place is None or not is_cell(place.id):
            return point
        sx, sy = self._cell_scale(om)
        return place.x + (point[0] - place.x) / sx, place.y + (point[1] - place.y) / sy

    # --- places found ----------------------------------------------------------------

    def overmap_begin(self, state) -> None:
        """The places known from the start, and the traveller's needs set to
        the story's opening, once."""
        om = self.overmap()
        if om is None or state.overmap.begun:
            return
        state.overmap.begun = True
        state.overmap.known = [p.name for p in om.places if p.known]
        needs = state.overmap.needs
        needs.ate = needs.drank = needs.slept = state.seconds

    def overmap_known(self, state=None) -> list[OverPlace]:
        """The places found so far that are on the map (one the plot keeps
        hidden is not, whatever was said of it)."""
        om = self.overmap()
        if om is None:
            return []
        state = state if state is not None else self.area_state()
        world = self._world_read()
        names = {p.name.casefold() for p in world.places.values()}
        known = {n.casefold() for n in state.overmap.known}
        return [
            p
            for p in om.places
            if p.name.casefold() in known and (p.template or p.name.casefold() in names)
        ]

    def _find_near(self, state, points: list[tuple[float, float]], reach: float) -> list[str]:
        """Places within `reach` of any of `points` found now: their lines."""
        om = self.overmap()
        if om is None:
            return []
        world = self._world_read()
        names = {p.name.casefold() for p in world.places.values()}
        known = {n.casefold() for n in state.overmap.known}
        lines = []
        for place in om.places:
            if place.name.casefold() in known:
                continue
            if not place.template and place.name.casefold() not in names:
                continue  # hidden by the plot
            px, py = place.x * KM, place.y * KM
            if any(math.hypot(px - x, py - y) <= reach for x, y in points):
                state.overmap.known.append(place.name)
                known.add(place.name.casefold())
                lines.append(f"Now on your map: {place.name}.")
        return lines

    def _arrived_in(self, state, area_id: str) -> list[str]:
        """The player is in a cell's area: the cell explored, the places in
        and near it found."""
        om = self.overmap()
        point = self.overmap_point(state)
        if om is None or point is None:
            return []
        cell = clamped_cell(om, *point)
        key = cell_key(cell)
        if key not in state.overmap.explored:
            state.overmap.explored.append(key)
        world = self._world_read()
        lines = []
        root = world.places.get(self._root_place(world, area_id) or "")
        if root is not None:
            own = next((p for p in om.places if p.name.casefold() == root.name.casefold()), None)
            if own is not None and own.name not in state.overmap.known:
                state.overmap.known.append(own.name)
        lines += self._find_near(state, [point], FOUND_NEAR_M)
        return lines

    def overmap_tell_of(self, words: str) -> str:
        """A place the keeper told of comes onto the map: the line, or "" for
        none by that name."""
        om = self.overmap()
        if om is None:
            return ""
        want = " ".join(words.split()).casefold().removeprefix("the ")
        found = next(
            (p for p in om.places if p.name.casefold().removeprefix("the ") == want), None
        ) or next((p for p in om.places if want and want in p.name.casefold()), None)
        if found is None:
            return ""
        state = self.area_state()
        if found.name in state.overmap.known:
            return ""
        state.overmap.known.append(found.name)
        self._set_live(state)
        return f"Now on your map: {found.name}."

    def cell_lived_in(self, area_id: str | None) -> bool:
        """Whether the made cell this area is, or lies in, rolled someone
        living in it, holding it or stopped there (`OutdoorTemplate.people`)."""
        cell = cell_of_place(area_id or "")
        if cell is None:
            return False
        made = self._art_read().cells.get(cell_key(cell))
        return bool(made is not None and made.lived_in)

    def place_abandoned(self, area_id: str | None) -> bool:
        """An area of a place the wider world made that nobody lives in:
        picked over long ago, what is left worn (`kits.picked_over`)."""
        world = self._world_read()
        root = self._root_place(world, area_id) if area_id else None
        return bool(root and is_cell(root) and not self.cell_lived_in(root))

    def _dwellers_stop(
        self, state, cell: tuple[int, int], fresh: bool, out: Travelled, there: Area
    ) -> None:
        """A cell entered for the first time that rolled someone living in
        it: the keeper is asked who, as for a happening on the way (one
        rolled already stands: the keeper is asked once)."""
        if not fresh or out.stop is not None or not self.cell_lived_in(there.id):
            return
        om = self.overmap()
        region = region_of(om, cell) if om is not None else None
        minute = (self.visual_start_minute() + state.seconds // 60) % (24 * 60)
        out.stop = Stop(
            kind="dwellers", leg=0, region=region.key if region else "", night=is_night(minute)
        )
        out.brief = self._stop_brief(out.stop, region, there.title, there.title)

    def _cell_flavour(self, world: WorldMap, area: Area) -> str:
        """A made cell's line the first time it is entered: the first
        sentence of what it is."""
        place = world.places.get(area.place_id)
        if place is None or not is_cell(place.id) or place.inside or not place.what:
            return ""
        return re.split(r"(?<=[.!?])\s", place.what.strip(), maxsplit=1)[0]

    # --- the traveller's needs -------------------------------------------------------

    def visual_warmth(self) -> int:
        """How warm the player is dressed: as for the season to begin with
        (the design's `dressed`), and each kind of warm thing carried."""
        design = self.visual_design()
        return design.dressed + warmth(r for _i, r in self.visual_inventory())

    def visual_needs(self, state=None) -> list[str]:
        """What the player is short of now ("hungry", "thirsty", "weary",
        "cold"), for the bar and the fight."""
        if self.overmap() is None:
            return []
        state = state if state is not None else self.area_state()
        needs = state.overmap.needs
        now = state.seconds
        out = []
        if now - needs.ate >= HUNGRY_AFTER:
            out.append("starving" if now - needs.ate >= STARVE_AFTER else "hungry")
        if now - needs.drank >= THIRSTY_AFTER:
            out.append("parched" if now - needs.drank >= _PARCHED else "thirsty")
        if now - needs.slept >= TIRED_AFTER:
            out.append("weary")
        region = self.overmap_region(state)
        area, _me = self._visual_me(state)
        if region is not None and area is not None and not area.indoor:
            if self.visual_warmth() < region.warm:
                out.append("cold")
        return out

    def needs_penalty(self) -> int:
        """What going without takes off the player's fighting."""
        short = self.visual_needs()
        return sum(1 for word in short if word != "cold")

    def needs_tick(self, state) -> list[str]:
        """Hunger and thirst past the worst hurt as time passes, a wound a
        day, wherever the player is (a journey counts them as it goes)."""
        if self.overmap() is None or not state.overmap.begun:
            return []
        needs = state.overmap.needs
        now = state.seconds
        lines = []
        starving = now - needs.ate - STARVE_AFTER
        if starving >= 0:
            due = starving // (24 * 3600) + 1
            while needs.starved < due:
                needs.starved += 1
                state.wounds += 1
                lines.append("Hunger takes the strength out of you.")
        parched = now - needs.drank - _PARCHED
        if parched >= 0:
            due = parched // (24 * 3600) + 1
            while needs.parched < due:
                needs.parched += 1
                state.wounds += 1
                lines.append("Thirst is wearing you down.")
        return lines

    def note_ate(self, state, kind: str) -> None:
        if kind == "food":
            state.overmap.needs.ate = state.seconds
            state.overmap.needs.starved = 0
        elif kind == "drink":
            state.overmap.needs.drank = state.seconds
            state.overmap.needs.parched = 0

    def note_slept(self, state) -> None:
        state.overmap.needs.slept = state.seconds

    def carried_provisions(self) -> tuple[int, int]:
        """(food, drink) the player carries, in portions."""
        return self._carried_units("food"), self._carried_units("drink")

    def _carried_units(self, kind: str) -> int:
        return sum(
            max(1, r.count)
            for _i, r in self.visual_inventory()
            if (r.kind or kind_of(r.name)) == kind
        )

    def _use_up(self, state, kind: str, units: int) -> None:
        """`units` of food or drink gone from what the player carries."""
        held = self.held_name()
        for record in state.things.values():
            if units <= 0:
                return
            if record.state != "taken" or record.by != held:
                continue
            if (record.kind or kind_of(record.name)) != kind:
                continue
            used = min(units, max(1, record.count))
            record.count -= used
            units -= used
            if record.count <= 0:
                record.state = "gone"

    # --- journeys --------------------------------------------------------------------

    def overmap_choices(self) -> list[OverChoice]:
        """The places found, with the walk to each from here (one way found
        for all of them at once), nearest first; the one the player is in
        marked."""
        om = self.overmap()
        if om is None:
            return []
        state = self.area_state()
        start = self.overmap_point(state)
        if start is None:
            return []
        times = times_from(om, start)
        here = self.overmap_cell(state)
        me = state.spots.get(self.held_name() or "")
        world = self._world_read()
        my_root = self._root_place(world, me.area) if me is not None else None
        out = []
        for place in self.overmap_known(state):
            point = (place.x * KM, place.y * KM)
            cell = clamped_cell(om, *point)
            area_id = self.cell_area_id(cell)
            at = cell == here or (
                area_id is not None and self._root_place(world, area_id) == my_root
            )
            out.append(
                OverChoice(
                    name=place.name,
                    x=point[0],
                    y=point[1],
                    walking=0 if at else walking_seconds(om, times, *point),
                    here=at,
                )
            )
        out.sort(key=lambda c: c.walking)
        return out

    def _traveller(self, state) -> Traveller:
        om = self.overmap()
        return Traveller(
            seconds=state.seconds,
            start_minute=self.visual_start_minute(),
            needs=state.overmap.needs.model_copy(),
            food=self._carried_units("food"),
            drink=self._carried_units("drink"),
            warm=self.visual_warmth(),
            wounds=state.wounds,
            max_wounds=self.visual_design().player_hits,
            regions={r.key: r for r in (om.regions if om else [])},
        )

    def _pace(self, state) -> float:
        """The traveller's pace against a rested, fed one's."""
        short = self.visual_needs(state)
        pace = self.visual_walk_factor(state)  # a broken leg (engine/health.py)
        if "weary" in short:
            pace *= 0.8
        if "starving" in short or "parched" in short:
            pace *= 0.8
        return pace

    def overmap_vehicles(self) -> list[tuple[str, str, float]]:
        """The vehicles the player can drive from here (one the keeper made
        to work, in their area): (its record id, its name, the km of fuel in
        its tank and the cans carried)."""
        state = self.area_state()
        area, _me = self._visual_me(state)
        if area is None:
            return []
        cans = self._carried_units("fuel")
        out = []
        for placement in area.placements:
            record = state.things.get(placement.id)
            if record is None or record.state in ("gone", "taken") or not drivable(record):
                continue
            tank = FULL_TANK_KM if record.fuel_km is None else record.fuel_km
            out.append((placement.id, bare(record.name), tank + cans * FUEL_CAN_KM))
        return out

    def overmap_estimate(
        self, x: float, y: float, *, vehicle: str | None = None
    ) -> Estimate | None:
        """What a journey to (x, y) would take, nothing happening on the way
        (`vehicle`: driven, by its record id)."""
        om = self.overmap()
        state = self.area_state()
        start = self.overmap_point(state)
        if om is None or start is None:
            return None
        legs = route(om, start, (x, y), slower=self._pace(state), drive=vehicle is not None)
        if not legs:
            return None
        return estimate(om, legs, state.seconds, self.visual_start_minute(), state.overmap.needs)

    def overmap_travel(
        self, x: float, y: float, name: str = "", *, vehicle: str | None = None
    ) -> Travelled | None:
        """Set off for (x, y) (a place found, by `name`, or any cell): walked
        stretch by stretch with its rolls, camping by night, eating on the
        way (or driven, `vehicle`: its record id, burning fuel, the vehicle
        along). Arrived, stopped in the cell where something happened (the
        keeper is asked what), out of fuel, or dead. None when there is no
        way."""
        om = self.overmap()
        state = self.area_state()
        held = self.held_name()
        start = self.overmap_point(state)
        if om is None or held is None or start is None:
            return None
        legs = route(om, start, (x, y), slower=self._pace(state), drive=vehicle is not None)
        if not legs:
            return None
        ride = None
        dry_at = None
        if vehicle is not None:
            ride = state.things.get(vehicle)
            if ride is None or not drivable(ride):
                return None
            tank = FULL_TANK_KM if ride.fuel_km is None else ride.fuel_km
            cans = self._carried_units("fuel")
            reach = tank + cans * FUEL_CAN_KM
            if reach <= 0:
                return None
            driven = 0.0
            before = start
            for n, leg in enumerate(legs):
                driven += math.hypot(leg.x - before[0], leg.y - before[1]) / KM
                before = (leg.x, leg.y)
                if driven > reach:
                    dry_at = n
                    break
            if dry_at is not None:
                legs = legs[: max(1, dry_at)]
            used = min(driven, reach)
            from_cans = max(0.0, used - tank)
            spent_cans = math.ceil(from_cans / FUEL_CAN_KM) if from_cans > 0 else 0
            self._use_up(state, "fuel", spent_cans)
            ride.fuel_km = max(0.0, tank + spent_cans * FUEL_CAN_KM - used)
        world = self._world_read()
        me = state.spots.get(held)
        frm_root = world.places.get(self._root_place(world, me.area if me else None) or "")
        frm = frm_root.name if frm_root is not None else "where you were"
        target_cell = clamped_cell(om, x, y)
        to = name or self.cell_name(target_cell)
        rng = random.Random(f"{self.visual_seed()}:{state.seconds}:{cell_key(target_cell)}")
        traveller = self._traveller(state)
        done: Journey = journey(legs, traveller, rng, place_name=to)
        if dry_at is not None and done.arrived:
            # The tank ran dry before the way's end: the drive ends there.
            done.arrived = False
            done.lines = [ln for ln in done.lines if not ln.startswith("You reach")]
            done.lines.append("The engine coughs and dies: out of fuel.")
        out = Travelled(seconds=done.seconds, arrived=done.arrived, dead=done.dead)
        how = f"drive the {bare(ride.name)}" if ride is not None else "set out"
        out.lines.append(f"You {how} for {to}.")
        out.lines += done.lines
        state.seconds += done.seconds
        state.overmap.needs = done.needs
        self._use_up(state, "food", done.eaten)
        self._use_up(state, "drink", done.drunk)
        state.wounds += done.wounds
        out.lines += self.advance_healing(state, done.seconds, resting=False)
        walked = legs[: done.legs + 1]
        out.lines += self._find_near(state, [(leg.x, leg.y) for leg in walked], FOUND_M)
        if done.dead or state.wounds >= self.visual_design().player_hits:
            state.dead = True
            out.dead = True
            out.lines.append("You don't make it. You are dead.")
            self._set_live(state)
            return out
        if done.arrived:
            cell, point = target_cell, (x, y)
        else:
            leg = legs[done.legs]
            cell, point = leg.cell, (leg.x, leg.y)
        fresh = cell_key(cell) not in state.overmap.explored
        before = walked[-2] if len(walked) > 1 else None
        came_from = (before.x, before.y) if before is not None else start
        spot = self._land(
            state,
            cell,
            point,
            came_from,
            road=bool(walked and walked[-1].road),
            at_edge=done.arrived,
        )
        if spot is None:
            self._set_live(state)
            return None
        state.spots[held] = spot
        out.lines += self._arrived_in(state, spot.area)
        if ride is not None:
            # The vehicle stands beside them where they stop.
            there = self.area(spot.area, things=state.things)
            if there is not None:
                taken = there.blocked() | {
                    (s.x, s.y) for s in state.spots.values() if s.area == there.id
                }
                vx, vy = stand_near(there, (spot.x, spot.y), taken | {(spot.x, spot.y)})
                ride.state, ride.by = "dropped", ""
                ride.spot = Spot(area=there.id, x=vx, y=vy)
                if ride.fuel_km is not None and ride.fuel_km < 15:
                    out.lines.append(f"The {bare(ride.name)} is nearly out of fuel.")
        self._set_live(state)
        self._companions_to(spot)
        there = self.area(spot.area)
        out.to = there.title if there is not None else to
        if done.stop is not None:
            out.stop = done.stop
            region = region_named(om, done.stop.region)
            out.brief = self._stop_brief(done.stop, region, frm, to)
        if there is not None and done.arrived:
            self._dwellers_stop(state, cell, fresh, out, there)
        self.visual_lay_out()
        return out

    def _land(
        self,
        state,
        cell: tuple[int, int],
        point: tuple[float, float],
        came_from: tuple[float, float],
        *,
        road: bool = False,
        at_edge: bool = False,
    ) -> Spot | None:
        """Where the player stands on arriving in a cell's area at `point`
        (true metres): arrived at a place, at its edge toward where they
        came from (the road there, when one comes in near it); stopped on
        the way, at the point itself, on the road when they were on one."""
        area_id = self.ensure_cell(cell, state)
        if area_id is None:
            return None
        world, art = self._world_read(), self._art_read()
        area = self.area(area_id, world, art, things=state.things)
        if area is None:
            return None
        if area.indoor:
            x, y = stand_near(area, (area.width // 2, area.height // 2), area.blocked())
            return Spot(area=area.id, x=x, y=y)
        inside = self._in_area(area, point)
        tile = clamped_tile(world, area, *inside)
        rect_point = tile_point(world, area, *tile) if tile is not None else None
        on_point = (
            rect_point is not None
            and math.hypot(rect_point[0] - inside[0], rect_point[1] - inside[1]) < area.tile_m * 2
        )
        if tile is None or not on_point or at_edge:
            # Come to the place (or outside the area's ground, a place's own
            # being smaller than its cell): in at its edge toward where they
            # came from.
            on_point = False
            tile = self._edge_toward(world, area, came_from)
        if tile is None:
            tile = (area.width // 2, area.height // 2)
        taken = area.blocked() | {(s.x, s.y) for s in state.spots.values() if s.area == area.id}
        if road or not on_point:
            # On the road they were on: anywhere in the cell, which it crosses
            # off its middle as often as not; come in at an edge, the road
            # where it crosses that edge, nearest the way they came.
            reach = max(area.width, area.height) if road and on_point else ARRIVAL_ROAD_REACH
            roads = [
                (math.hypot(x - tile[0], y - tile[1]), x, y)
                for y in range(max(0, tile[1] - reach), min(area.height, tile[1] + reach + 1))
                for x in range(max(0, tile[0] - reach), min(area.width, tile[0] + reach + 1))
                if area.rows[y][x] in "=b" and (x, y) not in taken
            ]
            if not roads and road and not on_point:
                roads = [
                    (math.hypot(x - tile[0], y - tile[1]), x, y)
                    for x, y in _edge_tiles(area)
                    if area.rows[y][x] in "=b" and (x, y) not in taken
                ]
            if roads:
                _d, x, y = min(roads)
                return Spot(area=area.id, x=x, y=y)
        x, y = stand_near(area, tile, taken, seed="arrive")
        if not self._joined(area, (x, y)):
            # The edge is rock or wall (a cave): in by the plan's own way in
            # when it names one, else the middle (live, a journey to the
            # Hidden Caves came in at the main chamber, beside its keeper).
            way_in = self._way_in_label(area, tile)
            x, y = stand_near(
                area, way_in or (area.width // 2, area.height // 2), taken, seed="arrive"
            )
        return Spot(area=area.id, x=x, y=y)

    @staticmethod
    def _way_in_label(area: Area, near: tuple[int, int]) -> tuple[int, int] | None:
        """The plan's labelled way in (a cave mouth, a gate, an entrance)
        nearest `near`, or None."""
        found = [
            (math.hypot(x - near[0], y - near[1]), x, y)
            for name, (x, y) in area.labels.items()
            if re.search(r"\b(mouth|gate|entrance|entry|way in|door|opening)\b", name, re.I)
        ]
        if not found:
            return None
        _d, x, y = min(found)
        return x, y

    def _edge_toward(self, world: WorldMap, area: Area, came_from: tuple[float, float]):
        """The tile on the area's edge in the direction of `came_from` (true
        metres): where a ray from the area's middle that way leaves it.
        Clamping the point's coordinates one by one put a traveller in a
        corner (live, Brannock's far south-west corner, 30 tiles from the
        road and the street)."""
        middle = (area.width // 2, area.height // 2)
        rect = area_rect(world, area)
        if rect is None:
            return middle
        west, north, width, height = rect
        cx, cy = west + width / 2, north - height / 2
        fx, fy = self._in_area(area, came_from)
        dx, dy = fx - cx, fy - cy
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return middle
        t = min(
            (width / 2) / abs(dx) if dx else math.inf,
            (height / 2) / abs(dy) if dy else math.inf,
        )
        point = (cx + dx * t * 0.98, cy + dy * t * 0.98)
        return clamped_tile(world, area, *point) or middle

    @staticmethod
    def _joined(area: Area, spot: tuple[int, int]) -> bool:
        from sealedlore.engine.session_visual import _reaches

        return _reaches(area, spot)

    def _stop_brief(self, stop: Stop, region: OverRegion | None, frm: str, to: str) -> str:
        """Why the keeper is asked: where the journey was, by day or night,
        what kind of thing happens, and the region's own examples of it."""
        held = self.held_name() or "The player"
        name = region.name if region is not None else "the open country"
        words = f" ({region.words})" if region is not None and region.words else ""
        when = "camped for the night" if stop.camping else "at night" if stop.night else "by day"
        if stop.kind == "dwellers":
            # Someone lives in the place come to (`OutdoorTemplate.people`):
            # who, in keeping with the place and the country, is the keeper's.
            like = (region.interest + region.opportunity + region.danger) if region else []
            return (
                f"{held} has come to {to}, in {name}{words}, {when}, and someone is here: "
                "whoever lives in this place, holds it or has stopped here, met now. Who they "
                "are is yours, in keeping with what the place is and the country round it"
                + (" (people like: " + "; ".join(like) + ")" if like else "")
                + ". They may be wary, welcoming, hiding, or dangerous; they have been here "
                "a while, with reasons of their own."
            )
        examples = getattr(region, stop.kind, []) if region is not None else []
        doing = f"was on the way from {frm} to {to}" if to else "was exploring"
        if frm == to:
            doing = "was sleeping out of doors" if stop.camping else "was exploring"
        lines = [
            f"{held} {doing}, in {name}{words}, {when}, when something happened here: "
            f"{KIND_WORDS.get(stop.kind, stop.kind)}."
        ]
        if examples:
            lines.append(f"The kind of thing that happens in {name}: " + "; ".join(examples) + ".")
        if stop.ice:
            lines.append("They were crossing the river on the ice.")
        return " ".join(lines)

    def overmap_rest_roll(self, seconds: int) -> Travelled | None:
        """A night slept out of doors in the wider world, as a camp on a
        journey: a roll at the region's night rate (asleep, no chance of
        anything good), the keeper asked what when it comes up. None when
        nothing happens, indoors, or with no overmap."""
        om = self.overmap()
        state = self.area_state()
        area, _me = self._visual_me(state)
        if om is None or area is None or area.indoor:
            return None
        if self.safe_here(state, area):
            # A guest, a friend's: the people of the place keep the night
            # (live, Kane's own men roused his guest at club-point).
            return None
        region = self.overmap_region(state)
        if region is None:
            return None
        rng = random.Random(f"{self.visual_seed()}:{state.seconds}:rest")
        if rng.random() >= roll_chance(region.chance * CAMP_SHARE, seconds):
            return None
        kind = _pick_kind(rng, weights(region, night=True, asleep=True))
        stop = Stop(kind=kind, leg=0, region=region.key, night=True, camping=True)
        here = area.title
        return Travelled(stop=stop, brief=self._stop_brief(stop, region, here, here), to=here)

    def visual_sleep_out(self) -> tuple[list[str], Travelled | None]:
        """Sleep till morning, in the wider world: out of doors the night has
        its roll, and something that comes up wakes the player part way
        through (the keeper is asked what). (the lines, what woke them)."""
        clock = self.map_clock()
        now = clock[1] if clock else 0
        minutes = (SLEEP_UNTIL - now) % (24 * 60) or 24 * 60
        woken = self.overmap_rest_roll(minutes * 60)
        if woken is None:
            return self.visual_sleep(), None
        state = self.area_state()
        part = max(30, int(minutes * random.Random(f"{state.seconds}:woken").uniform(0.2, 0.9)))
        lines = [
            f"You sleep {part // 60} hours, until something wakes you.",
            *self.visual_wait(part),
        ]
        state = self.area_state()
        self.note_slept(state)
        self._set_live(state)
        return lines, woken

    # --- walking on into the next cell ----------------------------------------------

    def _beyond_cell(self, state, side: str) -> tuple[int, int] | None:
        """The next cell past this edge whose area is not this one (a plot
        place's ground covers several)."""
        om = self.overmap()
        me = state.spots.get(self.held_name() or "")
        point = self.overmap_point(state)
        if om is None or me is None or point is None:
            return None
        world = self._world_read()
        mine = self._root_place(world, me.area)
        cell = clamped_cell(om, *point)
        for _ in range(8):
            cell = neighbour(cell, side)
            if not on_map(om, cell):
                return None
            area_id = self.cell_area_id(cell)
            if area_id is None or self._root_place(world, area_id) != mine:
                return cell
        return None

    def overmap_edge(self, side: str) -> tuple[str, int] | None:
        """What lies past this edge, and the walk to the edge: (its name,
        seconds), or None at the world's end."""
        state = self.area_state()
        cell = self._beyond_cell(state, side)
        if cell is None:
            return None
        edge = self.edge_tile(side)
        here = self.area(edge[0].area) if edge is not None else None
        walk = walk_metres(here, edge[1]) if edge is not None and here is not None else 0.0
        return self.cell_name(cell), int(walk / WALK_M_PER_S) + 30

    def overmap_cross(self, side: str) -> Travelled | None:
        """On past this edge into the next cell: the walk to the edge, the
        cell made if it is new (and a roll for what is there the first
        time), the player at its near edge."""
        om = self.overmap()
        state = self.area_state()
        held = self.held_name()
        cell = self._beyond_cell(state, side)
        edge = self.edge_tile(side)
        if om is None or held is None or cell is None or edge is None:
            return None
        here = self.area(edge[0].area)
        seconds = (int(walk_metres(here, edge[1]) / WALK_M_PER_S) if here is not None else 0) + 30
        fresh = cell_key(cell) not in state.overmap.explored
        area_id = self.ensure_cell(cell, state)
        if area_id is None:
            return None
        world, art = self._world_read(), self._art_read()
        there = self.area(area_id, world, art, things=state.things)
        if there is None or there.indoor:
            return None
        landing = self._edge_landing(state, there, side, edge[0])
        if landing is None:
            return None
        state.spots[held] = landing
        state.seconds += seconds
        out = Travelled(seconds=seconds, arrived=True, to=there.title)
        out.lines += self._arrived_in(state, landing.area)
        self._set_live(state)
        self._companions_to(landing)
        if fresh:
            region = region_of(om, cell)
            rng = random.Random(f"{self.visual_seed()}:{cell_key(cell)}:explore")
            chance = region.chance if region is not None else 5.0
            if rng.random() < roll_chance(chance, EXPLORE_SECONDS):
                minute = (self.visual_start_minute() + state.seconds // 60) % (24 * 60)
                night = is_night(minute)
                kind = _pick_kind(rng, weights(region, night=night, asleep=False))
                out.stop = Stop(kind=kind, leg=0, region=region.key if region else "", night=night)
                out.brief = self._stop_brief(out.stop, region, there.title, there.title)
        self._dwellers_stop(state, cell, fresh, out, there)
        self.visual_lay_out()
        return out

    def _edge_landing(self, state, there: Area, side: str, edge: Spot) -> Spot | None:
        """Where walking on past an edge comes in: the far area's near edge,
        across from where the player left (their place along the edge kept),
        on walkable ground joined to the rest."""
        here = self.area(edge.area)
        frac_x = edge.x / max(1, (here.width - 1)) if here is not None else 0.5
        frac_y = edge.y / max(1, (here.height - 1)) if here is not None else 0.5
        if side == "north":
            tile = (round(frac_x * (there.width - 1)), there.height - 1)
        elif side == "south":
            tile = (round(frac_x * (there.width - 1)), 0)
        elif side == "east":
            tile = (0, round(frac_y * (there.height - 1)))
        else:
            tile = (there.width - 1, round(frac_y * (there.height - 1)))
        taken = there.blocked() | {(s.x, s.y) for s in state.spots.values() if s.area == there.id}
        landing = self._landing(there, Spot(area=there.id, x=tile[0], y=tile[1]), side)
        if landing is None or not self._joined(there, landing):
            landing = stand_near(there, tile, taken, seed="edge")
            if not self._joined(there, landing):
                return None
        return Spot(area=there.id, x=landing[0], y=landing[1])
