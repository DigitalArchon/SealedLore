"""Where each place sits on the map (engine/world.py): pure and deterministic.

A place is placed once, when it first appears, and never moved: the story
has been there, so that is where it is. That is also how the map grows past
its edge: a journey into unmapped country brings new places, placed beyond
the old ones, and they are as fixed as the rest from then on.

Positions are metres east (x) and north (y) of the first place. What the
story said decides them, in this order:

1. a point the author marked on the map (`at`: a mark in their turn);
2. a link to a place already placed: its direction and distance, as the text
   gave them ("five miles north", "two hours on foot", "around back"). A
   link to the place's own container ("a rift above the forest") is not
   one: being inside says where it is;
3. a place it is inside: somewhere within its parent, as near the author's
   character as there is room when they are in or at it (a forest 16 km
   across catching fire "across the river" from the village burns by the
   village, not 6 km off);
4. otherwise beside where the author's character is, or the last place.

A vehicle is the exception: it moves, so it has no spot of its own, nor
has anything inside it. It is wherever the story last put it (`inside`: the
bay it docked in), and is drawn there (`position`).

Nothing the text didn't say is invented: a link with no direction takes the
first free bearing, one with no distance sits beside its neighbour. Places
never overlap their siblings.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from sealedlore.engine.chronicle_read import COUNT_PATTERN, count_of
from sealedlore.models.world import WorldLink, WorldMap, WorldPlace

# How wide a place of each kind is drawn and kept apart from its neighbours,
# in metres. A plausible footprint, not a measurement.
KIND_SIZE: dict[str | None, float] = {
    "region": 40_000.0,
    "settlement": 1_500.0,
    "site": 300.0,
    "area": 120.0,
    "route": 60.0,
    "building": 40.0,
    "vehicle": 25.0,
    "room": 8.0,
    None: 100.0,
}
# Children are drawn within their parent, never wider than this share of it.
CHILD_SHARE = 0.45
# A place's plan is drawn as a rectangle this many rows to its columns
# (world_art.PLAN_ASPECT, 24/40): a child placed in the parent must lie in
# that rectangle, not just the parent's square, or it falls off the plan.
PLAN_ASPECT = 0.6

_LENGTH_UNITS: dict[str, float] = {
    "millimetre": 0.001,
    "centimetre": 0.01,
    "inch": 0.0254,
    "foot": 0.3048,
    "feet": 0.3048,
    "pace": 0.75,
    "step": 0.75,
    "yard": 0.9144,
    "metre": 1.0,
    "meter": 1.0,
    "km": 1_000.0,
    "kilometre": 1_000.0,
    "kilometer": 1_000.0,
    "klick": 1_000.0,
    "click": 1_000.0,
    "mile": 1_609.344,
    "league": 4_828.0,
    "au": 1.496e11,
    "light-year": 9.461e15,
    "light year": 9.461e15,
    "parsec": 3.086e16,
}
_TIME_UNITS: dict[str, float] = {"minute": 1 / 60, "hour": 1.0, "day": 24.0, "week": 168.0}
# Metres an hour, by how the text says people travel. A day of walking is
# not 24 hours on foot, so a day's travel counts eight hours of it.
_SPEEDS: tuple[tuple[str, float], ...] = (
    (r"\b(fly|flight|flying|plane|shuttle|aircraft)", 500_000.0),
    (r"\b(drive|driving|car|truck|van|motor|bike|bus|train)", 50_000.0),
    (r"\b(ride|riding|horse|wagon|cart|sled|sleigh)", 12_000.0),
    (r"\b(walk|walking|foot|march|hike|trek)", 5_000.0),
)
_DEFAULT_SPEED = 5_000.0

_UNIT_WORDS = "|".join(
    sorted(map(re.escape, [*_LENGTH_UNITS, *_TIME_UNITS]), key=len, reverse=True)
)
_AMOUNT = re.compile(
    rf"(?<![\w-])({COUNT_PATTERN})(\s+and\s+a\s+half)?\s*(?:-\s*)?({_UNIT_WORDS})(?:e?s)?"
    r"(?:'s|s')?(\s+and\s+a\s+half)?\b",
    re.IGNORECASE,
)
_HALF_UNIT = re.compile(rf"\bhalf\s+an?\s+({_UNIT_WORDS})\b", re.IGNORECASE)
_CLOSE = re.compile(
    r"\b(next door|across the (street|road|square|yard|hall|corridor|way)|around (the )?back|"
    r"behind|beside|next to|adjoining|out back|through the door|outside|nearby|a few steps)",
    re.IGNORECASE,
)

# Bearings in degrees clockwise from north.
_COMPASS: dict[str, float] = {
    "north": 0,
    "north-northeast": 22.5,
    "northeast": 45,
    "north-east": 45,
    "east-northeast": 67.5,
    "east": 90,
    "east-southeast": 112.5,
    "southeast": 135,
    "south-east": 135,
    "south-southeast": 157.5,
    "south": 180,
    "south-southwest": 202.5,
    "southwest": 225,
    "south-west": 225,
    "west-southwest": 247.5,
    "west": 270,
    "west-northwest": 292.5,
    "northwest": 315,
    "north-west": 315,
    "north-northwest": 337.5,
    "up": 45,
    "upstairs": 45,
    "above": 45,
    "down": 225,
    "downstairs": 225,
    "below": 225,
}
_BEARING = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, _COMPASS), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def size_of(place: WorldPlace) -> float:
    return place.size if place.size is not None else KIND_SIZE.get(place.kind, KIND_SIZE[None])


def bearing(direction: str | None) -> float | None:
    """The compass bearing a link's direction names, or None."""
    match = _BEARING.search(direction or "")
    return _COMPASS[match.group(1).lower()] if match else None


