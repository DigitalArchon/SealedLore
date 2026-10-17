"""How a deck in space is drawn (the author, Oct 17 2026: space interiors
"look quite boring, but there is no reason for them to look dull").

A deck used to be the tile set's one floor on every tile under one pale
light. Here each room is drawn for its use (engine/deck_plan.USES, read onto
`Room.use`): its floor graded towards its own colour and marked as such a
room is (a command deck's lit grid, engineering's grating, hazard stripes
round a cargo bay or a lock, a cabin's carpet), lit in its own colour over a
darker ambient; corridors carry light strips along their walls; viewports
show the stars and let cold light in; the hull has a rim and running lights;
lifts are lit pads. Everything is drawn over the tile set's own textures, so
the set's style still decides the look.

Called by gui/area_render._render_indoor for a deck; numpy and QPainter only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QRadialGradient

if TYPE_CHECKING:
    from sealedlore.engine.area import Area

# Each use: (floor colour the texture is graded towards, how far, light colour)
ZONES: dict[str, tuple[tuple[int, int, int], float, tuple[int, int, int]]] = {
    "corridor": ((92, 104, 122), 0.45, (120, 210, 255)),
    "command": ((52, 78, 128), 0.6, (110, 170, 255)),
    "medical": ((222, 232, 236), 0.6, (200, 255, 240)),
    "engineering": ((92, 84, 78), 0.55, (255, 150, 70)),
    "cargo": ((118, 112, 98), 0.45, (255, 226, 170)),
    "hangar": ((104, 106, 110), 0.45, (220, 230, 255)),
    "docking": ((100, 112, 126), 0.45, (180, 220, 255)),
    "quarters": ((128, 96, 74), 0.6, (255, 205, 150)),
    "dining": ((140, 104, 70), 0.6, (255, 190, 120)),
    "promenade": ((168, 180, 192), 0.5, (220, 240, 255)),
    "science": ((128, 118, 172), 0.55, (190, 150, 255)),
    "security": ((92, 88, 96), 0.5, (255, 96, 86)),
    "lounge": ((60, 70, 122), 0.6, (150, 165, 255)),
    "garden": ((86, 128, 74), 0.6, (200, 255, 170)),
    "storage": ((88, 90, 94), 0.45, (205, 205, 210)),
    "airlock": ((110, 104, 96), 0.4, (255, 80, 70)),
}
HAZARD = frozenset({"cargo", "hangar", "docking", "airlock"})
CARPET = frozenset({"quarters", "dining", "lounge"})
AMBIENT = 0.6  # a deck's light before its lamps: dimmer than a house's, so they show


def _np():
    import numpy

    return numpy


def zone_of_tiles(area: Area) -> list[list[str]]:
    """Each tile's use: its room's, "corridor" for the space the rooms open
    off, "" for walls and the void."""
    h, w = area.height, area.width
    uses = {room.index: room.use or "corridor" for room in area.rooms}
    out = [[""] * w for _ in range(h)]
    for y in range(h):
        row = area.room_of[y] if y < len(area.room_of) else []
        for x in range(w):
            index = row[x] if x < len(row) else -1
            if index is not None and index >= 0:
                out[y][x] = uses.get(index, "corridor")
            elif area.rows[y][x] in "_&+":
                out[y][x] = "corridor"
    return out


def grade(img, area: Area, px: int, zones: list[list[str]]):
    """The floor graded towards each room's colour, its texture kept: the
    tile set's floor read for its light and dark, coloured by the zone."""
    np = _np()
    h, w = area.height, area.width
    colour = np.zeros((h, w, 3), np.float32)
    amount = np.zeros((h, w), np.float32)
    for y in range(h):
        for x in range(w):
            zone = ZONES.get(zones[y][x])
            if zone is not None:
                colour[y, x] = zone[0]
                amount[y, x] = zone[1]
    big_c = np.repeat(np.repeat(colour, px, 0), px, 1)
    big_a = np.repeat(np.repeat(amount, px, 0), px, 1)[..., None]
    H, W = big_a.shape[:2]
    part = img[:H, :W]
    lum = part.mean(-1, keepdims=True)
    coloured = lum / 128.0 * big_c
    part[:] = part * (1 - big_a) + coloured * big_a
    # A cabin's or a bar's carpet: a fine weave over the plating's seams.
    carpet = np.zeros((h, w), np.float32)
    for y in range(h):
        for x in range(w):
            carpet[y, x] = zones[y][x] in CARPET
    if carpet.any():
        rng = np.random.default_rng(len(area.id) * 7 + 3)
        weave = rng.normal(0, 7, (H, W)).astype(np.float32)
        big = np.repeat(np.repeat(carpet, px, 0), px, 1)[:H, :W]
        part[:] = part * (1 - 0.35 * big[..., None]) + (lum * 0.95 + weave[..., None]) * (
            0.35 * big[..., None]
        )
    return img


