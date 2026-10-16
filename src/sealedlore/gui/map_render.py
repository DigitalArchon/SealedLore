"""A local area's plan as one picture, as the map's local level draws it:
the area's plan in its look, buildings roofed from outside, a building the
story changed open in its new state. The image model is given it to paint
the isometric view from (engine/world_pictures.py), so it is drawn with
QImage alone: it runs on the map pictures' thread, not the GUI's.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPainter

from sealedlore.engine.world_art import changed_buildings, decked, plan_ground
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
