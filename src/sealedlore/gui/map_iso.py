"""The program's own isometric render of a local area's plan, for an image
model to repaint as the isometric view (Map → Isometric): the ground tiles
projected as diamonds, each building a block with a pitched roof, town walls
as low blocks, fences as posts, the plan's things as columns, posts and
mounds, a building the story changed shown burning or in ruins.

Measured (SealedLoreDocs design/map.md, Oct 15 2026): sent this instead of
the top-down plan, every model kept the layout better (Qwen Image 1.75 →
3.1-3.5 of 5, FLUX.2 klein 9B 2.1 → 3.1, Seedream 5.0 Pro 2.75 → 3.5), and
with every building alike as well as with each in its own colour, which
leaked into the pictures. Drawn with QImage alone, on the painting's thread
(engine/session_world_art.py): the engine draws nothing.
"""

from __future__ import annotations

import zlib

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF

from sealedlore.engine.world_art import changed_buildings, plan_ground
from sealedlore.engine.world_pictures import seen_change
from sealedlore.gui.image_codec import encoded
from sealedlore.gui.map_render import drawn_box
from sealedlore.gui.map_tiles import LOOKS, Look, building_boxes
from sealedlore.models.world import WorldArt, WorldMap

# The ground diamond's share of the picture's width.
MARGIN = 0.96
# Things on the plan, by the words in their names: drawn tall, as a lamp, or
# as a heap; anything else a small block.
TALL = ("tower", "stack", "chimney", "steeple", "mast", "spire")
LAMP = ("lamp", "lantern", "torch")
MOUND = ("pile", "heap", "barricade", "stack", "rubble", "plough", "hay", "scrap", "wreck")
WELL = ("well", "trough", "basin")
FIRE = ("#ff8a2a", "#d8521a", "#ffd24a")
RUIN = ("#8a847a", "#6a655c", "#9f9a90")


def _shade(colour: str, k: float) -> QColor:
    q = QColor(colour)
    return QColor(*(max(0, min(255, int(c * k))) for c in (q.red(), q.green(), q.blue())))


def _var(x: int, y: int) -> int:
    """Most tiles plain, a few lighter or darker: texture, not a chessboard."""
    v = zlib.crc32(f"{x},{y}".encode()) % 7
    return 0 if v < 5 else (1 if v == 5 else 2)


class _Iso:
    """The projection: tile (x, y) to screen, north at the top."""

    def __init__(self, cols: int, rows: int, size: tuple[int, int]) -> None:
        self.width, self.height = size
        tw = int(2 * MARGIN * self.width / max(1, cols + rows))
        self.tw = max(2, tw - tw % 2)
        self.th = self.tw // 2
        diamond_w = (cols + rows) * self.tw / 2
        diamond_h = (cols + rows) * self.th / 2
        self.ox = (self.width - diamond_w) / 2 + rows * self.tw / 2
        self.oy = (self.height - diamond_h) / 2 + self.th * 1.5  # room for the tallest things

    def pt(self, x: float, y: float, z: float = 0.0) -> QPointF:
        return QPointF(self.ox + (x - y) * self.tw / 2, self.oy + (x + y) * self.th / 2 - z)

    def tile(self, x: int, y: int) -> QPolygonF:
        return QPolygonF(
            [self.pt(x, y), self.pt(x + 1, y), self.pt(x + 1, y + 1), self.pt(x, y + 1)]
        )


def iso_image(world: WorldMap, art: WorldArt, area_id: str, size: tuple[int, int]) -> QImage | None:
    """The area's plan rendered isometric at `size` (the picture the model
    will paint, so input and output are one shape), or None when it has no
    plan."""
    area = world.places.get(area_id)
    layout = art.layout_shown(area_id, area.state) if area else None
    if area is None or layout is None or not layout.rows:
        return None
    ground = plan_ground(world, art, area, str(layout.spec.get("ground", "")))
    look = LOOKS.get(ground, LOOKS[""])
    rows = [r.ljust(max(len(r) for r in layout.rows)) for r in layout.rows]
    # A building the story changed, by its box in this plan: burning or in ruins.
    changed: dict[tuple[int, int, int, int], str] = {}
    for child in changed_buildings(world, art, area_id):
        box = drawn_box(layout, child)
        if box is not None:
            changed[box] = seen_change(art, child) or child.state
    return render_rows(rows, look, layout.objects, size, changed=changed)