def marks(p: QPainter, area: Area, px: int, zones: list[list[str]]) -> None:
    """What is painted on a deck's floor: a command deck's lit grid,
    engineering's grating, hazard stripes round bays and locks, the light
    strips along the corridors' walls, the lifts' pads."""
    rows = area.rows
    h, w = area.height, area.width

    def at(x: int, y: int) -> str:
        return rows[y][x] if 0 <= x < w and 0 <= y < h else " "

    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    for room in area.rooms:
        use = room.use or "corridor"
        x0, y0, x1, y1 = room.box
        cells = [
            (x, y)
            for y in range(y0, y1 + 1)
            for x in range(x0, x1 + 1)
            if area.room_of[y][x] == room.index
        ]
        if use == "command":
            pen = QPen(QColor(110, 180, 255, 70), max(1.0, px * 0.04))
            p.setPen(pen)
            for x, y in cells:
                p.drawRect(QRectF(x * px, y * px, px, px))
        elif use == "engineering":
            pen = QPen(QColor(20, 18, 16, 110), max(1.0, px * 0.06))
            p.setPen(pen)
            step = px / 4
            for x, y in cells:
                X, Y = x * px, y * px
                for k in range(1, 4):
                    p.drawLine(QPointF(X + k * step, Y + 2), QPointF(X + k * step, Y + px - 2))
        elif use == "medical":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(80, 210, 170, 40))
            cx, cy = (room.cx + 0.5) * px, (room.cy + 0.5) * px
            arm = min(x1 - x0 + 1, y1 - y0 + 1) * px * 0.18
            p.drawRect(QRectF(cx - arm, cy - arm / 3, 2 * arm, 2 * arm / 3))
            p.drawRect(QRectF(cx - arm / 3, cy - arm, 2 * arm / 3, 2 * arm))
        elif use == "promenade":
            p.setPen(QPen(QColor(255, 255, 255, 34), max(1.0, px * 0.05)))
            for x, y in cells:
                if (x - x0) % 3 == 0:
                    p.drawLine(QPointF(x * px, y * px), QPointF(x * px, (y + 1) * px))
                if (y - y0) % 3 == 0:
                    p.drawLine(QPointF(x * px, y * px), QPointF((x + 1) * px, y * px))
        if use in HAZARD:
            _hazard_border(p, area, room.index, cells, px)
        if use == "hangar" and (x1 - x0) >= 6 and (y1 - y0) >= 4:
            cx, cy = (room.cx + 0.5) * px, (room.cy + 0.5) * px
            r = min(x1 - x0, y1 - y0) * px * 0.35
            p.setPen(QPen(QColor(240, 200, 60, 150), max(2.0, px * 0.1)))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r, r)
    # Light strips: a bright line along every corridor wall, inset a little.
    strip = QColor(150, 225, 255, 200)
    t = max(1.5, px * 0.07)
    inset = px * 0.12
    p.setPen(Qt.NoPen)
    p.setBrush(strip)
    for y in range(h):
        for x in range(w):
            if zones[y][x] != "corridor" or rows[y][x] not in "_&":
                continue
            X, Y = x * px, y * px
            if at(x, y - 1) in "#W":
                p.drawRect(QRectF(X, Y + inset, px, t))
            if at(x, y + 1) in "#W":
                p.drawRect(QRectF(X, Y + px - inset - t, px, t))
            if at(x - 1, y) in "#W":
                p.drawRect(QRectF(X + inset, Y, t, px))
            if at(x + 1, y) in "#W":
                p.drawRect(QRectF(X + px - inset - t, Y, t, px))
    p.restore()


