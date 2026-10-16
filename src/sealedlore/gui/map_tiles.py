"""The map's ground, drawn by the program: the tiles a patch of land or a
place's plan is made of (`engine/world_art.TILES`), in a small pixel-art
style seen from three-quarters above.

The models only say what is where (a field, a road, a pond, the woods, the
walls of a house); everything here is how it looks. Each kind of tile has a
few variants, picked by where it is, so a field isn't a pattern; water and
roads soften into what is around them; trees, shrubs and rocks are small
sprites standing on the ground with their shadows, drawn back to front; walls
show their top and, facing south, their face, and a building seen from
outside (`roofs=True`) is roofed over, with its door and windows on the
front. Nothing is random between runs: the same rows give the same picture.

One pixel of art is one pixel of a tile (`TILE` across); the view scales the
whole image up, never smooths it.
"""

from __future__ import annotations

import random
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

from sealedlore.gui.image_codec import image_from_data

TILE = 16
# A ring of walls wider than this is a town's wall or a barricade, not a
# building to roof over (live, Hellsville's closed barricade roofed the town).
MAX_ROOF_TILES = 16
# A patch of land on the region fades out over this many tiles at its edge,
# into the ground under it or the fog, instead of ending hard (Oct 16 2026:
# Local and Region didn't read as one world, each patch a hard-edged card).
LAND_FADE_TILES = 6


@dataclass(frozen=True)
class Look:
    """One setting's colours. `base`, `light`, `shade`, `deep`: the open
    ground; the rest by what they colour."""

    base: str
    light: str
    shade: str
    deep: str
    speck: str  # a fleck on open ground: a stone, a sparkle
    bloom: tuple[str, ...]  # small flowers or the like; empty for none
    brush: tuple[str, str]  # stalks, and their shadow
    road: tuple[str, str, str]  # packed, lighter, ruts
    water: tuple[str, str, str]  # body, light, deep edge
    canopy: tuple[str, str, str]  # tree: dark, mid, light
    snow: bool  # snow on trees, roofs and banks
    wall: tuple[str, str, str] = ("#6e4c30", "#8b6340", "#a97a4f")  # dark, mid, light
    roof: tuple[str, str, str] = ("#7d2f2c", "#a3453b", "#c4614d")
    floor: str = "planks"  # planks | panels | flags
    trees: str = "living"  # living | dead
    glow: bool = False  # water glows (lava)


LOOKS: dict[str, Look] = {
    "snow": Look(
        "#e4ebf3",
        "#f6f9fc",
        "#cdd7e4",
        "#b2c0d3",
        "#ffffff",
        (),
        ("#b39a6a", "#8c7650"),
        ("#c9bba6", "#ddd2c2", "#9e8a70"),
        ("#bfe0ef", "#e6f5fb", "#86b6cf"),
        ("#24553f", "#34724f", "#4f9466"),
        True,
    ),
    "grass": Look(
        "#6aa83c",
        "#86c24b",
        "#579632",
        "#447a29",
        "#4d8a2d",
        ("#f4e06a", "#f0f0f0", "#e98aa4"),
        ("#4d8a2d", "#36661f"),
        ("#b08a5c", "#c9a370", "#8a6a44"),
        ("#4f8fd8", "#80b6ee", "#35679f"),
        ("#2f6e33", "#3f8b3c", "#62ad4a"),
        False,
    ),
    "dirt": Look(
        "#a8835a",
        "#bf9a6b",
        "#94714b",
        "#7d5e3e",
        "#7a6650",
        (),
        ("#7d8a45", "#5d6a32"),
        ("#8e6c48", "#a17d55", "#725435"),
        ("#4f8fd8", "#80b6ee", "#35679f"),
        ("#3d6b35", "#4f8742", "#6fa65a"),
        False,
    ),
    "sand": Look(
        "#e2c98c",
        "#f0dca5",
        "#d1b577",
        "#bc9e62",
        "#b39a6a",
        (),
        ("#a6a058", "#7f7a3f"),
        ("#c9ad72", "#d9c08a", "#a88d58"),
        ("#3fa8c8", "#7fd0e6", "#2b7f9c"),
        ("#5f7f3a", "#7a9a48", "#9cba5c"),
        False,
    ),
    "rock": Look(
        "#8e9097",
        "#a6a8ae",
        "#7b7d84",
        "#66686f",
        "#5a5c63",
        (),
        ("#7d8a45", "#5d6a32"),
        ("#7a7064", "#8d8377", "#5f564c"),
        ("#4f8fd8", "#80b6ee", "#35679f"),
        ("#3d6b35", "#4f8742", "#6fa65a"),
        False,
    ),
    "floor": Look(
        "#b8875a",
        "#cd9d6c",
        "#a3754b",
        "#8a603b",
        "#7a5535",
        (),
        ("#7d8a45", "#5d6a32"),
        ("#8e6c48", "#a17d55", "#725435"),
        ("#4f8fd8", "#80b6ee", "#35679f"),
        ("#3d6b35", "#4f8742", "#6fa65a"),
        False,
    ),
    "space": Look(
        "#10121c",
        "#1a1d2b",
        "#0b0c14",
        "#07080d",
        "#e8ecff",
        (),
        ("#3a3f55", "#262a3a"),
        ("#3a3f52", "#4a5066", "#2a2e3d"),
        ("#2b4a7a", "#4a72aa", "#1c3358"),
        ("#2a4a3a", "#3a6a4a", "#5a8a6a"),
        False,
    ),
}
LOOKS["hell"] = Look(
    "#2b1b1d",
    "#3b2527",
    "#1f1315",
    "#150c0e",
    "#5c2a20",
    (),
    ("#4a3a30", "#2a2018"),
    ("#3a2e2a", "#4a3c36", "#241c1a"),
    ("#e8641c", "#ffb347", "#a8320e"),
    ("#1a1214", "#2a1e1e", "#3a2a28"),
    False,
    wall=("#1c1a1f", "#2e2b33", "#45414b"),
    roof=("#2a1416", "#3d1d20", "#5a2a2c"),
    floor="flags",
    trees="dead",
    glow=True,
)
LOOKS["metal"] = Look(
    "#59616f",
    "#6b7383",
    "#49505e",
    "#3a404c",
    "#8a94a6",
    (),
    ("#3f7f5a", "#2c5c40"),
    ("#3e4552", "#4e5666", "#2c313b"),
    ("#2a8fd8", "#7fd0ff", "#1a5a90"),
    ("#2f6e33", "#3f8b3c", "#62ad4a"),
    False,
    wall=("#2b303a", "#454c59", "#6d7686"),
    roof=("#2b303a", "#3b414d", "#525a68"),
    floor="panels",
    glow=True,
)
LOOKS["cobble"] = Look(
    "#77716b",
    "#8b847d",
    "#645e59",
    "#4f4a46",
    "#4f7a3a",
    (),
    ("#4f7a3a", "#36562a"),
    ("#5d5752", "#6e6862", "#46413d"),
    ("#3f6f8f", "#6f9fbf", "#2a4f6a"),
    ("#2f5e33", "#3f7b3c", "#5a9a4a"),
    False,
    wall=("#6e3a2c", "#8e4c39", "#ad6550"),
    roof=("#3a4048", "#4e5560", "#68707c"),
    floor="flags",
)
LOOKS[""] = LOOKS["grass"]