def render_rows(
    rows: list[str],
    look: Look,
    objects: dict[str, str],
    size: tuple[int, int],
    *,
    changed: dict[tuple[int, int, int, int], str] | None = None,
) -> QImage:
    """`rows` (engine/world_art.TILES) drawn isometric in `look`."""
    cols, rows_n = len(rows[0]), len(rows)
    iso = _Iso(cols, rows_n, size)
    image = QImage(iso.width, iso.height, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(look.base))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    boxes = building_boxes(tuple(rows))
    in_building = {
        (x, y) for x0, x1, y0, y1 in boxes for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
    }
    _ground(painter, iso, rows, look, in_building)
    items: list[tuple[int, str, object, object, str]] = []
    for y in range(rows_n):
        for x in range(cols):
            c = rows[y][x]
            if (x, y) in in_building:
                continue
            if c == "#":
                items.append((x + y, "wall", x, y, ""))
            elif c == "%":
                items.append((x + y, "fence", x, y, ""))
            elif c == "T":
                items.append((x + y, "tree", x, y, ""))
            elif c == "^":
                items.append((x + y, "rock", x, y, ""))
            elif c.isdigit() and c in objects:
                items.append((x + y, "thing", x, y, objects[c].lower()))
    for box in boxes:
        items.append((box[1] + box[3], "building", box, rows, ""))
    items.sort(key=lambda item: item[0])  # back to front
    for _depth, kind, a, b, name in items:
        if kind == "building":
            _building(painter, iso, a, b, look, (changed or {}).get(a, ""))  # type: ignore[arg-type]
        elif kind == "wall":
            _block(
                painter,
                iso,
                a,
                a + 1,
                b,
                b + 1,
                iso.tw * 0.55,
                look.wall[1],
                look.wall[0],
                look.wall[2],
                look.wall[1],
            )  # type: ignore[operator]
        elif kind == "fence":
            _fence(painter, iso, a, b)  # type: ignore[arg-type]
        elif kind == "tree":
            _tree(painter, iso, a, b, look)  # type: ignore[arg-type]
        elif kind == "rock":
            _mound(painter, iso, a, b, 0.35, "#8e9097", "#66686f")  # type: ignore[arg-type]
        else:
            _thing(painter, iso, a, b, name, look)  # type: ignore[arg-type]
    painter.end()
    return image


def _ground(painter: QPainter, iso: _Iso, rows: list[str], look: Look, in_building: set) -> None:
    painter.setPen(Qt.NoPen)
    floor = (
        ("#8f949c", "#9aa0a8", "#858a92")
        if look.floor == "panels"
        else ("#b8b2a4", "#c2bcae", "#aea89a")
    )
    for y, row in enumerate(rows):
        for x, c in enumerate(row):
            if (x, y) in in_building:
                continue
            v = _var(x, y)
            if c == "~":
                colour = look.water[(0, 1, 0)[v]]
            elif c == "=":
                colour = look.road[(0, 1, 0)[v]]
            elif c == "_":
                colour = floor[v]
            elif c == ":":
                colour = ("#6f675d", "#7d7569", "#5f584f")[v]
            elif c == "*":
                colour = FIRE[v]
            elif c == "s":
                colour = LOOKS["sand"].base
            elif c == "^":
                colour = ("#8e9097", "#a6a8ae", "#7b7d84")[v]
            elif c == "T":
                colour = look.shade
            elif c == ",":
                colour = look.brush[0] if v else look.base
            else:
                colour = (look.base, look.light, look.shade)[v]
            painter.setBrush(QColor(colour))
            painter.drawPolygon(iso.tile(x, y))


