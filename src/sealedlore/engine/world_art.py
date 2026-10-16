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

import json
import math
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.world import key
from sealedlore.engine.world_layout import moves, position, size_of
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.world import (
    PlaceLayout,
    TerrainPatch,
    WorldArt,
    WorldMap,
    WorldPlace,
    WorldSprite,
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
    " ": "nothing: space, or the dark of rock",
}
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
}

LAYOUT_WIDTH, LAYOUT_HEIGHT = 40, 24
PLAN_ASPECT = LAYOUT_HEIGHT / LAYOUT_WIDTH
# A place wider than this gets a grid twice as fine (80×48): at 40 tiles a
# 1.5 km town had 37 m tiles, and its buildings ballooned to fit them.
WIDE_PLAN_FROM = 800.0
# A plan's tile is its place's width over the grid, never finer than this.
MIN_PLAN_TILE = 0.5
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


def clean_svg(text: str) -> str | None:
    """The reply's SVG rebuilt from plain shapes alone, or None when there is
    none worth keeping. Anything else (scripts, links, images, text, styles,
    entities) never reaches the view, whatever the model wrote."""
    match = re.search(r"<svg\b.*?</svg>", text or "", re.S | re.I)
    if match is None or len(match.group(0)) > MAX_SVG_CHARS:
        return None
    source = match.group(0)
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


def rasterise(
    spec: dict[str, Any], width: int, height: int
) -> tuple[list[str], dict[str, str], dict[str, list[int]]]:
    """(tile rows, numbered objects, named buildings by [x, y]): the shapes
    drawn in order, later over earlier. Anything off the grid is clipped."""
    grid = [[GROUND.get(str(spec.get("ground")), ".")] * width for _ in range(height)]

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
        elif kind == "object" and len(objects) < MAX_OBJECTS:
            if 0 <= x < width and 0 <= y < height:
                number = str(len(objects) + 1)
                grid[y][x] = number
                objects[number] = str(f.get("name") or "")[:80]
    return ["".join(row) for row in grid], objects, labels


def parse_shapes(text: str) -> dict[str, Any]:
    spec = extract_json(text, dict, "layout")
    if not isinstance(spec.get("features"), list):
        raise ValueError("the layout has no features")
    return spec


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


def setting_of(world_bible: str | None, title: str) -> str:
    """The setting, briefly, for an art request: the world's opening words."""
    text = " ".join((world_bible or "").split())
    if not text:
        return title
    cut = text[:300]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > 80 else cut).rstrip(". ")


def _palette_line() -> str:
    return ", ".join(PALETTE.values())


def sprite_messages(what: str, setting: str, texts: PromptTexts) -> list[PromptMessage]:
    return [
        _message("system", texts.fill("map.sprite", palette=_palette_line())),
        _message("user", texts.fill("map.sprite.ask", what=what, setting=setting)),
    ]


def change_messages(
    first: WorldSprite, what: str, change: str, setting: str, texts: PromptTexts
) -> list[PromptMessage]:
    """The thing again in a new state, as an edit of its own last drawing."""
    return [
        *sprite_messages(what, setting, texts),
        _message("assistant", f"```svg\n{first.svg}\n```"),
        _message("user", texts.fill("map.sprite.change", what=what, change=change)),
    ]


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
    """Metres a tile of the place's plan covers."""
    return max(size_of(place) / plan_grid(place)[0], MIN_PLAN_TILE)


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


def in_sight(world: WorldMap, held: str | None) -> set[str]:
    """What shows through the fog for the character `held`: what the story
    has seen or told of, where they have been (an imported map's start
    before a passage has placed them), what that is in, and what is in it
    (stand somewhere and you know what it is in and what is in it)."""
    here = world.where.get(held or "") or world.start
    visited = set(world.visited.get(held or "", []))
    if world.start is not None and not visited:
        visited.add(world.start)
    been = visited | ({here} - {None})
    been |= containers(world, been)
    around = {p.id for p in world.places.values() if p.inside in been}
    return set(world.seen) | been | around


