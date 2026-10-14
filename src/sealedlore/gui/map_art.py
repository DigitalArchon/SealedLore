"""The map's art as pictures (engine/world_art.py): sprites turned into pixel
art in the story's palette, and tiles into ground.

A sprite is a cleaned SVG; it is drawn at 32×32, every pixel snapped to the
palette, given a one-pixel dark outline, and kept by its id, so a map of a
hundred places draws each once. The look is the program's, whatever model
drew the shapes: the measured sprites read best this way (SealedLoreDocs
design/map.md). QtSvg is imported on first use, not at startup.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

from sealedlore.engine.world_art import PALETTE

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
