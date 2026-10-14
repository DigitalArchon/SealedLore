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
# A building fixed in its parent's plan is at least this many tiles across.
MIN_BUILDING_TILES = 4
TERRAIN_TILES = 40
# The land around a place: four times its width, within these bounds.
TERRAIN_MIN_SIDE, TERRAIN_MAX_SIDE = 400.0, 40_000.0
# Land along a way between two known places is drawn in patches this wide.
WAY_PATCH_SIDE = 1_600.0
MAX_OBJECTS = 9
# Places drawn as a sprite on the map; regions, routes, areas and rooms are
# drawn as land and layouts instead.
SPRITE_KINDS = frozenset({"settlement", "building", "site", "vehicle", None})
LAYOUT_KINDS = frozenset({"settlement", "building", "site", "area", "room", None})
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


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
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


def _fixed_lines(fixed: Sequence[dict[str, Any]]) -> str:
    lines = []
    for f in fixed:
        span = f"x {f['x']}-{f['x'] + f['w'] - 1}, y {f['y']}-{f['y'] + f['h'] - 1}"
        what = " ".join(str(f.get("what") or "").split())
        lines.append(f"- {f['name']} ({f['place_kind']}): {span}" + (f": {what}" if what else ""))
    return "\n".join(lines)


def with_fixed(spec: dict[str, Any], fixed: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The model's shapes with the map's buildings where the map has them:
    a building the model drew under a fixed name is dropped (its doors and
    windows kept), and each fixed building is drawn at its own box."""
    names = {key(f["name"]) for f in fixed if f["kind"] == "building"}
    features = [f for f in spec.get("features", []) if isinstance(f, dict)]
    own = {
        key(str(f.get("name") or "")): f
        for f in features
        if f.get("kind") == "building" and f.get("name")
    }
    kept = [
        f
        for f in features
        if not (f.get("kind") == "building" and key(str(f.get("name") or "")) in names)
    ]
    for f in fixed:
        if f["kind"] != "building":
            continue
        drawn = own.get(key(f["name"]))
        doors = [d for d in (drawn or {}).get("doors") or [] if isinstance(d, dict)]
        windows = [d for d in (drawn or {}).get("windows") or [] if isinstance(d, dict)]
        for opening in (*doors, *windows):  # within the box's own sides
            along = f["w"] if str(opening.get("side")) in ("north", "south") else f["h"]
            opening["at"] = min(max(_int(opening.get("at"), 1), 1), max(along - 2, 1))
        kept.append(
            {
                "kind": "building",
                "name": f["name"],
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
    place: WorldPlace,
    patch: tuple[float, float, float],
    setting: str,
    texts: PromptTexts,
    *,
    what: str | None = None,
    ways: Sequence[str] | None = None,
    nearby_ground: str = "",
) -> list[PromptMessage]:
    """The land in `patch`: around `place` (what the story said of it), or,
    with `what`, the country along a way (`ways`: its words). `nearby_ground`
    is what the land already drawn nearest to it is on, so the country
    stays one country (live, the farm's land was snow, the town's dirt and
    the road's grass: the setting line said nothing of winter)."""
    x0, y0, side = patch
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
        ways = _ways(world, place)
    return [
        _message("system", _grid_fill(texts, "map.terrain", tiles, tiles, _tile_words(tile))),
        _message(
            "user",
            texts.fill(
                "map.terrain.ask",
                place=what or _what(place),
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
) -> list[ArtJob]:
    """What the map still needs drawn, nearest the story first: the land
    around and the layout of every place the story has been, then sprites of
    places and things, a changed thing redrawn from its last sprite. Nothing
    already drawn, and nothing that has failed too often, is asked again.
    `sprites` off: an image model draws them instead (engine/world_pictures.py)."""
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
    # Plans: the local area of every place the character is or has been
    # (the farm, not only its farmhouse), and the inside of each building.
    wanted: dict[str, WorldPlace] = {}
    for pid in visited:
        place = world.places.get(pid)
        if place is None:
            continue
        area = local_area(world, pid)
        if area is not None and area.kind in LAYOUT_KINDS and size_of(area) <= LOCAL_MAX_SIZE:
            wanted[area.id] = area
        if place.kind == "building":
            wanted[pid] = place
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
        place_id=job.subject_id,
        x=x,
        y=y,
        side=side,
        rows=rows,
        ground=str(spec.get("ground") or ""),
        model=model,
        made_at=made_at,
    )


def adopt(art: WorldArt, drawn: WorldSprite | PlaceLayout | TerrainPatch) -> None:
    """Keep a finished drawing. A place's layout or land is never replaced:
    land that would lie on land already drawn is dropped."""
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