@dataclass(frozen=True)
class LookColours:
    """A tile set's colours for the region's drawing: the mean of its
    out-ground, out-wall and out-roof textures (`texture_colours`), so a
    farmhouse is the one colour on the plan and on the play view. A colour
    left None keeps the Look's own."""

    ground: tuple[int, int, int] | None = None
    wall: tuple[int, int, int] | None = None
    roof: tuple[int, int, int] | None = None

    @property
    def base(self) -> str | None:
        return _hex(self.ground) if self.ground else None


def _hex(rgb: tuple[int, int, int], factor: float = 1.0) -> str:
    return "#" + "".join(f"{min(255, max(0, round(c * factor))):02x}" for c in rgb)


def _shades(rgb: tuple[int, int, int]) -> tuple[str, str, str]:
    """(dark, mid, light) round a mean colour."""
    return _hex(rgb, 0.72), _hex(rgb), _hex(rgb, 1.22)


# The looks whose open ground isn't a set's out-ground: a floor, a deck.
INDOOR_LOOKS = frozenset({"floor", "space", "metal"})


def look_with(look_name: str, colours: LookColours | None) -> Look:
    """The look in the set's colours where the set gives them. The open
    ground's shades are kept for the indoor and deck looks (INDOOR_LOOKS): a
    set's out-ground isn't their floor."""
    look = LOOKS.get(look_name, LOOKS[""])
    if colours is None:
        return look
    changes: dict = {}
    if colours.ground and look_name not in INDOOR_LOOKS:
        changes.update(
            base=_hex(colours.ground),
            light=_hex(colours.ground, 1.12),
            shade=_hex(colours.ground, 0.86),
            deep=_hex(colours.ground, 0.72),
        )
    if colours.wall:
        changes["wall"] = _shades(colours.wall)
    if colours.roof:
        changes["roof"] = _shades(colours.roof)
    return replace(look, **changes) if changes else look


_SET_COLOURS: dict[str, LookColours] = {}


