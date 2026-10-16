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
from dataclasses import dataclass, field
from typing import Any

from sealedlore.engine.tile_sets import LIGHT_WORDS
from sealedlore.engine.world import key
from sealedlore.engine.world_art import (
    FILLS,
    decked,
    fixed_in_plan,
    local_area,
    plan_tile,
    rasterise,
    with_fixed,
)
from sealedlore.models.area import FurnishPlan, Spot
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
# Where someone may stand, and what a piece of furniture may stand on.
WALKABLE = frozenset("._=+,:s")
FLOOR_TILES = frozenset("_+")
WALLS = frozenset("#W")
# Things a person can't walk through when they're in the plan as a digit.
SMALL = (
    "chair",
    "stool",
    "lamp",
    "lantern",
    "candle",
    "bucket",
    "basket",
    "bottle",
    "book",
    "mug",
    "cup",
    "plate",
    "box",
    "crate",
    "sack",
    "bag",
    "pot",
    "jar",
    "tool",
    "rifle",
    "gun",
    "knife",
    "sword",
    "bow",
    "axe",
    "key",
    "map",
    "ledger",
    "bread",
    "apple",
    "rope",
    "bell",
    "skull",
    "bones",
    "kettle",
    "radio",
)
BIG = (
    "table",
    "bed",
    "bunk",
    "stove",
    "counter",
    "shelf",
    "shelves",
    "cabinet",
    "wardrobe",
    "altar",
    "throne",
    "hearth",
    "workbench",
    "bench",
    "desk",
    "piano",
    "truck",
    "cart",
    "car",
    "tractor",
    "engine",
    "boiler",
    "furnace",
    "vat",
    "tower",
    "well",
    "statue",
    "fountain",
    "tree",
    "oak",
    "haystack",
    "barricade",
    "console",
    "locker",
    "sofa",
    "couch",
    "cupboard",
    "pantry",
    "dresser",
    "stairs",
    "staircase",
    "lift",
    "loader",
    "stall",
)
FOOD = (
    "bread",
    "apple",
    "stew",
    "soup",
    "meat",
    "cheese",
    "fruit",
    "pie",
    "cake",
    "food",
    "meal",
    "berries",
    "fish",
    "ham",
    "biscuit",
    "ration",
    "canned goods",
    "vegetables",
    "potato",
    "egg",
    "honey",
    "jerky",
    "ale",
    "wine",
    "cider",
    "water bucket",
)


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
    """A building's door in an outdoor area that leads into its own area."""

    x: int
    y: int
    place_id: str
    side: str  # the building's wall the door is in: north, south, east, west


@dataclass
class Area:
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
    # The names of the buildings in `buildings`, by place id.
    names: dict[str, str] = field(default_factory=dict)
    # What the rules repaired, for the log and the tests.
    repairs: list[str] = field(default_factory=list)

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


def _chain(world: WorldMap, place_id: str | None) -> list[WorldPlace]:
    out: list[WorldPlace] = []
    seen: set[str] = set()
    current = world.places.get(place_id or "")
    while current is not None and current.id not in seen:
        seen.add(current.id)
        out.append(current)
        current = world.places.get(current.inside or "")
    return out


def has_inside(art: WorldArt, place: WorldPlace) -> bool:
    """A building (or room) with a plan of its own: its inside is an area."""
    return (
        place.kind in ("building", "room") and art.layout_shown(place.id, place.state) is not None
    )


def is_deck(world: WorldMap, art: WorldArt, place_id: str | None) -> bool:
    """A station or ship: a plan whose ground is space. Its rooms are drawn
    on the deck and joined by it, so they are the deck's, not areas of their
    own."""
    place = world.places.get(place_id or "")
    if place is None:
        return False
    layout = art.layout_shown(place.id, place.state)
    return layout is not None and str((layout.spec or {}).get("ground") or "") == "space"


def area_of(world: WorldMap, art: WorldArt, place_id: str | None) -> str | None:
    """The area someone at `place_id` stands in: the innermost building
    round them with an inside of its own, else the local area's plan (the
    farm for its yard, the town for its street). A room of a deck is the
    deck. None where no plan is drawn yet."""
    chain = _chain(world, place_id)
    decks = [p for p in chain if is_deck(world, art, p.id)]
    if decks:
        return decks[-1].id
    for place in chain:
        if has_inside(art, place):
            return place.id
    local = local_area(world, place_id)
    if local is not None and art.layout_shown(local.id, local.state) is not None:
        return local.id
    return None


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


def _roads_on_open_ground(rows: list[str], roads: Sequence[dict[str, Any]]) -> list[str]:
    """Roads by distance from their line, on open ground only: a road never
    runs through a building (the rule), and a diagonal is a line, not the
    rasteriser's staircase."""
    if not roads:
        return rows
    grid = [list(r) for r in rows]
    h, w = len(grid), len(grid[0]) if grid else 0
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


