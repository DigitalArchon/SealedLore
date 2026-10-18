"""Edit Mode on the map (the author, Oct 18 2026): "start in an actual Edit
Mode, where they can optionally change anything they want, including adding
and removing buildings, editing internals including walls and doors", and
have it "available during any game with a map … so they don't always have to
ask as the Director and hope it interprets the request correctly". Pure.

An area is built afresh from its plan every time (engine/area.py); the
author's changes to its tiles are kept beside the plan (`WorldArt.tile_edits`)
and laid over the area as it is built (`apply_tile_edits`), which works out
again what the tiles make: the rooms, the doorways, the ways out and the
doors into the buildings. The rules the program holds a drawn plan to (every
room reached, a door for every building) are not imposed on the author's
own tiles: what they paint is what the map is."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from sealedlore.engine.area import (
    WALKABLE,
    Area,
    Entrance,
    Placement,
    Room,
    _door_side,
    _exits,
    find_rooms,
    name_rooms,
    openings,
)
from sealedlore.engine.world_art import GROUND
from sealedlore.models.world import TileEdits

# What the author can paint, in the palette's order, with its name. Stairs
# are left out: they join a plan's levels, which painting can't make.
EDIT_TILES: dict[str, str] = {
    "_": "Floor",
    "#": "Wall",
    "+": "Door",
    "W": "Window",
    ".": "Ground",
    ",": "Grass or brush",
    "=": "Path or road",
    "b": "Bridge",
    "s": "Sand",
    "T": "Trees",
    "^": "Rock",
    "~": "Water",
    "%": "Fence",
    ":": "Rubble",
    " ": "Nothing (the dark)",
}
# What a building is made of, for finding one by a click on it (a plan's own
# building has no place on the map: a wall ring, its doors and windows, and
# its inside, blank outdoors).
FABRIC = frozenset("#W+_& ")
# A run of fabric bigger than this share of the area is the area itself (a
# deck, a town's wall), not one building.
MOST_OF_AREA = 0.35


def edit_key(x: int, y: int) -> str:
    return f"{x},{y}"


def parse_key(text: str) -> tuple[int, int] | None:
    try:
        x, y = (int(part) for part in text.split(","))
    except ValueError:
        return None
    return x, y


def ground_tile(area: Area) -> str:
    """The open ground of an area, as painted where a building came off."""
    if area.indoor:
        return "_"
    ch = GROUND.get(area.look, ".")
    return "." if ch == " " else ch


def apply_tile_edits(area: Area, edits: TileEdits | None) -> Area:
    """The area with the author's tiles over it: what stood on a tile they
    made a wall, water or the like is gone, and the rooms, doorways, ways out
    and doors into its buildings are as the tiles now make them. Edits made
    when the area had another size (a plan drawn again from scratch) are not
    laid: they would land on the wrong tiles."""
    if edits is None or not edits.tiles or tuple(edits.size) != (area.width, area.height):
        return area
    grid = [list(row) for row in area.rows]
    changed = False
    for text, ch in edits.tiles.items():
        tile = parse_key(text)
        if tile is None or ch not in EDIT_TILES:
            continue
        x, y = tile
        if 0 <= y < area.height and 0 <= x < area.width and grid[y][x] != ch:
            grid[y][x] = ch
            changed = True
    if not changed:
        return area
    rows = ["".join(row) for row in grid]

    def stands(p: Placement) -> bool:
        return all(
            0 <= y < len(rows)
            and 0 <= x < len(rows[0])
            and (rows[y][x] in WALKABLE or rows[y][x].isdigit())
            for x, y in p.tiles()
        )

    placements = [p for p in area.placements if stands(p)]
    doorways = openings(rows)
    rooms, room_of = area.rooms, area.room_of
    if area.indoor:
        rooms, room_of = find_rooms(rows, area.objects, doorways)
        name_rooms(rooms, area.labels)
        _keep_room_words(area, rooms, room_of)
        placements = [_in_room(p, room_of) for p in placements]
    exits = _exits(rows) if area.indoor else list(area.exits)
    entrances = _entrances(area, rows)
    return replace(
        area,
        rows=rows,
        placements=placements,
        doorways=doorways,
        rooms=rooms,
        room_of=room_of,
        exits=exits,
        entrances=entrances,
        repairs=[*area.repairs, "the author's own tiles laid"],
    )


def _keep_room_words(area: Area, rooms: list[Room], room_of: list[list[int]]) -> None:
    """A room the author's walls left much as it was keeps its name, purpose
    and use (the furnisher's "kitchen" stays the kitchen), by the old room
    most of its tiles were in."""
    votes: dict[int, dict[int, int]] = {}
    for y, row in enumerate(room_of):
        for x, new in enumerate(row):
            if new < 0 or y >= len(area.room_of) or x >= len(area.room_of[y]):
                continue
            old = area.room_of[y][x]
            if old >= 0:
                tally = votes.setdefault(new, {})
                tally[old] = tally.get(old, 0) + 1
    for room in rooms:
        tally = votes.get(room.index)
        if not tally:
            continue
        best = max(tally, key=tally.__getitem__)
        if best < len(area.rooms) and tally[best] * 2 >= room.cells:
            old = area.rooms[best]
            room.name, room.purpose = old.name, old.purpose
            room.use = old.use or room.use


def _in_room(p: Placement, room_of: list[list[int]]) -> Placement:
    x, y = int(p.centre[0]), int(p.centre[1])
    index = room_of[y][x] if 0 <= y < len(room_of) and 0 <= x < len(room_of[y]) else -1
    return replace(p, room=index if index >= 0 else None)


def _entrances(area: Area, rows: list[str]) -> list[Entrance]:
    """Stairs still stairs; and every door now in the wall of a building
    that leads inside (a door painted in leads in as the old one did, one
    painted over no longer does)."""
    out = [e for e in area.entrances if e.stairs and rows[e.y][e.x] == "&"]
    leads_in = {e.place_id for e in area.entrances if not e.stairs}
    for place_id, box in area.buildings.items():
        if place_id not in leads_in:
            continue
        x0, y0, x1, y1 = box
        for y in range(max(y0, 0), min(y1, len(rows) - 1) + 1):
            for x in range(max(x0, 0), min(x1, len(rows[0]) - 1) + 1):
                if rows[y][x] == "+" and (x in (x0, x1) or y in (y0, y1)):
                    out.append(Entrance(x, y, place_id, _door_side(box, x, y)))
    return out


def walled_box(
    area: Area, x0: int, y0: int, x1: int, y1: int, wall: str = "#"
) -> dict[tuple[int, int], str]:
    """A room (indoors) or a walled or fenced yard (outdoors) drawn from one
    corner to the other: its edge made `wall`, with a door in the middle of
    the side that opens onto the most open ground (south first). At least
    three tiles each way; {} when smaller."""
    x0, x1 = sorted((max(0, min(x0, area.width - 1)), max(0, min(x1, area.width - 1))))
    y0, y1 = sorted((max(0, min(y0, area.height - 1)), max(0, min(y1, area.height - 1))))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return {}
    out: dict[tuple[int, int], str] = {}
    for x in range(x0, x1 + 1):
        out[(x, y0)] = wall
        out[(x, y1)] = wall
    for y in range(y0, y1 + 1):
        out[(x0, y)] = wall
        out[(x1, y)] = wall
    if wall == "%":
        door = "."  # a gate is a gap in a fence
    else:
        door = "+"
    sides = {
        "south": ((x0 + x1) // 2, y1, 0, 1),
        "north": ((x0 + x1) // 2, y0, 0, -1),
        "east": (x1, (y0 + y1) // 2, 1, 0),
        "west": (x0, (y0 + y1) // 2, -1, 0),
    }

    def open_beyond(x: int, y: int, dx: int, dy: int) -> bool:
        bx, by = x + dx, y + dy
        return 0 <= by < area.height and 0 <= bx < area.width and area.rows[by][bx] in WALKABLE

    side = next(
        (name for name, (x, y, dx, dy) in sides.items() if open_beyond(x, y, dx, dy)), "south"
    )
    x, y, _dx, _dy = sides[side]
    out[(x, y)] = door
    return out


def building_at(area: Area, x: int, y: int) -> tuple[str | None, set[tuple[int, int]]]:
    """The building under a click: (the map's place id, every tile of its
    box) for one the map knows; (None, its tiles) for one only the plan drew
    (a run of wall, door, window and inside joined to the click); (None, set())
    for none."""
    for place_id, (x0, y0, x1, y1) in area.buildings.items():
        if x0 <= x <= x1 and y0 <= y <= y1:
            return place_id, {(bx, by) for by in range(y0, y1 + 1) for bx in range(x0, x1 + 1)}
    if area.indoor or not (0 <= y < area.height and 0 <= x < area.width):
        return None, set()
    if area.rows[y][x] not in FABRIC - {" "}:
        return None, set()
    limit = int(area.width * area.height * MOST_OF_AREA)
    seen = {(x, y)}
    todo = [(x, y)]
    while todo:
        cx, cy = todo.pop()
        for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
            if (nx, ny) in seen or not (0 <= ny < area.height and 0 <= nx < area.width):
                continue
            if area.rows[ny][nx] in FABRIC:
                seen.add((nx, ny))
                todo.append((nx, ny))
                if len(seen) > limit:
                    return None, set()
    return None, seen


def cleared(area: Area, tiles: Iterable[tuple[int, int]]) -> dict[tuple[int, int], str]:
    """These tiles made the area's open ground (a building taken off)."""
    ground = ground_tile(area)
    return {tile: ground for tile in tiles}


def edits_of(edits: TileEdits | None) -> Mapping[tuple[int, int], str]:
    """The edits as tiles."""
    out: dict[tuple[int, int], str] = {}
    for text, ch in (edits.tiles if edits is not None else {}).items():
        tile = parse_key(text)
        if tile is not None:
            out[tile] = ch
    return out
