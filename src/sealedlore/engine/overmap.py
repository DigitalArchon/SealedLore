"""A visual story's wider world, the rules of it (models/overmap.py; the
author, Oct 26 2026; SealedLoreDocs design/visual-stories.md §19-20). Pure:
the grid of cells, which region a point is in, the roads and rivers through
each cell, the way between two points and how long it takes, and a journey
along it: the clock, the camps at night, the food, water and warmth a
traveller needs, and at each stretch the roll for whether anything happens.

Code decides whether something happens (the region's chance an hour,
turned into a chance for the time spent in each stretch) and of what kind
(its split, the danger share half again at night); the keeper, a model,
decides what it is (engine/keeper.py `journey_messages`). The session
applies it all (engine/session_overmap.py).

Kilometres in the overmap, metres everywhere the program measures (as the
map's places are): x east, y north.
"""

from __future__ import annotations

import heapq
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field

from sealedlore.models.overmap import Needs, OverLine, Overmap, OverRegion

KM = 1000.0
# A stretch of a route is a block of cells this many across and down: the
# way is found over blocks (15,625 of them on a 100 x 60 km world), then each
# block walked is a stretch with its own roll.
BLOCK = 2
# What runs through a cell (bits).
ROAD, TRACK, RIVER, CREEK, RAIL = 1, 2, 4, 8, 16
LINE_BITS = {"road": ROAD, "track": TRACK, "river": RIVER, "creek": CREEK, "rail": RAIL}
# Pace by what is underfoot, as a share of the pace on a road.
TRACK_PACE = 0.8
RAIL_PACE = 0.75
# Driving (a vehicle the keeper made to work): metres a second on a road or
# track, and across open country; never across a river off a bridge.
DRIVE_ROAD = 12.0
DRIVE_COUNTRY = 3.0
# Off a bridge, a river is crossed on the ice: slow, and a risk.
RIVER_PACE = 0.3
CREEK_PACE = 0.6
ICE_DANGER = 6.0  # in a hundred, on top of the region's roll
# The day a traveller walks; the rest of it they camp.
DAY_START = 7 * 60 + 30
DAY_END = 17 * 60 + 30
# At night they push on rather than camp when this little of the way is left.
PUSH_ON_SECONDS = 2 * 3600
# Food and water: eaten on the way once this long since the last.
EAT_AFTER = 18 * 3600
DRINK_AFTER = 18 * 3600
# Going without: hurt from this long, a wound a day.
STARVE_AFTER = 48 * 3600
PARCHED_AFTER = 36 * 3600
DAY = 24 * 3600
# Short of warmth, a wound for every this long travelled in the cold.
COLD_HURTS = 6 * 3600
# Without sleep this long, slower.
TIRED_AFTER = 24 * 3600
WEARY_PACE = 0.8
# At camp the roll is at this share of the region's chance an hour.
CAMP_SHARE = 0.5
# The kinds of happening, in the split's order.
KINDS = ("danger", "opportunity", "interest")


# --- the grid --------------------------------------------------------------------


def size(om: Overmap) -> tuple[int, int]:
    """(columns, rows) of cells."""
    return (
        max(1, round((om.east - om.west) / om.cell_w)),
        max(1, round((om.north - om.south) / om.cell_h)),
    )


def cell_of(om: Overmap, x: float, y: float) -> tuple[int, int] | None:
    """The cell (column from the west, row from the north) holding a point
    in metres; None off the overmap."""
    cols, rows = size(om)
    i = math.floor((x / KM - om.west) / om.cell_w)
    j = math.floor((om.north - y / KM) / om.cell_h)
    if not (0 <= i < cols and 0 <= j < rows):
        return None
    return i, j


def clamped_cell(om: Overmap, x: float, y: float) -> tuple[int, int]:
    cols, rows = size(om)
    i = math.floor((x / KM - om.west) / om.cell_w)
    j = math.floor((om.north - y / KM) / om.cell_h)
    return min(max(i, 0), cols - 1), min(max(j, 0), rows - 1)