def _outside_cells(rows: list[str]) -> set[tuple[int, int]]:
    """What lies outside an indoor plan's walls: reached from the grid's
    edge over ground that isn't floor, a wall, a door or a thing (a gap in
    the outer wall is floor, so the outside never runs in through it)."""
    h, w = len(rows), len(rows[0]) if rows else 0

    def outdoors(x: int, y: int) -> bool:
        ch = rows[y][x]
        return ch not in "#W+_" and not ch.isdigit()

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
            elif edge and ch == "_":
                out.append((x, y))
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


def _door_side(box: tuple[int, int, int, int], x: int, y: int) -> str:
    x0, y0, x1, y1 = box
    if y == y1:
        return "south"
    if y == y0:
        return "north"
    return "west" if x == x0 else "east"


def _object_verbs(name: str) -> tuple[str, ...]:
    lowered = name.lower()
    verbs = ["use"]
    small = any(re.search(rf"\b{w}", lowered) for w in SMALL)
    big = any(re.search(rf"\b{w}", lowered) for w in BIG)
    if any(word in lowered for word in FOOD):
        verbs = ["eat", "take", "use"] if not big else ["eat", "use"]
    elif small and not big:
        verbs.append("take")
    verbs.append("destroy")
    return tuple(dict.fromkeys(verbs))


