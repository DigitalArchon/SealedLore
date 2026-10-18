"""Map-first play (SealedLoreDocs design/map-first.md): pure. An area is a
place's plan made the law of space: a grid of tiles at walking scale, its
rooms, doorways, furniture and things, the doors that lead into the
buildings in it, and the rules every area holds to.

Built afresh from what the story keeps (the plan's shapes, the map, the
room's furnishing, the story's density), so the same story always builds the
same area and nothing about it needs saving but where people stand
(models/area.py). The kit this was measured with is SealedLoreDocs
tools/bakeoff/ (zones.py, dress.py, boxes.py, captions.py, furnish.py).

The rules, held whoever proposes a change (design §2):
- every room can be reached (the sample farmhouse had a sealed middle row: a
  wall is opened between it and a room that can be);
- a road never runs through a building (the sample farm's ran through the
  farmhouse): roads are laid on open ground only;
- a building the map knows has a door;
- the tiles in front of every doorway, and a walkway between a room's
  doorways, stay clear of furniture, and no piece cuts off floor that could
  be reached before it was placed.
"""

from __future__ import annotations

import copy
import math
import re
import zlib
from collections import deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sealedlore.engine.deck_plan import DRAWN_PIECES, use_of
from sealedlore.engine.levels import (
    LevelLinks,
    level_area_id,
    level_name,
    linked_levels,
    parse_level,
)
from sealedlore.engine.marks import footprint
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.takeable import FIXED_MARK, fixed_thing, item_verbs, named_in, the
from sealedlore.engine.tile_sets import LIGHT_WORDS, roof_material
from sealedlore.engine.world import key
from sealedlore.engine.world_art import (
    FILLS,
    GROUND,
    TERRAIN_TILES,
    TILES,
    decked,
    fixed_in_plan,
    ground_of,
    has_inside,
    holding_place,
    is_deck,  # noqa: F401  (moved to world_art; still imported from here by tests)
    land_under,
    outside_part,
    parse_window,
    patch_at,
    patch_under,
    plan_place,
    plan_rect,
    plan_tile,
    rasterise,
    window_id,
    window_rect,
    with_fixed,
)
from sealedlore.engine.world_layout import position, size_of
from sealedlore.models.area import FurnishPlan, Spot, ThingRecord
from sealedlore.models.world import WorldArt, WorldMap, WorldPlace

# A tile outdoors, at game scale (design §2: game scale, not true scale).
OUTDOOR_TILE_M = 1.5
# An outdoor plan is scaled up so its tiles come near OUTDOOR_TILE_M, to at
# most this many times and this many tiles across.
MAX_SCALE = 3
MAX_COLUMNS = 160
# Walking pace: 5 km/h (the author's ruling: within an area, moving takes
# little time; between areas, the way's distance).
WALK_M_PER_S = 1.4
# The density dial: furnishing counts times this (measured: the farmhouse
# 9 / 49 / 86 pieces).
DENSITY = {"sparse": 0.5, "normal": 1.0, "full": 1.7}
# A road's deck over water: the play view's own tile, never a plan's.
BRIDGE = "b"
# Where someone may stand, and what a piece of furniture may stand on.
WALKABLE = frozenset("._=+,:s&" + BRIDGE)
FLOOR_TILES = frozenset("_+")
WALLS = frozenset("#W")


# --- What an area is -------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    """Something standing on the floor: a piece of furniture (from the
    room's furnishing) or a thing the plan drew (a numbered object). Each
    blocks walking over its footprint."""

    id: str
    name: str
    x: int
    y: int
    w: int
    h: int
    verbs: tuple[str, ...]
    kind: str  # "furniture" or "object"
    room: int | None = None
    # The author's character destroyed it (a record on the turn's snapshot,
    # `apply_things`): still here, drawn broken, no longer to be destroyed.
    broken: bool = False
    # A deck's set piece, placed by the program before the furniture
    # (engine/deck_plan.py): one it draws itself (DRAWN_PIECES: "reactor",
    # "shaft", "beds"), "set" for one drawn from its sprite; "" for anything else.
    piece: str = ""

    def tiles(self) -> list[tuple[int, int]]:
        return [(self.x + dx, self.y + dy) for dy in range(self.h) for dx in range(self.w)]

    @property
    def centre(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    @property
    def lit(self) -> bool:
        return any(word in self.name.lower() for word in LIGHT_WORDS)


@dataclass
class Room:
    index: int
    name: str
    cells: int
    box: tuple[int, int, int, int]
    cx: float
    cy: float
    objects: list[str] = field(default_factory=list)
    purpose: str = ""
    # What the room is for (engine/deck_plan.USES, or "corridor"): a deck's
    # rooms say it in their plan; a drawn plan's are read from their names
    # and purposes (`name_rooms`). The renderer's zone: its floor and light.
    use: str = ""


@dataclass(frozen=True)
class Doorway:
    """A door (`+`) or a gap in a wall line: (x0, y0)-(x1, y1), the way its
    wall runs ("h" east-west, "v" north-south)."""

    x0: int
    y0: int
    x1: int
    y1: int
    way: str

    def tiles(self) -> list[tuple[int, int]]:
        return [(x, y) for y in range(self.y0, self.y1 + 1) for x in range(self.x0, self.x1 + 1)]

    @property
    def middle(self) -> tuple[int, int]:
        return (self.x0 + self.x1) // 2, (self.y0 + self.y1) // 2


@dataclass(frozen=True)
class Entrance:
    """A building's door in an outdoor area that leads into its own area;
    or, indoors, a stairs tile that leads to another level of the same plan
    (engine/levels.py): `place_id` is then that level's area id and `side`
    "up" or "down"."""

    x: int
    y: int
    place_id: str
    side: str  # the building's wall the door is in: north, south, east, west

    @property
    def stairs(self) -> bool:
        return self.side in ("up", "down")


@dataclass
class Area:
    # The place whose plan this is; for a window of open country, the place
    # holding the whole window (a region, a desert) or "" in no place.
    place_id: str
    name: str
    indoor: bool
    # The ground's look ("snow", "floor", "space"): how it is drawn.
    look: str
    rows: list[str]
    tile_m: float
    objects: dict[str, str]
    labels: dict[str, tuple[int, int]]
    placements: list[Placement] = field(default_factory=list)
    rooms: list[Room] = field(default_factory=list)
    room_of: list[list[int]] = field(default_factory=list)
    doorways: list[Doorway] = field(default_factory=list)
    # Outdoors: the doors into buildings with an inside of their own, and
    # each building's box (x0, y0, x1, y1) by place id.
    entrances: list[Entrance] = field(default_factory=list)
    buildings: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    # Indoors: the doors out, and the area they lead to.
    exits: list[tuple[int, int]] = field(default_factory=list)
    parent: str | None = None
    # The names of the buildings in `buildings`, by place id, and what each
    # one's roof is made of by its own words ("stone"; "" for the set's own:
    # `tile_sets.roof_material`).
    names: dict[str, str] = field(default_factory=dict)
    roofs: dict[str, str] = field(default_factory=dict)
    # What the rules repaired, for the log and the tests.
    repairs: list[str] = field(default_factory=list)
    # A window of open country (`build_window`): its rectangle on the map
    # (west, north, width, height) in metres, the land patch it is built
    # from ("" while none is drawn there), and the name of the place holding
    # it ("" in no place), for "in open country, in the Great Erg".
    rect: tuple[float, float, float, float] | None = None
    patch_id: str = ""
    within: str = ""
    # One of a plan's further levels (engine/levels.py `build_level`): its
    # index (0 is the plan itself) and name ("Upper floor", "Deck 2").
    level: int = 0
    level_name: str = ""
    # A deck laid out by the program: its hull's mass, a row per row of the
    # plan, "H" plating, "R" rock, "E" an engine, " " none (engine/deck_plan.py;
    # drawn by gui/deck_look.py, never walked on: the rows keep " " there).
    hull: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        """What a `Spot.area` names: the place's id, a level's id, or a
        window's own id."""
        if self.rect is None:
            return level_area_id(self.place_id, self.level)
        west, north, width, _height = self.rect
        return window_id(west, north, width)

    @property
    def title(self) -> str:
        """The area as the play view names it: "The farmhouse · Upper floor"."""
        return f"{self.name} · {self.level_name}" if self.level_name else self.name

    def stairs_entrances(self) -> list[Entrance]:
        return [e for e in self.entrances if e.stairs]

    @property
    def window(self) -> bool:
        return self.rect is not None

    @property
    def width(self) -> int:
        return len(self.rows[0]) if self.rows else 0

    @property
    def height(self) -> int:
        return len(self.rows)

    def at(self, x: int, y: int) -> str:
        if 0 <= y < self.height and 0 <= x < self.width:
            return self.rows[y][x]
        return " "

    def blocked(self) -> set[tuple[int, int]]:
        """Tiles a placement stands on."""
        return {t for p in self.placements for t in p.tiles()}

    def walkable(self, x: int, y: int, blocked: Collection[tuple[int, int]] = ()) -> bool:
        return self.at(x, y) in WALKABLE and (x, y) not in blocked

    def placement_at(self, x: int, y: int) -> Placement | None:
        return next((p for p in self.placements if (x, y) in p.tiles()), None)

    def entrance_at(self, x: int, y: int) -> Entrance | None:
        return next((e for e in self.entrances if (e.x, e.y) == (x, y)), None)

    def room_at(self, x: int, y: int) -> Room | None:
        if not self.room_of or not (0 <= y < self.height and 0 <= x < self.width):
            return None
        index = self.room_of[y][x]
        return self.rooms[index] if 0 <= index < len(self.rooms) else None


# --- Which area a place is in ---------------------------------------------


def area_of(
    world: WorldMap,
    art: WorldArt,
    place_id: str | None,
    point: tuple[float, float] | None = None,
) -> str | None:
    """The area someone at `place_id` stands in: the innermost building
    round them with an inside of its own, else the local area's plan (the
    farm for its yard, the town for its street), else a wider place's up
    the chain (`world_art.plan_place`; a room of a deck is the deck). Where
    no plan covers them (a region, a desert, between places, no place at
    all) a window of the land at `point` (their tile on the map; else the
    place's spot), so wherever they are there is ground to stand on: the
    author, Oct 16 2026, the world is limitless. None only where there is
    no point to stand at either."""
    place = plan_place(world, art, place_id)
    if place is not None:
        return place.id
    if point is None:
        point = position(world, place_id)
    if point is None:
        return None
    return window_id(*window_rect(art, *point))


# --- Building an area ---------------------------------------------------------