def metres(distance: str | None) -> float | None:
    """How far a distance in the text's words is, in metres; None when it
    says nothing measurable ("a long way"). A travel time counts at the pace
    the words name (on foot, by car…), on foot when they name none."""
    text = " ".join((distance or "").lower().split())
    if not text:
        return None
    amount: float | None = None
    unit = ""
    half = _HALF_UNIT.search(text)
    match = _AMOUNT.search(text)
    if half and (match is None or half.start() <= match.start()):
        amount, unit = 0.5, half.group(1).lower()
    elif match:
        phrase = match.group(1)
        try:
            amount = float(phrase)
        except ValueError:
            amount = float(count_of(phrase))
        if match.group(2) or match.group(4):
            amount += 0.5
        unit = match.group(3).lower()
    if amount is None or amount <= 0:
        return None
    if unit in _LENGTH_UNITS:
        return amount * _LENGTH_UNITS[unit]
    hours = amount * _TIME_UNITS[unit]
    if unit in ("day", "week"):
        hours = amount * (8 if unit == "day" else 56)  # travelling days, not whole ones
    speed = next((v for pattern, v in _SPEEDS if re.search(pattern, text)), _DEFAULT_SPEED)
    return hours * speed


def _stable_angle(name: str) -> float:
    """A bearing that depends only on a name: where a search for a free spot
    starts when the text gave no direction."""
    digest = hashlib.sha256(name.lower().encode()).digest()
    return (digest[0] * 256 + digest[1]) % 360


def _offset(angle: float, distance: float) -> tuple[float, float]:
    radians = math.radians(angle)
    return math.sin(radians) * distance, math.cos(radians) * distance


def _clear(
    x: float, y: float, size: float, others: Sequence[WorldPlace], ignore: str | None = None
) -> bool:
    for other in others:
        if other.id == ignore or other.x is None or other.y is None:
            continue
        gap = (size + size_of(other)) / 2
        if math.hypot(x - other.x, y - other.y) < gap:
            return False
    return True


def _siblings(places: dict[str, WorldPlace], parent: str | None) -> list[WorldPlace]:
    return [p for p in places.values() if p.inside == parent and p.x is not None]