def out_of_the_fog(world: WorldMap, held: str | None) -> set[str]:
    """The places whose plans and pictures may be shown: those in sight
    (`in_sight`), and what stands in a local area that is (a building's
    inside, seen when zoomed in). A local area of its own must be in sight
    itself: a site in known country (a wreck in the ashfields) is not. A
    plot file's map can come with plans of places the story hasn't reached
    (a sample's are drawn whole), and showing one lifted the fog off it."""
    shown = in_sight(world, held)
    out = set()
    for pid in world.places:
        area = local_area(world, pid)
        if pid in shown or (area is not None and area.id != pid and area.id in shown):
            out.add(pid)
    return out


def containers(world: WorldMap, ids: Collection[str]) -> set[str]:
    """Every place one of `ids` is inside, all the way up."""
    out: set[str] = set()
    for pid in ids:
        seen: set[str] = set()
        current = world.places.get(pid)
        while current is not None and current.inside and current.inside not in seen:
            seen.add(current.inside)
            out.add(current.inside)
            current = world.places.get(current.inside)
    return out


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
        if child.inside != place.id or child.x is None or child.y is None:
            continue
        if moves(world.places, child):
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
    """The finest land drawn under a point, or None."""
    for patch in sorted(art.terrain, key=lambda p: p.side):
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


