"""Cells of a visual story's overmap, made the first time they are entered
(models/overmap.py; the author, Oct 26 2026: any part of the map can be
explored, "dynamically generated ... based on the general instructions of
the overmap as to what that region contains"). Pure.

A cell is a place of the map with a plan, as the plot file's places are, so
everything the play view does works on it: walking, doors into its
buildings (their insides made from the world kit), things to take, the
dead. Its plan is laid out by the program from what the overmap says is
there (the region's ground and cover, the roads, rivers and rail line
running through it) and an outdoor template of the kit (a farmstead, a
campsite, a crash site…), seeded by the story and the cell, so the same
playthrough always finds the same country.

Its area shows the whole cell (400 x 240 m on the overmap) drawn at the
size of the plot file's farm (240 x 144 m, 2 m a tile), so neighbouring
cells meet edge to edge on the overmap and every area is at one scale.
"""

from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

from sealedlore.engine.kits import rng_for
from sealedlore.engine.overmap import (
    cell_centre,
    cell_key,
    cell_rect,
    region_of,
)
from sealedlore.models.kit import OutBuilding, OutdoorTemplate, OutObject
from sealedlore.models.overmap import OverLine, Overmap, OverRegion
from sealedlore.models.world import PlaceLayout, WorldPlace

CELL_PREFIX = "cell:"
# A cell's area across, in metres, and its plan's grid (6 m a plan tile).
AREA_M = 240.0
COLUMNS, ROWS = 40, 24
PLAN_TILE = AREA_M / COLUMNS
# Buildings keep this many plan tiles clear of the edge and of each other.
EDGE_MARGIN = 2
# The ground's cover by kind (engine/area.py `dress`): (tile, noise scale,
# threshold, density) for each, in order.
COVERS: dict[str, list[tuple[str, float, float, float]]] = {
    "open": [("T", 9, 0.74, 0.35), (",", 5, 0.66, 0.35), ("^", 4, 0.88, 0.3)],
    "sparse": [("T", 9, 0.62, 0.55), (",", 5, 0.66, 0.35), ("^", 4, 0.80, 0.4)],
    "woods": [("T", 7, 0.44, 0.75), (",", 4, 0.55, 0.45), ("^", 5, 0.86, 0.3)],
    "forest": [("T", 6, 0.34, 0.85), (",", 4, 0.50, 0.45), ("^", 6, 0.88, 0.3)],
    "marsh": [("~", 6, 0.58, 0.85), (",", 3, 0.38, 0.7), ("T", 9, 0.82, 0.3)],
    "rocky": [("^", 5, 0.50, 0.7), ("T", 8, 0.60, 0.5), (",", 4, 0.62, 0.35)],
    "rubble": [(":", 4, 0.48, 0.6), ("^", 6, 0.80, 0.35), (",", 5, 0.75, 0.3)],
    "scrub": [(",", 4, 0.45, 0.6), ("T", 8, 0.70, 0.45), ("^", 5, 0.86, 0.3)],
}
TERRAIN_COVER = {
    "fields": "open",
    "woods": "forest",
    "hills": "rocky",
    "marsh": "marsh",
    "plains": "open",
    "ruins": "rubble",
    "scrub": "scrub",
    "lake": "open",
}
# What open country is called where a template gives no name, by terrain.
TERRAIN_NAMES = {
    "fields": "Snowbound fields",
    "woods": "Pine woods",
    "hills": "Rocky hillside",
    "marsh": "Frozen marsh",
    "plains": "Empty farmland",
    "ruins": "Burned streets",
    "scrub": "Scrubland",
    "lake": "A frozen shore",
}
# Widths in plan tiles, by line.
ROAD_WIDTH = {"highway": 2}
RIVER_WIDTH = {"river": 2, "creek": 1}


def cell_place_id(cell: tuple[int, int]) -> str:
    return CELL_PREFIX + cell_key(cell)


def is_cell(place_id: str | None) -> bool:
    return bool(place_id) and str(place_id).startswith(CELL_PREFIX)


def cell_of_place(place_id: str | None) -> tuple[int, int] | None:
    """The cell a cell's place (or anything in it) is made for."""
    if not is_cell(place_id):
        return None
    head = str(place_id)[len(CELL_PREFIX) :].split(":")[0].split("#")[0]
    try:
        i, j = (int(p) for p in head.split(","))
    except ValueError:
        return None
    return i, j