def cell_key(cell: tuple[int, int]) -> str:
    return f"{cell[0]},{cell[1]}"


def parse_cell_key(text: str) -> tuple[int, int] | None:
    try:
        i, j = (int(p) for p in text.split(","))
    except ValueError:
        return None
    return i, j


def cell_rect(om: Overmap, cell: tuple[int, int]) -> tuple[float, float, float, float]:
    """(west, south, east, north) of a cell, in metres."""
    i, j = cell
    west = (om.west + i * om.cell_w) * KM
    north = (om.north - j * om.cell_h) * KM
    return west, north - om.cell_h * KM, west + om.cell_w * KM, north


def cell_centre(om: Overmap, cell: tuple[int, int]) -> tuple[float, float]:
    west, south, east, north = cell_rect(om, cell)
    return (west + east) / 2, (south + north) / 2


def neighbour(cell: tuple[int, int], side: str) -> tuple[int, int]:
    dx, dy = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}[side]
    return cell[0] + dx, cell[1] + dy


def on_map(om: Overmap, cell: tuple[int, int]) -> bool:
    cols, rows = size(om)
    return 0 <= cell[0] < cols and 0 <= cell[1] < rows


def region_at(om: Overmap, x: float, y: float) -> OverRegion | None:
    """The region at a point in metres: the first whose box holds it."""
    kx, ky = x / KM, y / KM
    for region in om.regions:
        for west, east, south, north in region.boxes:
            if west <= kx < east and south <= ky < north:
                return region
    return om.regions[-1] if om.regions else None


def region_of(om: Overmap, cell: tuple[int, int]) -> OverRegion | None:
    return region_at(om, *cell_centre(om, cell))


def region_named(om: Overmap, key: str) -> OverRegion | None:
    return next((r for r in om.regions if r.key == key), None)


# --- the lines -------------------------------------------------------------------


@dataclass
class Index:
    """What is worked out once per overmap: the bits of what runs through
    each cell (`ROAD`…), and each block's pace and region."""

    om: Overmap
    lines: dict[tuple[int, int], int]
    # Per block: metres a second walking it, its region's key, its bits.
    pace: list[list[float]]
    regions: list[list[str]]
    bits: list[list[int]]
    # Per block with a road or track: the cells of it the road runs through.
    road_cells: dict[tuple[int, int], list[tuple[int, int]]]
    # Per block, driving: metres a second (0 where no vehicle goes).
    drive: list[list[float]]


_INDEXES: dict[int, tuple[Overmap, Index]] = {}


def index_of(om: Overmap) -> Index:
    """The overmap's index, kept while the overmap object is (one per story)."""
    kept = _INDEXES.get(id(om))
    if kept is not None and kept[0] is om:
        return kept[1]
    made = _index(om)
    if len(_INDEXES) > 8:
        _INDEXES.clear()
    _INDEXES[id(om)] = (om, made)
    return made


def _cells_of_line(om: Overmap, line: OverLine) -> list[tuple[int, int]]:
    """The cells a line passes through, in order, each once in a row."""
    out: list[tuple[int, int]] = []
    step = min(om.cell_w, om.cell_h) / 3
    for (ax, ay), (bx, by) in zip(line.points, line.points[1:], strict=False):
        n = max(1, int(math.hypot(bx - ax, by - ay) / step))
        for k in range(n + 1):
            x, y = ax + (bx - ax) * k / n, ay + (by - ay) * k / n
            cell = cell_of(om, x * KM, y * KM)
            if cell is not None and (not out or out[-1] != cell):
                out.append(cell)
    return out