def has_inside(art: WorldArt, place: WorldPlace) -> bool:
    """A building (or room) with a plan of its own: its inside is an area."""
    return (
        place.kind in ("building", "room", "vehicle")
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
    ground = str((layout.spec or {}).get("ground") or "")
    return ground == "space" or land_under(world, art, place) == "space"


def _chain(world: WorldMap, place_id: str | None) -> list[WorldPlace]:
    out: list[WorldPlace] = []
    seen: set[str] = set()
    current = world.places.get(place_id or "")
    while current is not None and current.id not in seen:
        seen.add(current.id)
        out.append(current)
        current = world.places.get(current.inside or "")
    return out


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
    for place in chain:
        if (
            place.kind in LAYOUT_KINDS
            and size_of(place) <= PLAN_MAX_SIZE
            and art.layout_shown(place.id, place.state) is not None
        ):
            return place
    return None


def in_open_country(world: WorldMap, art: WorldArt, place_id: str | None) -> bool:
    """Whether someone at `place_id` is in country no plan will cover: on no
    plan now, and the local area there is none, a region or a route, or too
    wide for a plan. Someone at a place whose plan is still to be drawn is
    not: its plan is on the way."""
    if plan_place(world, art, place_id) is not None:
        return False
    local = local_area(world, place_id)
    return (
        local is None
        or local.kind in ("region", "route")
        or local.kind not in LAYOUT_KINDS
        or size_of(local) > PLAN_MAX_SIZE
    )


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
    """The land patch drawn exactly at this rectangle (within a metre), or None."""
    for patch in art.terrain:
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
WALKABLE = frozenset("._=+,:s")
# The tile a deck's blank is filled with (TILES["_"]).
FLOOR = "_"


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


def standing_spot(rows: tuple[str, ...], spot: tuple[int, int]) -> tuple[int, int]:
    """The nearest tile to `spot` someone can stand on (WALKABLE): a label
    the model put on space or a wall would stand them there. `spot` itself
    when it can be stood on, or when nothing can."""
    width = max((len(r) for r in rows), default=0)

    def at(x: int, y: int) -> str:
        return rows[y][x] if 0 <= y < len(rows) and 0 <= x < len(rows[y]) else " "

    if at(*spot) in WALKABLE:
        return spot
    seen, queue = {spot}, [spot]
    while queue:
        nxt = []
        for x, y in queue:
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if (nx, ny) in seen or not (0 <= nx < width and 0 <= ny < len(rows)):
                    continue
                if at(nx, ny) in WALKABLE:
                    return nx, ny
                seen.add((nx, ny))
                nxt.append((nx, ny))
        queue = nxt
    return spot


def plan_ground(world: WorldMap, art: WorldArt, place: WorldPlace, ground: str) -> str:
    """The look a plan is drawn in: its open ground as the land it stands on
    has it (live, a barn's plan came back on summer grass in a snowbound
    country); a floor or rock its own. A plan in space is a deck ("metal":
    panels, bulkheads, stars only outside the hull): live, Deep Space 9's
    rooms were wooden walls and red roofs on a field of stars."""
    land = land_under(world, art, place)
    if ground == "space" or land == "space":
        return "metal"
    if ground and ground not in OPEN_GROUND:
        return ground
    return land if land in OPEN_GROUND else ground


def changed_buildings(world: WorldMap, art: WorldArt, area_id: str) -> list[WorldPlace]:
    """The buildings in a plan the story changed whose plan in that state is
    drawn: shown open in place of their roofs."""
    out = []
    for child in world.places.values():
        if child.inside != area_id or child.kind != "building" or not child.state:
            continue
        layout = art.layout_for(child.id, child.state)
        if layout is not None and layout.rows:
            out.append(child)
    return out


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


def layout_messages(
    world: WorldMap,
    place: WorldPlace,
    setting: str,
    texts: PromptTexts,
    fixed: Sequence[dict[str, Any]] = (),
) -> list[PromptMessage]:
    width, height = plan_grid(place)
    tile = plan_tile(place)
    parts: list[str] = []
    if place.kind == "building":
        parts.append(texts["map.layout.building"])
    else:  # a vehicle's plan is its decks, as a station's
        parts.append(texts["map.layout.enclosed"])
    fixed_ids = {f["place_id"] for f in fixed}
    inside = [
        p
        for p in world.places.values()
        if p.inside == place.id and not moves(world.places, p) and p.id not in fixed_ids
    ]
    if inside:
        parts.append("In it: " + "; ".join(_what(p) for p in inside[:12]))
    if fixed:
        parts.append(texts.fill("map.layout.fixed", places=_fixed_lines(fixed)))
    things = [t for t in world.things.values() if t.at == place.id][:MAX_OBJECTS]
    if place.state:
        parts.insert(0, f"As it is now: {place.state}")
    if things:
        parts.append("Things here: " + "; ".join(_thing_words(t) for t in things))
    ways = _ways(world, place)
    if ways:
        parts.append("Ways out: " + "; ".join(ways))
    return [
        _message("system", _grid_fill(texts, "map.layout", width, height, _tile_words(tile))),
        _message(
            "user",
            texts.fill(
                "map.layout.ask", place=_what(place), parts="\n".join(parts), setting=setting
            ),
        ),
    ]


def layout_change_messages(
    world: WorldMap,
    place: WorldPlace,
    before: PlaceLayout,
    setting: str,
    texts: PromptTexts,
    fixed: Sequence[dict[str, Any]] = (),
) -> list[PromptMessage]:
    """The place laid out again in its new state, as an edit of its last plan."""
    return [
        *layout_messages(world, place, setting, texts, fixed),
        _message("assistant", json.dumps(before.spec)),
        _message("user", texts.fill("map.layout.change", place=place.name, change=place.state)),
    ]


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


def terrain_patch(place: WorldPlace) -> tuple[float, float, float] | None:
    """(x, y, side) of the land drawn around a place, or None when it isn't placed."""
    if place.x is None or place.y is None:
        return None
    side = min(max(size_of(place) * 4, TERRAIN_MIN_SIDE), TERRAIN_MAX_SIDE)
    return place.x, place.y, side


def terrain_messages(
    world: WorldMap,
    place: WorldPlace | None,
    patch: tuple[float, float, float],
    setting: str,
    texts: PromptTexts,
    *,
    what: str | None = None,
    ways: Sequence[str] | None = None,
    nearby_ground: str = "",
) -> list[PromptMessage]:
    """The land in `patch`: around `place` (what the story said of it), or,
    with `what`, the country along a way or a window of open country
    (`ways`: its words; `place` may then be None). `nearby_ground` is what
    the land already drawn nearest to it is on, so the country stays one
    country (live, the farm's land was snow, the town's dirt and the road's
    grass: the setting line said nothing of winter)."""
    x0, y0, side = patch
    if place is None:
        what, ways = what or "open country", ways or []
    if nearby_ground:
        setting = f"{setting}. " + texts.fill("map.terrain.nearby", ground=nearby_ground)
    tiles = TERRAIN_TILES
    tile = side / tiles
    found = []
    for other in world.places.values():
        spot = position(world, other.id)
        if spot is None or other.inside is not None and other.inside in world.places:
            continue
        col = int((spot[0] - (x0 - side / 2)) / tile)
        row = int(((y0 + side / 2) - spot[1]) / tile)
        if 0 <= col < tiles and 0 <= row < tiles:
            found.append(f"- {_what(other)}: at x {col}, y {row}")
    if ways is None:
        ways = _ways(world, place) if place is not None else []
    return [
        _message("system", _grid_fill(texts, "map.terrain", tiles, tiles, _tile_words(tile))),
        _message(
            "user",
            texts.fill(
                "map.terrain.ask",
                place=what or _what(place),  # type: ignore[arg-type]
                places="\n".join(found) or "(none but this one)",
                ways=("Ways out: " + "; ".join(ways)) if ways else "",
                setting=setting,
            ),
        ),
    ]


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


def plan_art(
    world: WorldMap,
    art: WorldArt,
    *,
    here: str | None,
    setting: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    limit: int = 12,
    sprites: bool = True,
    point: tuple[float, float] | None = None,
) -> list[ArtJob]:
    """What the map still needs drawn, nearest the story first: the land
    around and the layout of every place the story has been, then sprites of
    places and things, a changed thing redrawn from its last sprite. Nothing
    already drawn, and nothing that has failed too often, is asked again.
    `sprites` off: an image model draws them instead (engine/world_pictures.py).
    `point`: where the author's character stands, in metres (their tile on
    the map; else `here`'s spot): in open country the window of land under
    it is drawn, and the windows its edges lead to."""
    jobs: list[ArtJob] = []
    visited = {pid for places in world.visited.values() for pid in places}
    if world.start is not None:
        visited.add(world.start)  # an imported map's start: where the story is before a passage
    if here is not None and here in world.places:
        visited.add(here)
    # Standing somewhere, the character knows what it is in: the farm from
    # its farmhouse.
    shown = set(world.seen) | visited | containers(world, visited)
    # A place has its own land when a patch is centred on it (places never
    # move). Land along a way is kept under the way's first place too, but
    # never centred on it: counted as that place's own, a town whose land
    # failed once was never drawn after a road patch from it came in.
    drawn_land = {patch.place_id for patch in art.terrain if _centred_on(world, patch)}
    planned: list[TerrainPatch] = []

    def failed(key: str) -> bool:
        return art.failures.get(key, 0) >= MAX_FAILURES

    def covered(x: float, y: float, side: float) -> bool:
        return any(_covers(other, x, y, side) for other in (*art.terrain, *planned))

    def ground_near(x: float, y: float) -> str:
        drawn = [p for p in art.terrain if p.ground]
        if not drawn:
            return ""
        return min(drawn, key=lambda p: math.hypot(p.x - x, p.y - y)).ground

    def distance(place_id: str | None) -> float:
        here_spot = position(world, here) if here else None
        spot = position(world, place_id)
        if here_spot is None or spot is None:
            return 0.0
        return ((here_spot[0] - spot[0]) ** 2 + (here_spot[1] - spot[1]) ** 2) ** 0.5

    # Land around every place the story knows of (the player knows the
    # country they start in, not only where they have been), round its
    # local area (the farm for its farmhouse, the caves and not the hills),
    # nearest the story first, so where it is now gets the land it stands
    # on. A road is a line, not a spot, and a region or anything wider than
    # a local area is the country itself: none gets a patch of its own (it
    # would be coarse and, once drawn, keep every place in it from finer
    # land; live, a 3 km "area" of hills took a 12 km patch that covered
    # the road to town, so no land was drawn along it).
    tops = set()
    for pid in shown:
        area = local_area(world, pid)
        if area is None or area.kind in ("route", "region") or size_of(area) > LOCAL_MAX_SIZE:
            continue
        tops.add(area.id)
    for top_id in sorted(tops, key=lambda pid: (distance(pid), world.places[pid].name)):
        top = world.places[top_id]
        patch = terrain_patch(top)
        key = f"terrain:{top.id}"
        if patch is None or top.id in drawn_land or failed(key):
            continue
        # Land already drawn is established: a place standing on it (the
        # barn behind the farmhouse) gets no second, different version.
        if covered(top.x, top.y, patch[2]):  # type: ignore[arg-type]
            continue
        planned.append(
            TerrainPatch(place_id=top.id, x=patch[0], y=patch[1], side=patch[2], rows=[])
        )
        jobs.append(
            ArtJob(
                key,
                "terrain",
                terrain_messages(
                    world, top, patch, setting, texts, nearby_ground=ground_near(top.x, top.y)
                ),
                top.id,
                name=top.name,
                patch=patch,
                order=0,
            )
        )
    # And along each way between two places the story knows of, in patches,
    # where no land is drawn yet: the road from the farm to the town is
    # country the player has seen.
    for link in world.links:
        if link.a not in shown or link.b not in shown:
            continue
        a, b = position(world, link.a), position(world, link.b)
        if a is None or b is None:
            continue
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        steps = int(length // WAY_PATCH_SIDE)
        if steps > MAX_WAY_PATCHES:
            continue
        for i in range(1, steps + 1):
            t = i * WAY_PATCH_SIDE / length
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            key = f"terrain:{link.a}:{link.b}:{i}"
            if covered(x, y, WAY_PATCH_SIDE) or failed(key):
                continue
            patch = (x, y, WAY_PATCH_SIDE)
            planned.append(TerrainPatch(place_id=link.a, x=x, y=y, side=WAY_PATCH_SIDE, rows=[]))
            first, second = world.places[link.a], world.places[link.b]
            words = ", ".join(w for w in (link.direction, link.distance) if w)
            jobs.append(
                ArtJob(
                    key,
                    "terrain",
                    terrain_messages(
                        world,
                        first,
                        patch,
                        setting,
                        texts,
                        what=texts.fill("map.terrain.way", start=first.name, end=second.name),
                        ways=[
                            f"from {first.name} to {second.name}" + (f" ({words})" if words else "")
                        ],
                        nearby_ground=ground_near(x, y),
                    ),
                    link.a,
                    name=f"{first.name} to {second.name}",
                    patch=patch,
                    order=0,
                )
            )
    # Open country (a region, a desert, between places; the author: the
    # world is limitless): the window of land the character stands on, and
    # the windows its edges lead to, nearest first, a few at a time. Only
    # where no plan will cover them: at a place whose plan is still to be
    # drawn they stand on the land already there until it is.
    spot = point if point is not None else (position(world, here) if here else None)
    if spot is not None and in_open_country(world, art, here):
        jobs += _window_jobs(world, art, spot, here, setting, texts, planned, failed, ground_near)
    # Plans: the local area of every place the character is or has been
    # (the farm, not only its farmhouse), and the inside of each building;
    # a place wider than a local area, with nothing finer round the
    # character, up to PLAN_MAX_SIZE (coarse, but there).
    wanted: dict[str, WorldPlace] = {}
    for pid in visited:
        place = world.places.get(pid)
        if place is None:
            continue
        area = local_area(world, pid)
        if area is not None and area.kind in LAYOUT_KINDS and size_of(area) <= PLAN_MAX_SIZE:
            wanted[area.id] = area
        if place.kind in ("building", "vehicle"):
            wanted[pid] = place  # a ship's plan is its decks, drawn once aboard
    # The buildings standing in the area the character is in: a Director's
    # watchtower at a mark could not be walked into until a read had put
    # someone in it (the second review).
    if here is not None and here in world.places:
        local = local_area(world, here)
        if local is not None and local.id in wanted:
            for child in world.places.values():
                if child.inside == local.id and child.kind == "building":
                    wanted.setdefault(child.id, child)
    # A building the story changed, standing in one of those plans, is drawn
    # anew in its state though nobody has been inside: the local map shows
    # it open in its plan (live, a barn blown up and burning stayed an
    # intact roof on the farm's plan for three passages).
    for area in list(wanted.values()):
        if area.kind == "building":
            continue
        for child in world.places.values():
            if child.inside == area.id and child.kind == "building" and child.state:
                wanted.setdefault(child.id, child)
    # A plan the author asked to be drawn again, afresh, wherever it is: the
    # plan it has stays until this one is drawn (`redrawn`), and stays for
    # good if it can't be.
    for pid in art.redraw:
        place = world.places.get(pid)
        key = f"layout:{pid}:redraw"
        if place is None or place.kind not in LAYOUT_KINDS or failed(key):
            continue
        fixed = fixed_in_plan(world, place)
        jobs.append(
            ArtJob(
                key,
                "layout",
                layout_messages(world, place, setting, texts, fixed),
                pid,
                name=place.name,
                state=place.state,
                # First, whatever is nearer the story: the area in view may
                # be anywhere, and twelve nearer pieces kept it out of the run.
                order=-1,
                extra={"fixed": fixed, "grid": plan_grid(place)},
            )
        )
    for pid, place in wanted.items():
        if art.layout_for(pid, place.state) is not None:
            continue
        key = f"layout:{pid}:{place.state}" if place.state else f"layout:{pid}"
        if failed(key):
            continue
        before = art.layout_for(pid)
        fixed = fixed_in_plan(world, place)
        messages = (
            layout_messages(world, place, setting, texts, fixed)
            if before is None or not before.spec
            else layout_change_messages(world, place, before, setting, texts, fixed)
        )
        jobs.append(
            ArtJob(
                key,
                "layout",
                messages,
                pid,
                name=place.name,
                state=place.state,
                order=1,
                extra={"fixed": fixed, "grid": plan_grid(place)},
            )
        )
    subjects: list[tuple[str, str, str, str, Literal["place", "thing"]]] = [
        (p.id, p.name, _what(p), p.state, "place")
        for p in world.places.values()
        if sprites and p.kind in SPRITE_KINDS and p.id in shown  # nothing under the fog
    ]
    subjects += [
        (t.id, t.name, _thing_words(t), t.state, "thing")
        for t in world.things.values()
        if sprites and (t.carried_by or t.at in shown)
    ]
    for subject_id, name, words, state, subject in subjects:
        if art.sprite_for(subject_id, state) is not None:
            continue
        key = f"sprite:{subject_id}:{state}" if state else f"sprite:{subject_id}"
        if failed(key):
            continue
        # A change is an edit of its last drawing, so it stays the same one.
        last = art.sprite_for(subject_id)
        messages = (
            sprite_messages(words, setting, texts)
            if last is None
            else change_messages(last, name, state or "as it was before", setting, texts)
        )
        jobs.append(
            ArtJob(
                key,
                "sprite",
                messages,
                subject_id,
                name=name,
                state=state,
                from_sprite=last.id if last else None,
                subject=subject,
                order=2,
            )
        )
    jobs.sort(key=lambda job: (job.order, distance(job.subject_id)))
    return jobs[:limit]


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


def open_country_words(world: WorldMap, x: float, y: float) -> str:
    """'3.2 km north-east of Hellsville, in the Great Erg': where a point
    in open country is, by the nearest known place and what holds it."""
    parts: list[str] = []
    nearest: tuple[float, WorldPlace] | None = None
    for place in world.places.values():
        if place.x is None or place.y is None or place.kind == "region":
            continue
        if place.id not in world.seen or moves(world.places, place):
            continue
        d = math.hypot(place.x - x, place.y - y)
        if nearest is None or d < nearest[0]:
            nearest = (d, place)
    if nearest is not None:
        d, place = nearest
        far = f"{d / 1000:.1f} km" if d >= 950 else f"{round(d, -1):.0f} m"
        parts.append(f"{far} {_compass(x - place.x, y - place.y)} of {place.name}")  # type: ignore[operator]
    holder = holding_place(world, x, y, 0.0, min_size=LOCAL_MAX_SIZE)
    if holder is not None:
        parts.append(f"in {holder.name}")
    return ", ".join(parts)


def _window_jobs(
    world: WorldMap,
    art: WorldArt,
    spot: tuple[float, float],
    here: str | None,
    setting: str,
    texts: PromptTexts,
    planned: list[TerrainPatch],
    failed,
    ground_near,
) -> list[ArtJob]:
    """Land for the window under `spot` and the cells its edges lead to,
    those nearest the character first, WINDOW_PATCHES at most, where no
    land is drawn or planned. A cell is asked for at the grid's own
    rectangle, so the window the character stands on fills in under them
    when its patch comes back (`window_rect`)."""
    jobs: list[ArtJob] = []
    west, north, side = window_rect(art, *spot)
    cx, cy = west + side / 2, north - side / 2
    wanted: list[tuple[float, tuple[float, float, float]]] = [
        (0.0, window_cell(art.terrain, *spot))
    ]
    for dx, dy in ((0.0, 1.0), (1.0, 0.0), (0.0, -1.0), (-1.0, 0.0)):
        # Just past the middle of each edge, into the cell there.
        px, py = cx + dx * (side / 2 + 1.0), cy + dy * (side / 2 + 1.0)
        wanted.append((math.hypot(px - spot[0], py - spot[1]), window_cell(art.terrain, px, py)))
    seen: set[tuple[float, float, float]] = set()
    for _distance, (w, n, s) in sorted(wanted, key=lambda item: item[0]):
        if (w, n, s) in seen or len(jobs) >= WINDOW_PATCHES:
            continue
        seen.add((w, n, s))
        x, y = w + s / 2, n - s / 2
        key = f"terrain:window:{w:.0f}:{n:.0f}:{s:.0f}"
        if _window_covered([*art.terrain, *planned], x, y, s) or failed(key):
            continue
        holder = holding_place(world, w, n, s)
        patch = (x, y, s)
        planned.append(
            TerrainPatch(place_id=holder.id if holder else "", x=x, y=y, side=s, rows=[])
        )
        jobs.append(
            ArtJob(
                key,
                "terrain",
                terrain_messages(
                    world,
                    holder,
                    patch,
                    setting,
                    texts,
                    what=texts.fill("map.terrain.open", where=open_country_words(world, x, y)),
                    ways=[],
                    nearby_ground=ground_near(x, y),
                ),
                here or "",  # sorted as where the character is: first
                name="open country",
                patch=patch,
                order=0,
                extra={"place_id": holder.id if holder else ""},
            )
        )
    return jobs


def finish(
    job: ArtJob, text: str, *, model: str, made_at: str
) -> WorldSprite | PlaceLayout | TerrainPatch:
    """What a reply draws, checked; ValueError when it draws nothing usable."""
    if job.kind == "sprite":
        svg = clean_svg(text)
        if svg is None:
            raise ValueError("the reply held no usable SVG")
        return WorldSprite(
            subject=job.subject,
            subject_id=job.subject_id,
            name=job.name,
            state=job.state,
            svg=svg,
            from_sprite=job.from_sprite,
            model=model,
            made_at=made_at,
        )
    spec = parse_shapes(text)
    if job.kind == "layout":
        fixed = job.extra.get("fixed") or []
        columns, grid_rows = job.extra.get("grid") or (LAYOUT_WIDTH, LAYOUT_HEIGHT)
        rows, objects, labels = rasterise(with_fixed(spec, fixed), columns, grid_rows)
        for f in fixed:  # what the map has here that isn't a building: named where it is
            if f["kind"] != "building":
                labels[f["name"][:60]] = [f["x"] + f["w"] // 2, f["y"] + f["h"] // 2]
        return PlaceLayout(
            place_id=job.subject_id,
            state=job.state,
            spec=spec,
            rows=rows,
            objects=objects,
            labels=labels,
            model=model,
            made_at=made_at,
        )
    # Land: roads and areas only; a building in it is the model's, not the map's.
    spec = {
        **spec,
        "features": [
            f for f in spec["features"] if isinstance(f, dict) and f.get("kind") in ("area", "road")
        ],
    }
    rows, _objects, _labels = rasterise(spec, TERRAIN_TILES, TERRAIN_TILES)
    x, y, side = job.patch  # type: ignore[misc]
    return TerrainPatch(
        place_id=str(job.extra.get("place_id", job.subject_id)),
        x=x,
        y=y,
        side=side,
        rows=rows,
        ground=str(spec.get("ground") or ""),
        model=model,
        made_at=made_at,
    )


def adopt(
    art: WorldArt, drawn: WorldSprite | PlaceLayout | TerrainPatch, *, redraw: bool = False
) -> None:
    """Keep a finished drawing. A place's layout or land is never replaced:
    land that would lie on land already drawn is dropped. A plan drawn again
    at the author's asking (`redraw`) replaces every plan the place had."""
    if redraw and isinstance(drawn, PlaceLayout):
        art.layouts[drawn.place_id] = {drawn.state: drawn}
        if drawn.place_id in art.redraw:
            art.redraw.remove(drawn.place_id)
        return
    if isinstance(drawn, WorldSprite):
        art.sprites.append(drawn)
    elif isinstance(drawn, PlaceLayout):
        art.layouts.setdefault(drawn.place_id, {}).setdefault(drawn.state, drawn)
    elif not any(_covers(p, drawn.x, drawn.y, drawn.side) for p in art.terrain):
        art.terrain.append(drawn)


def describe(drawn: Sequence[object]) -> str:
    counts = {"sprite": 0, "layout": 0, "land": 0}
    for item in drawn:
        if isinstance(item, WorldSprite):
            counts["sprite"] += 1
        elif isinstance(item, PlaceLayout):
            counts["layout"] += 1
        elif isinstance(item, TerrainPatch):
            counts["land"] += 1
    return json.dumps(counts)