def _hazard_border(p: QPainter, area: Area, index: int, cells, px: int) -> None:
    """Yellow and black stripes round the inside of a bay's or a lock's
    walls."""
    band = px * 0.28
    cellset = set(cells)
    p.save()
    for x, y in cells:
        X, Y = x * px, y * px
        sides = [
            ((x, y - 1), QRectF(X, Y, px, band)),
            ((x, y + 1), QRectF(X, Y + px - band, px, band)),
            ((x - 1, y), QRectF(X, Y, band, px)),
            ((x + 1, y), QRectF(X + px - band, Y, band, px)),
        ]
        for (nx, ny), rect in sides:
            if (nx, ny) in cellset:
                continue
            ch = area.rows[ny][nx] if 0 <= ny < area.height and 0 <= nx < area.width else " "
            if ch not in "#W":
                continue
            p.fillRect(rect, QColor(230, 186, 40, 210))
            p.setClipRect(rect)
            p.setPen(QPen(QColor(25, 22, 18, 220), band * 0.45))
            for k in range(-2, 6):
                o = k * band
                p.drawLine(
                    QPointF(rect.x() + o, rect.bottom()), QPointF(rect.x() + o + band, rect.y())
                )
            p.setClipping(False)
    p.restore()


def lift_pad(p: QPainter, x: int, y: int, px: int) -> None:
    """A lift: a dark pad, a lit ring, an arrow up and one down."""
    cx, cy = (x + 0.5) * px, (y + 0.5) * px
    r = px * 0.44
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(16, 22, 34, 235))
    p.drawEllipse(QPointF(cx, cy), r, r)
    glow = QRadialGradient(QPointF(cx, cy), r)
    glow.setColorAt(0.0, QColor(90, 200, 255, 120))
    glow.setColorAt(0.7, QColor(60, 150, 230, 40))
    glow.setColorAt(1.0, QColor(60, 150, 230, 0))
    p.setBrush(QBrush(glow))
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(130, 225, 255, 230), max(1.5, px * 0.06)))
    p.drawEllipse(QPointF(cx, cy), r * 0.82, r * 0.82)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(170, 235, 255, 220))
    a = r * 0.3
    for sign in (-1, 1):
        tip = QPointF(cx, cy + sign * a * 1.6)
        p.drawPolygon(
            [tip, QPointF(cx - a, cy + sign * a * 0.4), QPointF(cx + a, cy + sign * a * 0.4)]
        )
    p.restore()


def viewport(p: QPainter, stars, X: float, top: float, px: int, depth: float) -> None:
    """A viewport in a hull wall: the wall's whole tile glass onto the stars,
    framed, with a streak of reflection."""
    rect = QRectF(X + px * 0.08, top + px * 0.08, px * 0.84, px * 0.84 + depth)
    p.save()
    p.drawImage(rect, stars, rect)
    p.fillRect(rect, QColor(60, 110, 200, 46))
    p.setPen(QPen(QColor(170, 190, 210, 220), max(1.5, px * 0.06)))
    p.setBrush(Qt.NoBrush)
    p.drawRect(rect)
    p.setPen(QPen(QColor(255, 255, 255, 70), max(1.0, px * 0.05)))
    p.drawLine(
        QPointF(rect.x() + rect.width() * 0.2, rect.bottom() - 2),
        QPointF(rect.x() + rect.width() * 0.55, rect.y() + 2),
    )
    p.restore()