def _spot(
    origin: tuple[float, float],
    distance: float,
    angle: float | None,
    size: float,
    others: Sequence[WorldPlace],
    name: str,
    fixed_angle: bool,
) -> tuple[float, float]:
    """The first free spot at `distance` from `origin`: on `angle` when the
    text gave one (moving further out along it if that spot is taken), else
    around the compass from a bearing the name decides."""
    start = angle if angle is not None else _stable_angle(name)
    reach = distance
    for _ring in range(12):
        bearings = [start] if fixed_angle else [start + 30 * i for i in range(12)]
        for b in bearings:
            dx, dy = _offset(b, reach)
            x, y = origin[0] + dx, origin[1] + dy
            if _clear(x, y, size, others):
                return x, y
        reach *= 1.35
    dx, dy = _offset(start, reach)
    return origin[0] + dx, origin[1] + dy


def _ancestors(places: dict[str, WorldPlace], place: WorldPlace) -> set[str]:
    out: set[str] = set()
    current = places.get(place.inside or "")
    while current is not None and current.id not in out:
        out.add(current.id)
        current = places.get(current.inside or "")
    return out


def _bounds(parent: WorldPlace, size: float = 0.0) -> tuple[float, float]:
    """(half width, half height) a place of `size` may be centred within,
    inside the parent's drawn rectangle."""
    half_w = size_of(parent) / 2 - size / 2
    half_h = size_of(parent) * PLAN_ASPECT / 2 - size / 2
    return half_w, half_h


def _in_parent(parent: WorldPlace, x: float, y: float, size: float = 0.0) -> bool:
    half_w, half_h = _bounds(parent, size)
    if half_w <= 0 or half_h <= 0:
        return False
    return abs(x - parent.x) <= half_w + 1e-6 and abs(y - parent.y) <= half_h + 1e-6  # type: ignore[operator]


def _within(
    parent: WorldPlace,
    size: float,
    others: Sequence[WorldPlace],
    origin: tuple[float, float],
    name: str,
) -> tuple[float, float] | None:
    """The free spot inside `parent` nearest `origin`, or None when there is
    none. The parent is its drawn rectangle on the map (`PLAN_ASPECT`)."""
    half_w, half_h = _bounds(parent, size)
    half = max(half_w, half_h)
    if half_w <= 0 or half_h <= 0:
        return None
    px, py = parent.x, parent.y  # type: ignore[assignment]

    def inside(x: float, y: float) -> bool:
        return _in_parent(parent, x, y, size)

    sx = min(max(origin[0], px - half_w), px + half_w)
    sy = min(max(origin[1], py - half_h), py + half_h)
    if _clear(sx, sy, size, others):
        return sx, sy
    step = max(size / 2, 1.0)
    start = _stable_angle(name)
    for ring in range(1, 160):
        reach = step * ring
        if reach > 2.9 * half:
            break
        for i in range(24):
            dx, dy = _offset(start + 15 * i, reach)
            x, y = sx + dx, sy + dy
            if inside(x, y) and _clear(x, y, size, others):
                return x, y
    return None


def _near_enough(parent: WorldPlace, point: tuple[float, float]) -> bool:
    """`point` (where the author's character is) in the parent's drawn
    rectangle, or near it: within half its size outside (a village 3 km from
    a 16 km forest's edge is at the forest; what the story puts in the
    forest then goes to the edge nearest the village, not its middle)."""
    half_w, half_h = _bounds(parent)
    reach_w, reach_h = half_w + size_of(parent) / 2, half_h + size_of(parent) / 2
    return abs(point[0] - parent.x) <= reach_w and abs(point[1] - parent.y) <= reach_h  # type: ignore[operator]


