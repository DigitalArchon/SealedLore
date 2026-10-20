"""The world designer (the author, Oct 18 2026: "like a procedurally generated
game, but instead of preprogrammed procedures to generate predictable new
spaces, the AI acts as a real designer, intelligently building the world as
the player goes").

Open country is a grid of regions about the size of a farm, REGION_SIDE
metres square, laid from where the story starts. Each is designed by a model
ahead of the player, the one they stand in and those round it: its land, at
the play view's walking scale, and the places, things and people in it, to
fit the story's world, what has happened, and the regions beside it (their
roads, rivers and woods run on across the edges). Designed once and kept, as
world and not story: every branch has it, as if it had always been there,
and it stays under fog on the maps until the player has been there.

Pure: the grid, the designer's messages, its reply read and held to the
grid, a design made into land, places, things and people, and the regions as
the map folds them in. The session runs it (engine/session_regions.py).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.marks import footprint
from sealedlore.engine.prompt_texts import PromptTexts
from sealedlore.engine.world import is_unknown
from sealedlore.engine.world_art import (
    PLAN_MAX_SIZE,
    REGION_GRID_SIDE,
    REGION_GRID_TILES,
    rasterise,
)
from sealedlore.engine.world_layout import meets, moves, position, size_of
from sealedlore.ids import new_id
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.world import (
    DesignedPerson,
    DesignedRegion,
    TerrainPatch,
    WorldArt,
    WorldDelta,
    WorldMap,
    WorldPlace,
    WorldThing,
)

# A region: about the size of a farm (the author's choice), walked at the
# play view's scale: the designer's 80 tiles of 5 m, drawn as 160 of 2.5 m.
REGION_SIDE = REGION_GRID_SIDE
DESIGN_GRID = 80
DESIGN_SCALE = REGION_GRID_TILES // DESIGN_GRID
REGION_TILES = DESIGN_GRID * DESIGN_SCALE
# A design is a long JSON reply.
REGION_MAX_TOKENS = 8000
# Places, things and people in a region at most, by Settings → Models → Open
# country (`Config.open_country`); the designer is told the numbers and its
# reply is held to them. "rich" is what every region had before Oct 21 2026.
# In 84 logged designs under it (six stories) a region held about 2 places,
# 4.3 things and 1.3 people, so its limits were seldom reached and only a
# lower limit makes fewer: "normal" caps things at 3 and people at 1 (a ring
# of nine at most 27 things and 9 people, against the ~39 and ~13 seen round
# the Doomsville farm), places kept at 3 (a building and a landmark give a
# region its character); "sparse" is country to cross rather than search, a
# thing and a person at most.
OPEN_COUNTRY: dict[str, tuple[int, int, int]] = {
    "sparse": (2, 1, 1),
    "normal": (3, 3, 1),
    "rich": (5, 8, 3),
}
MAX_PLACES, MAX_THINGS, MAX_PEOPLE = OPEN_COUNTRY["rich"]
MAX_FEATURES = 120
# A thing this close to a place's walls is that place's.
BESIDE = 10.0
# Nobody the designer puts in the player's own region stands this close to
# them: they would be simply there (the arrival rule).
CLEAR_OF_PLAYER = 120.0
# Someone moved into a region stands this far inside its edge.
INSET = 5.0
# Designed people's labels start here, clear of the story's own unknowns.
LABEL_FROM = 101
# The rings of regions designed round the player's: theirs, then the ring.
RING = 1
# The designer's kinds, as the map has them: a building is entered (its
# inside is drawn later); a landmark is a spot with a name, drawn with the
# region's shapes; a settlement gets a plan of its own.
KINDS = {"building": "building", "landmark": "area", "settlement": "settlement"}
GROUNDS = ("snow", "grass", "dirt", "sand", "rock")
# Shapes the designer may lay (buildings and objects are the program's).
SHAPES = frozenset({"area", "road", "fence", "wall"})
# What a region's land may hold, walked on or not, as the play view reads it.
WALKABLE_LAND = frozenset("._,=sb:")
# What runs on across an edge, worth telling the region beside it.
CROSSING = {"=": "a road", "~": "water", "T": "woods", "^": "rock", "%": "a fence"}


# --- the grid -------------------------------------------------------------------


def origin_of(world: WorldMap, art: WorldArt) -> tuple[float, float]:
    """The grid's north-west corner: kept once set, else the region where
    the story starts centred on it (its first place, else the map's middle)."""
    if art.region_origin and len(art.region_origin) == 2:
        return float(art.region_origin[0]), float(art.region_origin[1])
    start = position(world, world.start) if world.start else None
    cx, cy = start if start is not None else (0.0, 0.0)
    return cx - REGION_SIDE / 2, cy + REGION_SIDE / 2


def cell_of(origin: tuple[float, float], x: float, y: float) -> tuple[int, int]:
    """(column, row) of the region holding a point; rows run south."""
    ox, oy = origin
    return math.floor((x - ox) / REGION_SIDE), math.floor((oy - y) / REGION_SIDE)


def cell_rect(origin: tuple[float, float], i: int, j: int) -> tuple[float, float, float]:
    """(west, north, side) of a region."""
    ox, oy = origin
    return ox + i * REGION_SIDE, oy - j * REGION_SIDE, REGION_SIDE


def cell_key(i: int, j: int) -> str:
    return f"{i}:{j}"


def parse_key(key: str) -> tuple[int, int] | None:
    try:
        i, j = (int(v) for v in key.split(":"))
    except ValueError:
        return None
    return i, j


def region_at(art: WorldArt, origin: tuple[float, float], x: float, y: float):
    """The designed region holding a point, or None."""
    return art.regions.get(cell_key(*cell_of(origin, x, y)))


def waves(origin: tuple[float, float], point: tuple[float, float], ring: int = RING):
    """The regions round a point in the order they are designed, as waves
    that border nothing else in their own wave: the one it is in, then the
    four beside it, then the corners (so each sees its neighbours' edges)."""
    ci, cj = cell_of(origin, *point)
    centre: list[tuple[int, int]] = [(ci, cj)]
    sides: list[tuple[int, int]] = []
    corners: list[tuple[int, int]] = []
    for dj in range(-ring, ring + 1):
        for di in range(-ring, ring + 1):
            if (di, dj) == (0, 0):
                continue
            (sides if di == 0 or dj == 0 else corners).append((ci + di, cj + dj))
    return [centre, sides, corners]


# --- what the designer is told ----------------------------------------------------


def _bearing(dx: float, dy: float) -> str:
    names = ("north", "north-east", "east", "south-east", "south", "south-west", "west")
    names = (*names, "north-west")
    angle = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return names[round(angle / 45) % 8]


def _metres(d: float) -> str:
    return f"{d / 1000:.1f} km" if d >= 1000 else f"{round(d / 10) * 10:.0f} m"


def where_words(
    world: WorldMap,
    rect: tuple[float, float, float],
    player: tuple[float, float] | None,
) -> str:
    """Where a region lies: from the player, and from the nearest known place."""
    west, north, side = rect
    cx, cy = west + side / 2, north - side / 2
    parts = []
    if player is not None:
        d = math.hypot(cx - player[0], cy - player[1])
        if abs(cx - player[0]) < side / 2 and abs(cy - player[1]) < side / 2:
            tile = side / DESIGN_GRID
            gx = int((player[0] - west) / tile)
            gy = int((north - player[1]) / tile)
            parts.append(
                f"the region the player is in, standing at x {gx}, y {gy}: put no one within "
                f"{int(CLEAR_OF_PLAYER / tile)} tiles of them, out of their sight"
            )
        else:
            parts.append(
                f"the region {_bearing(cx - player[0], cy - player[1])} of the one the player is in"
            )
    nearest = None
    for place in world.places.values():
        if place.x is None or place.y is None or place.kind in ("region", "route"):
            continue
        d = math.hypot(place.x - cx, place.y - cy)
        if nearest is None or d < nearest[0]:
            nearest = (d, place)
    if nearest is not None:
        d, place = nearest
        if d < side / 2:
            parts.append(f"{place.name} is on it")
        else:
            parts.append(
                f"its middle is {_metres(d)} {_bearing(cx - place.x, cy - place.y)} of {place.name}"
            )
    return "; ".join(parts) or "open country"


def place_lines(
    world: WorldMap,
    rect: tuple[float, float, float],
    *,
    designed: set[str] = frozenset(),  # type: ignore[assignment]
    reach: float = REGION_SIDE,
) -> str:
    """The story's places on the region (with its tiles, kept clear) and
    near it (with distance and bearing from its middle), a line each; not
    the `designed` ones (the regions beside it are told as such: given as
    the story's, a design copied its neighbour's pickup and grave)."""
    west, north, side = rect
    tile = side / DESIGN_GRID
    cx, cy = west + side / 2, north - side / 2
    on, near = [], []
    for place in world.places.values():
        if place.x is None or place.y is None or place.kind in ("region", "route"):
            continue
        if place.id in designed:
            continue
        half = size_of(place) / 2
        what = f": {place.what}" if place.what else ""
        if (
            place.x + half > west
            and place.x - half < west + side
            and place.y - half < north
            and place.y + half > north - side
        ):
            x0 = max(0, int((place.x - half - west) / tile))
            x1 = min(DESIGN_GRID - 1, int((place.x + half - west) / tile))
            y0 = max(0, int((north - (place.y + half)) / tile))
            y1 = min(DESIGN_GRID - 1, int((north - (place.y - half)) / tile))
            on.append(f"- {place.name} ({place.kind or 'place'}), x {x0}-{x1}, y {y0}-{y1}{what}")
            continue
        d = math.hypot(place.x - cx, place.y - cy) - half
        if d <= reach + side / 2:
            near.append(
                (
                    d,
                    f"- {place.name} ({place.kind or 'place'}), {_metres(max(d, 0))} "
                    f"{_bearing(place.x - cx, place.y - cy)} of its middle{what}",
                )
            )
    near.sort(key=lambda pair: pair[0])
    lines = on + [line for _d, line in near[:10]]
    return "\n".join(lines) or "- none"


def _runs(cells: Sequence[str]) -> list[tuple[str, int, int]]:
    """(tile, first, last) for each run of a crossing tile along an edge."""
    out: list[tuple[str, int, int]] = []
    for i, ch in enumerate(cells):
        if ch not in CROSSING:
            continue
        if out and out[-1][0] == ch and out[-1][2] == i - 1:
            out[-1] = (ch, out[-1][1], i)
        else:
            out.append((ch, i, i))
    return out


def edge_lines(art: WorldArt, origin: tuple[float, float], i: int, j: int) -> str:
    """The designed regions beside one, a line each: their name, what they
    are, and what crosses into this one along the shared edge, in this
    region's tiles (a road at y 38-41 along the west edge)."""
    sides = {
        "north": ((i, j - 1), lambda rows: rows[-1], "x"),
        "south": ((i, j + 1), lambda rows: rows[0], "x"),
        "west": ((i - 1, j), lambda rows: [r[-1] for r in rows], "y"),
        "east": ((i + 1, j), lambda rows: [r[0] for r in rows], "y"),
    }
    lines = []
    for side, (cell, strip, axis) in sides.items():
        region = art.regions.get(cell_key(*cell))
        if region is None:
            continue
        patch = next((p for p in art.terrain if p.designed and p.place_id == region.place_id), None)
        name = next((p.name for p in region.places if p.id == region.place_id), "a region")
        what = next((p.what for p in region.places if p.id == region.place_id), "")
        crossing = ""
        if patch is not None and patch.rows:
            edge = list(strip(patch.rows))
            scale = max(1, len(edge) // DESIGN_GRID)
            coarse = [edge[k] for k in range(0, len(edge), scale)][:DESIGN_GRID]
            runs = [f"{CROSSING[ch]} at {axis} {a}-{b}" for ch, a, b in _runs(coarse)]
            crossing = ("; along your " + side + " edge: " + ", ".join(runs)) if runs else ""
        lines.append(f"- {side}: {name}, {what}{crossing}".rstrip(", "))
    return "\n".join(lines) or "- none designed yet"


def taken_lines(art: WorldArt, most: int = 60, story: Sequence[str] = ()) -> str:
    """What the designer has made elsewhere already: each region's name and
    the names of its places and things, for the next to be its own (in one
    wave, two regions were both "the Hollis back forty", and a child's boot
    lay in four); then the story's own place names (`story`), as many
    again at most, so a story with many places never crowds the designer's
    own out (the story's places on and near the cell are given beside)."""
    names: list[str] = []
    for key in sorted(art.regions):
        region = art.regions[key]
        names += [p.name for p in region.places]
        names += [t.name for t in region.things]
    names = list(dict.fromkeys(names))[:most]
    names += [n for n in dict.fromkeys(story) if n not in names][:most]
    return "; ".join(names) or "nothing yet"


def limits_for(open_country: str) -> tuple[int, int, int]:
    """(places, things, people) at most in a region, for Settings' choice;
    "normal" for one the program doesn't know."""
    return OPEN_COUNTRY.get(open_country, OPEN_COUNTRY["normal"])


def _counted(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def region_messages(
    texts: PromptTexts,
    *,
    setting: str,
    story: str,
    where: str,
    places: str,
    edges: str,
    ground: str,
    taken: str = "nothing yet",
    limits: tuple[int, int, int] = OPEN_COUNTRY["rich"],
) -> list[PromptMessage]:
    most_places, most_things, most_people = limits
    system = texts.fill(
        "map.region.design",
        side=f"{REGION_SIDE:.0f}",
        grid=str(DESIGN_GRID),
        last=str(DESIGN_GRID - 1),
        tile=f"{REGION_SIDE / DESIGN_GRID:g}",
        # With their words ("1 person"): "rich" reads as it always has.
        max_places=_counted(most_places, "place", "places"),
        max_things=_counted(most_things, "thing", "things"),
        max_people=_counted(most_people, "person", "people"),
    )
    user = texts.fill(
        "map.region.ask",
        setting=setting,
        story=story,
        where=where,
        places=places,
        edges=edges,
        ground=ground,
        taken=taken,
    )
    return [
        PromptMessage(role="system", parts=(ContentPart(text=system),)),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


# --- the reply ----------------------------------------------------------------------


@dataclass
class RegionDesign:
    """A designer's reply, read and held to the grid."""

    name: str
    what: str
    ground: str
    features: list[dict[str, Any]] = field(default_factory=list)
    places: list[dict[str, Any]] = field(default_factory=list)
    things: list[dict[str, Any]] = field(default_factory=list)
    people: list[dict[str, Any]] = field(default_factory=list)


def _cell(value: Any) -> int:
    try:
        return max(0, min(DESIGN_GRID - 1, int(round(float(value)))))
    except (TypeError, ValueError, OverflowError):
        return DESIGN_GRID // 2


def _plain(name: str) -> str:
    words = " ".join(name.lower().split())
    return words[4:] if words.startswith("the ") else words


def _span(value: Any, default: int = 3) -> int:
    """A width or height in the designer's tiles, at least one."""
    try:
        return max(1, min(DESIGN_GRID, int(round(float(value)))))
    except (TypeError, ValueError, OverflowError):
        return default


def _text(value: Any, most: int) -> str:
    return " ".join(str(value or "").split())[:most]


def parse_region(text: str, limits: tuple[int, int, int] = OPEN_COUNTRY["rich"]) -> RegionDesign:
    """The designer's reply as a design, held to `limits` (places, things
    and people at most: `limits_for`); ValueError for one with no name or
    no land."""
    most_places, most_things, most_people = limits
    data = extract_json(text, dict, "region")
    name = _text(data.get("name"), 80)
    if not name:
        raise ValueError("the region has no name")
    features = [
        f
        for f in (data.get("features") if isinstance(data.get("features"), list) else [])
        if isinstance(f, dict) and f.get("kind") in SHAPES
    ][:MAX_FEATURES]
    ground = _text(data.get("ground"), 12).lower()
    places = []
    for p in data.get("places") if isinstance(data.get("places"), list) else []:
        if not isinstance(p, dict) or not _text(p.get("name"), 80):
            continue
        places.append(
            {
                "name": _text(p.get("name"), 80),
                "kind": KINDS.get(_text(p.get("kind"), 20).lower(), "area"),
                "what": _text(p.get("what"), 300),
                "x": _cell(p.get("x")),
                "y": _cell(p.get("y")),
                "w": _span(p.get("w")),
                "h": _span(p.get("h")),
            }
        )
    things = [
        {"name": _text(t.get("name"), 80), "x": _cell(t.get("x")), "y": _cell(t.get("y"))}
        for t in (data.get("things") if isinstance(data.get("things"), list) else [])
        if isinstance(t, dict) and _text(t.get("name"), 80)
    ]
    people = [
        {
            "who": _text(p.get("who"), 120),
            "doing": _text(p.get("doing"), 160),
            "x": _cell(p.get("x")),
            "y": _cell(p.get("y")),
        }
        for p in (data.get("people") if isinstance(data.get("people"), list) else [])
        if isinstance(p, dict) and _text(p.get("who"), 120)
    ]
    return RegionDesign(
        name=name,
        what=_text(data.get("what"), 600),
        ground=ground if ground in GROUNDS else "dirt",
        features=features,
        places=places[:most_places],
        things=things[:most_things],
        people=people[:most_people],
    )


# --- the design made into land, places, things and people ---------------------------


def _passable(rows: list[list[str]]) -> None:
    """Every edge's middle reached from the region's middle: where it isn't,
    a way is cut straight to it (a bridge over water, a path through the
    rest), so no region shuts the player in or out."""
    n = len(rows)
    mid = n // 2

    def reached() -> set[tuple[int, int]]:
        start = (mid, mid)
        if rows[mid][mid] not in WALKABLE_LAND:
            rows[mid][mid] = "="
        seen = {start}
        todo = deque([start])
        while todo:
            x, y = todo.popleft()
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < n and 0 <= ny < n and (nx, ny) not in seen:
                    if rows[ny][nx] in WALKABLE_LAND:
                        seen.add((nx, ny))
                        todo.append((nx, ny))
        return seen

    got = reached()
    for ex, ey in ((mid, 0), (mid, n - 1), (0, mid), (n - 1, mid)):
        if (ex, ey) in got:
            continue
        steps = max(abs(ex - mid), abs(ey - mid))
        for k in range(steps + 1):
            x = round(mid + (ex - mid) * k / steps)
            y = round(mid + (ey - mid) * k / steps)
            if rows[y][x] not in WALKABLE_LAND:
                rows[y][x] = "b" if rows[y][x] == "~" else "="
        got = reached()


def build_region(
    design: RegionDesign,
    *,
    key: str,
    rect: tuple[float, float, float],
    holder_id: str | None,
    labels_from: int,
    model: str = "",
    made_at: str = "",
    story: Sequence[WorldPlace] = (),
    player: tuple[float, float] | None = None,
    grounds: Sequence[Box] | None = None,
) -> tuple[DesignedRegion, TerrainPatch]:
    """A design as the region the map keeps and its land: the shapes drawn
    on the designer's grid and each tile drawn DESIGN_SCALE times over, at
    walking scale; its own place (kind "region", holding the rest) and the
    places, things (each where it lies) and people (labelled from
    `labels_from`) in it. Nothing is put on a story place's own ground
    (`grounds`, `ground_boxes`; the story places' squares when not given):
    a place there would be the story's drawn again, and a thing or a person
    stood on ground played on its plan, where nobody sees them."""
    west, north, side = rect
    tile = side / DESIGN_GRID
    # Rock is the look of the open ground, walked on; the rock the designer
    # lays as shapes is rock.
    spec = {
        "ground": "dirt" if design.ground == "rock" else design.ground,
        "features": design.features,
    }
    rows, _objects, _labels = rasterise(spec, DESIGN_GRID, DESIGN_GRID)
    grid = [list(row) for row in rows]
    _passable(grid)
    play = ["".join(ch * DESIGN_SCALE for ch in row) for row in grid for _ in range(DESIGN_SCALE)]

    def at(gx: float, gy: float) -> tuple[float, float]:
        return west + (gx + 0.5) * tile, north - (gy + 0.5) * tile

    own = {_plain(n) for s in story for n in (s.name, *s.aliases)}
    ground = list(grounds) if grounds is not None else _squares(story)
    # A region named for the story's own place (the designer called the cell
    # inside the 1500 m town "Marrowgate"; live, Oct 18 2026): the read's
    # "inside: Marrowgate" then found the region first, and a new house in
    # the town went into it, its visitor stood out on open country.
    name = design.name
    if _plain(name) in own:
        name = f"the outskirts of {design.name}"
    region = WorldPlace(
        id=new_id(),
        name=name,
        kind="region",
        what=design.what,
        inside=holder_id,
        x=west + side / 2,
        y=north - side / 2,
        size=side,
    )
    places = [region]
    for p in design.places:
        w = min(p["w"], DESIGN_GRID - p["x"])
        h = min(p["h"], DESIGN_GRID - p["y"])
        x, y = at(p["x"] + (w - 1) / 2, p["y"] + (h - 1) / 2)
        size = max(w, h) * tile
        if _plain(p["name"]) in own or _on_ground(ground, x, y, size / 2):
            # The story's own place, drawn again (a second farmhouse). Not
            # for one in a forest too wide for a plan: every place designed
            # in The Silverwood was dropped as overlapping the forest.
            continue
        if p["kind"] == "settlement":
            size = max(size, 60.0)
        places.append(
            WorldPlace(
                name=p["name"],
                kind=p["kind"],
                what=p["what"],
                inside=region.id,
                x=x,
                y=y,
                size=max(size, 4.0),
            )
        )
    things: list[WorldThing] = []
    spots: dict[str, list[float]] = {}
    for t in design.things:
        x, y = at(t["x"], t["y"])
        if _on_ground(ground, x, y):
            # On ground played on its own plan (The Ashen Reach's Shaft Nine
            # doesn't fill its region; the review, Oct 20 2026), where the
            # plan shows only its own things.
            continue
        near = min(
            (p for p in places[1:]),
            key=lambda p: math.hypot((p.x or 0) - x, (p.y or 0) - y),
            default=None,
        )
        # Beside a place (within BESIDE metres of its walls) it is that
        # place's; in the open, the region's.
        holder = (
            near.id
            if near is not None
            and math.hypot((near.x or 0) - x, (near.y or 0) - y) <= size_of(near) / 2 + BESIDE
            else region.id
        )
        thing = WorldThing(name=t["name"], at=holder)
        things.append(thing)
        spots[thing.id] = [x, y]
    people = []
    for p in design.people:
        x, y = at(p["x"], p["y"])
        if player is not None and math.hypot(x - player[0], y - player[1]) < CLEAR_OF_PLAYER:
            continue  # simply there, beside the player: never
        if _on_ground(ground, x, y):
            continue  # on a story place's own plan: unseen there, listed elsewhere
        n = len(people)
        people.append(
            DesignedPerson(
                label=f"Unknown {labels_from + n}", who=p["who"], doing=p["doing"], x=x, y=y
            )
        )
    designed = DesignedRegion(
        key=key,
        west=west,
        north=north,
        side=side,
        place_id=region.id,
        places=places,
        things=things,
        thing_spots=spots,
        people=people,
        model=model,
        made_at=made_at,
    )
    patch = TerrainPatch(
        place_id=region.id,
        x=west + side / 2,
        y=north - side / 2,
        side=side,
        rows=play,
        ground=design.ground,
        model=model,
        made_at=made_at,
        designed=True,
    )
    return designed, patch


def next_label(art: WorldArt, world: WorldMap | None = None) -> int:
    """The number the next designed person's label starts from: past every
    label the designer has given, and past every label the story's map has
    made (`world.unknowns_made`, which the fold raises to the designed labels
    too, so the story's next unknown after the first wave took the next
    number in the designer's range, and the next wave gave it again: the
    designed person was then the story's unknown on every fold; the review,
    Oct 20 2026)."""
    used = [
        int(p.label.rsplit(" ", 1)[-1])
        for r in art.regions.values()
        for p in r.people
        if p.label.rsplit(" ", 1)[-1].isdigit()
    ]
    if world is not None:
        used.append(world.unknowns_made)
        used += [int(label.split()[-1]) for label in world.unknowns if is_unknown(label)]
        used += [int(name.split()[-1]) for name in world.where if is_unknown(name)]
    return max([LABEL_FROM - 1, *used]) + 1


# --- the regions on the map ---------------------------------------------------------


def designed_delta(art: WorldArt) -> WorldDelta | None:
    """The designer's regions as the map folds them in, under the story
    (`world.world_at`): their places, things and people, the places seen
    once the region has been explored."""
    if not art.regions:
        return None
    delta = WorldDelta()
    explored = set(art.explored)
    for key in sorted(art.regions):
        region = art.regions[key]
        delta.places += [p.model_copy() for p in region.places]
        delta.things += [t.model_copy() for t in region.things]
        for person in region.people:
            delta.where[person.label] = region.place_id
            delta.unknowns[person.label] = person.who
        if key in explored:
            delta.seen += [p.id for p in region.places]
    return delta


def has_ground(world: WorldMap | dict[str, WorldPlace], place: WorldPlace) -> bool:
    """Whether a story place is ground of its own, played on a plan (a
    farm, a town, a site, a building), not open country however it is named:
    a region, a road, a vehicle (it has no spot of its own) or anything wider
    than a plan is drawn for (PLAN_MAX_SIZE: The Silverwood's 16 km forest is
    played as open country, its land in windows). `world`: the map, or its
    places by id."""
    places = world.places if isinstance(world, WorldMap) else world
    return (
        place.kind not in ("region", "route")
        and not moves(places, place)
        and size_of(place) <= PLAN_MAX_SIZE
    )


Box = tuple[float, float, float, float]  # west, south, east, north, in metres


def ground_boxes(world: WorldMap, art: WorldArt, places: Sequence[WorldPlace]) -> list[Box]:
    """The ground of each of `places` that has ground of its own
    (`has_ground`), as the map lies it out: its plan's rectangle where it has
    one, else its square (`marks.footprint`)."""
    out = []
    for place in places:
        if place.id in world.places and has_ground(world, place):
            box = footprint(world, art, place.id)
            if box is not None:
                out.append(box)
    return out


def _squares(story: Sequence[WorldPlace]) -> list[Box]:
    """`ground_boxes` with no map: each story place's square."""
    by_id = {p.id: p for p in story}
    out = []
    for s in story:
        if s.x is None or s.y is None or not has_ground(by_id, s):
            continue
        half = size_of(s) / 2
        out.append((s.x - half, s.y - half, s.x + half, s.y + half))
    return out


def _on_ground(boxes: Sequence[Box], x: float, y: float, half: float = 0.0) -> bool:
    """Whether a point (with `half`, a square round it) lies on any box."""
    return any(
        x + half > west and x - half < east and y + half > south and y - half < north
        if half
        else west <= x <= east and south <= y <= north
        for west, south, east, north in boxes
    )


def file_covers(
    world: WorldMap, art: WorldArt, ids: Collection[str], point: tuple[float, float]
) -> bool:
    """Whether one of a plot file's places (`ids`: `Story.world_base`'s, as
    the map has them now) with ground of its own (`has_ground`) covers a
    point: its plan's rectangle where it has one, else its square
    (`marks.footprint`): off the farm's plan is off the farm."""
    for place_id in ids:
        place = world.places.get(place_id)
        if place is None or not has_ground(world, place):
            continue
        box = footprint(world, art, place_id)
        if box is None:
            continue
        west, south, east, north = box
        if west <= point[0] <= east and south <= point[1] <= north:
            return True
    return False


def unexplored_ids(art: WorldArt) -> set[str]:
    """The ids of the places and things the designer made in regions the
    author's character has not been in yet (`WorldArt.explored`): nothing is
    drawn for them until then. Live (the review, Oct 20 2026), opening the
    Doomsville map sample wanted ~30 sprites for the scatter round the farm,
    a sled, rifles, flares, before John had taken a step: the people the
    designer put in each region had "visited" it, which put its things in
    view of the map pictures."""
    explored = set(art.explored)
    return {
        item.id
        for key, region in art.regions.items()
        if key not in explored
        for item in (*region.places, *region.things)
    }


def designed_point(art: WorldArt, name: str) -> tuple[float, float] | None:
    """Where the designer put someone, in metres, or None."""
    for region in art.regions.values():
        for person in region.people:
            if person.label == name:
                return person.x, person.y
    return None


def designed_spot(art: WorldArt, thing_id: str) -> tuple[float, float] | None:
    """Where the designer put a thing, in metres, or None."""
    for region in art.regions.values():
        spot = region.thing_spots.get(thing_id)
        if spot is not None and len(spot) == 2:
            return float(spot[0]), float(spot[1])
    return None


def standing_region(art: WorldArt, point: tuple[float, float]) -> tuple[float, float, float]:
    """The region of the grid (west, north, side) a point lies in."""
    origin = (float(art.region_origin[0]), float(art.region_origin[1]))
    return cell_rect(origin, *cell_of(origin, *point))


def into_place(
    world: WorldMap, art: WorldArt, place_id: str | None, point: tuple[float, float] | None
) -> tuple[float, float] | None:
    """Where someone the map has at a place stands, from their own point:
    that point, unless the place lies wholly in other regions of the grid (a
    read moved them there, its passage naming it: `world.resolve_reply(
    standing_in=)`). Then the nearest ground of the place to it, a few metres
    in: kept at their own point, they stood on in the region they had left
    while the map had them in the next."""
    if point is None or not art.region_origin:
        return point
    place = world.places.get(place_id or "")
    centre = position(world, place_id) if place is not None else None
    if place is None or centre is None or place.kind == "route":
        return point
    half = size_of(place) / 2
    if meets(centre, half, standing_region(art, point)):
        return point
    inset = min(INSET, half)
    x = min(max(point[0], centre[0] - half + inset), centre[0] + half - inset)
    y = min(max(point[1], centre[1] - half + inset), centre[1] + half - inset)
    return x, y


def for_the_read(world: WorldMap, art: WorldArt, point: tuple[float, float] | None) -> WorldMap:
    """The map as the map read is shown it: of the designer's regions, only
    the one the author's character stands in, with all that is in it (none
    when they stand nowhere yet). The rest go with their places, things and
    people; their names still find them, since the read's reply is matched
    against the whole map. Live (Oct 18 2026) the read was shown every
    designed region and took John's own fields for the North Woodlot next
    door, whose words said dead farmland."""
    if not art.regions:
        return world
    here = None
    if point is not None and art.region_origin:
        origin = (float(art.region_origin[0]), float(art.region_origin[1]))
        here = region_at(art, origin, *point)
    hidden = {r.place_id for r in art.regions.values() if r is not here}
    if not hidden:
        return world
    # Everything inside a hidden region goes with it (a place the story
    # added inside one too).
    grown = True
    while grown:
        inner = {k for k, p in world.places.items() if p.inside in hidden and k not in hidden}
        grown = bool(inner)
        hidden |= inner
    places = {k: p for k, p in world.places.items() if k not in hidden}
    where = {n: p for n, p in world.where.items() if p not in hidden}
    gone = set(world.where) - set(where)
    things = {
        k: t
        for k, t in world.things.items()
        if (t.carried_by not in gone if t.carried_by else t.at not in hidden)
    }
    return world.model_copy(
        update={
            "places": places,
            "links": [k for k in world.links if k.a in places and k.b in places],
            "where": where,
            "things": things,
            "outside": [n for n in world.outside if n in where],
            "unknowns": {
                k: v for k, v in world.unknowns.items() if k in where or k not in world.where
            },
        }
    )
