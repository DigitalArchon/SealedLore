"""Marked spots: a point on the map the author put in their turn (map-first
play; the author, Oct 16 2026: "a coordinate system so the user can click an
area to add the coordinate and say what to do at that specific place").

A mark is written into the turn as its coordinate, `⌖(412, -88)`: metres
east and north of the map's first place, as every place's spot is
(engine/world_layout.py). The text carries it, so a mark needs no state of
its own: Regenerate, Take back and an edit keep it as written, and one typed
by hand (or as `@(412, -88)`) works the same. The program resolves it to the
place it lies in and, where that place has an area, the tile; the
storyteller is told what is there in words (`map.marks`), and the map read
may put a new place or a person at it (`map_read.marks`), which the code
then does exactly.
"""

from __future__ import annotations

import math
import re

from sealedlore.engine.world_art import plan_rect
from sealedlore.engine.world_layout import position, size_of
from sealedlore.models.world import WorldArt, WorldMap

MARK = re.compile(r"[⌖@]\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)")
# A turn names at most this many marks for the storyteller and the read.
MAX_MARKS = 6


def footprint(
    world: WorldMap, art: WorldArt, place_id: str
) -> tuple[float, float, float, float] | None:
    """(west, south, east, north) of a place on the map: its plan's rectangle
    where it has a plan, else its square. A vehicle (and what is inside one)
    is where it is docked now (`position`): a mark on a ship's deck is on
    the ship, not in the bay."""
    place = world.places.get(place_id)
    spot = position(world, place_id) if place is not None else None
    if place is None or spot is None:
        return None
    if art.layout_shown(place.id, place.state) is not None:
        rect = plan_rect(world, place)
        if rect is not None:
            west, north, width, height = rect
            return west, north - height, west + width, north
    half = size_of(place) / 2
    return spot[0] - half, spot[1] - half, spot[0] + half, spot[1] + half


def place_at(world: WorldMap, art: WorldArt, x: float, y: float) -> str | None:
    """The innermost place the point lies in: the smallest whose footprint
    holds it. None in open country."""
    best: tuple[float, int, str] | None = None
    for place in world.places.values():
        if place.kind == "region":
            continue
        box = footprint(world, art, place.id)
        if box is None:
            continue
        west, south, east, north = box
        if not (west <= x <= east and south <= y <= north):
            continue
        depth = _depth(world, place.id)
        spot = position(world, place.id)
        near = math.hypot(x - spot[0], y - spot[1]) if spot is not None else math.inf
        # The smallest, the deepest, then the one whose middle is nearest:
        # two sheds' footprints are 20 m wide whatever their size.
        candidate = (size_of(place), -depth, near, place.id)
        if best is None or candidate < best:
            best = candidate
    return best[3] if best is not None else None


def _depth(world: WorldMap, place_id: str) -> int:
    depth, seen = 0, set()
    current = world.places.get(place_id)
    while current is not None and current.inside and current.id not in seen:
        seen.add(current.id)
        depth += 1
        current = world.places.get(current.inside)
    return depth


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


def compass(dx: float, dy: float) -> str:
    """The compass way of a step east `dx`, north `dy` (metres)."""
    angle = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return _COMPASS[round(angle / 45) % 8]