def place_one(
    places: dict[str, WorldPlace],
    links: Sequence[WorldLink],
    place: WorldPlace,
    near: str | None,
    near_point: tuple[float, float] | None = None,
) -> None:
    """Give `place` its position and size, among `places` already placed.
    `near`: the place the story is at, for a place with nothing else to go
    on; `near_point`: where the author's character stands, in metres, when
    the program knows (map-first play), else that place's spot."""
    parent = places.get(place.inside or "")
    if parent is not None and parent.x is None:
        parent = None
    size = KIND_SIZE.get(place.kind, KIND_SIZE[None]) if place.size is None else place.size
    if parent is not None:
        size = min(size, size_of(parent) * CHILD_SHARE)
    place.size = size
    others = _siblings(places, place.inside if parent is not None else None)
    if parent is None:
        # Top level: kept apart from every placed place that has no parent.
        others = [p for p in places.values() if p.x is not None and p.inside not in places]

    above = _ancestors(places, place)
    for link in links:
        if place.id not in (link.a, link.b):
            continue
        other = places.get(link.b if link.a == place.id else link.a)
        if other is None or other.x is None or other.id == place.id:
            continue
        if other.id in above or place.id in _ancestors(places, other):
            continue  # its container (or something in it): inside says where
        angle = bearing(link.direction)
        if angle is not None and link.b != place.id:
            angle = (angle + 180) % 360  # the link runs from this place to the other
        gap = (size + size_of(other)) / 2
        stated = metres(link.distance)
        close = bool(_CLOSE.search(link.direction or ""))
        distance = max(stated, gap) if stated is not None else gap * (1.0 if close else 1.5)
        x, y = _spot(
            (other.x, other.y), distance, angle, size, others, place.name, angle is not None
        )
        if parent is not None and not _in_parent(parent, x, y, size):
            # "Inside the forest, five miles north of the village" with the
            # village outside the forest: inside wins, and the spot is found
            # within the parent below (the review, Oct 16 2026).
            break
        place.x, place.y = x, y
        return

    if near_point is None:
        anchor = places.get(near or "")
        if anchor is not None and anchor.x is not None and anchor.y is not None:
            near_point = (anchor.x, anchor.y)
    if parent is not None and near_point is not None and _near_enough(parent, near_point):
        # Where the story is: everything placed but what it is in, and what
        # is as big as its parent (a range of hills it lies in by position).
        crowd = [
            p
            for p in places.values()
            if p.x is not None
            and p.y is not None
            and p.id != place.id
            and p.id not in above
            and size_of(p) < size_of(parent)
            and abs(p.x - parent.x) <= (size_of(parent) + size_of(p)) / 2  # type: ignore[operator]
            and abs(p.y - parent.y) <= (size_of(parent) + size_of(p)) / 2  # type: ignore[operator]
            and not moves(places, p)
        ]
        spot = _within(parent, size, crowd, near_point, place.name)
        if spot is not None:
            place.x, place.y = spot
            return
    if parent is not None:
        radius = size_of(parent) / 2 - size / 2
        if radius <= 0 or not others:
            place.x, place.y = parent.x, parent.y
            if others and not _clear(parent.x, parent.y, size, others):  # type: ignore[arg-type]
                place.x, place.y = _spot(
                    (parent.x, parent.y),
                    size,
                    None,
                    size,
                    others,
                    place.name,
                    False,  # type: ignore[arg-type]
                )
            return
        place.x, place.y = _spot(
            (parent.x, parent.y),  # type: ignore[arg-type]
            min(radius, max(size, radius * 0.5)),
            None,
            size,
            others,
            place.name,
            False,
        )
        return

    anchor = places.get(near or "")
    if anchor is not None and anchor.x is not None:
        # Beside the place the story is at: at the same level as it.
        top = anchor
        while top.inside in places and places[top.inside].x is not None:
            top = places[top.inside]
        distance = (size + size_of(top)) / 2 * 1.5
        place.x, place.y = _spot(
            (top.x, top.y),
            distance,
            None,
            size,
            others,
            place.name,
            False,  # type: ignore[arg-type]
        )
        return
    if not others:
        place.x, place.y = 0.0, 0.0
        return
    last = others[-1]
    place.x, place.y = _spot(
        (last.x, last.y),  # type: ignore[arg-type]
        (size + size_of(last)) / 2 * 1.5,
        None,
        size,
        others,
        place.name,
        False,
    )