def _building(painter: QPainter, iso: _Iso, box, rows: list[str], look: Look, change: str) -> None:
    x0, x1, y0, y1 = box
    w, d = x1 - x0 + 1, y1 - y0 + 1
    h = iso.tw * (0.8 + min(0.7, w * d / 120))
    wall_l, wall_d = look.wall[2], look.wall[1]
    roof, roof_dark = look.roof[1], look.roof[0]
    ruined = any(word in change for word in ("ruin", "walls down", "collapsed", "razed"))
    burning = "fire" in change or "burn" in change
    if ruined:
        h *= 0.45
        wall_l, wall_d, roof, roof_dark = RUIN[2], RUIN[1], RUIN[0], RUIN[1]
    if burning:
        roof, roof_dark = FIRE[0], FIRE[1]
    _block(
        painter, iso, x0, x1 + 1, y0, y1 + 1, h, wall_l, wall_d, roof, roof_dark, ridge=not ruined
    )
    for x in range(x0, x1 + 1):  # doors and windows on the visible faces
        c = rows[y1][x]
        if c in "+W":
            _opening(painter, iso, x, y1 + 1, x + 1, y1 + 1, h, c == "+")
    for y in range(y0, y1 + 1):
        c = rows[y][x1]
        if c in "+W":
            _opening(painter, iso, x1 + 1, y, x1 + 1, y + 1, h, c == "+")
    if burning:
        painter.setPen(Qt.NoPen)
        for i in range(max(2, w)):
            top = iso.pt(x0 + (i + 0.5) * w / max(2, w), y0 + d / 2, h + iso.tw * 0.3)
            painter.setBrush(QColor(FIRE[2 if i % 2 else 0]))
            painter.drawEllipse(top, iso.tw * 0.18, iso.tw * 0.5)


def _block(painter, iso, x0, x1, y0, y1, h, wall_l, wall_d, roof, roof_dark, ridge=False) -> None:
    painter.setPen(QPen(_shade(wall_d, 0.8), 1))
    east = QPolygonF([iso.pt(x1, y0), iso.pt(x1, y1), iso.pt(x1, y1, h), iso.pt(x1, y0, h)])
    south = QPolygonF([iso.pt(x0, y1), iso.pt(x1, y1), iso.pt(x1, y1, h), iso.pt(x0, y1, h)])
    painter.setBrush(QColor(wall_d))
    painter.drawPolygon(east)
    painter.setBrush(QColor(wall_l))
    painter.drawPolygon(south)
    painter.setPen(QPen(_shade(roof_dark, 0.8), 1))
    if ridge and (x1 - x0) >= 2 and (y1 - y0) >= 2:
        rise = h * 0.35
        if (x1 - x0) >= (y1 - y0):
            ym = (y0 + y1) / 2
            a = QPolygonF(
                [
                    iso.pt(x0, y0, h),
                    iso.pt(x1, y0, h),
                    iso.pt(x1, ym, h + rise),
                    iso.pt(x0, ym, h + rise),
                ]
            )
            b = QPolygonF(
                [
                    iso.pt(x0, ym, h + rise),
                    iso.pt(x1, ym, h + rise),
                    iso.pt(x1, y1, h),
                    iso.pt(x0, y1, h),
                ]
            )
        else:
            xm = (x0 + x1) / 2
            a = QPolygonF(
                [
                    iso.pt(x0, y0, h),
                    iso.pt(xm, y0, h + rise),
                    iso.pt(xm, y1, h + rise),
                    iso.pt(x0, y1, h),
                ]
            )
            b = QPolygonF(
                [
                    iso.pt(xm, y0, h + rise),
                    iso.pt(x1, y0, h),
                    iso.pt(x1, y1, h),
                    iso.pt(xm, y1, h + rise),
                ]
            )
        painter.setBrush(QColor(roof_dark))
        painter.drawPolygon(a)
        painter.setBrush(QColor(roof))
        painter.drawPolygon(b)
    else:
        top = QPolygonF(
            [iso.pt(x0, y0, h), iso.pt(x1, y0, h), iso.pt(x1, y1, h), iso.pt(x0, y1, h)]
        )
        painter.setBrush(QColor(roof))
        painter.drawPolygon(top)


def _opening(painter, iso, xa, ya, xb, yb, h, door: bool) -> None:
    colour = QColor("#3a2a1c") if door else QColor("#d8ecf4")
    top = h * (0.75 if door else 0.7)
    bottom = 0 if door else h * 0.35
    a, b = iso.pt(xa, ya), iso.pt(xb, yb)
    ax, ay = a.x() + (b.x() - a.x()) * 0.25, a.y() + (b.y() - a.y()) * 0.25
    bx, by = a.x() + (b.x() - a.x()) * 0.75, a.y() + (b.y() - a.y()) * 0.75
    painter.setPen(Qt.NoPen)
    painter.setBrush(colour)
    painter.drawPolygon(
        QPolygonF(
            [
                QPointF(ax, ay - bottom),
                QPointF(bx, by - bottom),
                QPointF(bx, by - top),
                QPointF(ax, ay - top),
            ]
        )
    )