def _scale_for(place: WorldPlace, columns: int) -> int:
    k = round(plan_tile(place) / OUTDOOR_TILE_M)
    return max(1, min(MAX_SCALE, k, MAX_COLUMNS // max(columns, 1)))


def scaled(spec: dict[str, Any], k: int) -> dict[str, Any]:
    """A plan's shapes `k` times larger (tools/bakeoff/zones.py): buildings,
    areas and walls scale; an object stays one tile, at the middle of its
    old one; a road widens to at most 6 tiles."""
    spec = copy.deepcopy(spec)
    for f in spec.get("features", []):
        if not isinstance(f, dict):
            continue
        for name in ("x", "y", "w", "h"):
            if name in f:
                try:
                    f[name] = int(f[name]) * k
                except (TypeError, ValueError):
                    f[name] = 0
        if f.get("kind") == "object":
            f["x"] = int(f.get("x", 0)) + k // 2
            f["y"] = int(f.get("y", 0)) + k // 2
        for name in ("points", "gaps"):
            if isinstance(f.get(name), list):
                f[name] = [
                    [int(p[0]) * k + k // 2, int(p[1]) * k + k // 2]
                    for p in f[name]
                    if isinstance(p, (list, tuple)) and len(p) >= 2
                ]
        if f.get("kind") in ("road", "passage"):
            try:
                f["width"] = min(int(f.get("width", 1)) * k, 6)
            except (TypeError, ValueError):
                f["width"] = k
        for name in ("doors", "windows"):
            for d in f.get(name) or []:
                if isinstance(d, dict):
                    try:
                        d["at"] = int(d.get("at", 0)) * k + k // 2
                    except (TypeError, ValueError):
                        d["at"] = k // 2
    return spec


# A wall run longer than this is a town's wall or a barricade, not a
# building's (gui/map_tiles.py roofs a ring no wider than 16 tiles).
TOWN_WALL_RUN = 16


def _wall_run(grid: list[list[str]], x: int, y: int, dx: int, dy: int) -> int:
    """How many wall tiles run through (x, y) along (dx, dy), both ways."""
    h, w = len(grid), len(grid[0])
    count = 1
    for sign in (1, -1):
        cx, cy = x + sign * dx, y + sign * dy
        while 0 <= cx < w and 0 <= cy < h and grid[cy][cx] == "#":
            count += 1
            cx, cy = cx + sign * dx, cy + sign * dy
    return count


def _roads_on_open_ground(rows: list[str], roads: Sequence[dict[str, Any]]) -> list[str]:
    """Roads by distance from their line, on open ground only: a road never
    runs through a building (the rule), and a diagonal is a line, not the
    rasteriser's staircase. Where a road crosses a town's wall it is a gate:
    live (Oct 17 2026) Hellsville's wall closed the road at both its gates,
    and nobody could walk into the town."""
    if not roads:
        return rows
    grid = [list(r) for r in rows]
    h, w = len(grid), len(grid[0]) if grid else 0
    gates: set[tuple[int, int]] = set()
    for f in roads:
        pts = [p for p in f.get("points") or [] if isinstance(p, (list, tuple)) and len(p) >= 2]
        try:
            radius = max(int(f.get("width", 1)), 1) / 2 + 0.2
        except (TypeError, ValueError):
            radius = 0.7
        for (ax, ay), (bx, by) in zip(pts, pts[1:], strict=False):
            dx, dy = bx - ax, by - ay
            length = max(dx * dx + dy * dy, 1e-6)
            x_lo, x_hi = (
                int(max(0, min(ax, bx) - radius - 1)),
                int(min(w, max(ax, bx) + radius + 2)),
            )
            y_lo, y_hi = (
                int(max(0, min(ay, by) - radius - 1)),
                int(min(h, max(ay, by) + radius + 2)),
            )
            for y in range(y_lo, y_hi):
                for x in range(x_lo, x_hi):
                    t = min(1.0, max(0.0, ((x - ax) * dx + (y - ay) * dy) / length))
                    if math.hypot(x - (ax + t * dx), y - (ay + t * dy)) <= radius:
                        if grid[y][x] in ".,s":
                            grid[y][x] = "="
                        elif grid[y][x] == "~":
                            # Over water a road is a bridge: live (Oct 18 2026)
                            # the toll bridge's road stopped at both banks, its
                            # arches stood in the river, and nobody could cross.
                            grid[y][x] = BRIDGE
                        elif grid[y][x] == "#" and (
                            # Across the wall, never along it.
                            (abs(dy) > abs(dx) and _wall_run(grid, x, y, 1, 0) > TOWN_WALL_RUN)
                            or (abs(dx) > abs(dy) and _wall_run(grid, x, y, 0, 1) > TOWN_WALL_RUN)
                        ):
                            gates.add((x, y))
    for x, y in gates:
        grid[y][x] = "+"
    return ["".join(r) for r in grid]


# What a bridge is built of: drawn as its deck, not as things in the river.
BRIDGE_PART = re.compile(
    r"\b(?:(?:foot)?bridges?|arch(?:es|way)?|piers?|spans?|pilings?|piles|trestles?|"
    r"abutments?|stepping stones?|causeway|planks?)\b",
    re.IGNORECASE,
)


def _bridge_parts(rows: list[str], objects: dict[str, str]) -> list[str]:
    """A plan's arches, piers and spans in the water are its bridge: each
    beside a bridge's deck (or between two of them) is deck too, and one in
    open water is water; neither stands as a thing. Live (Oct 18 2026) the
    toll bridge's "three stone arches" were four crates in the river."""
    parts = {digit: name for digit, name in objects.items() if BRIDGE_PART.search(name)}
    if not parts:
        return rows
    grid = [list(r) for r in rows]
    h, w = len(grid), len(grid[0]) if grid else 0

    def around(x: int, y: int) -> list[str]:
        return [
            grid[ny][nx]
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
            if 0 <= nx < w and 0 <= ny < h
        ]

    for _round in range(2):  # an arch beside an arch beside the deck
        for y in range(h):
            for x in range(w):
                digit = grid[y][x]
                if digit not in parts:
                    continue
                near = around(x, y)
                if BRIDGE in near:
                    grid[y][x] = BRIDGE
                elif _round and "~" in near:
                    grid[y][x] = "~"
                else:
                    continue
                objects.pop(digit, None)
    return ["".join(r) for r in grid]


# A plan's object that is a way in or a window: a tile of the wall, not a thing.
_DOORISH = re.compile(
    r"\b(?:doors?|doorways?|gates?|gateways?|entrances?|hatch(?:es)?|trapdoors?|windows?)\b",
    re.IGNORECASE,
)


def _doors_in_walls(rows: list[str], objects: dict[str, str]) -> list[str]:
    """A plan's object named for a door, gate or window and drawn in a wall
    (wall on both sides of it) is that door or window: live (Oct 18 2026)
    the map shop's "door to the lane" stood in its south wall as a thing,
    drawn with a picture of its own, and the storyteller was told it was
    fixed in place when the author locked it."""
    if not any(_DOORISH.search(name) for name in objects.values()):
        return rows
    grid = [list(r) for r in rows]
    h, w = len(grid), len(grid[0]) if grid else 0
    wall = WALLS | {"+"}

    def at(x: int, y: int) -> str:
        return grid[y][x] if 0 <= x < w and 0 <= y < h else ""

    for digit, name in list(objects.items()):
        if not _DOORISH.search(name):
            continue
        spot = next(((x, y) for y in range(h) for x in range(w) if grid[y][x] == digit), None)
        if spot is None:
            continue
        x, y = spot
        across = at(x - 1, y) in wall and at(x + 1, y) in wall
        along = at(x, y - 1) in wall and at(x, y + 1) in wall
        if across or along:
            window = re.search(r"\bwindows?\b", name, re.IGNORECASE) is not None
            grid[y][x] = "W" if window else "+"
            objects.pop(digit)
    return ["".join(r) for r in grid]


# Ground cover, per look: (tile, noise scale in tiles, threshold, density).
COVER: dict[str, list[tuple[str, float, float, float]]] = {
    "snow": [("T", 9, 0.62, 0.55), (",", 5, 0.66, 0.35), ("^", 4, 0.80, 0.4)],
    "grass": [("T", 8, 0.45, 0.75), (",", 4, 0.55, 0.5), ("^", 5, 0.85, 0.3)],
    "dirt": [(",", 6, 0.60, 0.4), ("^", 5, 0.75, 0.4), ("T", 9, 0.78, 0.4)],
    "hell": [("^", 7, 0.62, 0.6), (":", 4, 0.66, 0.4), ("*", 6, 0.86, 0.35)],
    "rock": [("^", 6, 0.55, 0.6), (":", 4, 0.65, 0.4)],
    "cobble": [(":", 6, 0.80, 0.25)],
}


def _noise(w: int, h: int, scale: float, seed: int):
    """Smooth value noise in [0, 1], two octaves (numpy, imported here:
    nothing in src/ imports it at the top)."""
    import numpy as np

    out = np.zeros((h, w))
    for octave, weight in ((1, 0.7), (2, 0.3)):
        cell = max(scale / octave, 1.0)
        gw, gh = int(w / cell) + 2, int(h / cell) + 2
        grid = np.random.default_rng(seed + octave).random((gh, gw))
        ys, xs = np.mgrid[0:h, 0:w] / cell
        x0, y0 = xs.astype(int), ys.astype(int)
        fx, fy = xs - x0, ys - y0
        fx, fy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
        a, b = grid[y0, x0], grid[y0, x0 + 1]
        c, d = grid[y0 + 1, x0], grid[y0 + 1, x0 + 1]
        out += weight * ((a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy)
    return out


def dress(rows: list[str], look: str, seed_key: str) -> list[str]:
    """The ground's own cover at walking scale (woods, brush, rock, rubble),
    by seeded noise, never within two tiles of anything the plan drew, so
    it never blocks a door or a road. At walking scale a plan's open ground
    is an empty field without it (the bake-off)."""
    cover = COVER.get(look, [])
    if not cover or not rows:
        return rows
    import numpy as np

    grid = np.array([list(r) for r in rows])
    h, w = grid.shape
    open_ground = grid == "."
    drawn = ~open_ground
    near = drawn.copy()
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            near |= np.roll(np.roll(drawn, dy, 0), dx, 1)
    free = open_ground & ~near
    seed = zlib.crc32(seed_key.encode())
    rng = np.random.default_rng(seed)
    placed = np.zeros((h, w), bool)
    for i, (ch, scale, threshold, density) in enumerate(cover):
        noise = _noise(w, h, scale, seed + 17 * i)
        put = free & (noise > threshold) & (rng.random((h, w)) < density)
        grid[put] = ch
        placed |= put
        free &= ~put
    rows = ["".join(r) for r in grid]
    return _keep_connected(rows, placed.tolist())


def _keep_connected(rows: list[str], placed: list[list[bool]]) -> list[str]:
    """The rule for dressing: it never cuts anything off. Each run of ground
    one can stand on that the cover walled in is joined to the largest run
    by clearing the fewest tiles of cover between them."""
    h, w = len(rows), len(rows[0]) if rows else 0
    grid = [list(r) for r in rows]

    def standable(x: int, y: int) -> bool:
        return grid[y][x] in WALKABLE

    label = [[-1] * w for _ in range(h)]
    sizes: list[int] = []
    for sy in range(h):
        for sx in range(w):
            if label[sy][sx] >= 0 or not standable(sx, sy):
                continue
            index = len(sizes)
            stack, count = [(sx, sy)], 0
            label[sy][sx] = index
            while stack:
                x, y = stack.pop()
                count += 1
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and label[ny][nx] < 0 and standable(nx, ny):
                        label[ny][nx] = index
                        stack.append((nx, ny))
            sizes.append(count)
    if len(sizes) < 2:
        return rows
    main = max(range(len(sizes)), key=sizes.__getitem__)
    joined = {main}
    for index in sorted(range(len(sizes)), key=lambda i: -sizes[i]):
        if index in joined:
            continue
        # Breadth first from this run, through ground and the cover alone,
        # to any run already joined; the cover on the way is cleared.
        start = [(x, y) for y in range(h) for x in range(w) if label[y][x] == index]
        prev: dict[tuple[int, int], tuple[int, int] | None] = {s: None for s in start}
        todo = deque(start)
        end = None
        while todo:
            x, y = todo.popleft()
            if label[y][x] in joined:
                end = (x, y)
                break
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if not (0 <= nx < w and 0 <= ny < h) or (nx, ny) in prev:
                    continue
                if standable(nx, ny) or placed[ny][nx]:
                    prev[(nx, ny)] = (x, y)
                    todo.append((nx, ny))
        if end is None:
            continue  # walled in by the plan itself, not the cover
        step = end
        while step is not None:
            x, y = step
            if placed[y][x]:
                grid[y][x] = "."
            step = prev[step]
        joined.add(index)
    return ["".join(r) for r in grid]


def openings(rows: Sequence[str], widest: int = 8) -> list[Doorway]:
    """Doorways: each door (`+`), and each gap in a wall line, a run of up to
    `widest` open tiles with wall at both ends and open ground on both sides
    (tools/bakeoff/boxes.py)."""
    h, w = len(rows), len(rows[0]) if rows else 0

    def wall(x: int, y: int) -> bool:
        return 0 <= x < w and 0 <= y < h and rows[y][x] in WALLS

    def open_(x: int, y: int) -> bool:
        return 0 <= x < w and 0 <= y < h and rows[y][x] not in WALLS and rows[y][x] != " "

    def side(x: int, y: int) -> bool:
        return not (0 <= x < w and 0 <= y < h) or open_(x, y)

    found: set[tuple[int, int]] = set()
    out: list[Doorway] = []
    for y in range(h):
        for x in range(w):
            if rows[y][x] == "+" and (x, y) not in found:
                horizontal = wall(x - 1, y) or wall(x + 1, y)
                found.add((x, y))
                out.append(Doorway(x, y, x, y, "h" if horizontal else "v"))
    for y in range(h):
        x = 0
        while x < w:
            if wall(x, y) and open_(x + 1, y):
                end = x + 1
                while end < w and open_(end, y) and end - x <= widest:
                    end += 1
                if wall(end, y) and end - x - 1 <= widest and wall(x - 1, y) and wall(end + 1, y):
                    run = [(cx, y) for cx in range(x + 1, end)]
                    if all(side(cx, y - 1) and side(cx, y + 1) for cx, _ in run) and not any(
                        c in found for c in run
                    ):
                        found.update(run)
                        out.append(Doorway(x + 1, y, end - 1, y, "h"))
                x = end if end > x else x + 1
            else:
                x += 1
    for x in range(w):
        y = 0
        while y < h:
            if wall(x, y) and open_(x, y + 1):
                end = y + 1
                while end < h and open_(x, end) and end - y <= widest:
                    end += 1
                if wall(x, end) and end - y - 1 <= widest and wall(x, y - 1) and wall(x, end + 1):
                    run = [(x, cy) for cy in range(y + 1, end)]
                    if all(side(x - 1, cy) and side(x + 1, cy) for _, cy in run) and not any(
                        c in found for c in run
                    ):
                        found.update(run)
                        out.append(Doorway(x, y + 1, x, end - 1, "v"))
                y = end if end > y else y + 1
            else:
                y += 1
    return out


_COLS = ("left", "middle", "right")
_ROWS = ("top", "middle", "bottom")


def _ninth(cx: float, cy: float, w: int, h: int) -> str:
    col, row = _COLS[min(int(cx / max(w, 1) * 3), 2)], _ROWS[min(int(cy / max(h, 1) * 3), 2)]
    if row == "middle" and col == "middle":
        return "centre"
    if row == "middle":
        return f"middle {col}"
    if col == "middle":
        return f"{row} middle"
    return f"{row}-{col}"


def find_rooms(
    rows: Sequence[str], objects: Mapping[str, str], doorways: Sequence[Doorway]
) -> tuple[list[Room], list[list[int]]]:
    """An indoor area's rooms: each run of open tiles a doorway or a wall
    bounds, six tiles or more, largest first, named by where it sits ("the
    top-left room"; a space running most of the way across is the hall the
    rooms open off). The furnishing's purpose names them better."""
    h, w = len(rows), len(rows[0]) if rows else 0
    door_cells = {t for d in doorways for t in d.tiles()}
    open_ = [
        [rows[y][x] not in "#W +" and (x, y) not in door_cells for x in range(w)] for y in range(h)
    ]
    label = [[-1] * w for _ in range(h)]
    seen = [[False] * w for _ in range(h)]
    regions: list[list[tuple[int, int]]] = []
    for sy in range(h):
        for sx in range(w):
            if not open_[sy][sx] or seen[sy][sx]:
                continue
            stack, cells = [(sx, sy)], []
            seen[sy][sx] = True
            while stack:
                cx, cy = stack.pop()
                cells.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h and open_[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            if len(cells) >= 6:
                regions.append(cells)
    rooms: list[Room] = []
    for cells in sorted(regions, key=len, reverse=True):
        index = len(rooms)
        for cx, cy in cells:
            label[cy][cx] = index
        xs, ys = [c[0] for c in cells], [c[1] for c in cells]
        box = (min(xs), min(ys), max(xs), max(ys))
        inside = sorted(
            {objects.get(rows[y][x], "") for x, y in cells if rows[y][x].isdigit()} - {""}
        )
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        rooms.append(Room(index, "", len(cells), box, cx, cy, inside))
    for room in rooms:
        x0, y0, x1, y1 = room.box
        if (x1 - x0 + 1) > 0.6 * w or (y1 - y0 + 1) > 0.8 * h:
            room.name = "central hall" if len(rooms) > 1 else "hall"
        else:
            room.name = f"{_ninth(room.cx, room.cy, w, h)} room"
    groups: dict[str, list[Room]] = {}
    for room in rooms:
        groups.setdefault(room.name, []).append(room)
    for same in groups.values():
        if len(same) > 1:
            same.sort(key=lambda r: -r.cells)
            for k, room in enumerate(same):
                room.name = ("larger " if k == 0 else "smaller ") + room.name
    return rooms, label


def name_rooms(
    rooms: Sequence[Room], labels: Mapping[str, tuple[int, int]], records: Any = None
) -> None:
    """Each room's name and use: from the plan's own room records (a deck the
    program laid out: name, use and box), else from a label standing in it
    (a drawn plan's named building or room) read for its use. The space the
    rooms open off is the corridor."""
    by_box = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []
    for room in rooms:
        cx, cy = int(round(room.cx)), int(round(room.cy))
        found = next(
            (
                r
                for r in by_box
                if len(r.get("box") or ()) == 4
                and r["box"][0] <= cx <= r["box"][2]
                and r["box"][1] <= cy <= r["box"][3]
            ),
            None,
        )
        if found is not None:
            room.name = str(found.get("name") or room.name)[:60]
            room.use = str(found.get("use") or "")
            continue
        if by_box:
            # The records name every room of a deck the program laid out:
            # the rest is corridor. (A ring's corridor runs in L-shaped
            # stretches; read by the one label in its box, one took the
            # customs post's use and was drawn red.)
            room.use = "corridor"
            continue
        x0, y0, x1, y1 = room.box
        inside = [n for n, (lx, ly) in labels.items() if x0 <= lx <= x1 and y0 <= ly <= y1]
        if len(inside) == 1:
            room.use = use_of(inside[0])
        if room.name in ("central hall", "hall"):
            room.use = room.use or "corridor"


def _outside_cells(rows: list[str]) -> set[tuple[int, int]]:
    """What lies outside an indoor plan's walls: reached from the grid's
    edge over ground that isn't floor, a wall, a door or a thing (a gap in
    the outer wall is floor, so the outside never runs in through it)."""
    h, w = len(rows), len(rows[0]) if rows else 0

    def outdoors(x: int, y: int) -> bool:
        ch = rows[y][x]
        return ch not in "#W+_&" and not ch.isdigit()

    todo = [
        (x, y)
        for y in range(h)
        for x in range(w)
        if (x in (0, w - 1) or y in (0, h - 1)) and outdoors(x, y)
    ]
    seen: set[tuple[int, int]] = set()
    while todo:
        x, y = todo.pop()
        if (x, y) in seen:
            continue
        seen.add((x, y))
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen and outdoors(nx, ny):
                todo.append((nx, ny))
    return seen


def _exits(rows: list[str]) -> list[tuple[int, int]]:
    """An indoor plan's ways out: a door beside what lies outside its walls
    or on the grid's edge, and floor running out to the edge (the sample
    farmhouse's way out is a gap in its north wall)."""
    h, w = len(rows), len(rows[0]) if rows else 0
    outside = _outside_cells(rows)
    out = []
    for y in range(h):
        for x in range(w):
            ch = rows[y][x]
            edge = x in (0, w - 1) or y in (0, h - 1)
            if ch == "+":
                near = [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
                if edge or any(n in outside for n in near):
                    out.append((x, y))
            elif edge and ch in WALKABLE:
                out.append((x, y))  # floor, or rubble where a wall fell
    return out


def _flood(rows: list[str], starts: Sequence[tuple[int, int]], passable) -> set[tuple[int, int]]:
    h, w = len(rows), len(rows[0]) if rows else 0
    seen: set[tuple[int, int]] = set()
    todo = [s for s in starts if passable(*s)]
    while todo:
        x, y = todo.pop()
        if (x, y) in seen:
            continue
        seen.add((x, y))
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen and passable(nx, ny):
                todo.append((nx, ny))
    return seen


def _open_sealed(rows: list[str], exits: list[tuple[int, int]], repairs: list[str]) -> list[str]:
    """Open a doorway into every walled-off part of an indoor plan (the
    rule: every room can be reached). A wall one tile thick between the
    sealed part and the reachable part becomes a door, the middle of the
    longest such stretch; repeated until nothing large is sealed."""
    grid = [list(r) for r in rows]
    h, w = len(grid), len(grid[0]) if grid else 0
    outside = _outside_cells(rows)

    def floor(x: int, y: int) -> bool:
        return grid[y][x] in WALKABLE or grid[y][x].isdigit()

    for _ in range(12):
        reached = _flood(
            ["".join(r) for r in grid],
            [(x, y) for x, y in exits]
            or [(x, y) for y in range(h) for x in range(w) if floor(x, y)][:1],
            lambda x, y: (floor(x, y)) and (x, y) not in outside,
        )
        sealed = [
            (x, y)
            for y in range(h)
            for x in range(w)
            if floor(x, y) and (x, y) not in reached and (x, y) not in outside
        ]
        if len(sealed) < 6:
            break
        sealed_set = set(sealed)
        candidates: list[tuple[int, int, str]] = []
        for y in range(1, h - 1):
            for x in range(1, w - 1):
                if grid[y][x] != "#":
                    continue
                if (x - 1, y) in sealed_set and (x + 1, y) in reached:
                    candidates.append((x, y, "v"))
                elif (x + 1, y) in sealed_set and (x - 1, y) in reached:
                    candidates.append((x, y, "v"))
                elif (x, y - 1) in sealed_set and (x, y + 1) in reached:
                    candidates.append((x, y, "h"))
                elif (x, y + 1) in sealed_set and (x, y - 1) in reached:
                    candidates.append((x, y, "h"))
        if not candidates:
            break
        candidates.sort(key=lambda c: (c[1], c[0]))
        x, y, _way = candidates[len(candidates) // 2]
        grid[y][x] = "+"
        repairs.append(f"opened a door at {x},{y} into a sealed room")
    return ["".join(r) for r in grid]


# What a door may open onto: open ground, never water, canopy or a wall.
OPEN_GROUND_TILES = frozenset(".,:=s_")
# A room the story keeps shut gets no door cut for it.
_SEALED = re.compile(
    r"\b(locked|sealed|bricked up|walled up|no way out|cannot leave|can't leave)\b", re.I
)


# A state that leaves a building without its roof, whatever its plan's walls:
# the model draws a burnt barn's outer walls whole and its fire inside.
_ROOFLESS = re.compile(
    r"\b(burn(?:s|t|ed|ing)?|on fire|ablaze|aflame|blaze|gutted|collaps(?:e|ed|ing)|"
    r"caved?|fallen|destroy(?:ed)?|ruin(?:s|ed)?|razed|blown|explod(?:e|ed)|rubble|"
    r"roofless|no roof)\b",
    re.I,
)
# The share of a state plan's inside under fire or rubble that leaves no roof.
ROOFLESS_SHARE = 0.1


def _roofless(rows: list[str], state: str) -> bool:
    """Whether a changed building is drawn open on the yard: its state says
    it burnt or fell, or its plan shows enough fire and rubble inside."""
    if _ROOFLESS.search(state or ""):
        return True
    inside = [t for row in rows[1:-1] for t in row[1:-1]]
    ruined = sum(1 for t in inside if t in "*:")
    return bool(inside) and ruined / len(inside) >= ROOFLESS_SHARE


def _roofs(world: WorldMap, buildings: Mapping[str, Any]) -> dict[str, str]:
    """Each building's roof material from its own words, where they name one."""
    out = {}
    for pid in buildings:
        place = world.places.get(pid)
        if place is not None and (found := roof_material(f"{place.name} {place.what}")):
            out[pid] = found
    return out


def _stamp_state(
    grid: list[list[str]], box: tuple[int, int, int, int], rows: list[str], state: str = ""
) -> None:
    """A changed building's box drawn from the plan of its state: the ring
    is wall where that plan's outer wall stands, rubble where it fell, a
    door where it has one; the inside shows fire, rubble and water.

    Each yard tile reads its whole share of the plan, not one point of it:
    a burning barn's 40x24 plan sampled into a few tiles missed its fires
    and kept its walls, and the barn stood roofed on the yard (live, Oct 17
    2026). A building left without a roof (`_roofless`) keeps at most half
    its ring standing, so it is drawn as a shell, never roofed."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0 + 1, y1 - y0 + 1
    pw, ph = len(rows[0]) if rows else 0, len(rows)
    if w <= 0 or h <= 0 or pw == 0 or ph == 0:
        return
    open_top = _roofless(rows, state)

    def share(x: int, y: int) -> list[str]:
        """The plan's tiles under yard tile (x, y), the ring's on the ring."""
        ax = int((x - x0) * pw / w)
        bx = max(ax + 1, int((x - x0 + 1) * pw / w))
        ay = int((y - y0) * ph / h)
        by = max(ay + 1, int((y - y0 + 1) * ph / h))
        if x in (x0, x1):
            ax, bx = (0, 1) if x == x0 else (pw - 1, pw)
        if y in (y0, y1):
            ay, by = (0, 1) if y == y0 else (ph - 1, ph)
        return [rows[py][px] for py in range(ay, min(by, ph)) for px in range(ax, min(bx, pw))]

    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if not (0 <= y < len(grid) and 0 <= x < len(grid[0])):
                continue
            tiles = share(x, y)
            ring = x in (x0, x1) or y in (y0, y1)
            if ring:
                # Gone in pairs all round: a broken shell, under the share
                # of standing wall the renderer roofs (gui/map_tiles.py).
                fallen = open_top and ((x + y) // 2) % 2 == 1
                if "+" in tiles:
                    grid[y][x] = "+"
                elif any(t in WALLS for t in tiles) and not fallen:
                    grid[y][x] = "#"
                elif "*" in tiles:
                    grid[y][x] = "*"
                else:
                    grid[y][x] = ":"
            elif "*" in tiles:
                grid[y][x] = "*"
            elif "~" in tiles:
                grid[y][x] = "~"
            elif open_top or ":" in tiles:
                grid[y][x] = ":"
            else:
                grid[y][x] = " "


def _door_for(grid: list[list[str]], box: tuple[int, int, int, int]) -> tuple[int, int] | None:
    """Where a map building with no door gets one: the middle of the first
    side (south, north, east, west) with open ground in front of it, else
    the south side with its front cleared (a door opened onto a pond the
    plan drew, and onto the grid's edge: the second review)."""
    x0, y0, x1, y1 = box
    h, w = len(grid), len(grid[0]) if grid else 0
    sides = [
        ((x0 + x1) // 2, y1, (x0 + x1) // 2, y1 + 1),
        ((x0 + x1) // 2, y0, (x0 + x1) // 2, y0 - 1),
        (x1, (y0 + y1) // 2, x1 + 1, (y0 + y1) // 2),
        (x0, (y0 + y1) // 2, x0 - 1, (y0 + y1) // 2),
    ]
    sides = [(x, y, fx, fy) for x, y, fx, fy in sides if 0 <= x < w and 0 <= y < h]
    for x, y, fx, fy in sides:
        if 0 <= fx < w and 0 <= fy < h and grid[fy][fx] in OPEN_GROUND_TILES:
            return x, y
    for x, y, fx, fy in sides:
        if 0 <= fx < w and 0 <= fy < h and grid[fy][fx] not in WALLS:
            grid[fy][fx] = "."
            return x, y
    return (sides[0][0], sides[0][1]) if sides else None


def _door_side(box: tuple[int, int, int, int], x: int, y: int) -> str:
    x0, y0, x1, y1 = box
    if y == y1:
        return "south"
    if y == y0:
        return "north"
    return "west" if x == x0 else "east"


def build_area(
    world: WorldMap,
    art: WorldArt,
    place_id: str,
    *,
    density: str = "normal",
    people: Collection[str] = (),
) -> Area | None:
    """The area of a place with a plan, built from it; None without one.
    `people` are names in the story: a plan's object named for one ("Marcus
    Webb" in the diner) is a person, not a thing."""
    place = world.places.get(place_id)
    if place is None:
        return None
    layout = art.layout_shown(place.id, place.state)
    if layout is None:
        return None
    spec = layout.spec if isinstance(layout.spec, dict) else {}
    look = ground_of(spec)
    if look != "space" and land_under(world, art, place) == "space":
        look = "space"  # a deck: its own word is "floor", the void around it space
    indoor = place.kind in ("building", "room", "vehicle") or look == "space"
    repairs: list[str] = []
    entrances: list[Entrance] = []
    buildings: dict[str, tuple[int, int, int, int]] = {}
    people_keys = {key(n) for n in people}
    links: LevelLinks | None = None
    generated = False
    if indoor:
        # The plan's own level, with the stairs the rules put in between it
        # and its other levels (engine/levels.py); a plan with none is as drawn.
        links = linked_levels(layout) if layout.levels else None
        rows = list(links.rows[0]) if links is not None else list(layout.rows)
        if links is not None:
            repairs += links.repairs
        generated = bool(spec.get("deck"))
        if not generated and (look == "space" or place.kind not in ("building", "room")):
            rows = list(decked(rows))  # a deck laid out by the program has its courtyards
        objects = dict(layout.objects)
        rows = _doors_in_walls(rows, objects)
        labels = {k: (int(v[0]), int(v[1])) for k, v in layout.labels.items() if len(v) >= 2}
        # The map's own scale (the plan's rectangle over its tiles): the
        # positions, earshot, walking and the marks used to disagree by up
        # to 25x on a deck (the second review). A deck laid out by the
        # program is at its own scale, a stride or so a tile.
        tile_m = float(spec.get("tile_m") or 0) or plan_tile(place)
        # A plan drawn for the place's state shows what the story did to it
        # (walls gone, a breach): the wall rules would undo it. A room the
        # story sealed keeps no door either.
        as_drawn = bool(place.state) and layout.state == place.state
        sealed = _SEALED.search(f"{place.what} {place.state}") is not None
        if place.kind in ("building", "room") and not as_drawn:
            rows = _whole_outer_wall(rows, repairs)
        exits = _exits(rows)
        if not exits and place.kind in ("building", "room") and not (as_drawn or sealed):
            rows, exits = _add_exit(rows, repairs)
        rows = _open_sealed(rows, exits, repairs)
        exits = _exits(rows)
    else:
        columns = len(layout.rows[0]) if layout.rows else 40
        grid_rows = len(layout.rows) or 24
        k = _scale_for(place, columns)
        fixed = fixed_in_plan(world, place)
        merged = with_fixed(spec, fixed) if spec.get("features") else spec
        big = scaled(merged, k)
        roads = [
            f for f in big.get("features", []) if isinstance(f, dict) and f.get("kind") == "road"
        ]
        rest = {
            **big,
            "features": [
                f
                for f in big.get("features", [])
                if not (isinstance(f, dict) and f.get("kind") == "road")
            ],
        }
        if big.get("features"):
            rows, objects, raw_labels = rasterise(rest, columns * k, grid_rows * k)
            rows = _roads_on_open_ground(rows, roads)
            rows = _bridge_parts(rows, objects)
            rows = _doors_in_walls(rows, objects)
        else:  # a plan with no shapes kept: its tiles, each k times over
            rows = ["".join(ch * k for ch in row) for row in layout.rows for _ in range(k)]
            objects, raw_labels = (
                dict(layout.objects),
                {n: [v[0] * k + k // 2, v[1] * k + k // 2] for n, v in layout.labels.items()},
            )
            # The map's buildings stamped on where it has them, so they get
            # doors and lead inside (without shapes `with_fixed` had nothing
            # to draw them into, and a building could neither be entered nor
            # left but at the area's middle; the review, Oct 16 2026).
            rows, big = _stamp_fixed(rows, fixed, k)
        labels = {n: (int(v[0]), int(v[1])) for n, v in raw_labels.items() if len(v) >= 2}
        for f in fixed:  # what the map has here that isn't a building: labelled where it is
            if f["kind"] != "building":
                labels[f["name"][:60]] = ((f["x"] + f["w"] // 2) * k, (f["y"] + f["h"] // 2) * k)
        rows = dress(rows, look, f"{place.id}:{place.state}")
        # True to the map: the plan's tile over the scale (~1.5 m, up to
        # 3 m where the scale is capped), so walking takes as long as the
        # map is wide.
        tile_m = plan_tile(place) / k
        exits = []
        rows, entrances, buildings = _fix_buildings(world, art, rows, big, fixed, repairs)
    labels = _inside_labels(world, place, labels)
    # Objects named for people are people.
    for digit, name in list(objects.items()):
        if key(name) in people_keys:
            objects.pop(digit)
            rows = [r.replace(digit, "_" if indoor else ".") for r in rows]
    doorways = openings(rows)
    rooms, room_of = find_rooms(rows, objects, doorways) if indoor else ([], [])
    if indoor:
        name_rooms(rooms, labels, spec.get("rooms"))
    if links is not None:
        entrances += _stairs_entrances(links, 0, place.id)
    area = Area(
        place_id=place.id,
        name=place.name,
        indoor=indoor,
        look=look or ("floor" if indoor else "grass"),
        rows=rows,
        tile_m=tile_m,
        objects=objects,
        labels=labels,
        rooms=rooms,
        room_of=room_of,
        doorways=doorways,
        entrances=entrances,
        buildings=buildings,
        names={pid: world.places[pid].name for pid in buildings if pid in world.places},
        roofs=_roofs(world, buildings),
        exits=exits,
        parent=area_of(world, art, place.inside) if place.inside else None,
        repairs=repairs,
    )
    area.placements = _object_placements(area)
    if indoor and generated:
        area.hull = _hull_rows(spec.get("hull"), rows)
        area.placements += deck_pieces(area, spec.get("rooms"))
    if indoor and rooms:
        plan = art.furnishing_for(place.id, place.state)
        if plan is not None:
            for room in rooms:
                purpose = next((r.purpose for r in plan.rooms if r.room == room.index), "")
                if purpose:
                    room.purpose = " ".join(purpose.split())[:60]
                    room.use = room.use or use_of(room.purpose)
            area.placements += furnish(area, plan, density)
    return area


# --- A plan's other levels (engine/levels.py) -------------------------------------


def _stairs_entrances(links: LevelLinks, index: int, place_id: str) -> list[Entrance]:
    """Each stairs tile of level `index` that leads somewhere, as an
    entrance to that level's area."""
    return [
        Entrance(x, y, level_area_id(place_id, to), "up" if to > index else "down")
        for (x, y), to in sorted(links.links.get(index, {}).items())
    ]


def _inside_labels(
    world: WorldMap, place: WorldPlace, labels: dict[str, tuple[int, int]]
) -> dict[str, tuple[int, int]]:
    """A building's labels without its porch, steps or yard: a read put them
    inside it, and a plan drawn then labelled them in its middle (AfterDark,
    Oct 18 2026: "The porch overhang" between the farmhouse's two halls).
    They are outside it (`world_art.outside_part`)."""
    outside = {
        key(p.name)
        for p in world.places.values()
        if p.inside == place.id and outside_part(world, p)
    }
    if not outside:
        return labels
    return {name: spot for name, spot in labels.items() if key(name) not in outside}


def build_level(
    world: WorldMap,
    art: WorldArt,
    place_id: str,
    index: int,
    *,
    density: str = "normal",
    people: Collection[str] = (),
) -> Area | None:
    """The area of one further level of a place's plan (an upper floor, a
    cellar, another deck), built as a building's inside is: on the plan's
    grid, its stairs the doors to the levels they reach (`Entrance` with
    side "up" or "down"), no door out of it but those. None without the
    plan or the level drawn. Furnished under its own area id
    (`WorldArt.furnishings["<place>#<n>"]`)."""
    place = world.places.get(place_id)
    if place is None or not index:
        return None
    layout = art.layout_shown(place.id, place.state)
    if layout is None or layout.level(index) is None:
        return None
    drawn = layout.level(index)
    links = linked_levels(layout)
    rows = list(links.rows.get(index) or [])
    if not rows:
        return None
    spec = layout.spec if isinstance(layout.spec, dict) else {}
    look = ground_of(spec)
    if look != "space" and land_under(world, art, place) == "space":
        look = "space"
    repairs: list[str] = list(links.repairs)
    generated = bool(spec.get("deck"))
    if not generated and (look == "space" or place.kind not in ("building", "room")):
        rows = list(decked(rows))
    as_drawn = bool(place.state) and layout.state == place.state
    if place.kind in ("building", "room") and not as_drawn:
        rows = _whole_outer_wall(rows, repairs)
    stairs = [t for t in links.links.get(index, {})]
    rows = _open_sealed(rows, stairs, repairs)
    objects = dict(drawn.objects)  # type: ignore[union-attr]
    rows = _doors_in_walls(rows, objects)
    labels = {k: (int(v[0]), int(v[1])) for k, v in drawn.labels.items() if len(v) >= 2}  # type: ignore[union-attr]
    labels = _inside_labels(world, place, labels)
    people_keys = {key(n) for n in people}
    for digit, name in list(objects.items()):
        if key(name) in people_keys:
            objects.pop(digit)
            rows = [r.replace(digit, "_") for r in rows]
    doorways = openings(rows)
    rooms, room_of = find_rooms(rows, objects, doorways)
    name_rooms(rooms, labels, (drawn.spec or {}).get("rooms"))  # type: ignore[union-attr]
    deck = look == "space"
    area = Area(
        place_id=place.id,
        name=place.name,
        indoor=True,
        look=look or "floor",
        rows=rows,
        tile_m=float(spec.get("tile_m") or 0) or plan_tile(place),
        objects=objects,
        labels=labels,
        rooms=rooms,
        room_of=room_of,
        doorways=doorways,
        entrances=_stairs_entrances(links, index, place.id),
        exits=[],
        # The way out of a level is down (or up) to the plan's own level.
        parent=place.id,
        repairs=repairs,
        level=index,
        level_name=level_name(layout, index, deck=deck),
    )
    area.placements = _object_placements(area)
    if generated:
        drawn_spec = drawn.spec or {}  # type: ignore[union-attr]
        area.hull = _hull_rows(drawn_spec.get("hull"), rows)
        area.placements += deck_pieces(area, drawn_spec.get("rooms"))
    if rooms:
        plan = art.furnishing_for(area.id, place.state)
        if plan is not None:
            for room in rooms:
                purpose = next((r.purpose for r in plan.rooms if r.room == room.index), "")
                if purpose:
                    room.purpose = " ".join(purpose.split())[:60]
                    room.use = room.use or use_of(room.purpose)
            area.placements += furnish(area, plan, density)
    return area


def stairs_landing(area: Area, tile: tuple[int, int], taken: Collection[tuple[int, int]] = ()):
    """Where someone arriving by the stairs at `tile` stands: a step off
    them, never on a stairs tile (they would be in the doorway again)."""
    off = set(taken) | {(e.x, e.y) for e in area.stairs_entrances()} | {tile}
    return stand_near(area, tile, off)


def level_of_area(area_id: str | None) -> tuple[str, int] | None:
    """(place id, level) when an area id names a level of a plan."""
    return parse_level(area_id)


def _fix_buildings(
    world: WorldMap,
    art: WorldArt,
    rows: list[str],
    big: dict[str, Any],
    fixed: Sequence[dict[str, Any]],
    repairs: list[str],
) -> tuple[list[str], list[Entrance], dict[str, tuple[int, int, int, int]]]:
    """The map's buildings in an outdoor grid, by the rules: each has a
    door, a changed one shows its state, and each with an inside of its own
    leads into it. (rows, the entrances, each building's box by place id)."""
    entrances: list[Entrance] = []
    buildings: dict[str, tuple[int, int, int, int]] = {}
    by_name = {key(f["name"]): f for f in fixed if f["kind"] == "building"}
    grid = [list(r) for r in rows]
    for f in big.get("features", []):
        if not isinstance(f, dict) or f.get("kind") != "building":
            continue
        fixed_one = by_name.get(key(str(f.get("name") or "")))
        if fixed_one is None:
            continue
        x0, y0 = int(f["x"]), int(f["y"])
        x1, y1 = x0 + int(f["w"]) - 1, y0 + int(f["h"]) - 1
        box = (x0, y0, x1, y1)
        child = world.places.get(fixed_one["place_id"])
        if child is None:
            continue
        buildings[child.id] = box
        state_plan = art.layout_for(child.id, child.state) if child.state else None
        if state_plan is not None and state_plan.rows:
            # Blown up, burnt: its box as its own plan has it, rubble
            # where the walls fell (and so no roof), fire inside, instead
            # of a whole roofed building (the second review).
            _stamp_state(grid, box, state_plan.rows, child.state)
        doors = [
            (x, y)
            for y in range(max(y0, 0), min(y1, len(grid) - 1) + 1)
            for x in range(max(x0, 0), min(x1, len(grid[0]) - 1) + 1)
            if grid[y][x] == "+" and (x in (x0, x1) or y in (y0, y1))
        ]
        if not doors:
            door = _door_for(grid, box)
            if door is not None:
                doors = [door]
                grid[door[1]][door[0]] = "+"
                repairs.append(f"gave {child.name} a door")
        for x, y in doors:
            # Into the building's inside, or, on a window of open country,
            # into a place with a plan of its own (`build_window` boxes it).
            if has_inside(art, child) or plan_place(world, art, child.id) is child:
                entrances.append(Entrance(x, y, child.id, _door_side(box, x, y)))
    # A building's inside is its own area, under its roof: drawn as floor
    # here, anyone stood "at" it was stood under the roof and could walk
    # in without its door (the second review). Blank: no walking, no sight.
    for f in big.get("features", []):
        if not isinstance(f, dict) or f.get("kind") != "building" or f.get("yard"):
            continue  # a yard's grounds stay pavement, walked on
        x0, y0 = int(f["x"]), int(f["y"])
        x1, y1 = x0 + int(f["w"]) - 1, y0 + int(f["h"]) - 1
        for y in range(y0 + 1, y1):
            for x in range(x0 + 1, x1):
                if 0 <= y < len(grid) and 0 <= x < len(grid[0]) and grid[y][x] == "_":
                    grid[y][x] = " "
    return ["".join(r) for r in grid], entrances, buildings


# A place boxed on a window of open country is at least this many tiles
# across, as a building fixed in a plan is (`fixed_in_plan`): a ring, a door
# and an inside need the room. These kinds are boxed (each gets a plan of
# its own once the story goes there); the rest are labelled.
WINDOW_BOX_TILES = 4
WINDOW_BOXED = frozenset({"building", "settlement", "site", "vehicle"})


def window_places(
    world: WorldMap, art: WorldArt, rect: tuple[float, float, float, float], tile: float
) -> list[dict[str, Any]]:
    """What the map has on a window of open country, as `fixed_in_plan`
    gives a plan's: the outermost places whose footprint reaches into the
    window (not those inside one of them: a farm's barn is reached from the
    farm's own plan; not a place wider than the window, which holds it),
    each as a tile box, cut at the window's edge where it runs past (a town
    lies across several windows, a part in each). A building, or a place
    with a plan of its own, is a "building" box with a door that leads into
    its area; the rest are labelled where they are."""
    west, north, width, height = rect
    candidates: dict[str, tuple[float, float, float, float]] = {}
    for place in world.places.values():
        if place.kind in ("region", "route") or size_of(place) > width:
            continue
        box = footprint(world, art, place.id)
        if box is None:
            continue
        w0, s0, e0, n0 = box
        if e0 <= west or w0 >= west + width or s0 >= north or n0 <= north - height:
            continue
        candidates[place.id] = box
    out: list[dict[str, Any]] = []
    columns, rows = round(width / tile), round(height / tile)
    for pid, (w0, s0, e0, n0) in candidates.items():
        place = world.places[pid]
        inner = False
        seen: set[str] = set()
        current = world.places.get(place.inside or "")
        while current is not None and current.id not in seen:
            seen.add(current.id)
            if current.id in candidates:
                inner = True
                break
            current = world.places.get(current.inside or "")
        if inner:
            continue
        x0, y0 = math.floor((w0 - west) / tile), math.floor((north - n0) / tile)
        x1, y1 = math.ceil((e0 - west) / tile) - 1, math.ceil((north - s0) / tile) - 1
        w = max(WINDOW_BOX_TILES, x1 - x0 + 1)
        h = max(3, y1 - y0 + 1)
        x0 -= (w - (x1 - x0 + 1)) // 2
        y0 -= (h - (y1 - y0 + 1)) // 2
        # Cut at the window's edge: the part of it that lies here.
        x1, y1 = min(x0 + w - 1, columns - 1), min(y0 + h - 1, rows - 1)
        x0, y0 = max(x0, 0), max(y0, 0)
        w, h = x1 - x0 + 1, y1 - y0 + 1
        if w < 2 or h < 2:
            continue
        # A place that is, or gets, a plan of its own is a box to enter by
        # its door; a pond or a clearing is a label where it is.
        boxed = place.kind in WINDOW_BOXED or plan_place(world, art, pid) is place
        out.append(
            {
                "kind": "building" if boxed else "place",
                # A farm, a village or a wreck on the land is its grounds, not
                # one roofed building: fenced, with a gate, paved inside (a
                # 400 m farm drew as one barn with a shingled roof).
                "yard": boxed and place.kind not in ("building", "vehicle"),
                "name": place.name,
                "place_id": pid,
                "place_kind": place.kind or "place",
                "x": x0,
                "y": y0,
                "w": w,
                "h": h,
                "what": place.what,
            }
        )
    return out


def _resampled(
    patch_rows: Sequence[str],
    source: tuple[float, float, float],
    rect: tuple[float, float, float, float],
    columns: int,
    rows: int,
) -> list[str]:
    """The land's tiles under each tile of a window lying within a coarser
    patch: nearest-neighbour, so the window keeps the map's own ground."""
    sx, sy, side = source
    west, north, width, height = rect
    ph = len(patch_rows)
    pw = len(patch_rows[0]) if ph else 0
    if not pw:
        return ["." * columns for _ in range(rows)]
    out = []
    for r in range(rows):
        my = north - (r + 0.5) * height / rows
        py = min(ph - 1, max(0, int((sy + side / 2 - my) / side * ph)))
        line = []
        for c in range(columns):
            mx = west + (c + 0.5) * width / columns
            px = min(pw - 1, max(0, int((mx - (sx - side / 2)) / side * pw)))
            line.append(patch_rows[py][px] if px < len(patch_rows[py]) else ".")
        out.append("".join(line))
    return out


def build_window(
    world: WorldMap,
    art: WorldArt,
    area_id: str,
    *,
    people: Collection[str] = (),
) -> Area | None:
    """The area of a window of open country (`area_of` names one by its
    rectangle): the land patch drawn there, its rows at its own tile (side
    over columns: 40 m for a 1.6 km patch, a long stride on the clock, and
    honest), the map's places in it boxed with doors (`window_places`), the
    land's trees, water and rock as its rows have them. No patch there yet:
    the coarser land under it resampled, else plain ground of the land
    drawn nearest (or dirt), so there is always ground to stand on while
    the patch is drawn (`world_art.plan_art`). The land's tiles are the
    plan's own legend (`world_art.TILES`: every ground and fill the land
    model may use is a plan tile too), so the view draws a window as it
    draws any outdoor plan. None for an id that names no window."""
    parsed = parse_window(area_id)
    if parsed is None:
        return None
    west, north, side = parsed
    rect = (west, north, side, side)
    patch = patch_at(art, west, north, side)
    repairs: list[str] = []
    if patch is not None and patch.rows:
        rows = [r.ljust(len(patch.rows[0])) for r in patch.rows]
        ground = patch.ground
    else:
        columns = rows_count = TERRAIN_TILES
        under = patch_under(art, west + side / 2, north - side / 2)
        if under is not None and under.rows:
            rows = _resampled(under.rows, (under.x, under.y, under.side), rect, columns, rows_count)
            ground = under.ground
        else:
            drawn = [p for p in art.terrain if p.ground]
            nearest = (
                min(
                    drawn,
                    key=lambda p: math.hypot(p.x - (west + side / 2), p.y - (north - side / 2)),
                )
                if drawn
                else None
            )
            ground = nearest.ground if nearest is not None else "dirt"
            rows = [GROUND.get(ground, ".") * columns for _ in range(rows_count)]
    rows = [
        "".join(ch if ch in TILES else "." for ch in row) for row in rows
    ]  # the legend, whatever the record holds
    columns, rows_count = len(rows[0]), len(rows)
    tile = side / columns
    fixed = window_places(world, art, rect, tile)
    rows, big = _stamp_fixed(rows, fixed, 1)
    labels: dict[str, tuple[int, int]] = {}
    for f in fixed:
        labels[f["name"][:60]] = (f["x"] + f["w"] // 2, f["y"] + f["h"] // 2)
    rows, entrances, buildings = _fix_buildings(world, art, rows, big, fixed, repairs)
    objects: dict[str, str] = {}
    holder = holding_place(world, west, north, side)
    area = Area(
        place_id=holder.id if holder is not None else "",
        name="open country",
        indoor=False,
        look=ground or "dirt",
        rows=rows,
        tile_m=tile,
        objects=objects,
        labels=labels,
        doorways=openings(rows),
        entrances=entrances,
        buildings=buildings,
        names={pid: world.places[pid].name for pid in buildings if pid in world.places},
        roofs=_roofs(world, buildings),
        exits=[],
        parent=None,
        repairs=repairs,
        rect=rect,
        patch_id=patch.id if patch is not None else "",
        within=holder.name if holder is not None else "",
    )
    area.placements = _object_placements(area)
    return area


def _stamp_fixed(
    rows: list[str], fixed: Sequence[dict[str, Any]], k: int
) -> tuple[list[str], dict[str, Any]]:
    """The fixed buildings as walls on a rows-only plan, `k` times scaled,
    and as features for the door and entrance pass to find."""
    grid = [list(r) for r in rows]
    features = []
    for f in fixed:
        if f.get("kind") != "building":
            continue
        x0, y0 = int(f["x"]) * k, int(f["y"]) * k
        w, h = int(f["w"]) * k, int(f["h"]) * k
        yard = bool(f.get("yard"))
        for y in range(y0, y0 + h):
            for x in range(x0, x0 + w):
                if 0 <= y < len(grid) and 0 <= x < len(grid[0]):
                    edge = y in (y0, y0 + h - 1) or x in (x0, x0 + w - 1)
                    # A yard (a place's grounds on a window of open country)
                    # is a fence round pavement, not a wall: the renderer
                    # roofs only walled rings.
                    grid[y][x] = ("%" if yard else "#") if edge else "_"
        features.append(
            {"kind": "building", "name": f["name"], "x": x0, "y": y0, "w": w, "h": h, "yard": yard}
        )
    return ["".join(r) for r in grid], {"features": features}


# A gap in a building's outer wall wider than this is a wall the plan lost
# (the sample bone kennels came without their south wall): all but a
# doorway's width of it is walled.
WIDEST_GAP = 8
DOORWAY = 3


def _whole_outer_wall(rows: list[str], repairs: list[str]) -> list[str]:
    """A building's outer wall kept whole: along each edge of its plan, the
    floor running out to the edge is a doorway; one tile of it alone, or
    more than WIDEST_GAP, is wall the plan lost, walled but for DOORWAY
    tiles in the middle of the longest run."""
    grid = [list(r) for r in rows]
    h, w = len(grid), len(grid[0]) if grid else 0
    edges = [
        [(x, 0) for x in range(w)],
        [(x, h - 1) for x in range(w)],
        [(0, y) for y in range(h)],
        [(w - 1, y) for y in range(h)],
    ]
    for edge in edges:
        runs: list[list[tuple[int, int]]] = []
        for x, y in edge:
            if grid[y][x] in WALKABLE and grid[y][x] != "+":
                if runs and runs[-1][-1] in ((x - 1, y), (x, y - 1)):
                    runs[-1].append((x, y))
                else:
                    runs.append([(x, y)])
        for run in runs:
            if 2 <= len(run) <= WIDEST_GAP:
                continue
            keep = set()
            if len(run) > WIDEST_GAP:
                middle = len(run) // 2
                keep = set(run[middle - DOORWAY // 2 : middle - DOORWAY // 2 + DOORWAY])
            for x, y in run:
                if (x, y) not in keep:
                    grid[y][x] = "#"
            repairs.append(f"walled a gap of {len(run)} in the outer wall")
    return ["".join(r) for r in grid]


def _add_exit(rows: list[str], repairs: list[str]) -> tuple[list[str], list[tuple[int, int]]]:
    """A building plan with no door out gets one, in the middle of its
    lowest wall that has floor inside it."""
    grid = [list(r) for r in rows]
    h, w = len(grid), len(grid[0]) if grid else 0
    for y in range(h - 1, 0, -1):
        xs = [x for x in range(1, w - 1) if grid[y][x] == "#" and grid[y - 1][x] in "_.,:"]
        if len(xs) >= 3:
            x = xs[len(xs) // 2]
            grid[y][x] = "+"
            repairs.append(f"gave the building a door at {x},{y}")
            out = ["".join(r) for r in grid]
            return out, [(x, y)]
    return rows, []


def _hull_rows(raw: Any, rows: Sequence[str]) -> list[str]:
    """A deck's hull marks as stored, held to its rows' size; [] for none."""
    if not isinstance(raw, list) or len(raw) != len(rows):
        return []
    out = []
    for line, row in zip(raw, rows, strict=True):
        line = str(line)[: len(row)].ljust(len(row))
        out.append("".join(ch if ch in "HRE" else " " for ch in line))
    return out


def deck_pieces(area: Area, records: Any) -> list[Placement]:
    """A deck's set pieces (engine/deck_plan.py `_pieces`) as placements, each
    in the room it stands in, named among what the room already has so the
    furnisher plans round it and adds no second reactor."""
    out: list[Placement] = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        for k, piece in enumerate(record.get("pieces") or []):
            if not isinstance(piece, dict):
                continue
            box = piece.get("box")
            name = " ".join(str(piece.get("name") or "").split())[:80]
            if not name or not isinstance(box, list) or len(box) != 4:
                continue
            try:
                x0, y0, x1, y1 = (int(v) for v in box)
            except (TypeError, ValueError):
                continue
            tiles = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
            if not tiles or not all(area.at(x, y) in FLOOR_TILES for x, y in tiles):
                continue
            room = area.room_at(x0, y0)
            kind = str(piece.get("piece") or "")
            out.append(
                Placement(
                    id=f"{area.id}:piece:{record.get('name', '')}:{k}",
                    name=name,
                    x=x0,
                    y=y0,
                    w=x1 - x0 + 1,
                    h=y1 - y0 + 1,
                    verbs=item_verbs(name),
                    kind="furniture",
                    room=room.index if room else None,
                    piece=kind if kind in DRAWN_PIECES else "set",
                )
            )
            if room is not None and name not in room.objects:
                room.objects.append(name)
    return out


def _object_placements(area: Area) -> list[Placement]:
    out = []
    for y, row in enumerate(area.rows):
        for x, ch in enumerate(row):
            if ch.isdigit() and ch in area.objects:
                name = area.objects[ch]
                room = area.room_at(x, y)
                out.append(
                    Placement(
                        id=f"{area.id}:object:{ch}:{x}:{y}",
                        name=name,
                        x=x,
                        y=y,
                        w=1,
                        h=1,
                        verbs=item_verbs(name),
                        kind="object",
                        room=room.index if room else None,
                    )
                )
    return out


# --- What became of the things (the turn's records) --------------------------

# What can still be done with a broken thing: used (the storyteller is told
# it is broken, and decides), taken if it was small enough; never destroyed
# or eaten again.
BROKEN_KEEPS = ("use", "take")


def apply_things(area: Area, records: Mapping[str, ThingRecord]) -> Area:
    """The area with the records on it (models/area.py `ThingRecord`; the
    author's rulings: a taken thing is with its taker, a broken one is
    visibly broken, in code, and anything the story moves is where it was
    moved to): a taken or gone thing is off the floor and its tile plain
    ground; a broken one stays, `broken`; one with a spot (dropped, moved,
    put there by the story, or broken and moved) lies there, in whichever
    area that is, as big as it was. A copy: the built area is kept as built
    and the records laid over it, so the cache needn't know them. The area
    itself when no record touches it."""
    ids = {p.id for p in area.placements}
    mine = {
        pid: r
        for pid, r in records.items()
        if pid in ids or (r.spot is not None and r.spot.area == area.id)
    }
    if not mine:
        return area
    rows = [list(r) for r in area.rows]
    ground = "_" if area.indoor else "."
    placements: list[Placement] = []
    for p in area.placements:
        record = mine.get(p.id)
        if record is None:
            placements.append(p)
            continue
        if record.state in ("taken", "gone") or record.spot is not None:
            for x, y in p.tiles():  # the plan's digit stood for it: plain ground now
                if 0 <= y < len(rows) and 0 <= x < len(rows[y]) and rows[y][x].isdigit():
                    rows[y][x] = ground
        elif record.state == "broken":
            placements.append(_broken(p))
        else:
            placements.append(p)
    for pid, record in mine.items():
        spot = record.spot
        if spot is None or spot.area != area.id or record.state not in ("dropped", "broken"):
            continue
        room = area.room_at(spot.x, spot.y)
        placed = Placement(
            id=pid,
            name=record.name,
            x=spot.x,
            y=spot.y,
            w=max(1, record.w),
            h=max(1, record.h),
            verbs=item_verbs(record.name),
            kind="object",
            room=room.index if room else None,
        )
        placements.append(_broken(placed) if record.state == "broken" else placed)
    return replace(area, rows=["".join(r) for r in rows], placements=placements)


def _broken(p: Placement) -> Placement:
    return replace(p, broken=True, verbs=tuple(v for v in p.verbs if v in BROKEN_KEEPS))


# --- Where a thing lies (the map's things, and what the story moved) -----------


def thing_tile(
    area: Area,
    near: tuple[int, int],
    taken: Collection[tuple[int, int]] = (),
    *,
    w: int = 1,
    h: int = 1,
    clear: bool = True,
) -> tuple[int, int]:
    """The top-left tile of a free footprint `w` x `h` near `near` for a thing
    to lie on: ground one could stand on, clear of every placement and of
    `taken`; with `clear`, also of doorways and the ways in and out, cutting
    no ground off that could be reached before (as furniture is placed).
    Without it (the story or the author said where: a bench shoved across
    the doorway blocks it), the nearest free footprint. Else the nearest
    free tile; else `near` clamped to the grid."""
    width, height = area.width, area.height
    blocked = set(taken) | area.blocked()
    doors = {t for d in area.doorways for t in d.tiles()}
    doors |= {(e.x, e.y) for e in area.entrances} | set(area.exits)
    keep = {(x + dx, y + dy) for x, y in doors for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    start = (min(max(near[0], 0), max(width - 1, 0)), min(max(near[1], 0), max(height - 1, 0)))
    order = sorted(
        (math.hypot(x - start[0], y - start[1]), x, y)
        for y in range(height)
        for x in range(width)
        if math.hypot(x - start[0], y - start[1]) <= 24
    )
    free = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if area.walkable(x, y) and (x, y) not in blocked
    }
    ins = [d.middle for d in area.doorways] + list(area.exits)
    ins += [(e.x, e.y + 1) for e in area.entrances] + [start]
    before: set[tuple[int, int]] | None = None
    fallback: tuple[int, int] | None = None
    tried = 0
    for _d, x, y in order:
        fp = [(x + dx, y + dy) for dy in range(h) for dx in range(w)]
        if not all(t in free for t in fp):
            continue
        if fallback is None:
            fallback = (x, y)
            if not clear:
                return x, y
        if any(t in keep for t in fp):
            continue
        if before is None:
            before = _reach(free, ins)
        after = _reach(free - set(fp), ins)
        if not ((before - set(fp)) - after):
            return x, y
        tried += 1
        if tried >= 24:
            break
    return fallback if fallback is not None else start


@dataclass(frozen=True)
class LaidThing:
    """One of the map's things laid on an area where it has no record yet:
    its id (the map's), name, the tile it lies on, and whether it is broken."""

    id: str
    name: str
    x: int
    y: int
    broken: bool = False


def lay_things(area: Area, laid: Sequence[LaidThing]) -> Area:
    """The area with the map's things (what a read or a plot file put at a
    place) standing on it, each over its own tile, a thing to see and use
    like any other (the author, Oct 17 2026: things the story adds must show
    on the play view)."""
    if not laid:
        return area
    placements = list(area.placements)
    for t in laid:
        room = area.room_at(t.x, t.y)
        placed = Placement(
            id=t.id,
            name=t.name,
            x=t.x,
            y=t.y,
            w=1,
            h=1,
            verbs=item_verbs(t.name),
            kind="object",
            room=room.index if room else None,
        )
        placements.append(_broken(placed) if t.broken else placed)
    return replace(area, placements=placements)


def carried_by(records: Mapping[str, ThingRecord], who: str) -> list[str]:
    """The things `who` carries, by name, in the order they were taken."""
    return [bare(r.name) for r in records.values() if r.state == "taken" and r.by == who]


# --- Furnishing (tools/bakeoff/furnish.py) --------------------------------------


def _reach(free: set[tuple[int, int]], starts: Sequence[tuple[int, int]]) -> set[tuple[int, int]]:
    seen: set[tuple[int, int]] = set()
    todo = [s for s in starts if s in free]
    while todo:
        x, y = todo.pop()
        if (x, y) in seen:
            continue
        seen.add((x, y))
        for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if n in free and n not in seen:
                todo.append(n)
    return seen


def _bfs_path(
    free: set[tuple[int, int]], a: tuple[int, int], b: tuple[int, int]
) -> list[tuple[int, int]]:
    prev: dict[tuple[int, int], tuple[int, int] | None] = {a: None}
    todo = deque([a])
    while todo:
        cur = todo.popleft()
        if cur == b:
            break
        x, y = cur
        for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if n in free and n not in prev:
                prev[n] = cur
                todo.append(n)
    if b not in prev:
        return []
    out, step = [], b
    while step is not None:
        out.append(step)
        step = prev[step]
    return out


def furnish(area: Area, plan: FurnishPlan, density: str = "normal") -> list[Placement]:
    """The plan's furniture placed in the area's rooms at a density: each
    piece against a wall, in the middle or in a corner as planned, never in
    front of a doorway or on the walkway between a room's doorways, and
    never cutting off floor that could be reached before it stood there.
    Deterministic: the same plan furnishes the same way."""
    w, h = area.width, area.height
    rows = area.rows
    floor = {(x, y) for y in range(h) for x in range(w) if rows[y][x] in FLOOR_TILES}
    taken = {(x, y) for y in range(h) for x in range(w) if rows[y][x] not in FLOOR_TILES}
    taken |= {t for f in area.placements if f.piece for t in f.tiles()}  # a deck's set pieces
    keep: set[tuple[int, int]] = set()
    door_tiles = [d.middle for d in area.doorways]
    for d in area.doorways:
        for y in range(max(0, d.y0 - 2), min(h, d.y1 + 3)):
            for x in range(max(0, d.x0 - 2), min(w, d.x1 + 3)):
                keep.add((x, y))
    walk = floor | {t for d in area.doorways for t in d.tiles()}
    for i in range(len(door_tiles)):
        for j in range(i + 1, len(door_tiles)):
            for x, y in _bfs_path(walk, door_tiles[i], door_tiles[j]):
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        keep.add((x + dx, y + dy))
    import random

    rng = random.Random(zlib.crc32(f"{area.id}:{density}".encode()))
    factor = DENSITY.get(density, 1.0)
    out: list[Placement] = []
    starts = door_tiles + [t for t in area.exits]
    # The step off a level's stairs stays clear too, and counts as a way in
    # (engine/levels.py): a room with no doorway but its stairs had nothing
    # to keep the floor reachable from.
    for e in area.stairs_entrances():
        for y in range(e.y - 1, e.y + 2):
            for x in range(e.x - 1, e.x + 2):
                keep.add((x, y))
        starts += [(e.x + dx, e.y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]

    def wall(x: int, y: int) -> bool:
        return 0 <= x < w and 0 <= y < h and rows[y][x] in WALLS

    for room_plan in plan.rooms:
        if not (0 <= room_plan.room < len(area.rooms)):
            continue
        room = area.rooms[room_plan.room]
        cells = {(x, y) for y in range(h) for x in range(w) if area.room_of[y][x] == room.index}
        for item in room_plan.items:
            iw, ih = max(1, min(4, item.w)), max(1, min(4, item.h))
            count = int(round(max(0, item.count) * factor))
            verbs = item_verbs(item.name, item.verbs)
            for n in range(count):
                spots = []
                for x, y in sorted(cells):
                    fp = [(x + dx, y + dy) for dy in range(ih) for dx in range(iw)]
                    if not all(c in cells and c not in taken and c not in keep for c in fp):
                        continue
                    touch_n = all(wall(x + dx, y - 1) for dx in range(iw))
                    touch_s = all(wall(x + dx, y + ih) for dx in range(iw))
                    touch_w = all(wall(x - 1, y + dy) for dy in range(ih))
                    touch_e = all(wall(x + iw, y + dy) for dy in range(ih))
                    walls = touch_n + touch_s + touch_w + touch_e
                    if item.where == "wall" and not walls:
                        continue
                    if item.where == "corner" and walls < 2:
                        continue
                    if item.where == "middle":
                        if walls:
                            continue
                        score = -abs(x + iw / 2 - room.cx) - abs(y + ih / 2 - room.cy)
                    else:
                        # The north wall reads best in three-quarter view.
                        score = touch_n * 2.0 + rng.random()
                    spots.append((score + rng.random() * 0.5, x, y, fp))
                if not spots:
                    break
                spots.sort(reverse=True)
                placed = False
                before = _reach(floor - taken, starts)
                for _score, x, y, fp in spots[:12]:
                    trial = taken | set(fp)
                    after = _reach(floor - trial, starts)
                    if not ((before - trial) - after):
                        taken = trial
                        out.append(
                            Placement(
                                id=f"{area.id}:{room.index}:{item.name}:{n}",
                                name=item.name,
                                x=x,
                                y=y,
                                w=iw,
                                h=ih,
                                verbs=verbs,
                                kind="furniture",
                                room=room.index,
                            )
                        )
                        placed = True
                        break
                if not placed:
                    break
    return out


# --- Walking and standing --------------------------------------------------------


_STEPS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def path(
    area: Area,
    start: tuple[int, int],
    goal: tuple[int, int],
    blocked: Collection[tuple[int, int]] = (),
) -> list[tuple[int, int]]:
    """The shortest walk from `start` to `goal` over tiles one can stand on,
    diagonals allowed but never across a corner; [] when there is none.
    The goal itself may be blocked (walking up to a person)."""
    if start == goal:
        return [start]
    blocked = set(blocked) | area.blocked()
    blocked.discard(start)
    open_goal = goal

    def free(x: int, y: int) -> bool:
        return (x, y) == open_goal or area.walkable(x, y, blocked)

    prev: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    cost = {start: 0.0}
    import heapq

    todo: list[tuple[float, tuple[int, int]]] = [(0.0, start)]
    while todo:
        _f, cur = heapq.heappop(todo)
        if cur == goal:
            break
        x, y = cur
        for dx, dy in _STEPS:
            nx, ny = x + dx, y + dy
            if not free(nx, ny):
                continue
            if dx and dy and not (free(x + dx, y) and free(x, y + dy)):
                continue
            step = 1.4142 if dx and dy else 1.0
            new = cost[cur] + step
            if new < cost.get((nx, ny), math.inf):
                cost[(nx, ny)] = new
                prev[(nx, ny)] = cur
                guess = math.hypot(goal[0] - nx, goal[1] - ny)
                heapq.heappush(todo, (new + guess, (nx, ny)))
    if goal not in prev:
        return []
    out, step_at = [], goal
    while step_at is not None:
        out.append(step_at)
        step_at = prev[step_at]
    return out[::-1]


def walk_metres(area: Area, steps: Sequence[tuple[int, int]]) -> float:
    total = 0.0
    for (ax, ay), (bx, by) in zip(steps, steps[1:], strict=False):
        total += math.hypot(bx - ax, by - ay)
    return total * area.tile_m


def walk_seconds(metres: float) -> int:
    return int(round(metres / WALK_M_PER_S))


def stand_near(
    area: Area,
    near: tuple[int, int],
    taken: Collection[tuple[int, int]] = (),
    *,
    seed: str = "",
    reach: int = 0,
) -> tuple[int, int]:
    """A tile someone can stand on near `near`, not taken by anyone else:
    the nearest, or with `reach` one of those up to `reach` tiles further,
    chosen by `seed` (so a group doesn't stand in a queue). `near` itself,
    clamped to the grid, when nothing can be stood on."""
    blocked = set(taken) | area.blocked()
    w, h = area.width, area.height
    start = (min(max(near[0], 0), max(w - 1, 0)), min(max(near[1], 0), max(h - 1, 0)))
    seen = {start}
    ring = [start]
    first: int | None = None
    found: list[tuple[int, int]] = []
    depth = 0
    while ring:
        here = sorted(t for t in ring if area.walkable(*t, blocked))
        if here and first is None:
            first = depth
        if first is not None:
            found += here
            if depth >= first + reach:
                break
        nxt = []
        for x, y in ring:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (x + dx, y + dy)
                if 0 <= n[0] < w and 0 <= n[1] < h and n not in seen:
                    seen.add(n)
                    nxt.append(n)
        ring = nxt
        depth += 1
    if not found:
        return start
    if not reach:
        return found[0]
    import random

    return random.Random(zlib.crc32(seed.encode())).choice(found)


def anchor_of(area: Area, world: WorldMap, place_id: str | None) -> tuple[int, int] | None:
    """Where in the area someone at `place_id` stands: in front of a
    building's door, at a place's label, in a room named for it; None for
    the area itself (or anywhere it doesn't mark)."""
    if place_id is None or place_id == area.place_id:
        return None
    if place_id in area.buildings:
        doors = [e for e in area.entrances if e.place_id == place_id]
        box = area.buildings[place_id]
        x0, y0, x1, y1 = box
        front = _front(doors[0], box) if doors else ((x0 + x1) // 2, y1 + 1)
        return _outside_near(area, box, front)
    place = world.places.get(place_id)
    if place is None:
        return None
    wanted = key(place.name)
    for name, spot in area.labels.items():
        if key(name) == wanted:
            return spot
    for room in area.rooms:
        if room.purpose and key(room.purpose) == wanted:
            return int(room.cx), int(room.cy)
    return None


def _outside_near(
    area: Area, box: tuple[int, int, int, int], near: tuple[int, int]
) -> tuple[int, int]:
    """`near` when one can stand on it, else the nearest tile outside the
    box one can (a door's front was a pond, and the ring search gave the
    door tile itself; the second review)."""
    x0, y0, x1, y1 = box
    if area.walkable(*near):
        return near
    best = None
    for radius in range(1, 6):
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                x, y = near[0] + dx, near[1] + dy
                if x0 <= x <= x1 and y0 <= y <= y1:
                    continue
                if area.walkable(x, y) and (best is None or dx * dx + dy * dy < best[0]):
                    best = (dx * dx + dy * dy, (x, y))
        if best is not None:
            return best[1]
    return near


def _front(entrance: Entrance, box: tuple[int, int, int, int]) -> tuple[int, int]:
    """The tile outside a building's door."""
    dx, dy = {"south": (0, 1), "north": (0, -1), "west": (-1, 0), "east": (1, 0)}[entrance.side]
    return entrance.x + dx, entrance.y + dy


def front_of(area: Area, place_id: str, side: str | None = None) -> tuple[int, int] | None:
    """The tile outside one of a building's doors in this (outdoor) area,
    on `side` when it has a door there."""
    doors = [e for e in area.entrances if e.place_id == place_id]
    if not doors:
        return None
    x0, y0, x1, y1 = area.buildings[place_id]
    # No door on that side: the one nearest that wall (the inside's way out
    # and the outside's door are drawn by different plans).
    toward = {"north": lambda e: e.y - y0, "south": lambda e: y1 - e.y}
    toward |= {"west": lambda e: e.x - x0, "east": lambda e: x1 - e.x}
    chosen = next((e for e in doors if e.side == side), None)
    if chosen is None:
        chosen = min(doors, key=toward[side]) if side in toward else doors[0]
    return _front(chosen, area.buildings[place_id])


def inside_door(area: Area, side: str | None = None) -> tuple[int, int] | None:
    """The floor tile just inside one of an indoor area's doors out, on
    `side` of the plan when there is one there (a door in the south wall
    outside leads in at the south)."""
    if not area.exits:
        return None
    w, h = area.width, area.height

    def side_of(x: int, y: int) -> str:
        distances = {"north": y, "south": h - 1 - y, "west": x, "east": w - 1 - x}
        return min(distances, key=distances.get)

    toward = {"north": lambda e: e[1], "south": lambda e: h - 1 - e[1]}
    toward |= {"west": lambda e: e[0], "east": lambda e: w - 1 - e[0]}
    chosen = next((e for e in area.exits if side_of(*e) == side), None)
    if chosen is None and side in toward:
        chosen = min(area.exits, key=toward[side])
    if chosen is None:
        # A door before a gap in the wall: live (Oct 18 2026) the map shop's
        # keeper stood in a gap of its north wall, its door in the south.
        chosen = next((e for e in area.exits if area.at(*e) == "+"), area.exits[0])
    x, y = chosen
    for dx, dy in ((0, -1), (0, 1), (1, 0), (-1, 0)):
        n = (x + dx, y + dy)
        if area.at(*n) in WALKABLE and area.room_at(*n) is not None:
            return n
    return stand_near(area, chosen)


def exit_side(area: Area, x: int, y: int) -> str:
    w, h = area.width, area.height
    distances = {"north": y, "south": h - 1 - y, "west": x, "east": w - 1 - x}
    return min(distances, key=distances.get)


# --- Positions, in words -----------------------------------------------------------

_COMPASS = (
    "north",
    "north-east",
    "east",
    "south-east",
    "south",
    "south-west",
    "west",
    "north-west",
)


def bearing(a: tuple[float, float], b: tuple[float, float]) -> str:
    """The compass way from a to b on the grid (y runs south)."""
    dx, dy = b[0] - a[0], a[1] - b[1]
    angle = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return _COMPASS[round(angle / 45) % 8]


def metres_words(metres: float) -> str:
    if metres < 1.5:
        return "beside"
    if metres < 10:
        return f"{metres:.0f} m"
    return f"{round(metres / 5) * 5:.0f} m"


def room_words(area: Area, room: Room | None) -> str:
    if room is None:
        return ""
    if room.purpose:
        return f"the {room.purpose.lower().removeprefix('the ')}"
    return f"the {room.name}"


def near_thing(area: Area, x: int, y: int, within: float = 1.6) -> Placement | None:
    """The placement nearest (x, y) within `within` tiles of its edge."""
    best, best_d = None, within
    for p in area.placements:
        cx = min(max(x, p.x), p.x + p.w - 1)
        cy = min(max(y, p.y), p.y + p.h - 1)
        d = math.hypot(cx - x, cy - y)
        if d <= best_d:
            best, best_d = p, d
    return best


def where_words(area: Area, x: int, y: int) -> str:
    """'in the kitchen, by the woodstove'; 'in the farm, by the well'."""
    parts = []
    room = area.room_at(x, y)
    if room is not None:
        parts.append(f"in {room_words(area, room)}")
    doorway = next((d for d in area.doorways if (x, y) in d.tiles()), None)
    if doorway is not None:
        parts.append("in a doorway")
    entrance = area.entrance_at(x, y)
    thing = near_thing(area, x, y)
    if thing is not None:
        parts.append(f"by the {_bare(thing.name)}")
    elif not area.indoor:
        for place_id, (x0, y0, x1, y1) in area.buildings.items():
            if x0 - 2 <= x <= x1 + 2 and y0 - 2 <= y <= y1 + 2:
                parts.append(f"outside {area.names.get(place_id, 'a building')}")
                break
        else:
            spot = min(
                area.labels.items(),
                key=lambda item: math.hypot(item[1][0] - x, item[1][1] - y),
                default=None,
            )
            if spot is not None and math.hypot(spot[1][0] - x, spot[1][1] - y) <= 6:
                parts.append(f"near {spot[0]}")
    if entrance is not None:
        parts.append("on the stairs" if entrance.stairs else "at its door")
    return ", ".join(parts)


def bare(name: str) -> str:
    """A thing's name without its article: "a wooden chest" is "wooden chest"."""
    return _bare(name)


def _bare(name: str) -> str:
    return re.sub(r"^(the|a|an)\s+", "", " ".join(name.split()), flags=re.IGNORECASE)


# The time of day by the clock's minute, for the storyteller (never the day:
# the author's ruling) and the paintings: each band's first hour and its words.
TIME_OF_DAY: tuple[tuple[float, str], ...] = (
    (0.0, "the small hours"),
    (5.0, "early morning"),
    (8.0, "mid-morning"),
    (11.0, "midday"),
    (13.0, "afternoon"),
    (16.0, "late afternoon"),
    (18.0, "dusk"),
    (19.5, "evening"),
    (22.0, "night"),
)


def time_of_day(minute: int) -> str:
    """The time of day at this minute of the clock's day, in words: "early
    morning", "midday", "dusk", "the small hours" (`TIME_OF_DAY`). Pure."""
    hour = (minute % (24 * 60)) / 60
    words = TIME_OF_DAY[0][1]
    for start, band in TIME_OF_DAY:
        if hour >= start:
            words = band
    return words


def now_words(minute: int | None, weather: str = "") -> str:
    """One line for the storyteller and the paintings: the time of day and
    the weather the story last stated ("It is dusk. The weather: snowing
    hard."), or "" with neither."""
    parts = []
    if minute is not None:
        parts.append(f"It is {time_of_day(minute)}.")
    weather = " ".join(weather.split()).rstrip(".")
    if weather:
        parts.append(f"The weather: {weather}.")
    return " ".join(parts)


def render_positions(
    area: Area,
    world: WorldMap,
    spots: Mapping[str, Spot],
    held: str,
    *,
    walked: str = "",
    action_line: str = "",
    limit: int = 8,
    things: Mapping[str, ThingRecord] | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
    elsewhere: Sequence[str] = (),
    level_names: Mapping[str, str] | None = None,
    now: str = "",
    mark_fixed: bool = False,
) -> str:
    """Where everyone stands, for the storyteller: exact, from the grid
    (design §5). `now`: the time of day and the weather (`now_words`), right
    under the heading. The author's character's place in the area (and its
    level, on a plan with floors or decks), who else is here with distance
    and bearing, what is within reach (a broken thing said to be broken),
    what each of them carries (`things`, the turn's records; the author's
    character first), the doorways out, the stairs to other levels, who is
    on another level of the place (`elsewhere`, lines the caller found), and
    how far they walked since the last passage. Never the clock's day (the
    author's ruling: the day is the plot's). `mark_fixed`: what can't be
    taken or moved (built in, rooted, bolted down) is marked so in reach, on
    a turn that may not move it."""
    me = spots.get(held)
    if me is None or me.area != area.id:
        return ""
    here = (me.x, me.y)
    lines = ["Where everyone stands (the map is exact: keep to it):"]
    if now:
        lines.append(now)
    words = where_words(area, *here)
    # A window of open country: "in open country, in the Great Erg".
    place = f"in {area.name}" + (f", in {area.within}" if area.within else "")
    if area.level_name:
        place += f", {area.level_name.lower()}"
    lines.append(f"- {held}: {place}" + (f", {words}" if words else "") + ".")
    my_room = area.room_at(*here)
    others = []
    for name, spot in spots.items():
        if name == held or spot.area != area.id:
            continue
        metres = math.hypot(spot.x - me.x, spot.y - me.y) * area.tile_m
        others.append((metres, name, spot))
    others.sort()
    for metres, name, spot in others[:limit]:
        room = area.room_at(spot.x, spot.y)
        apart = ""
        if area.indoor and room is not None and my_room is not None and room.index != my_room.index:
            apart = f", in {room_words(area, room)} (another room)"
        where = where_words(area, spot.x, spot.y)
        far = metres_words(metres)
        way = bearing(here, (spot.x, spot.y))
        how = f"beside {held}" if far == "beside" else f"{far} {way} of {held}"
        tail = f", {where}" if where and not apart else apart
        lines.append(f"- {name}: {how}{tail}; {sight_words(area, here, (spot.x, spot.y))}.")
    reach = sorted(
        (
            (math.hypot(p.centre[0] - me.x - 0.5, p.centre[1] - me.y - 0.5) * area.tile_m, p)
            for p in area.placements
        ),
        key=lambda item: item[0],
    )
    nearest: dict[str, tuple[float, int]] = {}
    fixed: set[str] = set()
    for m, p in reach:
        if m <= 4.0:
            label = _bare(p.name) + (" (broken)" if p.broken else "")
            first, count = nearest.get(label, (m, 0))
            nearest[label] = (first, count + 1)
            if mark_fixed and fixed_thing(p.name):
                fixed.add(label)
    close = [
        f"{the(name)} ({'at hand' if m < 1.5 else metres_words(m)}"
        + (f"; {n - 1} more near" if n > 1 else "")
        + (f"; {FIXED_MARK})" if name in fixed else ")")
        for name, (m, n) in nearest.items()
    ][:limit]
    if close:
        lines.append("Within reach: " + "; ".join(close) + ".")
    if things:
        # Who carries what: the author's character always, the others when
        # they are in this area.
        present = [held] + [n for _m, n, _s in others]
        for name in present:
            carried = carried_by(things, name)
            if carried:
                lines.append(texts.fill("map.carries", name=name, things=", ".join(carried)))
    if area.indoor and my_room is not None:
        ways = []
        for d in area.doorways:
            sides = {r.index for x, y in _beside(d) if (r := area.room_at(x, y)) is not None}
            if my_room.index in sides:
                other = [area.rooms[i] for i in sides if i != my_room.index]
                out = d.middle in area.exits or any(t in area.exits for t in d.tiles())
                if out:
                    ways.append(f"{bearing(here, d.middle)}, out of {area.name}")
                elif other:
                    ways.append(f"{bearing(here, d.middle)}, to {room_words(area, other[0])}")
        if ways:
            lines.append("Doorways from here: " + "; ".join(dict.fromkeys(ways)) + ".")
    stairs = stairs_lines(area, here, level_names)
    if stairs:
        lines.append("Stairs from here: " + "; ".join(stairs) + ".")
    for line in elsewhere:
        lines.append(line)
    if walked:
        lines.append(f"Since the last passage: {walked}.")
    if action_line:
        lines.append(action_line)
    return "\n".join(lines)


def stairs_lines(
    area: Area, here: tuple[int, int], level_names: Mapping[str, str] | None = None
) -> list[str]:
    """'up to the upper floor, 8 m north', for each stairs in the area, the
    nearest first (`level_names`: each level area's name, by id; a level
    not named is "the level above/below")."""
    found = []
    names = level_names or {}
    for e in sorted(
        area.stairs_entrances(), key=lambda e: math.hypot(e.x - here[0], e.y - here[1])
    ):
        name = names.get(e.place_id) or ("the level above" if e.side == "up" else "the level below")
        metres = math.hypot(e.x - here[0], e.y - here[1]) * area.tile_m
        how = "at hand" if metres < 1.5 else f"{metres_words(metres)} {bearing(here, (e.x, e.y))}"
        found.append(f"{e.side} to {name}, {how}")
    return list(dict.fromkeys(found))[:4]


def _beside(d: Doorway) -> list[tuple[int, int]]:
    if d.way == "h":
        return [(x, d.y0 - 1) for x in range(d.x0, d.x1 + 1)] + [
            (x, d.y1 + 1) for x in range(d.x0, d.x1 + 1)
        ]
    return [(d.x0 - 1, y) for y in range(d.y0, d.y1 + 1)] + [
        (d.x1 + 1, y) for y in range(d.y0, d.y1 + 1)
    ]


def walked_words(seconds: int, frm: str = "", to: str = "") -> str:
    """'about twenty minutes on foot, from the farm to Hellsville'."""
    minutes = seconds / 60
    if minutes < 1:
        span = "less than a minute on foot"
    elif minutes < 1.5:
        span = "about a minute on foot"
    elif minutes < 60:
        span = f"about {round(minutes)} minutes on foot"
    else:
        hours = minutes / 60
        span = f"about {hours:.0f} hours on foot" if hours >= 1.75 else "about an hour on foot"
    if frm and to and frm != to:
        return f"{span}, from {frm} to {to}"
    return span


def anchors_near(area: Area, x: int, y: int, limit: int = 12) -> list[str]:
    """Named spots near (x, y), nearest first, for the read to choose from
    (design §5: a closed list, as the plot's places are): the things, the
    rooms and the doorways of the room (x, y) is in."""
    found: list[tuple[float, str]] = []
    for p in area.placements:
        d = math.hypot(p.centre[0] - x, p.centre[1] - y)
        found.append((d, the(p.name)))
    for room in area.rooms:
        found.append((math.hypot(room.cx - x, room.cy - y) + 0.5, room_words(area, room)))
    for name, (dx, dy) in doorway_spots(area, x, y):
        found.append((math.hypot(dx - x, dy - y), name))
    names = dict.fromkeys(name for _d, name in sorted(found))
    return list(names)[:limit]


# A doorway is where people stand in prose ("Ruth came as far as the
# doorway"), and the read reached for it with none on its list (live, Oct 17
# 2026): it was dropped and she stayed 20 m off in the kitchen.
_DOORWAY = frozenset({"doorway", "door", "doorframe", "threshold", "door frame"})


def doorway_spots(area: Area, x: int, y: int) -> list[tuple[str, tuple[int, int]]]:
    """The doorways of the room (x, y) is in, each named for where it
    leads ("the doorway to the kitchen", "the doorway out of The barn")."""
    room = area.room_at(x, y) if area.indoor else None
    if room is None:
        return []
    found = []
    for d in area.doorways:
        sides = {r.index for bx, by in _beside(d) if (r := area.room_at(bx, by)) is not None}
        if room.index not in sides:
            continue
        other = [area.rooms[i] for i in sides if i != room.index]
        if d.middle in area.exits or any(t in area.exits for t in d.tiles()):
            name = f"the doorway out of {area.name}"
        elif other:
            name = f"the doorway to {room_words(area, other[0])}"
        else:
            continue
        found.append((name, d.middle))
    return found


def anchor_tile(
    area: Area, name: str, near: tuple[int, int] | None = None
) -> tuple[int, int] | None:
    """The tile a named spot stands for (`anchors_near`'s names). `near`:
    where the spots were listed from, so a bare "the doorway" is the
    nearest doorway of that room."""
    wanted = key(_bare(name))
    for p in area.placements:
        if key(_bare(p.name)) == wanted:
            return int(p.centre[0]), int(p.centre[1])
    for room in area.rooms:
        if key(_bare(room_words(area, room))) == wanted or key(room.name) == wanted:
            return int(room.cx), int(room.cy)
    if near is not None:
        doors = doorway_spots(area, *near)
        for door, tile in doors:
            if key(door) == wanted:
                return tile
        if wanted in _DOORWAY and doors:
            return min(doors, key=lambda d: math.hypot(d[1][0] - near[0], d[1][1] - near[1]))[1]
        if wanted in _DOORWAY:
            # Standing in no room (in a doorway, outdoors): the nearest door
            # of the area, a doorway, a way out or a building's door.
            every = [d.middle for d in area.doorways] + list(area.exits)
            every += [(e.x, e.y) for e in area.entrances if not e.stairs]
            if every:
                return min(every, key=lambda t: math.hypot(t[0] - near[0], t[1] - near[1]))
    return None


# Words that say where something went, and a door named among them.
_WENT = re.compile(
    r"\b(?:to|against|across|behind|beside|by|at|into|under|blocking|blocks|blocked|"
    r"wedged|next to|near|towards?|in front of|over to|up to|before|barring|bars)\b"
)
_A_DOOR = re.compile(
    r"\b(?:doors?|doorways?|doorframe|door frame|doorstep|doorsill|threshold|entrance|way in|"
    r"gates?)\b"
)


def spot_in_words(area: Area, words: str, near: tuple[int, int]) -> str:
    """The spot a few words of the read's say a thing went to ("dragged to the
    door", "shoved across the doorway, blocking it", "moved to the stove"):
    a door or doorway (the nearest, `anchor_tile`), else the spot near `near`
    the words name best (`anchors_near`); "" when they say no place it went.
    The read put where a thing ended up in its state as often as in "spot"
    (the replay, Oct 17 2026)."""
    said = " ".join(words.lower().split())
    if not said or _WENT.search(said) is None:
        return ""
    if _A_DOOR.search(said) is not None:
        return "the doorway"
    best, score = "", 0
    for name in anchors_near(area, *near, limit=40):
        found = named_in(name, said)
        if found > score:
            best, score = name, found
    return best


def fills_word(ch: str) -> str:
    """A tile's kind in words, for a hover."""
    for word, tile in FILLS.items():
        if tile == ch:
            return word
    return ""


# --- The map's metres and an area's tiles --------------------------------------
# An area is its place's plan laid over the plan's rectangle on the map
# (`plan_rect`), or a window of open country over its own rectangle, so a
# tile is a point in metres east and north, as marks (engine/marks.py) and
# the map's places are.


def area_rect(world: WorldMap, area: Area) -> tuple[float, float, float, float] | None:
    """(west, north, width, height) in metres of the area on the map."""
    if area.rect is not None:
        return area.rect
    place = world.places.get(area.place_id)
    return plan_rect(world, place) if place is not None else None


def tile_point(world: WorldMap, area: Area, x: float, y: float) -> tuple[float, float] | None:
    """The point in metres (east, north) at the middle of tile (x, y)."""
    rect = area_rect(world, area)
    if rect is None or not area.width or not area.height:
        return None
    west, north, width, height = rect
    return west + (x + 0.5) * width / area.width, north - (y + 0.5) * height / area.height


def point_tile(world: WorldMap, area: Area, mx: float, my: float) -> tuple[int, int] | None:
    """The tile of the area at a point in metres; None outside it."""
    rect = area_rect(world, area)
    if rect is None or not area.width or not area.height:
        return None
    west, north, width, height = rect
    x = math.floor((mx - west) / width * area.width)
    y = math.floor((north - my) / height * area.height)
    if 0 <= x < area.width and 0 <= y < area.height:
        return x, y
    return None


def clamped_tile(world: WorldMap, area: Area, mx: float, my: float) -> tuple[int, int] | None:
    """The tile at a point, or the nearest tile of the area to a point
    outside it (someone placed at a window's very edge); None with no
    rectangle."""
    rect = area_rect(world, area)
    if rect is None or not area.width or not area.height:
        return None
    west, north, width, height = rect
    x = math.floor((mx - west) / width * area.width)
    y = math.floor((north - my) / height * area.height)
    return min(max(x, 0), area.width - 1), min(max(y, 0), area.height - 1)


def metres_per_tile(world: WorldMap, area: Area) -> float:
    """How wide a tile of the area is on the map (its plan's rectangle over
    its tiles; outdoors that is the walking scale; a window's the land's
    own)."""
    return area.tile_m  # one scale for the grid and the map (`build_area`)


# --- Who can see whom (design §3: line of sight across the grid) ---------------
# Walls and rock stop sight; windows, fences, doorways and furniture don't;
# trees thin it, and a few deep stop it. Earshot is a sight line within a
# few strides, or the same room. Told to the storyteller with the positions
# (not measured), and shown on the play view; the scene's roster is still the
# scene read's.

OPAQUE = frozenset("#^")
TREES_BLOCK = 3
EARSHOT_M = 20.0


def _line(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    """The tiles a straight line from the middle of a to the middle of b
    crosses, both ends included."""
    (x0, y0), (x1, y1) = a, b
    steps = max(abs(x1 - x0), abs(y1 - y0))
    if steps == 0:
        return [a]
    out: list[tuple[int, int]] = []
    for i in range(steps + 1):
        t = i / steps
        tile = (round(x0 + (x1 - x0) * t), round(y0 + (y1 - y0) * t))
        if not out or out[-1] != tile:
            out.append(tile)
    return out


def _opaque(area: Area) -> frozenset[str]:
    """What stops sight here: walls and rock, and off a deck the blank
    tiles too (unlit rock or the dark inside a building's plan; on a deck
    they are open space)."""
    return OPAQUE if area.look == "space" else OPAQUE | {" "}


def sees(area: Area, a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Whether someone at tile a can see tile b: nothing opaque between. A
    line never slips between two opaque tiles that touch at a corner (the
    corner rule `path` keeps)."""
    opaque = _opaque(area)
    trees = 0
    line = _line(a, b)
    for (px, py), (x, y) in zip(line, line[1:], strict=False):
        if px != x and py != y and area.at(x, py) in opaque and area.at(px, y) in opaque:
            return False
        if (x, y) == b:
            break
        ch = area.at(x, y)
        if ch in opaque:
            return False
        if ch == "T":
            trees += 1
            if trees >= TREES_BLOCK:
                return False
    return True


def in_earshot(area: Area, a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Whether people at a and b can hear each other speak."""
    metres = math.hypot(a[0] - b[0], a[1] - b[1]) * area.tile_m
    if metres > EARSHOT_M:
        return False
    room_a, room_b = area.room_at(*a), area.room_at(*b)
    if room_a is not None and room_b is not None and room_a.index == room_b.index:
        return True
    return sees(area, a, b)


def sight_words(area: Area, a: tuple[int, int], b: tuple[int, int]) -> str:
    """'in sight, within earshot', 'out of sight', for the positions."""
    seen, heard = sees(area, a, b), in_earshot(area, a, b)
    if seen:
        return "in sight, within earshot" if heard else "in sight"
    return "out of sight, within earshot" if heard else "out of sight"


def seen_tiles(area: Area, origin: tuple[int, int]) -> list[list[bool]]:
    """Which tiles someone at `origin` can see: a ray to each. Indoors only
    (an area of 40 x 24 tiles is ~20 ms); outdoors the people's lines are
    enough."""
    w, h = area.width, area.height
    out = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            out[y][x] = sees(area, origin, (x, y))
    return out