def hull(p: QPainter, area: Area, px: int, face_h: float) -> None:
    """The hull's outside, where a wall meets space: a band of dark plating
    with a lit edge, and running lights at the corners (red to port, green
    to starboard, as the west and east halves)."""
    rows = area.rows
    h, w = area.height, area.width

    def space(x: int, y: int) -> bool:
        return not (0 <= x < w and 0 <= y < h) or rows[y][x] == " "

    band = px * 0.22
    plate = QColor(58, 64, 74)
    lit = QColor(150, 170, 190)
    p.save()
    for y in range(h):
        for x in range(w):
            if rows[y][x] not in "#W":
                continue
            X, Y = x * px, y * px - face_h * 0.35
            if space(x, y - 1):
                p.fillRect(QRectF(X, Y - band, px, band), plate)
                p.fillRect(QRectF(X, Y - band, px, max(1.0, px * 0.04)), lit)
            if space(x - 1, y):
                p.fillRect(QRectF(X - band, Y, band, px), plate)
                p.fillRect(QRectF(X - band, Y, max(1.0, px * 0.04), px), lit)
            if space(x + 1, y):
                p.fillRect(QRectF(X + px, Y, band, px), plate)
                p.fillRect(QRectF(X + px + band - max(1.0, px * 0.04), Y, px * 0.04, px), lit)
            corners = sum(space(x + dx, y + dy) for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)))
            open_sides = space(x, y - 1) + space(x - 1, y) + space(x + 1, y) + space(x, y + 1)
            if open_sides >= 2 and corners >= 3 and (x + y) % 3 == 0:
                colour = QColor(255, 70, 60) if x < w / 2 else QColor(80, 255, 120)
                p.setPen(Qt.NoPen)
                halo = QRadialGradient(QPointF(X + px / 2, Y + px / 2), px * 0.5)
                halo.setColorAt(0, QColor(colour.red(), colour.green(), colour.blue(), 160))
                halo.setColorAt(1, QColor(colour.red(), colour.green(), colour.blue(), 0))
                p.setBrush(QBrush(halo))
                p.drawEllipse(QPointF(X + px / 2, Y + px / 2), px * 0.5, px * 0.5)
                p.setBrush(colour)
                p.drawEllipse(QPointF(X + px / 2, Y + px / 2), px * 0.09, px * 0.09)
    p.restore()


def lights(area: Area, px: int, zones: list[list[str]]) -> list[tuple[float, float, float, tuple]]:
    """Each room's lamps in its zone's colour, the corridors' strips as a run
    of small lights, a glow at each lift, cold light in at each viewport."""
    out: list[tuple[float, float, float, tuple]] = []
    for room in area.rooms:
        use = room.use or "corridor"
        colour = ZONES.get(use, ZONES["storage"])[2]
        if use == "corridor":
            continue
        x0, y0, x1, y1 = room.box
        rw, rh = x1 - x0 + 1, y1 - y0 + 1
        if room.cells >= 30 and rw >= 6:
            for fx in (0.28, 0.72):
                out.append(
                    ((x0 + rw * fx) * px, (room.cy + 0.5) * px, px * max(3.5, rh * 0.8), colour)
                )
        else:
            out.append(
                ((room.cx + 0.5) * px, (room.cy + 0.5) * px, px * max(3.0, rw * 0.7), colour)
            )
    rows = area.rows
    h, w = area.height, area.width
    # The strips' glow: many soft pools, so a corridor reads evenly lit (a
    # few strong ones left it blotched light and dark).
    soft = tuple(int(c * 0.5) for c in ZONES["corridor"][2])
    for y in range(h):
        for x in range(w):
            ch = rows[y][x]
            if ch == "&":
                out.append(((x + 0.5) * px, (y + 0.5) * px, px * 2.2, (110, 210, 255)))
            elif zones[y][x] == "corridor" and ch == "_" and (x + y) % 2 == 0:
                walls = sum(
                    rows[ny][nx] in "#W"
                    for nx, ny in ((x, y - 1), (x, y + 1), (x - 1, y), (x + 1, y))
                    if 0 <= nx < w and 0 <= ny < h
                )
                if walls:
                    out.append(((x + 0.5) * px, (y + 0.5) * px, px * 2.4, soft))
            elif ch == "W":
                for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and rows[ny][nx] in "_&":
                        out.append(((nx + 0.5) * px, (ny + 0.5) * px, px * 2.6, (150, 185, 255)))
    return out


def light_rgb(lights_in, h: int, w: int, mask_at=None):
    """The coloured glow of every light summed, each kept within its room
    by `mask_at` (as gui/area_render._light), as an (h, w, 3) array in 0..~1.5."""
    np = _np()
    glow = np.zeros((h, w, 3), np.float32)
    for lx, ly, rad, colour in lights_in:
        y0, y1 = int(max(0, ly - rad)), int(min(h, ly + rad))
        x0, x1 = int(max(0, lx - rad)), int(min(w, lx + rad))
        if y1 <= y0 or x1 <= x0:
            continue
        mask = mask_at(lx, ly) if mask_at is not None else None
        if mask is False:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1]
        dd = ((xx - lx) ** 2 + (yy - ly) ** 2) / rad**2
        part = np.clip(1 - dd, 0, 1) ** 2
        if mask is not None:
            part = part * mask[y0:y1, x0:x1]
        glow[y0:y1, x0:x1] += part[..., None] * (np.array(colour, np.float32) / 255.0)
    return np.clip(glow, 0, 1.5)
