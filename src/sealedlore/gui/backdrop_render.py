"""The window's side of painted backdrops (engine/backdrops.py): the render
the painter is sent, the painting trimmed back to the area's shape, and the
painting as the play view's ground. QImage only, run on the painting's
thread through the codec lock (gui/image_codec.py); the engine draws
nothing.

The render is the area's ground with the things that stay put drawn on
(`painted_things`: the fixed ones and the furniture): what can be carried or
driven stays off it and is a sprite over the painting, as people, place
marks and anything the story has since moved are. It is padded onto a
grey square (one shape in and out for the image model; the request says the
bands are padding), and the painting's bands are cut back off by the same
share, so a map spot lands on the painting by a plain scale."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter

from sealedlore.engine.area import Area
from sealedlore.gui.area_render import (
    Ground,
    px_for,
)
from sealedlore.gui.image_codec import encoded, image_from_data

BAND = QColor(128, 128, 128)


def area_picture_size(area: Area) -> tuple[int, int]:
    """The size of the area's render in pixels (indoors a wall's face
    taller than its tiles: `render_ground`)."""
    px = px_for(area)
    extra = round(px * 0.7) if area.indoor else 0
    return area.width * px, area.height * px + extra


def padded_square(image: QImage, side: int) -> QImage:
    """`image` scaled to `side` wide on a grey square."""
    if image.isNull() or image.width() <= 0:
        return QImage()
    scale = side / image.width()
    h = max(1, int(round(image.height() * scale)))
    scaled = image.scaled(side, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    out = QImage(side, side, QImage.Format_RGB32)
    out.fill(BAND)
    p = QPainter(out)
    p.drawImage(0, (side - h) // 2, scaled)
    p.end()
    return out


def trim_painting(data: bytes, area: Area) -> bytes:
    """The painting with the grey bands cut off: the area's own shape, by
    the share its render took of the square. Kept as JPEG (a backdrop is
    read whole each time the area is shown)."""
    painting = image_from_data(data)
    if painting.isNull():
        raise ValueError("the painting could not be read")
    w, h = area_picture_size(area)
    share = h / w
    band = int(round(painting.width() * share))
    band = max(1, min(band, painting.height()))
    top = (painting.height() - band) // 2
    cut = painting.copy(0, top, painting.width(), band)
    return encoded(cut.convertToFormat(QImage.Format_RGB32), "JPEG", 90)


def backdrop_ground(data: bytes | None, area: Area) -> Ground | None:
    """The painting as the area's ground: scaled to the render's size, so
    the view's tiles, sprites and clicks land on it as on the tiles."""
    painting = image_from_data(data)
    if painting.isNull():
        return None
    w, h = area_picture_size(area)
    scaled = painting.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return Ground(scaled.convertToFormat(QImage.Format_RGB32), None)
