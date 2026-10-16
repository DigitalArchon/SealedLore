"""The map's art as pictures (engine/world_art.py): sprites turned into pixel
art in the story's palette, and tiles into ground.

A sprite is a cleaned SVG; it is drawn at 32×32, every pixel snapped to the
palette, given a one-pixel dark outline, and kept by its id, so a map of a
hundred places draws each once. The look is the program's, whatever model
drew the shapes: the measured sprites read best this way (SealedLoreDocs
design/map.md). QtSvg is imported on first use, not at startup.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

from sealedlore.engine.world_art import PALETTE
from sealedlore.gui.image_codec import image_from_data

SPRITE_PIXELS = 32
OUTLINE = QColor(PALETTE["k"])
_PALETTE_RGB = [QColor(v).getRgb()[:3] for v in PALETTE.values()]

# The map's own colours: a map is a picture, the same in every theme.
GROUND_COLOURS: dict[str, str] = {
    "snow": "#e9ecef",
    "grass": "#9fc27e",
    "dirt": "#b89b72",
    "sand": "#e3d29a",
    "rock": "#8a8178",
    "floor": "#d8c7a5",
    "space": "#141420",
    "": "#d9d3c2",
}
TILE_COLOURS: dict[str, str] = {
    ",": "#a9c48a",
    "T": "#2f6b3a",
    "^": "#7b746c",
    "~": "#7fb3d5",
    "=": "#b9a27f",
    "#": "#3d3a45",
    "+": "#c2873f",
    "_": "#d8c7a5",
    "W": "#9fd8ef",
    "%": "#7a5230",
    ":": "#9a8f86",
    "s": "#e3d29a",
    " ": "#141420",
}
OBJECT_COLOUR = "#f2c14e"
# What lies outside everything drawn: the unmapped.
FOG = "#2a2d36"


def _nearest(r: int, g: int, b: int) -> tuple[int, int, int]:
    return min(_PALETTE_RGB, key=lambda p: (p[0] - r) ** 2 + (p[1] - g) ** 2 + (p[2] - b) ** 2)


def pixel_art(svg: str) -> QImage | None:
    """The sprite as 32×32 pixel art, or None if Qt can't draw it."""
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(svg.encode()))
    if not renderer.isValid():
        return None
    size = SPRITE_PIXELS
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    solid = [[False] * size for _ in range(size)]
    for y in range(size):
        for x in range(size):
            colour = image.pixelColor(x, y)
            if colour.alpha() >= 110:
                r, g, b = _nearest(colour.red(), colour.green(), colour.blue())
                image.setPixelColor(x, y, QColor(r, g, b, 255))
                solid[y][x] = True
            else:
                image.setPixelColor(x, y, QColor(0, 0, 0, 0))
    for y in range(size):
        for x in range(size):
            if not solid[y][x] and any(
                0 <= y + dy < size and 0 <= x + dx < size and solid[y + dy][x + dx]
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
            ):
                image.setPixelColor(x, y, OUTLINE)
    return image


@lru_cache(maxsize=512)
def sprite_pixmap(sprite_id: str, svg: str, pixels: int) -> QPixmap | None:
    """The sprite at `pixels` across, nearest-neighbour: crisp at any size."""
    image = pixel_art(svg)
    if image is None:
        return None
    return QPixmap.fromImage(
        image.scaled(pixels, pixels, Qt.KeepAspectRatio, Qt.FastTransformation)
    )


def tile_image(rows: tuple[str, ...], ground: str = "") -> QImage:
    """Tile rows as an image, one pixel a tile: scaled up by the view."""
    height = len(rows)
    width = max((len(r) for r in rows), default=1)
    image = QImage(width, max(height, 1), QImage.Format_ARGB32)
    image.fill(QColor(GROUND_COLOURS.get(ground, GROUND_COLOURS[""])))
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == ".":
                continue
            colour = OBJECT_COLOUR if ch.isdigit() else TILE_COLOURS.get(ch)
            if colour is not None:
                image.setPixelColor(x, y, QColor(colour))
    return image


@lru_cache(maxsize=256)
def tile_pixmap(rows: tuple[str, ...], ground: str = "") -> QPixmap:
    return QPixmap.fromImage(tile_image(rows, ground))


# --- Pictures from an image model (engine/world_pictures.py) -----------------

# How close to white (or to the corner's colour) counts as the background.
_NEAR_WHITE = 226
_NEAR_CORNER = 60


def _cut_out(image: QImage) -> QImage | None:
    """The picture with its plain background taken away (flooded in from the
    edges: near-white, or the colour of its corner), cropped to what is left.
    None when nothing is left."""
    image = image.convertToFormat(QImage.Format_ARGB32)
    w, h = image.width(), image.height()
    corner = image.pixelColor(1, 1)

    def background(c: QColor) -> bool:
        if c.red() > _NEAR_WHITE and c.green() > _NEAR_WHITE and c.blue() > _NEAR_WHITE:
            return True
        return (
            abs(c.red() - corner.red())
            + abs(c.green() - corner.green())
            + abs(c.blue() - corner.blue())
            < _NEAR_CORNER
        )

    clear = QColor(0, 0, 0, 0)
    seen = bytearray(w * h)
    todo = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)]
    todo += [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]
    while todo:
        x, y = todo.pop()
        if not (0 <= x < w and 0 <= y < h) or seen[y * w + x]:
            continue
        seen[y * w + x] = 1
        if not background(image.pixelColor(x, y)):
            continue
        image.setPixelColor(x, y, clear)
        todo += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    left, top, right, bottom = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if image.pixelColor(x, y).alpha():
                left, right = min(left, x), max(right, x)
                top, bottom = min(top, y), max(bottom, y)
    if right < 0:
        return None
    return image.copy(left, top, right - left + 1, bottom - top + 1)


# The cut-outs, by picture id and height: small images, so a map of many
# pictures costs little. Keyed on the data itself (lru_cache) the cache held
# every picture's whole file, up to 256 of them.
_PICTURE_SPRITES: OrderedDict[tuple[str, int], QImage] = OrderedDict()
_PICTURE_SPRITES_MAX = 256


def picture_sprite(key: str, height: int, read: Callable[[], bytes | None]) -> QImage | None:
    """A map picture cut out of its background and shrunk to `height` pixels
    of the map's art, its edge hard (no half-clear fringe). `key` is the
    picture's id; `read` gives its file, and is called only when the cut-out
    isn't cached, so a redraw of the map reads no files."""
    cached = _PICTURE_SPRITES.get((key, height))
    if cached is not None:
        _PICTURE_SPRITES.move_to_end((key, height))
        return cached
    data = read()
    image = image_from_data(data)
    if image.isNull():
        return None
    work = image.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    cut = _cut_out(work)
    if cut is None:
        return None
    small = cut.scaledToHeight(max(4, height), Qt.SmoothTransformation)
    small = small.convertToFormat(QImage.Format_ARGB32)
    for y in range(small.height()):
        for x in range(small.width()):
            colour = small.pixelColor(x, y)
            colour.setAlpha(255 if colour.alpha() > 128 else 0)
            small.setPixelColor(x, y, colour)
    _PICTURE_SPRITES[(key, height)] = small
    while len(_PICTURE_SPRITES) > _PICTURE_SPRITES_MAX:
        _PICTURE_SPRITES.popitem(last=False)
    return small