def _index(om: Overmap) -> Index:
    lines: dict[tuple[int, int], int] = {}
    for line in om.lines:
        bit = LINE_BITS.get(line.kind, 0)
        for cell in _cells_of_line(om, line):
            lines[cell] = lines.get(cell, 0) | bit
    cols, rows = size(om)
    bcols, brows = math.ceil(cols / BLOCK), math.ceil(rows / BLOCK)
    road = om.road_kmh * KM / 3600
    pace = [[0.0] * bcols for _ in range(brows)]
    regions = [[""] * bcols for _ in range(brows)]
    bits = [[0] * bcols for _ in range(brows)]
    road_cells: dict[tuple[int, int], list[tuple[int, int]]] = {}
    drive = [[0.0] * bcols for _ in range(brows)]
    for bj in range(brows):
        for bi in range(bcols):
            got = 0
            for dj in range(BLOCK):
                for di in range(BLOCK):
                    cell = (bi * BLOCK + di, bj * BLOCK + dj)
                    here = lines.get(cell, 0)
                    got |= here
                    if here & (ROAD | TRACK):
                        road_cells.setdefault((bi, bj), []).append(cell)
            centre = _block_centre(om, (bi, bj))
            region = region_at(om, *centre)
            regions[bj][bi] = region.key if region is not None else ""
            share = region.pace if region is not None else 0.6
            if got & ROAD:
                share = 1.0
            elif got & TRACK:
                share = max(share, TRACK_PACE)
            elif got & RAIL:
                share = max(share, RAIL_PACE)
            if got & RIVER and not got & ROAD:
                share = min(share, RIVER_PACE)
            elif got & CREEK and not got & (ROAD | TRACK):
                share = min(share, CREEK_PACE)
            pace[bj][bi] = road * share
            bits[bj][bi] = got
            if got & (ROAD | TRACK):
                drive[bj][bi] = DRIVE_ROAD
            elif not got & (RIVER | CREEK):
                drive[bj][bi] = DRIVE_COUNTRY
    return Index(
        om=om,
        lines=lines,
        pace=pace,
        regions=regions,
        bits=bits,
        road_cells=road_cells,
        drive=drive,
    )


def _block_of(om: Overmap, x: float, y: float) -> tuple[int, int]:
    i, j = clamped_cell(om, x, y)
    return i // BLOCK, j // BLOCK


def _block_centre(om: Overmap, block: tuple[int, int]) -> tuple[float, float]:
    """The middle of a block, in metres (of the cells it holds that are on the map)."""
    cols, rows = size(om)
    i0, j0 = block[0] * BLOCK, block[1] * BLOCK
    i1, j1 = min(i0 + BLOCK, cols) - 1, min(j0 + BLOCK, rows) - 1
    a, b = cell_centre(om, (i0, j0)), cell_centre(om, (i1, j1))
    return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2


def lines_at(om: Overmap, cell: tuple[int, int]) -> int:
    return index_of(om).lines.get(cell, 0)


# --- the way ---------------------------------------------------------------------


@dataclass(frozen=True)
class Leg:
    """A stretch of a journey: a block walked across, with the cell it is
    walked through (its middle's), where that is, how long it takes, the
    region, and what is underfoot."""

    cell: tuple[int, int]
    x: float
    y: float
    seconds: int
    region: str
    road: bool = False
    ice: bool = False


_STEPS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def _dijkstra(
    index: Index,
    start: tuple[int, int],
    goal: tuple[int, int] | None = None,
    *,
    drive: bool = False,
) -> tuple[dict[tuple[int, int], float], dict[tuple[int, int], tuple[int, int]]]:
    om = index.om
    grid = index.drive if drive else index.pace
    brows, bcols = len(grid), len(grid[0])
    sx, sy = om.cell_w * KM * BLOCK, om.cell_h * KM * BLOCK
    best: dict[tuple[int, int], float] = {start: 0.0}
    came: dict[tuple[int, int], tuple[int, int]] = {}
    queue = [(0.0, start)]
    while queue:
        cost, here = heapq.heappop(queue)
        if cost > best.get(here, math.inf):
            continue
        if here == goal:
            break
        hi, hj = here
        for dx, dy in _STEPS:
            ni, nj = hi + dx, hj + dy
            if not (0 <= ni < bcols and 0 <= nj < brows):
                continue
            if grid[hj][hi] <= 0 or grid[nj][ni] <= 0:
                continue
            speed = (grid[hj][hi] + grid[nj][ni]) / 2
            step = math.hypot(dx * sx, dy * sy) / speed
            total = cost + step
            if total < best.get((ni, nj), math.inf):
                best[(ni, nj)] = total
                came[(ni, nj)] = here
                heapq.heappush(queue, (total, (ni, nj)))
    return best, came