@dataclass
class MadeCell:
    """A cell laid out: its place (the area's), the places on it (its
    buildings), its plan, and what is laid once the area is built (things,
    loot), with the dead that wander it."""

    key: str
    template: str
    place: WorldPlace
    children: list[WorldPlace]
    layout: PlaceLayout
    objects: list[OutObject] = field(default_factory=list)
    loot: list[tuple[str, int]] = field(default_factory=list)
    dead: int = 0
    # Someone lives here, holds it or has stopped here (the template's `people`).
    lived_in: bool = False


def choose_template(region: OverRegion | None, seed: str, cell: tuple[int, int]) -> str:
    """What exploring finds in a cell of this region, by the region's
    weights; "" for plain country."""
    finds = [(k, w) for k, w in (region.finds if region else []) if w > 0]
    if not finds:
        return ""
    rng = rng_for(seed, "cell", cell_key(cell))
    total = sum(w for _k, w in finds)
    pick = rng.uniform(0, total)
    for key, weight in finds:
        pick -= weight
        if pick <= 0:
            return key
    return finds[-1][0]


# --- where things are on the plan ---------------------------------------------------


def _to_plan(rect: tuple[float, float, float, float], x: float, y: float) -> tuple[float, float]:
    west, south, east, north = rect
    return (x - west) / (east - west) * COLUMNS, (north - y) / (north - south) * ROWS


def _runs(om: Overmap, line: OverLine, rect) -> list[list[tuple[float, float]]]:
    """The line on the cell's plan: runs of points (plan tiles) that lie on
    it or just past its edge, so a road runs out of the area."""
    runs: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    pad = 1.5
    for (ax, ay), (bx, by) in zip(line.points, line.points[1:], strict=False):
        a = _to_plan(rect, ax * 1000, ay * 1000)
        b = _to_plan(rect, bx * 1000, by * 1000)
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 0.25))
        for k in range(n + 1):
            x, y = a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n
            inside = -pad <= x <= COLUMNS + pad and -pad <= y <= ROWS + pad
            if inside:
                current.append((x, y))
            elif current:
                runs.append(current)
                current = []
    if current:
        runs.append(current)
    return [r for r in runs if len(r) > 1]


def _int_points(run: Sequence[tuple[float, float]], every: int = 4) -> list[list[int]]:
    out: list[list[int]] = []
    for n, (x, y) in enumerate(run):
        if n % every and n != len(run) - 1:
            continue
        point = [round(x), round(y)]
        if not out or out[-1] != point:
            out.append(point)
    return out


class _Taken:
    """The plan's tiles already spoken for (a road, water, a building)."""

    def __init__(self) -> None:
        self.tiles: set[tuple[int, int]] = set()

    def mark(self, x: int, y: int, w: int = 1, h: int = 1, margin: int = 0) -> None:
        for yy in range(y - margin, y + h + margin):
            for xx in range(x - margin, x + w + margin):
                self.tiles.add((xx, yy))

    def free(self, x: int, y: int, w: int, h: int, margin: int = 0) -> bool:
        if x - margin < 0 or y - margin < 0 or x + w + margin > COLUMNS or y + h + margin > ROWS:
            return False
        return not any(
            (xx, yy) in self.tiles
            for yy in range(y - margin, y + h + margin)
            for xx in range(x - margin, x + w + margin)
        )


def _box(size: float) -> tuple[int, int]:
    """A building's box in plan tiles, as `world_art.fixed_in_plan` draws it."""
    w = min(max(4, round(size / PLAN_TILE)), COLUMNS)
    h = min(max(3, round(w * ROWS / COLUMNS)), ROWS)
    return w, h


def _spot_near(
    taken: _Taken,
    w: int,
    h: int,
    want: tuple[float, float],
    rng: random.Random,
    margin: int = 1,
) -> tuple[int, int] | None:
    """The free top-left nearest `want` (a little jitter, so two cells of
    one template differ)."""
    best = None
    for y in range(EDGE_MARGIN, ROWS - h - EDGE_MARGIN + 1):
        for x in range(EDGE_MARGIN, COLUMNS - w - EDGE_MARGIN + 1):
            if not taken.free(x, y, w, h, margin):
                continue
            d = math.hypot(x + w / 2 - want[0], y + h / 2 - want[1]) + rng.random() * 2
            if best is None or d < best[0]:
                best = (d, x, y)
    return (best[1], best[2]) if best is not None else None


