"""The map's art (models/world.py WorldArt): pure. What needs drawing, the
requests that draw it, and the checks every reply passes before it is kept.

Measured in SealedLoreDocs design/map.md: a text model writing a small SVG
that code turns into pixel art in a fixed palette reads at a glance, where a
model typing pixels doesn't; a changed state asked as an edit of the model's
own drawing stays the same object; a place laid out as shapes the program
draws is coherent where typed tile grids came back the wrong size or
garbled. So the model names shapes and the program draws.

Everything is drawn once and kept: a place looks the same every time the
story comes back to it, and the land around a place, drawn the first time the
story reaches it, is part of the map from then on.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sealedlore.engine import deck_plan
from sealedlore.engine.levels import (
    STAIRS,
)
from sealedlore.engine.prompt_texts import PromptTexts
from sealedlore.engine.world import key
from sealedlore.engine.world_layout import SMALL_CRAFT, moves, position, size_of
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.world import (
    LevelLayout,
    PlaceLayout,
    TerrainPatch,
    WorldArt,
    WorldMap,
    WorldPlace,
    WorldThing,
)

# The story's palette: the 16 colours every sprite is drawn in (the view
# snaps each pixel to the nearest), letters for the record.
PALETTE: dict[str, str] = {
    "k": "#1b1d2b",
    "G": "#4a5468",
    "g": "#8f9bb0",
    "w": "#f2f2ee",
    "N": "#5a3826",
    "n": "#94603e",
    "s": "#dcb08c",
    "y": "#f6cf6e",
    "o": "#e6773f",
    "r": "#b3384a",
    "p": "#7d4a8f",
    "b": "#3557b8",
    "c": "#6fd3e8",
    "E": "#2a6b5c",
    "e": "#43a44f",
    "l": "#a8dc6a",
}

# The tiles a layout or a patch of land is drawn in, and what each is. The
# view gives each its look; the models never see these characters.
TILES: dict[str, str] = {
    ".": "open ground",
    ",": "brush or tall grass",
    "T": "trees",
    "^": "rock",
    "~": "water or ice",
    "=": "road or path",
    "#": "wall",
    "+": "door",
    "_": "floor",
    "W": "window",
    "%": "fence or barricade",
    ":": "rubble",
    "*": "fire",
    "s": "sand",
    "&": "stairs or a ladder or a lift",
    " ": "nothing: space, or the dark of rock",
}
# Words a reply uses for the void round a hull. The enclosed prompt itself
# says 'Space ("dark") only outside the hull', and live (Oct 17 2026) GLM 5.3
# Flash answered a station's plan with "ground": "dark": the void was drawn
# as walkable plating and the seven decks it listed were never drawn
# (`has_levels` took only "floor" or "space").
SPACE_WORDS = frozenset({"space", "dark", "void", "vacuum", "stars", "starfield", "outer space"})


def ground_of(spec: dict[str, Any] | None) -> str:
    """A plan's ground word as the program reads it: lower case, and any
    word for the void round a hull read as "space"."""
    word = " ".join(str((spec or {}).get("ground") or "").lower().split())
    return "space" if word in SPACE_WORDS else word


GROUND: dict[str, str] = {
    "snow": ".",
    "grass": ".",
    "dirt": ".",
    "sand": "s",
    "rock": "^",
    "floor": "_",
    "space": " ",
}
FILLS: dict[str, str] = {
    "water": "~",
    "ice": "~",
    "rock": "^",
    "trees": "T",
    "brush": ",",
    "rubble": ":",
    "fire": "*",
    "floor": "_",
    "ground": ".",
    "dark": " ",
    "stairs": "&",
}

LAYOUT_WIDTH, LAYOUT_HEIGHT = 40, 24
PLAN_ASPECT = LAYOUT_HEIGHT / LAYOUT_WIDTH
# A place wider than this gets a grid twice as fine (80×48): at 40 tiles a
# 1.5 km town had 37 m tiles, and its buildings ballooned to fit them.
WIDE_PLAN_FROM = 800.0
# A plan's tile is its place's width over the grid, never finer than this.
MIN_PLAN_TILE = 0.5
# Out of doors, a plan's tile is at least a stride: a boundary stone (3 m)
# was drawn on a 20 m plan at half a metre a tile, its people a quarter of
# the size they were at the toll bridge beside it (Oct 18 2026). Now its plan
# is the 60 m of scrub round it, at the play view's walking scale.
MIN_OUTDOOR_TILE = 1.5
# The local map at a place is the plan of the outermost place it is in that
# is at most this wide: the farm for its farmhouse, the town for its diner,
# the caves and not the hills they are in. Wider places are country.
LOCAL_MAX_SIZE = 2_500.0
# A place wider than a local area still gets a plan when it is the innermost
# place someone stands in (a 4 km city with nothing finer round the
# character), up to this wide: on the 80-column grid a plan's tile is the
# width over 80, so 4 km is 50 m a tile and 8 km 100 m, which the grid can
# still carry (coarse, but there). Open country wider than this, a region, a
# desert, a sea, or no place at all, is played on windows of the land
# instead (`window_rect`; engine/area.py `build_window`): the author, Oct 16
# 2026, "the world should be limitless, short of the limits of the player's
# hardware and funds for API calls".
PLAN_MAX_SIZE = 8_000.0
# A building fixed in its parent's plan is at least this many tiles across.
MIN_BUILDING_TILES = 4
TERRAIN_TILES = 40
# The land around a place: four times its width, within these bounds.
TERRAIN_MIN_SIDE, TERRAIN_MAX_SIDE = 400.0, 40_000.0
# Land along a way between two known places is drawn in patches this wide.
WAY_PATCH_SIDE = 1_600.0
# Open country is played on windows of the land (engine/area.py
# `build_window`): the land patch under the character where one is drawn
# and no wider than WINDOW_SIDE, else a cell of a fixed grid this wide
# (40 m a tile at 40 columns), or a finer cell where the grid cell's middle
# is already under land (a place's own patch lies anywhere, never on the
# grid): 800 m (20 m a tile), then 400 m (10 m a tile). A cell is drawn as a
# land patch when the character stands on it, and the cells its edges lead
# to next, WINDOW_PATCHES at most a read, so a walk across a desert costs a
# patch or two at a time.
WINDOW_SIDE = WAY_PATCH_SIDE
WINDOW_LEVELS = (1, 2, 4)
WINDOW_PATCHES = 3
# The world designer's regions (engine/regions.py REGION_SIDE, kept equal):
# open country on their grid once a story has one, played at REGION_TILES.
REGION_GRID_SIDE = 400.0
REGION_GRID_TILES = 160
# A way longer than this many patches gets none: it isn't country the player
# has walked, and a link read in light-years ("Bajor, five light-years away")
# planned 3e13 patches and hung the thread that asked (the review, Oct 15
# 2026). The land round each end's place is drawn as before.
MAX_WAY_PATCHES = 32
MAX_OBJECTS = 9
# Places drawn as a sprite on the map; regions, routes, areas and rooms are
# drawn as land and layouts instead.
SPRITE_KINDS = frozenset({"settlement", "building", "site", "vehicle", None})
LAYOUT_KINDS = frozenset({"settlement", "building", "site", "area", "room", "vehicle", None})
# A job that keeps failing is let be after this many tries.
MAX_FAILURES = 2
SPRITE_MAX_TOKENS = 4000
LAYOUT_MAX_TOKENS = 4000


# --- SVG, cleaned ---------------------------------------------------------------

_SVG_NS = "http://www.w3.org/2000/svg"
_SHAPES = {"g", "rect", "circle", "ellipse", "polygon", "polyline", "path", "line"}
_ATTRIBUTES = {
    "x",
    "y",
    "width",
    "height",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "points",
    "d",
    "x1",
    "y1",
    "x2",
    "y2",
    "fill",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "fill-rule",
    "transform",
}
_PAINT = re.compile(r"^(none|#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6})$")
_NUMBERS = re.compile(r"^[-+0-9.eE,\s]*$")
_PATH = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9.eE,\s+-]*$")
_TRANSFORM = re.compile(r"^((translate|rotate|scale|matrix|skewX|skewY)\([-+0-9.eE,\s]*\)\s*)*$")
MAX_SVG_CHARS = 40_000


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _copy_shapes(source: ElementTree.Element, target: ElementTree.Element, depth: int = 0) -> int:
    if depth > 8:
        return 0
    count = 0
    for child in source:
        name = _local(child.tag) if isinstance(child.tag, str) else ""
        if name not in _SHAPES:
            continue
        kept = {k: v for k, v in child.attrib.items() if _safe_attribute(_local(k), v)}
        element = ElementTree.SubElement(target, name, {_local(k): v for k, v in kept.items()})
        count += 1 if name != "g" else _copy_shapes(child, element, depth + 1)
    return count


def _safe_attribute(name: str, value: str) -> bool:
    if name not in _ATTRIBUTES or len(value) > 8_000:
        return False
    if name in ("fill", "stroke"):
        return bool(_PAINT.match(value.strip()))
    if name == "d":
        return bool(_PATH.match(value))
    if name == "transform":
        return bool(_TRANSFORM.match(value.strip()))
    if name in ("stroke-linecap", "stroke-linejoin", "fill-rule"):
        return value in ("butt", "round", "square", "miter", "bevel", "nonzero", "evenodd")
    return bool(_NUMBERS.match(value))


# --- Layouts and land, drawn by the program ------------------------------------


# A coordinate or size in a drawing reply is held to this many tiles past
# the grid: "w": 1e9 made the building loop run for the rest of the session
# (the review, Oct 15 2026), and 1e999 (infinity) overflowed `round`.
COORD_LIMIT = 512


def _int(value: Any, default: int = 0) -> int:
    try:
        return max(-COORD_LIMIT, min(COORD_LIMIT, int(round(float(value)))))
    except (TypeError, ValueError, OverflowError):
        return default


def _line(points: Any) -> list[tuple[int, int]]:
    """Tiles along straight runs between points."""
    pts = [
        (_int(p[0]), _int(p[1]))
        for p in points or []
        if isinstance(p, (list, tuple)) and len(p) >= 2
    ]
    out: list[tuple[int, int]] = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(steps + 1):
            out.append((round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps)))
    return out or pts


def clean_svg(text: str) -> str | None:
    """The reply's SVG rebuilt from plain shapes alone, or None when there is
    none worth keeping. Anything else (scripts, links, images, text, styles,
    entities) never reaches the view, whatever the model wrote."""
    match = re.search(r"<svg\b.*?</svg>", text or "", re.S | re.I)
    source = match.group(0) if match is not None else _fragment_as_svg(text or "")
    if source is None or len(source) > MAX_SVG_CHARS:
        return None
    if re.search(r"<!(DOCTYPE|ENTITY)", source, re.I):
        return None
    try:
        root = ElementTree.fromstring(source)
    except ElementTree.ParseError:
        return None
    out = ElementTree.Element("svg", {"xmlns": _SVG_NS, "viewBox": "0 0 64 64"})
    count = _copy_shapes(root, out)
    if count == 0:
        return None
    return ElementTree.tostring(out, encoding="unicode")


def _fragment_as_svg(text: str) -> str | None:
    """A reply that drew shapes with no <svg> root (a "<g>…</g>" in a
    ```svg fence: GLM 5.3 Flash answered a rift so twice, live, and the
    plain shapes were thrown away), wrapped as the document the prompt
    asked for; None when there are no shapes in it."""
    fenced = re.search(r"```[a-zA-Z]*\s*(.*?)```", text, re.S)
    body = (fenced.group(1) if fenced is not None else text).strip()
    if not re.search(r"<(g|rect|circle|ellipse|polygon|polyline|path|line)\b", body, re.I):
        return None
    return f'<svg xmlns="{_SVG_NS}" viewBox="0 0 64 64">{body}</svg>'


def _copy_shapes(source: ElementTree.Element, target: ElementTree.Element, depth: int = 0) -> int:
    if depth > 8:
        return 0
    count = 0
    for child in source:
        name = _local(child.tag) if isinstance(child.tag, str) else ""
        if name not in _SHAPES:
            continue
        kept = {k: v for k, v in child.attrib.items() if _safe_attribute(_local(k), v)}
        element = ElementTree.SubElement(target, name, {_local(k): v for k, v in kept.items()})
        count += 1 if name != "g" else _copy_shapes(child, element, depth + 1)
    return count


def _safe_attribute(name: str, value: str) -> bool:
    if name not in _ATTRIBUTES or len(value) > 8_000:
        return False
    if name in ("fill", "stroke"):
        return bool(_PAINT.match(value.strip()))
    if name == "d":
        return bool(_PATH.match(value))
    if name == "transform":
        return bool(_TRANSFORM.match(value.strip()))
    if name in ("stroke-linecap", "stroke-linejoin", "fill-rule"):
        return value in ("butt", "round", "square", "miter", "bevel", "nonzero", "evenodd")
    return bool(_NUMBERS.match(value))


def cleaned_art(art: WorldArt) -> WorldArt:
    """`art` as read from its file with every sprite rebuilt by `clean_svg`,
    as drawn art always is: a scenario file or a sample's art beside its
    plot file may be anyone's. Art that is clean comes back the same."""
    sprites = []
    for sprite in art.sprites:
        svg = clean_svg(sprite.svg)
        if svg is not None:
            sprites.append(sprite if svg == sprite.svg else sprite.model_copy(update={"svg": svg}))
    art.sprites = sprites
    return art


def decked(rows: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """A station's or ship's plan as one deck (the author, Oct 15 2026:
    Deep Space 9's rooms were "not actually joined except by empty space",
    with people standing in space). The model draws rooms inside a hull and
    leaves what lies between them blank, and blank (" ") on a deck is space.
    Blank tiles the hull encloses (not reachable from the grid's edge
    through blank) are floor, and so is a gap of up to DECK_GAP blank tiles
    between two drawn tiles along a row or a column. Space outside the hull
    stays space."""
    width = max((len(r) for r in rows), default=0)
    grid = [list(r.ljust(width)) for r in rows]
    height = len(grid)
    void = " "
    filled = [row[:] for row in grid]

    def runs(line: list[str]) -> list[tuple[int, int]]:
        found, start = [], None
        for i, char in enumerate([*line, "#"]):
            if char == void and start is None:
                start = i
            elif char != void and start is not None:
                found.append((start, i))
                start = None
        return found

    for y in range(height):
        for start, end in runs(grid[y]):
            if 0 < start and end < width and end - start <= DECK_GAP:
                for x in range(start, end):
                    filled[y][x] = FLOOR
    for x in range(width):
        column = [grid[y][x] for y in range(height)]
        for start, end in runs(column):
            if 0 < start and end < height and end - start <= DECK_GAP:
                for y in range(start, end):
                    filled[y][x] = FLOOR
    # Space is what the edge reaches through blank tiles; the rest is deck.
    outside: set[tuple[int, int]] = set()
    edge = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if (x in (0, width - 1) or y in (0, height - 1)) and filled[y][x] == void
    ]
    while edge:
        x, y = edge.pop()
        if (x, y) in outside:
            continue
        outside.add((x, y))
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < width and 0 <= ny < height and filled[ny][nx] == void:
                edge.append((nx, ny))
    for y in range(height):
        for x in range(width):
            if filled[y][x] == void and (x, y) not in outside:
                filled[y][x] = FLOOR
    return tuple("".join(row) for row in filled)


def deck_layout(job: ArtJob, text: str, *, model: str, made_at: str) -> PlaceLayout:
    """A brief's reply laid out as the plan (the main deck) with every other
    deck as its levels, all drawn at once. ValueError when the brief has no
    rooms or they won't fit."""
    named = tuple(tuple(n) for n in job.extra.get("named") or ())
    brief = deck_plan.parse_brief(text, named=named)  # type: ignore[arg-type]
    plan = deck_plan.generate(
        brief,
        job.subject_id,
        hull=str(job.extra.get("hull") or ""),
        ring=bool(job.extra.get("ring", True)),
    )
    main = plan.decks[0]
    others = [d for i, d in sorted(plan.decks.items()) if i != 0]
    spec = {
        "ground": "space",
        "deck": True,
        "shape": plan.shape,
        "hull_kind": plan.hull,
        "hull": main.hull,
        "tile_m": plan.tile_m,
        "brief": brief.as_dict(),
        "rooms": main.room_records(),
        "main": main.name,
        "levels": [{"index": d.index, "name": d.name} for d in others],
        "features": [],
    }
    return PlaceLayout(
        place_id=job.subject_id,
        state=job.state,
        spec=spec,
        rows=main.rows,
        labels=main.labels,
        model=model,
        made_at=made_at,
        levels=[
            LevelLayout(
                index=d.index,
                name=d.name,
                place_id=job.subject_id,
                state=job.state,
                spec={"rooms": d.room_records(), "hull": d.hull},
                rows=d.rows,
                labels=d.labels,
                model=model,
                made_at=made_at,
            )
            for d in others
        ],
    )


def rasterise(
    spec: dict[str, Any], width: int, height: int
) -> tuple[list[str], dict[str, str], dict[str, list[int]]]:
    """(tile rows, numbered objects, named buildings by [x, y]): the shapes
    drawn in order, later over earlier. Anything off the grid is clipped."""
    grid = [[GROUND.get(ground_of(spec), ".")] * width for _ in range(height)]

    def put(x: int, y: int, ch: str) -> None:
        if 0 <= x < width and 0 <= y < height:
            grid[y][x] = ch

    objects: dict[str, str] = {}
    labels: dict[str, list[int]] = {}
    features = spec.get("features") if isinstance(spec.get("features"), list) else []
    for f in features:
        if not isinstance(f, dict):
            continue
        kind = f.get("kind")
        x, y = _int(f.get("x")), _int(f.get("y"))
        w, h = max(_int(f.get("w"), 1), 1), max(_int(f.get("h"), 1), 1)
        if kind == "area":
            ch = FILLS.get(str(f.get("fill")), ".")
            cx, cy = x + (w - 1) / 2, y + (h - 1) / 2
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    if f.get("shape") == "ellipse" and (
                        ((xx - cx) / (w / 2)) ** 2 + ((yy - cy) / (h / 2)) ** 2 > 1
                    ):
                        continue
                    put(xx, yy, ch)
        elif kind in ("road", "passage"):
            ch = "=" if kind == "road" else "_"
            thick = min(max(_int(f.get("width"), 1), 1), 6)
            for px, py in _line(f.get("points")):
                for d in range(thick):
                    put(px + d, py, ch)
                    put(px, py + d, ch)
        elif kind == "building":
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    edge = yy in (y, y + h - 1) or xx in (x, x + w - 1)
                    put(xx, yy, "#" if edge else "_")
            for key, ch in (("windows", "W"), ("doors", "+")):
                for d in f.get(key) or []:
                    if not isinstance(d, dict):
                        continue
                    at = _int(d.get("at"))
                    spot = {
                        "north": (x + at, y),
                        "south": (x + at, y + h - 1),
                        "west": (x, y + at),
                        "east": (x + w - 1, y + at),
                    }.get(str(d.get("side")))
                    if spot:
                        put(*spot, ch)
            name = str(f.get("name") or "").strip()
            if name:
                labels[name[:60]] = [x + w // 2, y + h // 2]
        elif kind in ("wall", "fence", "window"):
            ch = {"wall": "#", "fence": "%", "window": "W"}[kind]
            gaps = {(gx, gy) for gx, gy in _line(f.get("gaps")) or []}
            for px, py in _line(f.get("points")):
                if (px, py) not in gaps:
                    put(px, py, ch)
        elif kind == "stairs":
            # Stairs, a ladder or a lift (engine/levels.py): a tile or a
            # few of them, the door between a plan's levels.
            for yy in range(y, y + min(h, 4)):
                for xx in range(x, x + min(w, 4)):
                    put(xx, yy, STAIRS)
        elif kind == "object" and len(objects) < MAX_OBJECTS:
            if 0 <= x < width and 0 <= y < height:
                number = str(len(objects) + 1)
                grid[y][x] = number
                objects[number] = str(f.get("name") or "")[:80]
    return ["".join(row) for row in grid], objects, labels


# --- What needs drawing ---------------------------------------------------------


@dataclass
class ArtJob:
    """One drawing to make: its key (also what a failure is counted by), the
    messages to send, and what it is of."""

    key: str
    kind: Literal["sprite", "layout", "terrain"]
    messages: list[PromptMessage]
    subject_id: str
    name: str = ""
    state: str = ""
    from_sprite: str | None = None
    subject: Literal["place", "thing"] = "place"
    # A patch of land: where and how wide.
    patch: tuple[float, float, float] | None = None
    order: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _message(role: str, text: str) -> PromptMessage:
    return PromptMessage(role=role, parts=(ContentPart(text=text),))  # type: ignore[arg-type]


def _palette_line() -> str:
    return ", ".join(PALETTE.values())


def _grid_fill(texts: PromptTexts, key: str, width: int, height: int, tile: str) -> str:
    return texts.fill(
        key,
        width=str(width),
        height=str(height),
        last_x=str(width - 1),
        last_y=str(height - 1),
        tile=tile,
    )


def _tile_words(metres: float) -> str:
    if metres < 1.5:
        return "one metre"
    if metres < 950:
        return f"{metres:.0f} metres"
    return f"{metres / 1000:.1f} km".replace(".0 km", " km")


def _what(place: WorldPlace) -> str:
    kind = place.kind or "place"
    return f"{place.name}, a {kind}" + (f": {place.what}" if place.what else "")


# --- Plans on the map ---------------------------------------------------------------


def plan_grid(place: WorldPlace) -> tuple[int, int]:
    """(tiles across, tiles down) of a place's plan."""
    if size_of(place) > WIDE_PLAN_FROM:
        return 2 * LAYOUT_WIDTH, 2 * LAYOUT_HEIGHT
    return LAYOUT_WIDTH, LAYOUT_HEIGHT


def plan_tile(place: WorldPlace) -> float:
    """Metres a tile of the place's plan covers: at least walking scale out
    of doors (MIN_OUTDOOR_TILE), so a small place's plan is the ground round
    it."""
    least = MIN_PLAN_TILE if place.kind in ("building", "room", "vehicle") else MIN_OUTDOOR_TILE
    return max(size_of(place) / plan_grid(place)[0], least)


def plan_rect(world: WorldMap, place: WorldPlace) -> tuple[float, float, float, float] | None:
    """(west, north, width, height) in metres of a place's plan on the map:
    centred on the place (a vehicle's: where it is), the grid at its tile.
    None for a place with no spot."""
    spot = position(world, place.id)
    if spot is None:
        return None
    tile = plan_tile(place)
    columns, rows = plan_grid(place)
    width, height = tile * columns, tile * rows
    return spot[0] - width / 2, spot[1] + height / 2, width, height


def local_area(world: WorldMap, place_id: str | None) -> WorldPlace | None:
    """The place whose plan is the local map at `place_id`: the outermost
    place it is in up to LOCAL_MAX_SIZE wide, else itself."""
    chain: list[WorldPlace] = []
    seen: set[str] = set()
    current = world.places.get(place_id or "")
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.append(current)
        current = world.places.get(current.inside or "")
    if not chain:
        return None
    best = chain[0]
    for place in chain[1:]:
        if size_of(place) <= LOCAL_MAX_SIZE:
            best = place
    return best


# The parts of a building that are outside it, by their name: whoever is
# there stands at its door on the ground round it, and its own plan (its
# inside) has no place for them. AfterDark (Oct 18 2026): the read put "The
# porch overhang" inside the farmhouse, its label stood in the house's
# middle, and Suzuka on it stood in the far hall.
_OUTSIDE_PART = re.compile(
    r"\b(porch|veranda|verandah|stoop|doorstep|steps|overhang|yard|backyard|garden|"
    r"driveway|forecourt|exterior|outside)\b",
    re.IGNORECASE,
)
_HOLDS_INSIDE = frozenset({"building", "room"})


def outside_part(world: WorldMap, place: WorldPlace | None) -> bool:
    """A place inside a building (or room) whose name says it is outside
    it: a porch, the steps, the yard."""
    if place is None or not place.inside:
        return False
    parent = world.places.get(place.inside)
    return (
        parent is not None
        and parent.kind in _HOLDS_INSIDE
        and bool(_OUTSIDE_PART.search(place.name))
    )


def _drawn_in(world: WorldMap, child: WorldPlace, plan_id: str) -> bool:
    """Whether `child` is drawn in the plan of `plan_id`: inside it, or
    inside a place in it that stands on its plan (a quarter, a square, a
    yard: anything but a building, room or vehicle, which have insides of
    their own). Live (Oct 18 2026) Cade's shop, in Saltmarsh's guild
    quarter, had no box on the town's plan and no door: nobody could go in."""
    at, seen = child.inside, set()
    while at and at not in seen:
        if at == plan_id:
            return True
        seen.add(at)
        parent = world.places.get(at)
        if parent is None or parent.kind in ("building", "room", "vehicle"):
            return False
        at = parent.inside
    return False


def fixed_in_plan(world: WorldMap, place: WorldPlace) -> list[dict[str, Any]]:
    """What the map already puts in a place's plan, where it stays: each
    place inside it with a spot of its own, as a tile box. A building is
    drawn there by the program (`with_fixed`); anything else is told to the
    model, which draws what is there, and labelled. The box keeps the plan's
    aspect, so a building's own plan fits its footprint exactly."""
    rect = plan_rect(world, place)
    if rect is None:
        return []
    west, north, _w, _h = rect
    tile = plan_tile(place)
    columns, rows = plan_grid(place)
    out: list[dict[str, Any]] = []
    for child in world.places.values():
        if not _drawn_in(world, child, place.id) or child.x is None or child.y is None:
            continue
        if moves(world.places, child) or outside_part(world, child):
            continue
        size = size_of(child)
        w = min(max(MIN_BUILDING_TILES, round(size / tile)), columns)
        h = min(max(3, round(w * PLAN_ASPECT)), rows)
        x = round((child.x - size / 2 - west) / tile)
        y = round((north - (child.y + size * PLAN_ASPECT / 2)) / tile)
        x = min(max(x, 0), columns - w)
        y = min(max(y, 0), rows - h)
        out.append(
            {
                "kind": "building" if child.kind == "building" else "place",
                "name": child.name,
                # The names it has had: a plan drawn before the author's
                # Direction renamed it names its box by the old one.
                "aliases": list(child.aliases),
                "place_id": child.id,
                "place_kind": child.kind or "place",
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "what": child.what,
            }
        )
    return out


# Ground a plan may share with the land around it (`plan_ground`).
OPEN_GROUND = frozenset({"snow", "grass", "dirt", "sand"})


def patch_under(art: WorldArt, x: float, y: float) -> TerrainPatch | None:
    """The finest land drawn under a point, or None; a designed region's
    before other land as wide (engine/regions.py)."""
    for patch in sorted(art.terrain, key=lambda p: (p.side, not p.designed)):
        half = patch.side / 2
        if abs(x - patch.x) <= half and abs(y - patch.y) <= half:
            return patch
    return None


def land_under(world: WorldMap, art: WorldArt, place: WorldPlace) -> str:
    """The ground of the finest land drawn under a place, or ""."""
    spot = position(world, place.id)
    if spot is None:
        return ""
    patch = patch_under(art, *spot)
    return patch.ground if patch is not None else ""


# --- Whose plan someone stands on, and the windows of open country ------------


# A vehicle no wider than this (a skiff, a cart, a rowing boat) has no inside
# to play on: its riders stand at its spot (the author, Oct 18 2026: a rowing
# skiff was drawn as a 40-tile boathouse with a workbench).
SMALL_VEHICLE_M = 12.0


def small_vehicle(place: WorldPlace) -> bool:
    """No wider than `SMALL_VEHICLE_M`, or named a small craft whatever its
    size says: a map from before kept the kind's 25 m for "a small boat"."""
    if place.kind != "vehicle":
        return False
    return size_of(place) <= SMALL_VEHICLE_M or bool(
        SMALL_CRAFT.search(f"{place.name} {place.what}")
    )


def has_inside(art: WorldArt, place: WorldPlace) -> bool:
    """A building (or room) with a plan of its own: its inside is an area."""
    return (
        place.kind in ("building", "room", "vehicle")
        and not small_vehicle(place)
        and art.layout_shown(place.id, place.state) is not None
    )


def is_deck(world: WorldMap, art: WorldArt, place_id: str | None) -> bool:
    """A station or ship: a plan whose ground is space. Its rooms are drawn
    on the deck and joined by it, so they are the deck's, not areas of their
    own."""
    place = world.places.get(place_id or "")
    if place is None:
        return False
    layout = art.layout_shown(place.id, place.state)
    if layout is None:
        return False
    # Its own ground, or the land it stands on: the enclosed prompt asks for
    # the hull as "floor" with space outside, so the deck's own word is
    # seldom "space" (the second review).
    return ground_of(layout.spec) == "space" or land_under(world, art, place) == "space"


def _chain(world: WorldMap, place_id: str | None) -> list[WorldPlace]:
    out: list[WorldPlace] = []
    seen: set[str] = set()
    current = world.places.get(place_id or "")
    while current is not None and current.id not in seen:
        seen.add(current.id)
        out.append(current)
        current = world.places.get(current.inside or "")
    return out


# An outdoor place no wider than this is a feature on the ground (a stone, a
# cairn, a well, a shrine): where a plan already covers its spot it stands
# on that plan, marked, and gets none of its own (`covering_plan`).
FEATURE_MAX_SIZE = 10.0


def covering_plan(world: WorldMap, art: WorldArt, place: WorldPlace) -> WorldPlace | None:
    """For a small outdoor place with no plan of its own (`FEATURE_MAX_SIZE`)
    and nothing round it with one: the place whose drawn outdoor plan covers
    its spot, the smallest such. Live (Oct 18 2026) a boundary stone, a marker
    post and a cairn within eight metres of each other each got a plan, and
    the same ground was drawn three times over, three ways."""
    if place.kind in ("building", "room", "vehicle") or size_of(place) > FEATURE_MAX_SIZE:
        return None
    if art.layout_shown(place.id, place.state) is not None:
        return None
    spot = position(world, place.id)
    if spot is None:
        return None
    best: tuple[float, WorldPlace] | None = None
    for other in world.places.values():
        if other.id == place.id or other.kind in ("building", "room", "vehicle", "route"):
            continue
        if other.kind not in LAYOUT_KINDS or local_area(world, other.id) is not other:
            continue
        if art.layout_shown(other.id, other.state) is None:
            continue
        rect = plan_rect(world, other)
        if rect is None:
            continue
        west, north, width, height = rect
        if west <= spot[0] <= west + width and north - height <= spot[1] <= north:
            if best is None or width < best[0]:
                best = (width, other)
    return best[1] if best is not None else None


def plan_place(world: WorldMap, art: WorldArt, place_id: str | None) -> WorldPlace | None:
    """The place whose plan someone at `place_id` stands on: the innermost
    building round them with an inside of its own, else the local area's
    plan (the farm for its yard, the town for its street), else a wider
    place up the chain that has one (a city too wide for a local area, drawn
    at PLAN_MAX_SIZE's grain). A room of a deck is the deck. None where no
    plan is drawn: open country, played on a window of the land."""
    chain = _chain(world, place_id)
    decks = [p for p in chain if is_deck(world, art, p.id)]
    if decks:
        return decks[-1]
    for place in chain:
        if has_inside(art, place):
            return place
    local = local_area(world, place_id)
    if local is not None and art.layout_shown(local.id, local.state) is not None:
        return local
    if local is not None:
        covering = covering_plan(world, art, local)
        if covering is not None:
            return covering
    for place in chain:
        if (
            place.kind in LAYOUT_KINDS
            and size_of(place) <= PLAN_MAX_SIZE
            and art.layout_shown(place.id, place.state) is not None
        ):
            return place
    return None


def _window_covered(patches: Sequence[TerrainPatch], x: float, y: float, side: float) -> bool:
    return any(_covers(p, x, y, side) for p in patches)


def window_cell(patches: Sequence[TerrainPatch], x: float, y: float) -> tuple[float, float, float]:
    """(west, north, side) of the grid cell to play on at a point under no
    land of its own: the WINDOW_SIDE cell holding the point, or, where that
    cell's middle is already under land at least half as wide (so a patch
    for it would be dropped, `adopt`), the finer cell holding it, down to
    the finest level; the coarsest cell when every level's middle is covered
    (a strip beside a patch: plain ground, walked across)."""
    for level in WINDOW_LEVELS:
        side = WINDOW_SIDE / level
        west, south = math.floor(x / side) * side, math.floor(y / side) * side
        if not _window_covered(patches, west + side / 2, south + side / 2, side):
            return west, south + side, side
    side = WINDOW_SIDE
    return math.floor(x / side) * side, math.floor(y / side) * side + side, side


def window_rect(art: WorldArt, x: float, y: float) -> tuple[float, float, float]:
    """(west, north, side) of the window of open country at a point: the
    land patch under it when one is drawn no wider than WINDOW_SIDE (its own
    rows at its own tile), else the grid cell there (`window_cell`), whose
    rows are the coarser land's under it resampled, or plain ground until
    its patch is drawn."""
    if art.region_origin and len(art.region_origin) == 2:
        # A story with designed regions plays open country on the region grid
        # (engine/regions.py), designed or not yet, so a spot stays where it is
        # when its region's design comes in.
        side = REGION_GRID_SIDE
        ox, oy = art.region_origin
        i, j = math.floor((x - ox) / side), math.floor((oy - y) / side)
        return ox + i * side, oy - j * side, side
    patch = patch_under(art, x, y)
    if patch is not None and patch.side <= WINDOW_SIDE + 1.0:
        return patch.x - patch.side / 2, patch.y + patch.side / 2, patch.side
    return window_cell(art.terrain, x, y)


WINDOW_PREFIX = "window:"


def window_id(west: float, north: float, side: float) -> str:
    """An area id for a window of open country: its rectangle on the map,
    so it needs no state and stands whether or not its land is drawn yet
    (`Spot.area`, engine/area.py)."""
    return f"{WINDOW_PREFIX}{west:.0f}:{north:.0f}:{side:.0f}"


def is_window(area_id: str | None) -> bool:
    return bool(area_id) and str(area_id).startswith(WINDOW_PREFIX)


def parse_window(area_id: str | None) -> tuple[float, float, float] | None:
    """(west, north, side) a window id names, or None for any other id."""
    if not is_window(area_id):
        return None
    try:
        west, north, side = (float(v) for v in str(area_id)[len(WINDOW_PREFIX) :].split(":"))
    except ValueError:
        return None
    if not (math.isfinite(west) and math.isfinite(north) and math.isfinite(side)) or side <= 0:
        return None
    return west, north, side


def patch_at(art: WorldArt, west: float, north: float, side: float) -> TerrainPatch | None:
    """The land patch drawn exactly at this rectangle (within a metre), or
    None; a designed region's before any other there."""
    for patch in sorted(art.terrain, key=lambda p: not p.designed):
        if (
            abs(patch.side - side) < 1.0
            and abs(patch.x - patch.side / 2 - west) < 1.0
            and abs(patch.y + patch.side / 2 - north) < 1.0
        ):
            return patch
    return None


# Gaps of up to this many blank tiles between a deck's rooms, along a row or
# a column, are corridor: the model leaves them blank, and blank on a deck
# is space.
DECK_GAP = 2
# Where someone may stand in a plan: not space, a wall, a window, water,
# trees, rock, a fence or fire.
WALKABLE = frozenset("._=+,:s&b")  # "b": the play view's bridge (engine/area.py)
# The tile a deck's blank is filled with (TILES["_"]).
FLOOR = "_"


def _fixed_lines(fixed: Sequence[dict[str, Any]]) -> str:
    lines = []
    for f in fixed:
        span = f"x {f['x']}-{f['x'] + f['w'] - 1}, y {f['y']}-{f['y'] + f['h'] - 1}"
        what = " ".join(str(f.get("what") or "").split())
        lines.append(f"- {f['name']} ({f['place_kind']}): {span}" + (f": {what}" if what else ""))
    return "\n".join(lines)


def _fixed_match(feature: dict[str, Any], fixed_one: dict[str, Any]) -> bool:
    """Whether a building the model drew is this fixed one: by place id when
    the shape carries one (`with_fixed` drew it), else by its name, under
    the place's name now or any it has had (`fixed_in_plan`'s aliases): a
    building the author's Direction renamed keeps its box."""
    if feature.get("place_id"):
        return feature["place_id"] == fixed_one.get("place_id")
    name = key(str(feature.get("name") or ""))
    known = {key(fixed_one["name"]), *(key(a) for a in fixed_one.get("aliases") or ())}
    return bool(name) and name in known


def with_fixed(spec: dict[str, Any], fixed: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The model's shapes with the map's buildings where the map has them:
    a building the model drew under a fixed name is dropped (its doors and
    windows kept), and each fixed building is drawn at its own box."""
    buildings = [f for f in fixed if f["kind"] == "building"]
    features = [f for f in spec.get("features", []) if isinstance(f, dict)]
    kept = [
        f
        for f in features
        if not (f.get("kind") == "building" and any(_fixed_match(f, b) for b in buildings))
    ]
    for f in buildings:
        drawn = next(
            (s for s in features if s.get("kind") == "building" and _fixed_match(s, f)), None
        )
        doors = [d for d in (drawn or {}).get("doors") or [] if isinstance(d, dict)]
        windows = [d for d in (drawn or {}).get("windows") or [] if isinstance(d, dict)]
        for opening in (*doors, *windows):  # within the box's own sides
            along = f["w"] if str(opening.get("side")) in ("north", "south") else f["h"]
            opening["at"] = min(max(_int(opening.get("at"), 1), 1), max(along - 2, 1))
        kept.append(
            {
                "kind": "building",
                "name": f["name"],
                "place_id": f.get("place_id"),
                "x": f["x"],
                "y": f["y"],
                "w": f["w"],
                "h": f["h"],
                "doors": doors or [{"side": "south", "at": f["w"] // 2}],
                "windows": windows,
            }
        )
    return {**spec, "features": kept}


def _thing_words(thing: WorldThing) -> str:
    return thing.name + (f" ({thing.state})" if thing.state else "")


def _ways(world: WorldMap, place: WorldPlace) -> list[str]:
    ways = []
    for link in world.links:
        if place.id not in (link.a, link.b):
            continue
        other = world.places.get(link.b if link.a == place.id else link.a)
        if other is None:
            continue
        words = ", ".join(w for w in (link.direction, link.distance) if w)
        if link.a == place.id:
            ways.append(f"to {other.name}" + (f" ({words})" if words else ""))
        else:
            ways.append(f"from {other.name}" + (f" ({words}, toward here)" if words else ""))
    return ways


def _centred_on(world: WorldMap, patch: TerrainPatch) -> bool:
    """Whether `patch` is its place's own land (`terrain_patch`), not land
    along a way kept under the place."""
    place = world.places.get(patch.place_id)
    if place is None or place.x is None or place.y is None:
        return False
    return abs(patch.x - place.x) < 1.0 and abs(patch.y - place.y) < 1.0


def _covers(patch: TerrainPatch, x: float, y: float, side: float | None = None) -> bool:
    """Whether `patch` has the land at (x, y): for land `side` wide wanted
    there, only a patch at least half as wide counts (a 400 m patch round a
    creek crossing kept the 1.6 km road past it from any land, live)."""
    if side is not None and patch.side < side / 2:
        return False
    half = patch.side / 2
    return abs(x - patch.x) <= half and abs(y - patch.y) <= half


def _top(world: WorldMap, place_id: str) -> WorldPlace | None:
    place = world.places.get(place_id)
    seen: set[str] = set()
    while place is not None and place.inside in world.places and place.id not in seen:
        seen.add(place.id)
        place = world.places[place.inside]  # type: ignore[index]
    return place


# --- A station's or ship's decks (engine/deck_plan.py) ------------------------------

# A hull's words, and the signs it is in space (a sailing ship has decks
# and a hold too: "ship" alone is not enough).
_HULL_WORDS = re.compile(
    r"\b(space ?stations?|stations?|starships?|spaceships?|space ?craft|starbases?|"
    r"outposts?|orbitals?|habitats?|ships?|vessels?|freighters?|shuttles?|cruisers?|"
    r"frigates?|corvettes?|destroyers?|carriers?|tugs?|haulers?|derelicts?|platforms?)\b",
    re.I,
)
_SPACE_SIGNS = re.compile(
    r"\b(space|orbit\w*|starships?|spaceships?|space ?craft|starbases?|hyperspace|warp|"
    r"jump ?(?:gates?|drives?|points?)|vacuum|zero-?g|airlocks?|gas giants?|asteroids?|"
    r"nebula\w*|light-?years?|planets?|interstellar|galax\w+)\b",
    re.I,
)


def holding_place(
    world: WorldMap, west: float, north: float, side: float, *, min_size: float = 0.0
) -> WorldPlace | None:
    """The smallest place at least `min_size` wide whose square holds the
    whole rectangle (a region, a desert, a city too wide for a plan), or
    None in no place at all."""
    best: tuple[float, WorldPlace] | None = None
    for place in world.places.values():
        if place.x is None or place.y is None or moves(world.places, place):
            continue
        if size_of(place) < min_size:
            continue
        half = size_of(place) / 2
        if (
            place.x - half <= west
            and west + side <= place.x + half
            and place.y - half <= north - side
            and north <= place.y + half
        ):
            if best is None or size_of(place) < best[0]:
                best = (size_of(place), place)
    return best[1] if best is not None else None


def _compass(dx: float, dy: float) -> str:
    names = ("north", "north-east", "east", "south-east", "south", "south-west", "west")
    names = (*names, "north-west")
    angle = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return names[round(angle / 45) % 8]