def _fence(painter, iso, x, y) -> None:
    h = iso.tw * 0.45
    centre = iso.pt(x + 0.5, y + 0.5)
    painter.setPen(QPen(QColor("#6b4a2e"), max(1, iso.tw // 12)))
    painter.drawLine(centre, QPointF(centre.x(), centre.y() - h))
    painter.setPen(QPen(QColor("#8b6340"), max(1, iso.tw // 16)))
    left, right = iso.pt(x, y + 0.5), iso.pt(x + 1, y + 0.5)
    painter.drawLine(QPointF(left.x(), left.y() - h * 0.6), QPointF(right.x(), right.y() - h * 0.6))


def _tree(painter, iso, x, y, look: Look) -> None:
    centre = iso.pt(x + 0.5, y + 0.5)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#5a3d22"))
    painter.drawRect(
        int(centre.x() - iso.tw * 0.05),
        int(centre.y() - iso.tw * 0.5),
        max(2, int(iso.tw * 0.1)),
        int(iso.tw * 0.5),
    )
    painter.setBrush(QColor(look.canopy[1] if look.trees == "living" else "#6b5a4a"))
    painter.drawEllipse(
        QPointF(centre.x(), centre.y() - iso.tw * 0.75), iso.tw * 0.4, iso.tw * 0.45
    )


def _mound(painter, iso, x, y, k: float, light: str, dark: str) -> None:
    centre = iso.pt(x + 0.5, y + 0.5)
    painter.setPen(QPen(QColor(dark), 1))
    painter.setBrush(QColor(light))
    painter.drawEllipse(
        QPointF(centre.x(), centre.y() - iso.tw * k * 0.4), iso.tw * 0.42, iso.tw * k
    )


def _thing(painter, iso, x, y, name: str, look: Look) -> None:
    centre = iso.pt(x + 0.5, y + 0.5)
    if any(t in name for t in TALL):
        h, r = iso.tw * 2.2, iso.tw * 0.22
        painter.setPen(QPen(QColor("#5f646b"), 1))
        painter.setBrush(QColor("#8a8f96"))
        painter.drawRect(int(centre.x() - r), int(centre.y() - h), int(2 * r), int(h))
        painter.setBrush(QColor("#5f646b"))
        painter.drawEllipse(QPointF(centre.x(), centre.y() - h), r, r / 2)
    elif any(t in name for t in LAMP):
        h = iso.tw * 1.1
        painter.setPen(QPen(QColor("#3a3a3a"), max(1, iso.tw // 10)))
        painter.drawLine(centre, QPointF(centre.x(), centre.y() - h))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#ffd86a"))
        painter.drawEllipse(QPointF(centre.x(), centre.y() - h), iso.tw * 0.14, iso.tw * 0.14)
    elif any(t in name for t in MOUND):
        hay = "hay" in name
        _mound(
            painter,
            iso,
            x,
            y,
            0.45,
            "#d8b24a" if hay else "#4a4642",
            "#a98a2e" if hay else "#2e2b28",
        )
    elif "oak" in name or "tree" in name:
        _tree(painter, iso, x, y, look)
    elif "gate" in name:
        _block(
            painter,
            iso,
            x,
            x + 1,
            y,
            y + 1,
            iso.tw * 0.8,
            "#4a4a50",
            "#333338",
            "#5a5a60",
            "#4a4a50",
        )
    elif any(t in name for t in WELL):
        _mound(painter, iso, x, y, 0.25, "#8a7a6a", "#5a4e42")
    else:
        _block(
            painter,
            iso,
            x,
            x + 1,
            y,
            y + 1,
            iso.tw * 0.5,
            "#9a8c7a",
            "#7a6c5a",
            "#b0a290",
            "#9a8c7a",
        )


def iso_png(world: WorldMap, art: WorldArt, area_id: str, size: tuple[int, int]) -> bytes | None:
    """`iso_image` as PNG bytes, for the image model."""
    image = iso_image(world, art, area_id, size)
    if image is None:
        return None
    return encoded(image, "PNG")