def item_verbs(name: str, planned: Sequence[str] = ()) -> tuple[str, ...]:
    """What can be done with a thing: what its plan says, else what its name
    suggests (food is eaten, small things taken, anything used or
    destroyed), in the menu's order."""
    order = ("use", "take", "eat", "destroy")
    chosen = set(planned) | set(_object_verbs(name)) if planned else set(_object_verbs(name))
    if planned and any(word in name.lower() for word in BIG):
        chosen.discard("take")
    return tuple(v for v in order if v in chosen)


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
    look = str(spec.get("ground") or "")
    indoor = place.kind in ("building", "room") or look == "space"
    repairs: list[str] = []
    entrances: list[Entrance] = []
    buildings: dict[str, tuple[int, int, int, int]] = {}
    people_keys = {key(n) for n in people}
    if indoor:
        rows = list(layout.rows)
        if look == "space" or place.kind not in ("building", "room"):
            rows = list(decked(rows))
        objects = dict(layout.objects)
        labels = {k: (int(v[0]), int(v[1])) for k, v in layout.labels.items() if len(v) >= 2}
        tile_m = max(plan_tile(place), 1.0) if place.kind in ("building", "room") else 1.5
        if place.kind in ("building", "room"):
            rows = _whole_outer_wall(rows, repairs)
        exits = _exits(rows)
        if not exits and place.kind in ("building", "room"):
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
        else:  # a plan with no shapes kept: its tiles, each k times over
            rows = ["".join(ch * k for ch in row) for row in layout.rows for _ in range(k)]
            objects, raw_labels = (
                dict(layout.objects),
                {n: [v[0] * k + k // 2, v[1] * k + k // 2] for n, v in layout.labels.items()},
            )
        labels = {n: (int(v[0]), int(v[1])) for n, v in raw_labels.items() if len(v) >= 2}
        for f in fixed:  # what the map has here that isn't a building: labelled where it is
            if f["kind"] != "building":
                labels[f["name"][:60]] = ((f["x"] + f["w"] // 2) * k, (f["y"] + f["h"] // 2) * k)
        rows = dress(rows, look, f"{place.id}:{place.state}")
        tile_m = OUTDOOR_TILE_M
        exits = []
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
            doors = [
                (x, y)
                for y in range(max(y0, 0), min(y1, len(grid) - 1) + 1)
                for x in range(max(x0, 0), min(x1, len(grid[0]) - 1) + 1)
                if grid[y][x] == "+" and (x in (x0, x1) or y in (y0, y1))
            ]
            if not doors and 0 <= y1 < len(grid) and 0 <= (x0 + x1) // 2 < len(grid[0]):
                doors = [((x0 + x1) // 2, y1)]
                grid[y1][(x0 + x1) // 2] = "+"
                repairs.append(f"gave {child.name} a door")
            for x, y in doors:
                if has_inside(art, child):
                    entrances.append(Entrance(x, y, child.id, _door_side(box, x, y)))
        rows = ["".join(r) for r in grid]
    # Objects named for people are people.
    for digit, name in list(objects.items()):
        if key(name) in people_keys:
            objects.pop(digit)
            rows = [r.replace(digit, "_" if indoor else ".") for r in rows]
    doorways = openings(rows)
    rooms, room_of = find_rooms(rows, objects, doorways) if indoor else ([], [])
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
        exits=exits,
        parent=area_of(world, art, place.inside) if place.inside else None,
        repairs=repairs,
    )
    area.placements = _object_placements(area)
    if indoor and rooms:
        plan = art.furnishing_for(place.id, place.state)
        if plan is not None:
            for room in rooms:
                purpose = next((r.purpose for r in plan.rooms if r.room == room.index), "")
                if purpose:
                    room.purpose = " ".join(purpose.split())[:60]
            area.placements += furnish(area, plan, density)
    return area


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
            if grid[y][x] == "_":
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


def _object_placements(area: Area) -> list[Placement]:
    out = []
    for y, row in enumerate(area.rows):
        for x, ch in enumerate(row):
            if ch.isdigit() and ch in area.objects:
                name = area.objects[ch]
                room = area.room_at(x, y)
                out.append(
                    Placement(
                        id=f"{area.place_id}:object:{ch}:{x}:{y}",
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

    rng = random.Random(zlib.crc32(f"{area.place_id}:{density}".encode()))
    factor = DENSITY.get(density, 1.0)
    out: list[Placement] = []
    starts = door_tiles + [t for t in area.exits]

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
                                id=f"{area.place_id}:{room.index}:{item.name}:{n}",
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
        if doors:
            return _front(doors[0], box)
        x0, y0, x1, y1 = box
        return (x0 + x1) // 2, y1 + 1
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
    chosen = next((e for e in doors if e.side == side), doors[0])
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

    chosen = next((e for e in area.exits if side_of(*e) == side), area.exits[0])
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
        parts.append("at its door")
    return ", ".join(parts)


def bare(name: str) -> str:
    """A thing's name without its article: "a wooden chest" is "wooden chest"."""
    return _bare(name)


def _bare(name: str) -> str:
    return re.sub(r"^(the|a|an)\s+", "", " ".join(name.split()), flags=re.IGNORECASE)


def render_positions(
    area: Area,
    world: WorldMap,
    spots: Mapping[str, Spot],
    held: str,
    *,
    walked: str = "",
    action_line: str = "",
    limit: int = 8,
) -> str:
    """Where everyone stands, for the storyteller: exact, from the grid
    (design §5). The author's character's place in the area, who else is
    here with distance and bearing, what is within reach, the doorways out,
    and how far they walked since the last passage. Never the clock's day
    (the author's ruling: the day is the plot's)."""
    me = spots.get(held)
    if me is None or me.area != area.place_id:
        return ""
    here = (me.x, me.y)
    lines = ["Where everyone stands (the map is exact: keep to it):"]
    words = where_words(area, *here)
    lines.append(f"- {held}: in {area.name}" + (f", {words}" if words else "") + ".")
    my_room = area.room_at(*here)
    others = []
    for name, spot in spots.items():
        if name == held or spot.area != area.place_id:
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
        lines.append(f"- {name}: {how}{tail}.")
    reach = sorted(
        (
            (math.hypot(p.centre[0] - me.x - 0.5, p.centre[1] - me.y - 0.5) * area.tile_m, p)
            for p in area.placements
        ),
        key=lambda item: item[0],
    )
    close = [f"the {_bare(p.name)} ({metres_words(m)})" for m, p in reach if m <= 4.0][:limit]
    if close:
        lines.append("Within reach: " + "; ".join(close) + ".")
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
    if walked:
        lines.append(f"Since the last passage: {walked}.")
    if action_line:
        lines.append(action_line)
    return "\n".join(lines)


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
    (design §5: a closed list, as the plot's places are)."""
    found: list[tuple[float, str]] = []
    for p in area.placements:
        d = math.hypot(p.centre[0] - x, p.centre[1] - y)
        found.append((d, f"the {_bare(p.name)}"))
    for room in area.rooms:
        found.append((math.hypot(room.cx - x, room.cy - y) + 0.5, room_words(area, room)))
    names = dict.fromkeys(name for _d, name in sorted(found))
    return list(names)[:limit]


def anchor_tile(area: Area, name: str) -> tuple[int, int] | None:
    """The tile a named spot stands for (`anchors_near`'s names)."""
    wanted = key(_bare(name))
    for p in area.placements:
        if key(_bare(p.name)) == wanted:
            return int(p.centre[0]), int(p.centre[1])
    for room in area.rooms:
        if key(_bare(room_words(area, room))) == wanted or key(room.name) == wanted:
            return int(room.cx), int(room.cy)
    return None


def fills_word(ch: str) -> str:
    """A tile's kind in words, for a hover."""
    for word, tile in FILLS.items():
        if tile == ch:
            return word
    return ""
