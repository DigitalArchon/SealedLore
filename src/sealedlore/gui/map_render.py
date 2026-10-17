"""A local area's plan as one picture, as the map's local level draws it:
the area's plan in its look, buildings roofed from outside, a building the
story changed open in its new state. The image model is given it to paint
the isometric view from (engine/world_pictures.py), so it is drawn with
QImage alone: it runs on the map pictures' thread, not the GUI's.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from sealedlore.engine.area import Area
from sealedlore.engine.world_art import changed_buildings, decked, plan_ground
from sealedlore.gui.area_render import TileSource, px_for, render_area
from sealedlore.gui.area_view import PERSON_TILES, PlaceMark, Walker
from sealedlore.gui.image_codec import encoded
from sealedlore.gui.map_tiles import TILE, building_boxes, render_tiles
from sealedlore.models.world import PlaceLayout, WorldArt, WorldMap, WorldPlace

# The picture's longest side: what the image model is sent.
PLAN_PICTURE_SIDE = 1024


def label_key(name: str) -> str:
    """A place's name as a plan's label might give it: no case, no "the",
    nothing in brackets ("barn (blown up, burning)" is the barn)."""
    name = name.lower().split("(")[0].strip()
    return name[4:] if name.startswith("the ") else name


def drawn_box(parent: PlaceLayout, child: WorldPlace) -> tuple[int, int, int, int] | None:
    """The box (x0, x1, y0, y1, in tiles) a parent's plan drew for one of its
    buildings, found by its name there: the box drawn is the one that counts,
    whatever size the map asks for now."""
    names = {label_key(n) for n in (child.name, *child.aliases)}
    spot = next((xy for label, xy in parent.labels.items() if label_key(label) in names), None)
    if spot is None:
        return None
    return next(
        (
            b
            for b in building_boxes(tuple(parent.rows))
            if b[0] <= spot[0] <= b[1] and b[2] <= spot[1] <= b[3]
        ),
        None,
    )


def plan_image(world: WorldMap, art: WorldArt, area_id: str) -> QImage | None:
    """The area's plan as the local map shows it from outside, or None when
    it has no plan."""
    area = world.places.get(area_id)
    layout = art.layout_shown(area_id, area.state) if area else None
    if area is None or layout is None or not layout.rows:
        return None
    ground = plan_ground(world, art, area, str(layout.spec.get("ground", "")))
    opened: list[tuple[tuple[int, int, int, int], QImage]] = []
    for child in changed_buildings(world, art, area_id):
        box = drawn_box(layout, child)
        child_layout = art.layout_for(child.id, child.state)
        if box is None or child_layout is None:
            continue
        child_ground = plan_ground(world, art, child, str(child_layout.spec.get("ground", "")))
        opened.append((box, render_tiles(tuple(child_layout.rows), child_ground)))
    # A deck has no roofs: its rooms are under the one hull, joined
    # (`decked`), as the map draws it.
    roofs = ground != "metal"
    rows = decked(tuple(layout.rows)) if ground == "metal" else tuple(layout.rows)
    image = render_tiles(rows, ground, roofs=roofs, cleared=tuple(box for box, _ in opened))
    if opened:
        painter = QPainter(image)
        for (x0, x1, y0, y1), inside in opened:
            target = QRect(x0 * TILE, y0 * TILE, (x1 - x0 + 1) * TILE, (y1 - y0 + 1) * TILE)
            scaled = inside.scaled(target.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawImage(
                target.x() + (target.width() - scaled.width()) // 2,
                target.y() + (target.height() - scaled.height()) // 2,
                scaled,
            )
        painter.end()
    side = max(image.width(), image.height())
    if side > PLAN_PICTURE_SIDE:
        image = image.scaled(
            image.width() * PLAN_PICTURE_SIDE // side,
            image.height() * PLAN_PICTURE_SIDE // side,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
    return image


def plan_png(world: WorldMap, art: WorldArt, area_id: str) -> bytes | None:
    """`plan_image` as PNG bytes, for the image model."""
    image = plan_image(world, art, area_id)
    if image is None:
        return None
    return encoded(image, "PNG")


# --- A painting's reference: the area as the play view shows it -------------------

# A figure with no sprite is drawn as the play view draws one.
_FIGURE = QColor(90, 110, 170)
# A place's mark (a rift, a well) stands this many tiles tall on the picture.
_PLACE_TILES = 1.6


def area_scene_image(
    area: Area,
    source: TileSource,
    walkers: Sequence[Walker],
    places: Sequence[PlaceMark],
    size: tuple[int, int],
    *,
    dress: bool = True,
) -> QImage:
    """The area as the play view shows it, for a painting's model to paint
    from (the author, Oct 17 2026: consistent with the actual map, with
    the characters and items present at the time it is taken): the tile
    picture (`render_area`), the places' marks and everyone's sprite at
    their tile, as `AreaView` stands them, fitted whole into `size` (the
    model's own shape, one shape in and out) on the ground's own colour.
    QImage alone, so it runs on the painting's thread."""
    picture = render_area(area, source, dress=dress)
    px = px_for(area)
    painter = QPainter(picture)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    for mark in places:
        _stand(painter, mark.image, (mark.x + 0.5) * px, (mark.y + 1.0) * px, px * _PLACE_TILES)
    # Back to front, as the view stacks them (z by row).
    for walker in sorted(walkers, key=lambda w: w.y):
        height = px * PERSON_TILES[area.indoor]
        _stand(painter, walker.image, (walker.x + 0.5) * px, (walker.y + 1.0) * px, height)
    painter.end()
    return fitted(picture, size)


def _stand(painter: QPainter, image: QImage | None, x: float, bottom: float, height: float) -> None:
    """A sprite (or a plain figure) `height` tall, its feet at (`x`, `bottom`)."""
    if image is not None and not image.isNull() and image.height():
        width = height * image.width() / image.height()
        painter.drawImage(QRectF(x - width / 2, bottom - height, width, height), image)
        return
    width = height * 0.6
    painter.setPen(Qt.NoPen)
    painter.setBrush(_FIGURE)
    painter.drawRoundedRect(QRectF(x - width / 2, bottom - height, width, height), 6, 6)


def fitted(picture: QImage, size: tuple[int, int]) -> QImage:
    """`picture` scaled to fit whole inside `size`, centred, the margins in
    the mean colour of the picture's edge (so the model sees ground on,
    not bars)."""
    width, height = max(1, size[0]), max(1, size[1])
    scaled = picture.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    out = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    out.fill(_edge_colour(picture))
    painter = QPainter(out)
    painter.drawImage((width - scaled.width()) // 2, (height - scaled.height()) // 2, scaled)
    painter.end()
    return out


def _edge_colour(picture: QImage) -> QColor:
    """The mean colour of a picture's outermost rows and columns."""
    w, h = picture.width(), picture.height()
    if w == 0 or h == 0:
        return QColor(24, 26, 32)
    samples = [picture.pixelColor(x, y) for x in range(0, w, max(1, w // 32)) for y in (0, h - 1)]
    samples += [picture.pixelColor(x, y) for y in range(0, h, max(1, h // 32)) for x in (0, w - 1)]
    n = max(1, len(samples))
    return QColor(
        sum(c.red() for c in samples) // n,
        sum(c.green() for c in samples) // n,
        sum(c.blue() for c in samples) // n,
    )


def area_scene_png(
    area: Area,
    source: TileSource,
    walkers: Sequence[Walker],
    places: Sequence[PlaceMark],
    size: tuple[int, int],
    *,
    dress: bool = True,
) -> bytes:
    """`area_scene_image` as PNG bytes, for the image model."""
    return encoded(area_scene_image(area, source, walkers, places, size, dress=dress), "PNG")