def texture_colours(name: str, read: Callable[[str], bytes | None]) -> LookColours:
    """The colours of the set `name` (read by `read`, by role), kept once
    read: the mean of each texture, by scaling it to a pixel."""
    if name not in _SET_COLOURS:
        found: dict[str, tuple[int, int, int]] = {}
        roles = (("ground", "out-ground"), ("wall", "out-wall"), ("roof", "out-roof"))
        for field_name, role in roles:
            data = read(role)
            image = image_from_data(data)
            if image.isNull():
                continue
            pixel = image.scaled(1, 1, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            colour = pixel.pixelColor(0, 0)
            found[field_name] = (colour.red(), colour.green(), colour.blue())
        _SET_COLOURS[name] = LookColours(**found)
    return _SET_COLOURS[name]


OUTLINE = "#1b1d2b"
WOOD = ("#6e4c30", "#8b6340", "#a97a4f")  # dark, mid, light: walls, fences, trunks
ROOF = ("#7d2f2c", "#a3453b", "#c4614d")
STONE = ("#5c5e66", "#7a7c84", "#9fa1a8")
GLASS = ("#5e8fb8", "#a9d4ee")
SHADOW = QColor(20, 24, 40, 70)
# Fire: its edge, its body and its heart; the ground under it, embers.
FLAME = ("#b8321c", "#f07a1e", "#ffd24a")
EMBER = (176, 52, 22, 225)

# Tile kinds, by the characters of engine/world_art.TILES.
OPEN, BRUSH, TREES, ROCK, WATER, ROAD = ".", ",", "T", "^", "~", "="
WALL, DOOR, FLOOR, WINDOW, FENCE, RUBBLE, SAND, VOID = "#", "+", "_", "W", "%", ":", "s", " "
FIRE = "*"  # something burning: scorched ground with flames standing on it
BUILT = frozenset({WALL, DOOR, WINDOW})


def _rng(*key: object) -> random.Random:
    # Not `hash`: Python salts it anew each run, and the same rows must draw
    # the same picture at every launch (and a person keep their look).
    return random.Random(zlib.crc32(repr(key).encode()))


def _c(value: str) -> QColor:
    return QColor(value)


def _image(width: int, height: int) -> QImage:
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    return image


def _dots(painter: QPainter, rng: random.Random, colour: str, count: int, size: int = 1) -> None:
    painter.setPen(Qt.NoPen)
    painter.setBrush(_c(colour))
    for _ in range(count):
        painter.drawRect(rng.randrange(TILE - size + 1), rng.randrange(TILE - size + 1), size, size)


# --- Ground tiles --------------------------------------------------------------


@lru_cache(maxsize=512)
def ground_tile(look_name: str, kind: str, variant: int) -> QImage:
    """One tile of open ground, sand, rock, floor or road, textured."""
    look = LOOKS.get(look_name, LOOKS[""])
    rng = _rng("ground", look_name, kind, variant)
    image = _image(TILE, TILE)
    painter = QPainter(image)
    if kind == FLOOR and look.floor == "panels":
        painter.fillRect(0, 0, TILE, TILE, _c("#7a8292"))
        painter.fillRect(0, 0, TILE, 1, _c("#9aa2b2"))
        painter.fillRect(0, 0, 1, TILE, _c("#9aa2b2"))
        painter.fillRect(0, TILE - 1, TILE, 1, _c("#565d6b"))
        painter.fillRect(TILE - 1, 0, 1, TILE, _c("#565d6b"))
        for bx, by in ((2, 2), (13, 2), (2, 13), (13, 13)):
            painter.fillRect(bx, by, 1, 1, _c("#4a505c"))
        if variant % 5 == 0:
            painter.fillRect(4, 7, 8, 2, _c("#5fd0ff"))
    elif kind == FLOOR and look.floor == "flags":
        painter.fillRect(0, 0, TILE, TILE, _c(look.shade))
        for fx, fy, fw, fh in ((0, 0, 9, 7), (10, 0, 6, 7), (0, 8, 6, 8), (7, 8, 9, 8)):
            painter.fillRect(
                fx, fy, fw - 1, fh - 1, _c(look.base if (fx + fy + variant) % 3 else look.light)
            )
    elif kind == FLOOR:
        planks = ("#b8875a", "#a87a4f", "#c29263")
        for row in range(TILE // 4):
            painter.fillRect(0, row * 4, TILE, 4, _c(planks[(row + variant) % 3]))
            painter.fillRect(0, row * 4 + 3, TILE, 1, _c("#8a603b"))
            seam = (rng.randrange(TILE) + row * 7) % TILE
            painter.fillRect(seam, row * 4, 1, 3, _c("#8a603b"))
    elif kind == ROAD:
        packed, light, rut = look.road
        painter.fillRect(0, 0, TILE, TILE, _c(packed))
        _dots(painter, rng, light, 10)
        _dots(painter, rng, rut, 6)
    elif kind == SAND:
        sand = LOOKS["sand"]
        painter.fillRect(0, 0, TILE, TILE, _c(sand.base))
        _dots(painter, rng, sand.light, 9)
        _dots(painter, rng, sand.shade, 9)
    elif kind == VOID:
        painter.fillRect(0, 0, TILE, TILE, _c("#07080d"))
        if look_name in ("space", "metal") and rng.random() < 0.3:  # outside a hull
            _dots(painter, rng, "#e8ecff", 1)
    elif look_name == "cobble":
        painter.fillRect(0, 0, TILE, TILE, _c(look.deep))
        for cy in range(0, TILE, 5):
            off = 0 if (cy // 5) % 2 else 3
            for cx in range(-off, TILE, 6):
                painter.setPen(Qt.NoPen)
                painter.setBrush(_c(look.base if rng.random() < 0.7 else look.shade))
                painter.drawRoundedRect(cx, cy, 5, 4, 1.5, 1.5)
                painter.fillRect(cx + 1, cy, 2, 1, _c(look.light))
        if variant % 6 == 0:
            _dots(painter, rng, look.speck, 3)
    else:
        painter.fillRect(0, 0, TILE, TILE, _c(look.base))
        _dots(painter, rng, look.light, 6 + variant % 3)
        _dots(painter, rng, look.shade, 6)
        if variant % 4 == 0:  # a soft patch, so a field isn't flat
            painter.setBrush(_c(look.shade))
            painter.drawEllipse(rng.randrange(2, 10), rng.randrange(2, 10), 5, 3)
        if look_name == "grass" and variant % 3 == 1:
            painter.setPen(_c(look.deep))
            for _ in range(3):
                x, y = rng.randrange(1, TILE - 1), rng.randrange(3, TILE)
                painter.drawLine(x, y, x - 1, y - 2)
                painter.drawLine(x, y, x + 1, y - 2)
        if look.bloom and variant % 5 == 2:
            _dots(painter, rng, look.bloom[variant % len(look.bloom)], 2)
        if look_name == "snow" and variant % 4 == 3:
            _dots(painter, rng, look.speck, 3)
    painter.end()
    return image


@lru_cache(maxsize=256)
def water_tile(look_name: str, variant: int, frozen: bool) -> QImage:
    look = LOOKS.get(look_name, LOOKS[""])
    body, light, deep = look.water
    rng = _rng("water", look_name, variant, frozen)
    image = _image(TILE, TILE)
    painter = QPainter(image)
    painter.fillRect(0, 0, TILE, TILE, _c(body))
    painter.setPen(_c(light))
    if look.glow:  # lava or coolant: a hot crust and bright seams
        _dots(painter, rng, light, 8, 2)
        painter.setPen(_c(deep))
        for _ in range(2):
            x, y = rng.randrange(TILE), rng.randrange(TILE)
            painter.drawLine(x, y, x + rng.randint(-4, 4), y + rng.randint(-4, 4))
    elif frozen:  # cracks in the ice
        painter.setPen(_c(deep))
        x, y = rng.randrange(TILE), rng.randrange(TILE)
        for _ in range(3):
            nx, ny = x + rng.randint(-5, 5), y + rng.randint(-5, 5)
            painter.drawLine(x, y, nx, ny)
            x, y = nx, ny
        _dots(painter, rng, light, 5, 2)
    else:  # ripples
        for _ in range(2):
            x, y = rng.randrange(1, TILE - 5), rng.randrange(2, TILE - 1)
            painter.drawLine(x, y, x + 3, y)
    painter.end()
    return image


# --- Things standing on the ground ----------------------------------------------


@lru_cache(maxsize=64)
def tree_sprite(look_name: str, variant: int) -> QImage:
    """A tree, about a tile and a half across, its base at the bottom middle."""
    look = LOOKS.get(look_name, LOOKS[""])
    dark, mid, light = look.canopy
    w, h = 22, 30
    image = _image(w, h)
    p = QPainter(image)
    p.setPen(Qt.NoPen)
    p.setBrush(_c(WOOD[0]))
    p.drawRect(w // 2 - 2, h - 8, 4, 8)
    p.setBrush(_c(WOOD[1]))
    p.drawRect(w // 2 - 1, h - 8, 2, 7)
    pine = look.snow or variant % 2 == 0
    if look.trees == "dead":
        p.setPen(_c(dark))
        for x0, y0, x1, y1 in (
            (11, 29, 11, 8),
            (11, 16, 5, 9),
            (11, 13, 17, 6),
            (11, 20, 16, 15),
            (5, 9, 3, 5),
            (17, 6, 19, 3),
            (11, 8, 9, 3),
        ):
            p.drawLine(x0, y0, x1, y1)
            p.drawLine(x0 + 1, y0, x1 + 1, y1)
        p.setPen(_c(mid))
        p.drawLine(12, 28, 12, 9)
        p.end()
        return _outlined(image)
    if pine:
        for i, (top, half) in enumerate(((1, 4), (7, 7), (13, 10))):
            tier_h = 9
            p.setBrush(_c(dark))
            p.drawPolygon(
                [
                    QPoint(w // 2, top),
                    QPoint(w // 2 - half, top + tier_h),
                    QPoint(w // 2 + half, top + tier_h),
                ]
            )
            p.setBrush(_c(mid))
            p.drawPolygon(
                [
                    QPoint(w // 2, top + 1),
                    QPoint(w // 2 - half + 2, top + tier_h - 1),
                    QPoint(w // 2, top + tier_h - 1),
                ]
            )
            if look.snow:
                p.setBrush(_c("#f4f8fc"))
                p.drawPolygon(
                    [
                        QPoint(w // 2, top),
                        QPoint(w // 2 - half // 2 - 1, top + 4 + i),
                        QPoint(w // 2 + half // 2 + 1, top + 3 + i),
                    ]
                )
            else:
                p.setBrush(_c(light))
                p.drawRect(w // 2 - half + 3, top + tier_h - 3, 2, 1)
    else:
        for cx, cy, r, colour in (
            (11, 13, 9, dark),
            (8, 11, 6, mid),
            (14, 10, 6, mid),
            (10, 8, 4, light),
            (15, 14, 3, light),
        ):
            p.setBrush(_c(colour))
            p.drawEllipse(QPoint(cx, cy), r, r - 1)
    p.end()
    return _outlined(image)


@lru_cache(maxsize=64)
def brush_sprite(look_name: str, variant: int) -> QImage:
    look = LOOKS.get(look_name, LOOKS[""])
    stalk, shade = look.brush
    rng = _rng("brush", look_name, variant)
    image = _image(12, 10)
    p = QPainter(image)
    if look.snow:  # a clump of dead grass through the snow, fanning out
        p.setPen(Qt.NoPen)
        p.setBrush(_c("#cdd7e4"))
        p.drawEllipse(1, 7, 10, 3)
        for i in range(7):
            lean = (i - 3) * 0.8 + rng.uniform(-0.5, 0.5)
            p.setPen(_c(stalk if i % 2 else shade))
            p.drawLine(6 + (i - 3) // 2, 8, int(6 + lean * 2), rng.randrange(1, 4))
    else:  # a bush
        p.setPen(Qt.NoPen)
        p.setBrush(_c(shade))
        p.drawEllipse(1, 3, 10, 7)
        p.setBrush(_c(stalk))
        p.drawEllipse(2, 2, 7, 5)
        p.setBrush(_c(LOOKS[look_name or "grass"].light if look_name in LOOKS else "#86c24b"))
        p.drawRect(4, 3, 2, 1)
    p.end()
    return image if look.snow else _outlined(image)


@lru_cache(maxsize=64)
def rock_sprite(variant: int, small: bool) -> QImage:
    w, h = (8, 6) if small else (14, 11)
    image = _image(w, h)
    p = QPainter(image)
    p.setPen(Qt.NoPen)
    p.setBrush(_c(STONE[0]))
    p.drawEllipse(0, 1, w, h - 1)
    p.setBrush(_c(STONE[1]))
    p.drawEllipse(1, 0, w - 3, h - 3)
    p.setBrush(_c(STONE[2]))
    p.drawRect(2 + variant % 2, 1, max(2, w // 4), 1)
    p.end()
    return _outlined(image)


@lru_cache(maxsize=8)
def flame_sprite(variant: int) -> QImage:
    """A tongue of fire: dark red at its edge, orange, a yellow heart."""
    w, h = 10, 13 + variant % 3
    image = _image(w, h)
    p = QPainter(image)
    p.setPen(Qt.NoPen)
    lean = (variant % 3) - 1
    for colour, inset, top in ((FLAME[0], 0, 0), (FLAME[1], 2, 3), (FLAME[2], 3, 7)):
        p.setBrush(_c(colour))
        p.drawEllipse(inset, top + 3, w - 2 * inset, h - top - 3)
        p.drawPolygon(
            [
                QPoint(inset + 1, top + 6),
                QPoint(w // 2 + lean, top),
                QPoint(w - inset - 1, top + 6),
            ]
        )
    p.end()
    return image


def _outlined(image: QImage) -> QImage:
    """A one-pixel dark outline around the opaque part, as the sprites have."""
    w, h = image.width(), image.height()
    out = image.copy()
    solid = [[image.pixelColor(x, y).alpha() > 100 for x in range(w)] for y in range(h)]
    edge = _c(OUTLINE)
    for y in range(h):
        for x in range(w):
            if solid[y][x]:
                continue
            if any(
                0 <= x + dx < w and 0 <= y + dy < h and solid[y + dy][x + dx]
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
            ):
                out.setPixelColor(x, y, edge)
    return out


# --- Composing a patch -------------------------------------------------------------


class _Grid:
    def __init__(self, rows: Sequence[str]) -> None:
        self.rows = list(rows)
        self.h = len(self.rows)
        self.w = max((len(r) for r in self.rows), default=0)

    def at(self, x: int, y: int) -> str:
        if 0 <= y < self.h and 0 <= x < len(self.rows[y]):
            return self.rows[y][x]
        return ""


def _variant(x: int, y: int, seed: int, count: int = 8) -> int:
    return (x * 73856093 ^ y * 19349663 ^ seed * 83492791) % count


def _buildings(grid: _Grid) -> list[set[tuple[int, int]]]:
    """Each building: a run of walls, doors and windows (touching, corners
    too) that rings something, taken as the whole of its extent. A path the
    plan drew in through a wall doesn't stop it being a building."""
    found: list[set[tuple[int, int]]] = []
    runs: list[set[tuple[int, int]]] = []
    seen: set[tuple[int, int]] = set()
    for y in range(grid.h):
        for x in range(grid.w):
            if grid.at(x, y) not in BUILT or (x, y) in seen:
                continue
            run, todo = set(), [(x, y)]
            while todo:
                cx, cy = todo.pop()
                if (cx, cy) in run or grid.at(cx, cy) not in BUILT:
                    continue
                run.add((cx, cy))
                todo += [(cx + dx, cy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
            seen |= run
            runs.append(run)
    # A path drawn in through a wall cuts it in two: runs whose extents touch
    # are one building.
    boxes = [_box(run) + (len(run),) for run in runs]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                touching = (
                    a[0] <= b[1] + 1 and b[0] <= a[1] + 1 and a[2] <= b[3] + 1 and b[2] <= a[3] + 1
                )
                # A house inside a town's wall is its own: a box that holds
                # the other whole is not a wall cut in two.
                a_holds_b = a[0] <= b[0] and b[1] <= a[1] and a[2] <= b[2] and b[3] <= a[3]
                b_holds_a = b[0] <= a[0] and a[1] <= b[1] and b[2] <= a[2] and a[3] <= b[3]
                if touching and not a_holds_b and not b_holds_a:
                    boxes[i] = (
                        min(a[0], b[0]),
                        max(a[1], b[1]),
                        min(a[2], b[2]),
                        max(a[3], b[3]),
                        a[4] + b[4],
                    )
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    for x0, x1, y0, y1, walls in boxes:
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue  # a lone wall, not a building
        if x1 - x0 > MAX_ROOF_TILES or y1 - y0 > MAX_ROOF_TILES:
            continue  # a town's wall or barricade rings the town: no roof over that
        if walls < 2 * (x1 - x0 + y1 - y0) * 0.6:
            continue  # too little of the ring stands: ruins, drawn as a plan
        found.append({(bx, by) for bx in range(x0, x1 + 1) for by in range(y0, y1 + 1)})
    return found


def _box(run: set[tuple[int, int]]) -> tuple[int, int, int, int]:
    xs = [x for x, _ in run]
    ys = [y for _, y in run]
    return min(xs), max(xs), min(ys), max(ys)


def render_tiles(
    rows: Sequence[str],
    ground: str = "",
    *,
    roofs: bool = False,
    seed: int = 0,
    cleared: Sequence[tuple[int, int, int, int]] = (),
    colours: LookColours | None = None,
) -> QImage:
    """`rows` drawn, `TILE` pixels a tile. `roofs` draws each closed building
    from outside, roofed; otherwise its floor and furniture show (a plan).
    `cleared` buildings (their boxes) are left as open ground: a picture of
    the building stands there instead (gui/world_view.py). `colours`: the
    tile set's, over the look's own (`look_with`)."""
    grid = _Grid(rows)
    look_name = ground if ground in LOOKS else ""
    look = look_with(look_name, colours)
    image = _image(max(grid.w, 1) * TILE, max(grid.h, 1) * TILE)
    p = QPainter(image)
    roofed: set[tuple[int, int]] = set()
    houses = _buildings(grid) if roofs else []
    clear: set[tuple[int, int]] = set()
    for x0, x1, y0, y1 in cleared:
        clear |= {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
    houses = [house for house in houses if not house <= clear]
    for house in houses:
        roofed |= house

    # 1. The ground under everything.
    for y in range(grid.h):
        for x in range(grid.w):
            ch = OPEN if (x, y) in clear else grid.at(x, y)
            v = _variant(x, y, seed)
            under = (
                OPEN
                if ch in (OPEN, BRUSH, TREES, ROCK, RUBBLE, FENCE, FIRE, "") or ch.isdigit()
                else ch
            )
            if ch == VOID and look_name not in ("space", "floor", "metal"):
                under = OPEN
            if ch == ROCK and look_name == "rock":
                under = OPEN
            if under in (WALL, DOOR, WINDOW):
                under = FLOOR
            if under == WATER:
                p.drawImage(x * TILE, y * TILE, ground_tile(look_name, OPEN, v))
                p.drawImage(x * TILE, y * TILE, water_tile(look_name, v % 4, look.snow))
            else:
                p.drawImage(x * TILE, y * TILE, ground_tile(look_name, under, v))
            if ch == TREES:  # the forest floor is in shade
                p.fillRect(x * TILE, y * TILE, TILE, TILE, QColor(30, 50, 40, 40))
            elif ch == FIRE:  # embers, so fire reads as fire even drawn small
                p.fillRect(x * TILE, y * TILE, TILE, TILE, QColor(*EMBER))
                ember = _rng("ember", x, y, seed)
                for _ in range(5):
                    p.fillRect(
                        x * TILE + ember.randrange(TILE - 2),
                        y * TILE + ember.randrange(TILE - 2),
                        2,
                        2,
                        _c(FLAME[2] if ember.random() < 0.4 else FLAME[1]),
                    )

    # 2. Edges: water gets a bank, a road frays into the ground around it.
    for y in range(grid.h):
        for x in range(grid.w):
            ch = grid.at(x, y)
            if ch == WATER:
                _water_edges(p, grid, x, y, look)
            elif ch == ROAD:
                _road_edges(p, grid, x, y, look, look_name, seed)

    # 3. Walls, doors, windows and fences, as plan or as building.
    for y in range(grid.h):
        for x in range(grid.w):
            ch = grid.at(x, y)
            if (x, y) in clear:
                continue
            if ch in BUILT and (x, y) not in roofed:
                _wall(p, grid, x, y, ch, look)
            elif ch == FENCE:
                _fence(p, grid, x, y, look)
    for house in houses:
        _roof(p, grid, house, look)

    # 4. What stands on the ground, back to front: trees, shrubs, rocks, rubble.
    standing: list[tuple[int, int, QImage, bool]] = []
    for y in range(grid.h):
        for x in range(grid.w):
            ch = OPEN if (x, y) in clear else grid.at(x, y)
            v = _variant(x, y, seed, 97)
            rng = _rng("stand", x, y, seed)
            cx, base = x * TILE + TILE // 2, y * TILE + TILE
            if ch == TREES:
                sprite = tree_sprite(look_name, v % 3)
                jx, jy = rng.randint(-3, 3), rng.randint(-2, 2)
                standing.append((base + jy, cx + jx, sprite, True))
            elif ch == BRUSH and v % 3 != 0:
                standing.append(
                    (
                        base - rng.randrange(4),
                        cx + rng.randint(-3, 3),
                        brush_sprite(look_name, v % 4),
                        False,
                    )
                )
            elif ch == ROCK:
                standing.append((base, cx + rng.randint(-2, 2), rock_sprite(v % 2, False), True))
            elif ch == FIRE:
                for _ in range(1 + v % 2):
                    standing.append(
                        (
                            base - rng.randrange(6),
                            cx + rng.randint(-4, 4),
                            flame_sprite(rng.randrange(3)),
                            False,
                        )
                    )
            elif ch == RUBBLE:
                for _ in range(2):
                    standing.append(
                        (
                            base - rng.randrange(8),
                            cx + rng.randint(-5, 5),
                            rock_sprite(v % 2, True),
                            False,
                        )
                    )
            elif (
                ch == OPEN
                and v % 97 == 0
                and _variant(x // 5, y // 5, seed, 3) == 0
                and look_name not in ("floor", "space")
            ):
                standing.append((base - 4, cx, rock_sprite(v % 2, True), False))
    for base, cx, sprite, shadow in sorted(standing, key=lambda s: (s[0], s[1])):
        if shadow:
            p.setPen(Qt.NoPen)
            p.setBrush(SHADOW)
            p.drawEllipse(QPoint(cx + 1, base - 1), sprite.width() // 2, 3)
        p.drawImage(cx - sprite.width() // 2, base - sprite.height(), sprite)
    p.end()
    return image


def _water_edges(p: QPainter, grid: _Grid, x: int, y: int, look: Look) -> None:
    body, light, deep = look.water
    bank = _c(look.light if look.snow else look.deep)
    left, top = x * TILE, y * TILE
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        if grid.at(x + dx, y + dy) in (WATER, ""):
            continue
        if dy == -1:
            p.fillRect(left, top, TILE, 2, bank)
            p.fillRect(left, top + 2, TILE, 1, _c(deep))
        elif dy == 1:
            p.fillRect(left, top + TILE - 2, TILE, 2, bank)
        elif dx == -1:
            p.fillRect(left, top, 2, TILE, bank)
            p.fillRect(left + 2, top, 1, TILE, _c(deep))
        else:
            p.fillRect(left + TILE - 2, top, 2, TILE, bank)
    # Round the outer corners.
    for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        if grid.at(x + dx, y) != WATER and grid.at(x, y + dy) != WATER:
            cx = left if dx < 0 else left + TILE - 4
            cy = top if dy < 0 else top + TILE - 4
            p.fillRect(cx, cy, 4, 4, bank)


def _road_edges(
    p: QPainter, grid: _Grid, x: int, y: int, look: Look, look_name: str, seed: int
) -> None:
    left, top = x * TILE, y * TILE
    rng = _rng("road", x, y, seed)
    edge = _c(look.shade)
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        if grid.at(x + dx, y + dy) in (ROAD, "", DOOR):
            continue
        for i in range(TILE):
            depth = rng.choice((0, 1, 1, 1, 2, 2))
            if dy == -1:
                p.drawImage(
                    QRect(left + i, top, 1, depth),
                    ground_tile(look_name, OPEN, i % 8),
                    QRect(i, 0, 1, depth),
                )
                p.fillRect(left + i, top + depth, 1, 1, edge)
            elif dy == 1:
                p.drawImage(
                    QRect(left + i, top + TILE - depth, 1, depth),
                    ground_tile(look_name, OPEN, i % 8),
                    QRect(i, 0, 1, depth),
                )
            elif dx == -1:
                p.drawImage(
                    QRect(left, top + i, depth, 1),
                    ground_tile(look_name, OPEN, i % 8),
                    QRect(0, i, depth, 1),
                )
                p.fillRect(left + depth, top + i, 1, 1, edge)
            else:
                p.drawImage(
                    QRect(left + TILE - depth, top + i, depth, 1),
                    ground_tile(look_name, OPEN, i % 8),
                    QRect(0, i, depth, 1),
                )


def _wall(p: QPainter, grid: _Grid, x: int, y: int, ch: str, look: Look) -> None:
    """A wall in a plan: its top, and its face where it looks south."""
    left, top = x * TILE, y * TILE
    face = grid.at(x, y + 1) not in BUILT
    top_h = 6 if face else TILE
    p.fillRect(left, top, TILE, top_h, _c(look.wall[0]))
    p.fillRect(left, top, TILE, 1, _c(look.wall[1]))
    if face:
        p.fillRect(left, top + top_h, TILE, TILE - top_h, _c(look.wall[1]))
        for row in range(top + top_h + 3, top + TILE, 3):
            p.fillRect(left, row, TILE, 1, _c(look.wall[0]))
        p.fillRect(left, top + TILE - 1, TILE, 1, QColor(20, 24, 40, 90))
    if ch == DOOR:
        p.fillRect(
            left + 4, top + (top_h if face else 3), 8, TILE - (top_h if face else 3), _c("#4a2f1c")
        )
        p.fillRect(left + 9, top + 11, 1, 1, _c("#f6cf6e"))
    elif ch == WINDOW:
        y0 = top + (top_h + 2 if face else 4)
        p.fillRect(left + 4, y0, 8, 6, _c(GLASS[0]))
        p.fillRect(left + 5, y0 + 1, 3, 2, _c(GLASS[1]))


def _fence(p: QPainter, grid: _Grid, x: int, y: int, look: Look) -> None:
    left, top = x * TILE, y * TILE
    across = grid.at(x - 1, y) == FENCE or grid.at(x + 1, y) == FENCE
    down = grid.at(x, y - 1) == FENCE or grid.at(x, y + 1) == FENCE
    if across or not down:
        p.fillRect(left, top + 6, TILE, 2, _c(look.wall[2]))
        p.fillRect(left, top + 10, TILE, 2, _c(look.wall[1]))
    if down:
        p.fillRect(left + 7, top, 2, TILE, _c(look.wall[1]))
    p.fillRect(left + 6, top + 3, 4, 11, _c(look.wall[0]))
    p.fillRect(left + 7, top + 3, 2, 1, _c(look.wall[2]))


def _roof(p: QPainter, grid: _Grid, house: set[tuple[int, int]], look: Look) -> None:
    """A building from outside, as the farm games draw one: a front wall of
    planks with its door and windows (where the plan put them on the south
    side), and a pitched, shingled roof over the rest of the footprint, its
    near slope facing us. Snow on it in a snowy country."""
    x0, x1, y0, y1 = _box(house)
    left, right = x0 * TILE, (x1 + 1) * TILE
    width = right - left
    bottom = (y1 + 1) * TILE
    front_h = min(TILE + 8, max(14, (y1 - y0) * TILE // 3))
    front_top = bottom - front_h
    roof_top = y0 * TILE - 4
    eave = 3
    rng = _rng("roof", x0, y0, x1, y1)
    # Shadow on the ground to the south-east.
    p.fillRect(left + 3, bottom, width, 4, SHADOW)
    p.fillRect(right, roof_top + 8, 4, bottom - roof_top - 8, SHADOW)
    # The front wall: planks, a darker foot, trim at the corners.
    p.fillRect(left, front_top, width, front_h, _c(look.wall[1]))
    for row in range(front_top + 2, bottom, 3):
        p.fillRect(left, row, width, 1, _c(look.wall[0]))
    p.fillRect(left, bottom - 3, width, 3, _c(STONE[0]))
    p.fillRect(left, front_top, 2, front_h, _c(look.wall[2]))
    p.fillRect(right - 2, front_top, 2, front_h, _c(look.wall[0]))
    door_xs = [x for x in range(x0, x1 + 1) if grid.at(x, y1) == DOOR]
    window_xs = [x for x in range(x0, x1 + 1) if grid.at(x, y1) == WINDOW]
    if not door_xs:  # every building has a way in that we can see
        door_xs = [(x0 + x1) // 2]
    if not window_xs and x1 - x0 >= 4:  # nor a blank face: a window each side of the door
        door = door_xs[0]
        window_xs = [x for x in (door - 2, door + 2) if x0 < x < x1]
    door_h = min(front_h - 3, 13)
    for x in door_xs:
        dx = x * TILE + 3
        p.fillRect(dx - 1, bottom - door_h - 4, 12, door_h + 1, _c(look.wall[0]))
        p.fillRect(dx, bottom - door_h - 3, 10, door_h, _c("#4a2f1c"))
        p.fillRect(dx + 1, bottom - door_h - 2, 4, door_h - 2, _c("#5d3c25"))
        p.fillRect(dx + 7, bottom - door_h // 2 - 3, 1, 1, _c("#f6cf6e"))
        p.fillRect(dx - 2, bottom, 14, 2, _c(STONE[1]))  # the step
    for x in window_xs:
        wx, wy = x * TILE + 3, front_top + max(2, (front_h - 13) // 2)
        p.fillRect(wx - 1, wy - 1, 12, 9, _c(look.wall[2]))
        p.fillRect(wx, wy, 10, 7, _c(GLASS[0]))
        p.fillRect(wx + 1, wy + 1, 3, 2, _c(GLASS[1]))
        p.fillRect(wx + 5, wy, 1, 7, _c(look.wall[2]))
        p.fillRect(wx - 1, wy + 8, 12, 1, _c(look.wall[0]))
    # A door on another side shows as a step out to it.
    for x, y in sorted(house):
        if grid.at(x, y) == DOOR and y != y1:
            p.fillRect(x * TILE + 3, y * TILE + (TILE - 4 if y == y0 else 0), 10, 4, _c(STONE[1]))
    # The roof: the far slope (narrow, light) and the near one (deep), with
    # rows of shingles, staggered, and eaves over the walls.
    roof_bottom = front_top + 2
    ridge = roof_top + max(5, (roof_bottom - roof_top) // 4)
    rl, rr = left - eave, right + eave
    p.fillRect(rl, roof_top, rr - rl, ridge - roof_top, _c(look.roof[2]))
    p.fillRect(rl, ridge, rr - rl, roof_bottom - ridge, _c(look.roof[1]))
    for n, row in enumerate(range(ridge + 1, roof_bottom - 1, 4)):
        p.fillRect(rl, row + 3, rr - rl, 1, _c(look.roof[0]))
        for col in range(rl + (n % 2) * 4, rr, 8):
            p.fillRect(col, row, 1, 3, _c(look.roof[0]))
        if n % 3 == 1:
            p.fillRect(rl, row, rr - rl, 1, _c(look.roof[2]))
    # The ridge and the eave's shadow line, in the roof's own dark (a fixed
    # dark red sat on a set's grey shingles).
    ridge_line = _c(look.roof[0]).darker(125)
    p.fillRect(rl, ridge - 1, rr - rl, 2, ridge_line)
    p.fillRect(rl, roof_bottom - 2, rr - rl, 2, ridge_line)
    if look.snow:
        p.fillRect(rl, roof_top, rr - rl, ridge - roof_top + 1, _c("#f2f6fa"))
        for col in range(rl, rr, 2):
            drop = rng.choice((2, 3, 3, 4, 5))
            p.fillRect(col, ridge + 1, 2, drop, _c("#eef3f8"))
            p.fillRect(col, ridge + 1 + drop, 2, 1, _c("#cdd7e4"))
        p.fillRect(rl, roof_bottom - 3, rr - rl, 2, _c("#eef3f8"))  # snow along the eave
        for col in range(rl + 2, rr - 2, 7):  # icicles under it
            p.fillRect(col, roof_bottom, 1, rng.randint(1, 3), _c("#e6f5fb"))
    if window_xs or width <= 8 * TILE:  # a house has a chimney
        cx = left + width * 3 // 4
        top_c = roof_top - 7
        p.fillRect(cx - 1, top_c - 1, 10, ridge - top_c + 6, _c(OUTLINE))
        p.fillRect(cx, top_c, 8, ridge - top_c + 4, _c(STONE[1]))
        p.fillRect(cx + 6, top_c, 2, ridge - top_c + 4, _c(STONE[0]))
        for row in range(top_c + 3, ridge + 3, 3):
            p.fillRect(cx, row, 8, 1, _c(STONE[0]))
        p.fillRect(cx - 1, top_c - 1, 10, 2, _c("#f2f6fa" if look.snow else STONE[2]))
    # Outline.
    edge = _c(OUTLINE)
    p.fillRect(rl, roof_top - 1, rr - rl, 1, edge)
    p.fillRect(rl - 1, roof_top, 1, roof_bottom - roof_top, edge)
    p.fillRect(rr, roof_top, 1, roof_bottom - roof_top, edge)
    p.fillRect(left - 1, roof_bottom, 1, bottom - roof_bottom, edge)
    p.fillRect(right, roof_bottom, 1, bottom - roof_bottom, edge)
    p.fillRect(left - 1, bottom, width + 2, 1, edge)


# --- For the view ---------------------------------------------------------------


@lru_cache(maxsize=48)
def tiles_pixmap(
    rows: tuple[str, ...],
    ground: str = "",
    roofs: bool = False,
    cleared: tuple[tuple[int, int, int, int], ...] = (),
    colours: LookColours | None = None,
) -> QPixmap:
    """`render_tiles`, kept: a scene is drawn again on every redraw of the view."""
    return QPixmap.fromImage(
        render_tiles(rows, ground, roofs=roofs, cleared=cleared, colours=colours)
    )


def faded_edges(image: QImage, fade: int) -> QImage:
    """`image` with its alpha ramped to nothing over `fade` pixels at each
    edge (never more than a quarter of its smaller side, so a small picture
    keeps a solid middle)."""
    import numpy as np

    fade = min(fade, min(image.width(), image.height()) // 4)
    if fade <= 0:
        return image
    work = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    h, w = work.height(), work.width()
    a = np.frombuffer(work.constBits(), np.uint8).reshape(h, work.bytesPerLine())[:, : w * 4]
    a = a.reshape(h, w, 4).astype(np.float32)
    ys = np.minimum(np.arange(h) + 0.5, h - np.arange(h) - 0.5)[:, None]
    xs = np.minimum(np.arange(w) + 0.5, w - np.arange(w) - 0.5)[None, :]
    ramp = np.clip(np.minimum(ys, xs) / fade, 0, 1)
    # Premultiplied: every channel takes the ramp, which keeps the colour.
    out = np.ascontiguousarray((a * ramp[..., None]).astype(np.uint8))
    return QImage(out.data, w, h, w * 4, QImage.Format_ARGB32_Premultiplied).copy()


@lru_cache(maxsize=48)
def land_pixmap(
    rows: tuple[str, ...], ground: str = "", colours: LookColours | None = None
) -> QPixmap:
    """A patch of land for the region: `render_tiles`, fading out over
    LAND_FADE_TILES at its edge, kept."""
    image = render_tiles(rows, ground, colours=colours)
    return QPixmap.fromImage(faded_edges(image, LAND_FADE_TILES * TILE))


@lru_cache(maxsize=8)
def ground_pixmap(width: int, height: int, colour: str, fade: int) -> QPixmap:
    """A flat ground in `colour`, `width` by `height`, fading out over
    `fade` pixels at its edge: laid under the region's patches and the
    ways between them, so they lie on one country and not each on the fog."""
    image = _image(max(width, 1), max(height, 1))
    image.fill(_c(colour))
    return QPixmap.fromImage(faded_edges(image, fade))


def building_boxes(rows: Sequence[str]) -> list[tuple[int, int, int, int]]:
    """Each building's (x0, x1, y0, y1) in tiles, as `render_tiles` roofs them."""
    return [_box(house) for house in _buildings(_Grid(rows))]


_SKIN = ("#f1c7a0", "#d9a27a", "#a8704a", "#7a4c30")
_HAIR = ("#2b1d14", "#5a3a22", "#a8743c", "#e0c070", "#8a8a8a", "#b0442c")
_CLOTHES = ("#3557b8", "#b3384a", "#2a6b5c", "#7d4a8f", "#94603e", "#4a5468", "#e6773f")


@lru_cache(maxsize=64)
def person_sprite(name: str) -> QImage:
    """A small figure for someone with no picture of their own yet: their
    colours read from their name, so each is told apart and keeps their look."""
    rng = _rng("person", name)
    skin, hair = rng.choice(_SKIN), rng.choice(_HAIR)
    coat, legs = rng.choice(_CLOTHES), rng.choice(("#2b2f3a", "#4a3a2a", "#3a4a5a"))
    image = _image(12, 20)
    p = QPainter(image)
    p.setPen(Qt.NoPen)
    p.fillRect(3, 15, 2, 4, _c(legs))  # legs
    p.fillRect(7, 15, 2, 4, _c(legs))
    p.fillRect(3, 19, 2, 1, _c("#1b1d2b"))
    p.fillRect(7, 19, 2, 1, _c("#1b1d2b"))
    p.fillRect(2, 8, 8, 8, _c(coat))  # body
    p.fillRect(1, 9, 1, 5, _c(coat))
    p.fillRect(10, 9, 1, 5, _c(coat))
    p.fillRect(1, 14, 1, 1, _c(skin))
    p.fillRect(10, 14, 1, 1, _c(skin))
    p.fillRect(3, 2, 6, 6, _c(skin))  # head
    p.fillRect(3, 1, 6, 2, _c(hair))
    p.fillRect(2, 2, 1, 4, _c(hair))
    p.fillRect(9, 2, 1, 4, _c(hair))
    p.fillRect(4, 5, 1, 1, _c("#1b1d2b"))  # eyes
    p.fillRect(7, 5, 1, 1, _c("#1b1d2b"))
    p.end()
    return _outlined(image)


@lru_cache(maxsize=16)
def prop_sprite(variant: int) -> QImage:
    """A small thing on the ground (a marked object, until it has a picture
    of its own): a crate, a barrel or a sack."""
    image = _image(12, 12)
    p = QPainter(image)
    p.setPen(Qt.NoPen)
    kind = variant % 3
    if kind == 0:  # crate
        p.fillRect(1, 3, 10, 8, _c(WOOD[1]))
        p.fillRect(1, 3, 10, 2, _c(WOOD[2]))
        p.fillRect(1, 7, 10, 1, _c(WOOD[0]))
        p.fillRect(5, 3, 2, 8, _c(WOOD[0]))
    elif kind == 1:  # barrel
        p.setBrush(_c(WOOD[1]))
        p.drawRoundedRect(2, 1, 8, 10, 3, 3)
        p.fillRect(2, 3, 8, 1, _c(STONE[0]))
        p.fillRect(2, 8, 8, 1, _c(STONE[0]))
        p.fillRect(4, 1, 2, 10, _c(WOOD[2]))
    else:  # sack
        p.setBrush(_c("#c9a46a"))
        p.drawEllipse(1, 3, 10, 8)
        p.fillRect(4, 1, 4, 3, _c("#a8844e"))
        p.fillRect(3, 6, 2, 3, _c("#e0c48e"))
    p.end()
    return _outlined(image)