def _the(name: str) -> str:
    """A building's name as a place's: "farmhouse" is "The farmhouse"."""
    name = " ".join(name.split())
    low = name.lower()
    if low.startswith(("the ", "a ", "an ")) or (name[:1].isupper() and " " not in name):
        return name[:1].upper() + name[1:]
    return "The " + name


def make_cell(
    om: Overmap,
    cell: tuple[int, int],
    seed: str,
    *,
    template_key: str = "",
    template: OutdoorTemplate | None = None,
    name: str = "",
    what: str = "",
    made_at: str = "",
) -> MadeCell:
    """The cell's place, its buildings and its plan (the rest is laid once
    the area is built: `lay_pieces`)."""
    region = region_of(om, cell)
    rng = rng_for(seed, "lay", cell_key(cell))
    rect = cell_rect(om, cell)
    cx, cy = cell_centre(om, cell)
    terrain = region.terrain if region is not None else "fields"
    cover = (template.cover if template and template.cover else "") or TERRAIN_COVER.get(
        terrain, "open"
    )
    place_id = cell_place_id(cell)
    title = (
        name
        or (template.name if template is not None else "")
        or TERRAIN_NAMES.get(terrain, "Open country")
    )
    words = " ".join(
        w
        for w in (what or (template.what if template else ""), region.words if region else "")
        if w
    )
    place = WorldPlace(
        id=place_id,
        name=title,
        kind="site" if template is not None and template.buildings else "area",
        what=words[:400],
        x=cx,
        y=cy,
        size=AREA_M,
    )
    features: list[dict] = []
    taken = _Taken()
    # What runs through it: rivers and creeks as water, the rail bed as
    # gravel, roads over all (a road over water is a bridge).
    roads: list[dict] = []
    near_road: list[tuple[int, int]] = []
    water_tiles: list[tuple[int, int]] = []
    for line in om.lines:
        for run in _runs(om, line, rect):
            if line.kind in ("river", "creek"):
                width = RIVER_WIDTH.get(line.kind, 1)
                for x, y in {(round(x), round(y)) for x, y in run}:
                    features.append(
                        {
                            "kind": "area",
                            "fill": "ice",
                            "x": x - width // 2,
                            "y": y - width // 2,
                            "w": width,
                            "h": width,
                        }
                    )
                    taken.mark(x - width // 2, y - width // 2, width, width, margin=1)
                    water_tiles.append((x, y))
            elif line.kind == "rail":
                for x, y in {(round(x), round(y)) for x, y in run}:
                    features.append(
                        {"kind": "area", "fill": "rubble", "x": x, "y": y, "w": 1, "h": 1}
                    )
                    taken.mark(x, y, margin=1)
            else:
                width = 2 if any(w in line.name.lower() for w in ROAD_WIDTH) else 1
                points = _int_points(run)
                roads.append({"kind": "road", "points": points, "width": width})
                for x, y in {(round(x), round(y)) for x, y in run}:
                    taken.mark(x, y, margin=width)
                    near_road.append((x, y))
    # Water the template asks for.
    if template is not None and template.water:
        _water(template.water, features, taken, rng, water_tiles)
    children: list[WorldPlace] = []
    boxes: list[tuple[int, int, int, int]] = []
    if template is not None:
        want = (COLUMNS / 2, ROWS / 2)
        if near_road:
            # Beside the road, not on it: the middle of the road's run.
            rx, ry = near_road[len(near_road) // 2]
            want = (rx + (4 if rx < COLUMNS / 2 else -4), ry + (4 if ry < ROWS / 2 else -4))
        for n, building in enumerate(b for b in template.buildings if _rolled(rng, b)):
            w, h = _box(building.size)
            spot = _spot_near(taken, w, h, want, rng, margin=1)
            if spot is None:
                continue
            x, y = spot
            taken.mark(x, y, w, h, margin=2)
            boxes.append((x, y, w, h))
            children.append(_building_place(place_id, n, building, rect, x, y, w, h))
            if n == 0:
                want = (x + w / 2, y + h / 2)
        if template.fence and boxes:
            features += _fence(boxes, near_road, taken)
        for _ in range(max(0, template.fields)):
            features += _field(boxes, taken, rng)
        if template.wall:
            features += _ring(template.wall, near_road)
    spec = {
        "ground": region.ground if region is not None else "snow",
        "features": features + roads,
        "cover": [list(c) for c in COVERS.get(cover, COVERS["open"])],
        "cell": cell_key(cell),
    }
    layout = PlaceLayout(
        place_id=place_id,
        spec=spec,
        rows=["." * COLUMNS] * ROWS,
        model="",
        made_at=made_at or f"cell:{cell_key(cell)}",
    )
    dead = 0
    if region is not None and region.dead > 0:
        dead = sum(1 for _ in range(4) if rng.random() < region.dead / 4)
    # Rolled last, so a cell made before this roll was added lays out the same.
    lived_in = template is not None and template.people > 0 and rng.random() < template.people / 100
    if lived_in and template.lived and not what:
        # Someone lives here: the place's own words say so, not the
        # template's deserted ones (the region's words kept after).
        place.what = " ".join(w for w in (template.lived, region.words if region else "") if w)[
            :400
        ]
    return MadeCell(
        key=cell_key(cell),
        template=template_key,
        place=place,
        children=children,
        layout=layout,
        objects=list(template.objects) if template is not None else [],
        loot=list(template.loot) if template is not None else [],
        dead=dead,
        lived_in=lived_in,
    )


def _rolled(rng: random.Random, building: OutBuilding) -> bool:
    return building.chance >= 100 or rng.randint(1, 100) <= building.chance


def _building_place(
    place_id: str,
    n: int,
    building: OutBuilding,
    rect: tuple[float, float, float, float],
    x: int,
    y: int,
    w: int,
    h: int,
) -> WorldPlace:
    """A building as a place of the map, at the box its plan gives it: its
    spot worked back from `fixed_in_plan`'s box (the area's plan is the
    cell's middle, AREA_M across)."""
    west, south, east, north = rect
    cx, cy = (west + east) / 2, (south + north) / 2
    plan_west = cx - AREA_M / 2
    plan_north = cy + AREA_M * ROWS / COLUMNS / 2
    size = w * PLAN_TILE
    return WorldPlace(
        id=f"{place_id}:b{n}",
        name=_the(building.name),
        kind="building",
        inside=place_id,
        what=building.what,
        state=building.state,
        x=plan_west + x * PLAN_TILE + size / 2,
        y=plan_north - y * PLAN_TILE - size * ROWS / COLUMNS / 2,
        size=size,
    )


def _water(
    kind: str,
    features: list[dict],
    taken: _Taken,
    rng: random.Random,
    water_tiles: list[tuple[int, int]],
) -> None:
    if kind == "lake":
        w, h = rng.randint(18, 26), rng.randint(9, 13)
        x = rng.choice((1, COLUMNS - w - 1))
        y = rng.randint(2, ROWS - h - 2)
        shapes = [(x, y, w, h)]
    elif kind == "pond":
        shapes = [(rng.randint(4, COLUMNS - 12), rng.randint(3, ROWS - 9), 7, 5)]
    else:  # pools
        shapes = [
            (rng.randint(2, COLUMNS - 8), rng.randint(2, ROWS - 6), rng.randint(3, 6), 3)
            for _ in range(rng.randint(3, 5))
        ]
    for x, y, w, h in shapes:
        if not taken.free(x, y, w, h):
            continue
        features.append(
            {"kind": "area", "fill": "ice", "shape": "ellipse", "x": x, "y": y, "w": w, "h": h}
        )
        taken.mark(x, y, w, h, margin=1)
        water_tiles.append((x + w // 2, y + h // 2))


def _side_toward(box: tuple[int, int, int, int], points: Sequence[tuple[int, int]]) -> str:
    x0, y0, x1, y1 = box
    if not points:
        return "south"
    px, py = min(points, key=lambda p: math.hypot(p[0] - (x0 + x1) / 2, p[1] - (y0 + y1) / 2))
    dx, dy = px - (x0 + x1) / 2, py - (y0 + y1) / 2
    if abs(dx) > abs(dy):
        return "east" if dx > 0 else "west"
    return "south" if dy > 0 else "north"


def _fence(
    boxes: Sequence[tuple[int, int, int, int]], roads: Sequence[tuple[int, int]], taken: _Taken
) -> list[dict]:
    """A fence round the buildings, its gate toward the road (else south)."""
    x0 = max(1, min(b[0] for b in boxes) - 2)
    y0 = max(1, min(b[1] for b in boxes) - 2)
    x1 = min(COLUMNS - 2, max(b[0] + b[2] for b in boxes) + 1)
    y1 = min(ROWS - 2, max(b[1] + b[3] for b in boxes) + 1)
    side = _side_toward((x0, y0, x1, y1), roads)
    mx, my = (x0 + x1) // 2, (y0 + y1) // 2
    gate = {
        "south": [[mx, y1], [mx + 1, y1]],
        "north": [[mx, y0], [mx + 1, y0]],
        "east": [[x1, my], [x1, my + 1]],
        "west": [[x0, my], [x0, my + 1]],
    }[side]
    for x in range(x0, x1 + 1):
        taken.mark(x, y0)
        taken.mark(x, y1)
    for y in range(y0, y1 + 1):
        taken.mark(x0, y)
        taken.mark(x1, y)
    return [
        {
            "kind": "fence",
            "points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]],
            "gaps": gate,
        }
    ]


def _field(
    boxes: Sequence[tuple[int, int, int, int]], taken: _Taken, rng: random.Random
) -> list[dict]:
    """A fenced field near the buildings, a gap in its fence."""
    w, h = rng.randint(7, 10), rng.randint(4, 6)
    want = (COLUMNS / 2, ROWS / 2)
    if boxes:
        bx, by, bw, bh = boxes[0]
        want = (bx + bw + w / 2 + 2, by + bh / 2)
    spot = _spot_near(taken, w, h, want, rng, margin=1)
    if spot is None:
        return []
    x, y = spot
    taken.mark(x, y, w, h, margin=1)
    x1, y1 = x + w - 1, y + h - 1
    return [
        {
            "kind": "fence",
            "points": [[x, y], [x1, y], [x1, y1], [x, y1], [x, y]],
            "gaps": [[x + w // 2, y1]],
        }
    ]


def _ring(kind: str, roads: Sequence[tuple[int, int]]) -> list[dict]:
    """A wall round the whole place (a stronghold's containers, a hamlet's
    stockade), a way through wherever a road crosses it, and a gate south."""
    x0, y0, x1, y1 = 3, 2, COLUMNS - 4, ROWS - 3
    gaps: list[list[int]] = [[COLUMNS // 2, y1], [COLUMNS // 2 + 1, y1]]
    for x, y in roads:
        if x in (x0, x1) or y in (y0, y1):
            gaps += [[x + dx, y + dy] for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
    return [
        {
            "kind": "fence" if kind == "barricade" else "wall",
            "points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]],
            "gaps": gaps,
        }
    ]


# --- what is laid once the area is built --------------------------------------------

# Ground a thing may stand on out of doors (open ground, brush, sand, rubble, road).
OPEN = frozenset(".,s:=")
WALKABLE = frozenset("._=+,:s&b")


def _reachable(
    rows: Sequence[str], start: tuple[int, int], blocked: Collection[tuple[int, int]]
) -> int:
    h, w = len(rows), len(rows[0])
    if not (0 <= start[0] < w and 0 <= start[1] < h) or start in blocked:
        return 0
    seen = {start}
    todo = deque([start])
    while todo:
        x, y = todo.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if (
                0 <= nx < w
                and 0 <= ny < h
                and (nx, ny) not in seen
                and (nx, ny) not in blocked
                and rows[ny][nx] in WALKABLE
            ):
                seen.add((nx, ny))
                todo.append((nx, ny))
    return len(seen)


def lay_pieces(
    rows: Sequence[str],
    objects: Sequence[OutObject],
    loot: Sequence[tuple[str, int]],
    tile_m: float,
    seed: str,
    key: str,
    *,
    buildings: Sequence[tuple[int, int, int, int]] = (),
    keep: Collection[tuple[int, int]] = (),
) -> tuple[list[dict], list[dict]]:
    """The template's things and loot on the built area's ground (tiles):
    each where it belongs (by the road, the buildings, the water, the
    middle), never in front of a door (`keep`), never cutting ground off.
    (pieces, loot) in the shapes a kit's are stored in."""
    rng = rng_for(seed, "pieces", key)
    h, w = len(rows), len(rows[0]) if rows else 0
    if not w:
        return [], []
    # What stands (for what can be reached) and what is kept clear besides
    # (the gap round each thing, in front of doors: for where things go).
    solid: set[tuple[int, int]] = set()
    blocked: set[tuple[int, int]] = set(keep)
    middle = (w // 2, h // 2)
    start = min(
        ((x, y) for y in range(h) for x in range(w) if rows[y][x] in WALKABLE),
        key=lambda p: abs(p[0] - middle[0]) + abs(p[1] - middle[1]),
        default=None,
    )
    if start is None:
        return [], []
    reach = _reachable(rows, start, solid)
    roads = [(x, y) for y in range(h) for x in range(w) if rows[y][x] == "="]
    water = [(x, y) for y in range(h) for x in range(w) if rows[y][x] == "~"]
    near_buildings = [((x0 + x1) // 2, (y0 + y1) // 2) for x0, y0, x1, y1 in buildings]
    pieces: list[dict] = []

    def target(near: str) -> tuple[int, int]:
        pool = {
            "road": roads,
            "water": water,
            "buildings": near_buildings,
        }.get(near)
        if pool:
            return rng.choice(pool)
        if near == "middle":
            return middle
        return rng.randrange(4, w - 4), rng.randrange(4, h - 4)

    for obj in objects:
        if obj.chance < 100 and rng.randint(1, 100) > obj.chance:
            continue
        count = obj.count if isinstance(obj.count, int) else rng.randint(*obj.count)
        pw = max(1, round(obj.w / max(tile_m, 0.1)))
        ph = max(1, round(obj.h / max(tile_m, 0.1)))
        for _ in range(max(0, count)):
            want = target(obj.near)
            spot = _free_spot(rows, blocked, want, pw, ph, rng)
            if spot is None:
                continue
            fp = {(spot[0] + dx, spot[1] + dy) for dy in range(ph) for dx in range(pw)}
            # Reached from the middle, or from beside the thing when it
            # stands on the middle (a crashed helicopter laid "middle" was
            # always refused: its own tiles were where the reach began).
            under = solid | fp
            origin = start if start not in under else _beside(rows, under, under)
            now = _reachable(rows, origin, under) if origin is not None else 0
            if now < reach - len(fp):
                continue  # it would cut ground off
            reach = now
            solid |= fp
            blocked |= fp
            # A step clear round it, so things don't stand shoulder to shoulder.
            blocked |= {
                (x + dx, y + dy) for x, y in fp for dx in (-1, 0, 1) for dy in (-1, 0, 1)
            } - fp
            piece = {"name": obj.name, "x": spot[0], "y": spot[1], "w": pw, "h": ph}
            if obj.stock:
                piece["stock"] = obj.stock
                piece["chance"] = obj.stock_chance
            pieces.append(piece)
    loot_out: list[dict] = []
    for table, chance in loot:
        for _ in range(2):
            want = target("buildings" if near_buildings else "middle")
            spot = _free_spot(rows, blocked, want, 1, 1, rng)
            if spot is not None:
                loot_out.append({"x": spot[0], "y": spot[1], "table": table, "chance": chance})
    return pieces, loot_out


def _beside(
    rows: Sequence[str], fp: Collection[tuple[int, int]], solid: Collection[tuple[int, int]]
) -> tuple[int, int] | None:
    """A walkable tile beside a footprint, not under anything."""
    h, w = len(rows), len(rows[0])
    for x, y in sorted(fp):
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if (
                0 <= nx < w
                and 0 <= ny < h
                and (nx, ny) not in fp
                and (nx, ny) not in solid
                and rows[ny][nx] in WALKABLE
            ):
                return nx, ny
    return None


def _free_spot(
    rows: Sequence[str],
    blocked: Collection[tuple[int, int]],
    want: tuple[int, int],
    pw: int,
    ph: int,
    rng: random.Random,
) -> tuple[int, int] | None:
    h, w = len(rows), len(rows[0])
    best = None
    reach = 14
    for y in range(max(1, want[1] - reach), min(h - ph - 1, want[1] + reach) + 1):
        for x in range(max(1, want[0] - reach), min(w - pw - 1, want[0] + reach) + 1):
            if any(
                rows[y + dy][x + dx] not in OPEN or (x + dx, y + dy) in blocked
                for dy in range(ph)
                for dx in range(pw)
            ):
                continue
            d = abs(x - want[0]) + abs(y - want[1]) + rng.random() * 3
            if best is None or d < best[0]:
                best = (d, x, y)
    return (best[1], best[2]) if best is not None else None