def route(
    om: Overmap,
    start: tuple[float, float],
    goal: tuple[float, float],
    *,
    slower: float = 1.0,
    drive: bool = False,
) -> list[Leg]:
    """The legs of the quickest way from `start` to `goal` (metres): each
    block crossed, the first the one set out from. `slower`: the traveller's
    pace against a rested one's (weary, hungry: under 1); `drive`: in a
    vehicle (roads fast, country slower, no river crossed off a bridge)."""
    index = index_of(om)
    a, b = _block_of(om, *start), _block_of(om, *goal)
    _best, came = _dijkstra(index, a, b, drive=drive)
    blocks = [b]
    while blocks[-1] != a:
        before = came.get(blocks[-1])
        if before is None:
            return []
        blocks.append(before)
    blocks.reverse()
    legs: list[Leg] = []
    sx, sy = om.cell_w * KM * BLOCK, om.cell_h * KM * BLOCK
    prev_point = start
    for n, block in enumerate(blocks):
        bi, bj = block
        middle = _block_centre(om, block)
        # The cell walked through: on the road where the block has one (the
        # road's cell nearest the block's middle), else the middle's own.
        roads = index.road_cells.get(block)
        cell = (
            min(roads, key=lambda c: math.dist(cell_centre(om, c), middle))
            if roads
            else clamped_cell(om, *middle)
        )
        point = goal if n == len(blocks) - 1 else cell_centre(om, cell)
        metres = math.hypot(point[0] - prev_point[0], point[1] - prev_point[1])
        if n == 0 and len(blocks) > 1:
            metres = max(metres, min(sx, sy) / 2)
        if drive:
            speed = index.drive[bj][bi] or index.pace[bj][bi]
        else:
            speed = index.pace[bj][bi] * max(0.2, slower)
        seconds = int(metres / speed) if speed > 0 else 0
        bits = index.bits[bj][bi]
        if n == len(blocks) - 1:
            cell = clamped_cell(om, *point)
        legs.append(
            Leg(
                cell=cell,
                x=point[0],
                y=point[1],
                seconds=seconds,
                region=index.regions[bj][bi],
                road=bool(bits & (ROAD | TRACK)),
                ice=bool(bits & RIVER) and not bits & ROAD,
            )
        )
        prev_point = point
    return legs


def times_from(
    om: Overmap, start: tuple[float, float], *, drive: bool = False
) -> dict[tuple[int, int], float]:
    """Seconds on foot (or driving) from `start` to every block (rested, no
    camps): the way to every known place at once, for the overmap's estimates."""
    index = index_of(om)
    best, _came = _dijkstra(index, _block_of(om, *start), drive=drive)
    return best


def walking_seconds(om: Overmap, times: dict[tuple[int, int], float], x: float, y: float) -> int:
    return int(times.get(_block_of(om, x, y), math.inf)) if times else 0


# --- a journey -------------------------------------------------------------------


