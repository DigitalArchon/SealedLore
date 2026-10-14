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
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
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
TERRAIN_TILES = 40
# The land around a place: four times its width, within these bounds.
TERRAIN_MIN_SIDE, TERRAIN_MAX_SIDE = 400.0, 40_000.0
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


def layout_messages(
    world: WorldMap, place: WorldPlace, setting: str, texts: PromptTexts
) -> list[PromptMessage]:
    width, height = LAYOUT_WIDTH, LAYOUT_HEIGHT
    tile = max(size_of(place) / width, 0.5)
    parts: list[str] = []
    inside = [
        p for p in world.places.values() if p.inside == place.id and not moves(world.places, p)
    ]
    if inside:
        parts.append("In it: " + "; ".join(_what(p) for p in inside[:12]))
    things = [t for t in world.things.values() if t.at == place.id][:MAX_OBJECTS]
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
) -> list[PromptMessage]:
    x0, y0, side = patch
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
    ways = _ways(world, place)
    return [
        _message("system", _grid_fill(texts, "map.terrain", tiles, tiles, _tile_words(tile))),
        _message(
            "user",
            texts.fill(
                "map.terrain.ask",
                place=_what(place),
                places="\n".join(found) or "(none but this one)",
                ways=("Ways out: " + "; ".join(ways)) if ways else "",
                setting=setting,
            ),
        ),
    ]


def _covers(patch: TerrainPatch, x: float, y: float) -> bool:
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
) -> list[ArtJob]:
    """What the map still needs drawn, nearest the story first: the land
    around and the layout of every place the story has been, then sprites of
    places and things, a changed thing redrawn from its last sprite. Nothing
    already drawn, and nothing that has failed too often, is asked again."""
    jobs: list[ArtJob] = []
    visited = {pid for places in world.visited.values() for pid in places}
    drawn_land = {patch.place_id for patch in art.terrain}
    planned: list[TerrainPatch] = []

    def failed(key: str) -> bool:
        return art.failures.get(key, 0) >= MAX_FAILURES

    def distance(place_id: str | None) -> float:
        here_spot = position(world, here) if here else None
        spot = position(world, place_id)
        if here_spot is None or spot is None:
            return 0.0
        return ((here_spot[0] - spot[0]) ** 2 + (here_spot[1] - spot[1]) ** 2) ** 0.5

    tops = {(_top(world, pid) or world.places[pid]).id for pid in visited if pid in world.places}
    # Nearest the story first, so where it is now gets the land it stands on.
    for top_id in sorted(tops, key=lambda pid: (distance(pid), world.places[pid].name)):
        top = world.places[top_id]
        patch = terrain_patch(top)
        key = f"terrain:{top.id}"
        if patch is None or top.id in drawn_land or failed(key):
            continue
        # Land already drawn is established: a place standing on it (the
        # barn behind the farmhouse) gets no second, different version.
        drawn = [*art.terrain, *planned]
        if any(_covers(other, top.x, top.y) for other in drawn):  # type: ignore[arg-type]
            continue
        planned.append(
            TerrainPatch(place_id=top.id, x=patch[0], y=patch[1], side=patch[2], rows=[])
        )
        jobs.append(
            ArtJob(
                key,
                "terrain",
                terrain_messages(world, top, patch, setting, texts),
                top.id,
                name=top.name,
                patch=patch,
                order=0,
            )
        )
    for pid in visited:
        place = world.places.get(pid)
        key = f"layout:{pid}"
        if place is None or place.kind not in LAYOUT_KINDS or pid in art.layouts or failed(key):
            continue
        jobs.append(
            ArtJob(
                key,
                "layout",
                layout_messages(world, place, setting, texts),
                pid,
                name=place.name,
                order=1,
            )
        )
    for place in world.places.values():
        key = f"sprite:{place.id}"
        if place.kind not in SPRITE_KINDS or art.sprite_for(place.id) or failed(key):
            continue
        jobs.append(
            ArtJob(
                key,
                "sprite",
                sprite_messages(_what(place), setting, texts),
                place.id,
                name=place.name,
                subject="place",
                order=2,
            )
        )
    for thing in world.things.values():
        last = art.sprite_for(thing.id)
        if last is not None and last.state == thing.state:
            continue
        key = f"sprite:{thing.id}:{thing.state}"
        if failed(key):
            continue
        what = _thing_words(thing) if last is None else thing.name
        messages = (
            sprite_messages(what, setting, texts)
            if last is None
            else change_messages(last, thing.name, thing.state or "as it is now", setting, texts)
        )
        jobs.append(
            ArtJob(
                key,
                "sprite",
                messages,
                thing.id,
                name=thing.name,
                state=thing.state,
                from_sprite=last.id if last else None,
                subject="thing",
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
        rows, objects, labels = rasterise(spec, LAYOUT_WIDTH, LAYOUT_HEIGHT)
        return PlaceLayout(
            place_id=job.subject_id,
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
        place_id=job.subject_id, x=x, y=y, side=side, rows=rows, model=model, made_at=made_at
    )


def adopt(art: WorldArt, drawn: WorldSprite | PlaceLayout | TerrainPatch) -> None:
    """Keep a finished drawing. A place's layout or land is never replaced."""
    if isinstance(drawn, WorldSprite):
        art.sprites.append(drawn)
    elif isinstance(drawn, PlaceLayout):
        art.layouts.setdefault(drawn.place_id, drawn)
    elif not any(p.place_id == drawn.place_id for p in art.terrain):
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