def place_new(
    world: WorldMap,
    places: Sequence[WorldPlace],
    links: Sequence[WorldLink],
    near: str | None,
    near_point: tuple[float, float] | None = None,
) -> None:
    """Position every place in `places` that has none yet, on top of `world`.
    Parents before children, and in the order the story named them."""
    known = {p.id: p for p in world.places.values()}
    for place in places:
        known[place.id] = place
    all_links = [*world.links, *links]
    waiting = [p for p in places if (p.x is None or p.y is None) and not moves(known, p)]
    for vehicle in places:
        if vehicle.kind == "vehicle" and vehicle.size is None:
            vehicle.size = KIND_SIZE["vehicle"]
    for _round in range(len(waiting) + 1):
        if not waiting:
            break
        still = []
        for place in waiting:
            parent = known.get(place.inside or "")
            if parent is not None and parent.x is None and parent in waiting:
                still.append(place)  # its parent first
                continue
            place_one(known, all_links, place, near, near_point)
        if len(still) == len(waiting):
            for place in still:  # a parent that can't be placed first: place anyway
                place_one(known, all_links, place, near, near_point)
            break
        waiting = still


def moves(places: dict[str, WorldPlace], place: WorldPlace) -> bool:
    """A vehicle, or a place inside one (its bridge, a cabin): it goes where
    the vehicle goes, so it has no spot of its own."""
    seen: set[str] = set()
    current: WorldPlace | None = place
    while current is not None and current.id not in seen:
        if current.kind == "vehicle":
            return True
        seen.add(current.id)
        current = places.get(current.inside or "")
    return False


def position(world: WorldMap, place_id: str | None) -> tuple[float, float] | None:
    """Where a place is drawn: its own spot, or, for a vehicle (or anything
    not placed), the spot of the place it is in."""
    seen: set[str] = set()
    while place_id is not None and place_id not in seen:
        seen.add(place_id)
        place = world.places.get(place_id)
        if place is None:
            return None
        if place.x is not None and place.y is not None:
            return place.x, place.y
        place_id = place.inside
    return None


def extent(world: WorldMap) -> tuple[float, float, float, float] | None:
    """(west, south, east, north) of everything placed: the map's edge."""
    placed = [p for p in world.places.values() if p.x is not None and p.y is not None]
    if not placed:
        return None
    return (
        min(p.x - size_of(p) / 2 for p in placed),  # type: ignore[operator]
        min(p.y - size_of(p) / 2 for p in placed),  # type: ignore[operator]
        max(p.x + size_of(p) / 2 for p in placed),  # type: ignore[operator]
        max(p.y + size_of(p) / 2 for p in placed),  # type: ignore[operator]
    )


def describe_metres(distance: float) -> str:
    """A distance as the notices say it: "40 m", "1.2 km", "8 km"."""
    if distance < 950:
        return f"{round(distance, -1) if distance >= 100 else round(distance):.0f} m"
    kilometres = distance / 1000
    return f"{kilometres:.1f} km" if kilometres < 10 else f"{kilometres:,.0f} km"


def geometry_conflicts(world: WorldMap, links: Sequence[WorldLink]) -> list[str]:
    """Links a passage stated between two places the map had already placed,
    whose distance is far from where they are: the map keeps its places and
    says so (an established place doesn't move)."""
    found = []
    for link in links:
        a, b = world.places.get(link.a), world.places.get(link.b)
        stated = metres(link.distance)
        if a is None or b is None or a.x is None or b.x is None or stated is None:
            continue
        apart = math.hypot(a.x - b.x, a.y - b.y)  # type: ignore[operator]
        floor = (size_of(a) + size_of(b)) / 2
        if apart > floor and not (stated / 2.5 <= apart <= stated * 2.5):
            found.append(
                f"{a.name} – {b.name}: the map has them about {describe_metres(apart)} apart; "
                f"the passage says {link.distance}"
            )
    return found