def minute_of(seconds: int, start_minute: int) -> int:
    """The minute of the day at `seconds` on the clock."""
    return (start_minute + seconds // 60) % (24 * 60)


def is_night(minute: int) -> bool:
    return minute < DAY_START or minute >= DAY_END


def until_morning(minute: int) -> int:
    """Seconds from `minute` to the next DAY_START."""
    return ((DAY_START - minute) % (24 * 60)) * 60


def roll_chance(chance: float, seconds: float) -> float:
    """The chance (0-1) that something happens in `seconds` at `chance` in a
    hundred an hour."""
    hours = max(0.0, seconds) / 3600
    return 1 - (1 - max(0.0, min(chance, 100.0)) / 100) ** hours


def weights(region: OverRegion | None, *, night: bool, asleep: bool) -> tuple[float, float, float]:
    """The split by kind: the danger share half again at night and the rest
    half; asleep, no opportunity at all."""
    split = region.split if region is not None else (40, 30, 30)
    danger, opportunity, interest = (float(v) for v in split)
    if night:
        danger, opportunity, interest = danger * 1.5, opportunity * 0.5, interest * 0.5
    if asleep:
        opportunity = 0.0
    return danger, opportunity, interest


def pick_kind(rng: random.Random, split: tuple[float, float, float]) -> str:
    total = sum(split)
    if total <= 0:
        return "interest"
    roll = rng.random() * total
    for kind, share in zip(KINDS, split, strict=True):
        roll -= share
        if roll < 0:
            return kind
    return KINDS[-1]


@dataclass
class Stop:
    """Where a journey was stopped, and why: something of this kind
    happened at this leg (`camping`: at the night's camp there)."""

    kind: str
    leg: int
    region: str
    night: bool
    camping: bool = False
    ice: bool = False


@dataclass
class Journey:
    """What came of setting out: the legs walked (the last the one it ended
    in), the time it took, what was eaten and drunk, the wounds, the lines
    for the log, and how it ended (arrived, stopped, or dead)."""

    legs: int = 0
    seconds: int = 0
    eaten: int = 0
    drunk: int = 0
    wounds: int = 0
    camps: int = 0
    lines: list[str] = field(default_factory=list)
    stop: Stop | None = None
    arrived: bool = False
    dead: bool = False
    needs: Needs = field(default_factory=Needs)


@dataclass
class Traveller:
    """Who sets out: the clock now, the day's minute at the clock's start,
    what they last had, what they carry of food, water and warmth, and how
    hurt they are."""

    seconds: int
    start_minute: int
    needs: Needs
    food: int = 0
    drink: int = 0
    warm: int = 0
    wounds: int = 0
    max_wounds: int = 3
    regions: dict[str, OverRegion] = field(default_factory=dict)


def _needs_now(t: Traveller, j: Journey, now: int, words: list[str]) -> None:
    """Eat and drink when it is time and there is something; hunger and
    thirst hurt past the worst, a wound a day."""
    needs = j.needs
    if now - needs.ate >= EAT_AFTER and t.food > 0:
        t.food -= 1
        j.eaten += 1
        needs.ate = now
        words.append("You eat on the way.")
    if now - needs.drank >= DRINK_AFTER and t.drink > 0:
        t.drink -= 1
        j.drunk += 1
        needs.drank = now
        words.append("You drink on the way.")
    starving = now - needs.ate - STARVE_AFTER
    if starving >= 0:
        due = starving // DAY + 1
        while needs.starved < due:
            needs.starved += 1
            j.wounds += 1
            words.append("Hunger takes the strength out of you.")
    else:
        needs.starved = 0
    parched = now - needs.drank - PARCHED_AFTER
    if parched >= 0:
        due = parched // DAY + 1
        while needs.parched < due:
            needs.parched += 1
            j.wounds += 1
            words.append("Thirst is wearing you down.")
    else:
        needs.parched = 0


def journey(
    legs: Sequence[Leg],
    traveller: Traveller,
    rng: random.Random,
    *,
    place_name: str = "",
) -> Journey:
    """Walk the legs from the traveller's moment: camping by night (pushing
    on when little is left), eating and drinking on the way, the cold and
    hunger hurting, and at each leg (and each camp) the roll. Stops at the
    first thing that happens, or when the wounds kill; else arrives."""
    j = Journey(needs=traveller.needs.model_copy())
    now = traveller.seconds
    left = sum(leg.seconds for leg in legs)
    for n, leg in enumerate(legs):
        region = traveller.regions.get(leg.region)
        minute = minute_of(now, traveller.start_minute)
        if is_night(minute) and left > PUSH_ON_SECONDS and n == 0:
            # Setting out at night with far to go: they wait for first light
            # where they are (still the place they set out from: no roll).
            now += until_morning(minute)
            j.needs.slept = now
            j.lines.append("You wait for first light before setting out.")
            minute = minute_of(now, traveller.start_minute)
        if is_night(minute) and left > PUSH_ON_SECONDS and n > 0:
            # Night: camp here until morning, with its own roll.
            wait = until_morning(minute)
            name = region.name if region is not None else "the open"
            j.lines.append(f"Night falls; you make camp in {name}.")
            j.camps += 1
            chance = (region.chance if region is not None else 5.0) * CAMP_SHARE
            if rng.random() < roll_chance(chance, wait):
                hit = rng.random() * wait
                now += int(hit)
                j.seconds = now - traveller.seconds
                j.legs = n
                kind = pick_kind(rng, weights(region, night=True, asleep=True))
                j.stop = Stop(kind=kind, leg=n, region=leg.region, night=True, camping=True)
                return j
            now += wait
            j.needs.slept = now
            if traveller.warm < 1:
                j.wounds += 1
                j.lines.append("A bitter night with nothing warm: the cold gets into you.")
        words: list[str] = []
        _needs_now(traveller, j, now, words)
        j.lines += words
        if traveller.wounds + j.wounds >= traveller.max_wounds:
            j.dead = True
            j.legs = n
            j.seconds = now - traveller.seconds
            return j
        seconds = leg.seconds
        if now - j.needs.slept >= TIRED_AFTER:
            seconds = int(seconds / WEARY_PACE)
        need = region.warm if region is not None else 1
        cold = traveller.warm < need
        night = is_night(minute_of(now, traveller.start_minute))
        chance = region.chance if region is not None else 5.0
        # No roll in the stretch set out from: that is still the place left.
        hit = n > 0 and rng.random() < roll_chance(chance, seconds)
        ice = n > 0 and leg.ice and rng.random() < ICE_DANGER / 100
        if hit or ice:
            part = int(seconds * rng.random())
            now += part
            if cold:
                j.needs.cold += part
            j.seconds = now - traveller.seconds
            j.legs = n
            kind = "danger" if ice else pick_kind(rng, weights(region, night=night, asleep=False))
            j.stop = Stop(kind=kind, leg=n, region=leg.region, night=night, ice=ice)
            return j
        now += seconds
        left -= leg.seconds
        if cold:
            j.needs.cold += seconds
            while j.needs.cold >= COLD_HURTS:
                j.needs.cold -= COLD_HURTS
                j.wounds += 1
                j.lines.append("You aren't dressed for this cold: it gets into you.")
        if traveller.wounds + j.wounds >= traveller.max_wounds:
            j.dead = True
            j.legs = n
            j.seconds = now - traveller.seconds
            return j
    j.legs = max(0, len(legs) - 1)
    j.seconds = now - traveller.seconds
    j.arrived = True
    if place_name:
        j.lines.append(f"You reach {place_name}.")
    return j


@dataclass(frozen=True)
class Estimate:
    """What the overmap tells before setting out: the walking time, the
    nights out, the regions crossed (by name, in order), the food and
    water it will take, and the warmth the coldest of them needs."""

    seconds: int
    walking: int
    nights: int
    regions: list[str]
    food: int
    drink: int
    warm: int
    road_share: float


def estimate(
    om: Overmap, legs: Sequence[Leg], seconds: int, start_minute: int, needs: Needs
) -> Estimate:
    """A dry run of the journey with nothing happening on the way."""
    regions: list[str] = []
    for leg in legs:
        region = region_named(om, leg.region)
        if region is not None and region.name not in regions:
            regions.append(region.name)
    warm = max((r.warm for r in om.regions if r.name in regions), default=1)
    traveller = Traveller(
        seconds=seconds,
        start_minute=start_minute,
        needs=needs.model_copy(),
        food=99,
        drink=99,
        warm=99,
        max_wounds=999,
        regions={r.key: r.model_copy(update={"chance": 0.0}) for r in om.regions},
    )
    done = journey(legs, traveller, random.Random(0))
    walking = sum(leg.seconds for leg in legs)
    road = sum(leg.seconds for leg in legs if leg.road)
    return Estimate(
        seconds=done.seconds,
        walking=walking,
        nights=done.camps,
        regions=regions,
        food=done.eaten,
        drink=done.drunk,
        warm=warm,
        road_share=road / walking if walking else 0.0,
    )
