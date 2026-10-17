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
from PySide6.QtGui import QBrush, QColor, QImage, QLinearGradient, QPainter, QPen, QRadialGradient

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
    mass = area.hull

    def space(x: int, y: int) -> bool:
        if not (0 <= x < w and 0 <= y < h):
            return True
        return rows[y][x] == " " and not (mass and mass[y][x] != " ")

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
    for f in area.placements:
        if f.piece in ("reactor", "shaft", "beds") and not f.broken:
            radius = px * (max(f.w, f.h) * (1.6 if f.piece == "reactor" else 1.0) + 1.5)
            out.append(((f.x + f.w / 2) * px, (f.y + f.h / 2) * px, radius, piece_light(f)))
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


# --- The hull's mass and the set pieces (engine/deck_plan.py) ----------------------

# Light from the upper left: the edges that catch it and those in shade.
RIM_LIT = QColor(196, 212, 230, 200)
RIM_DARK = QColor(8, 10, 16, 220)
ROCK_LIT = QColor(214, 196, 172, 170)
ROCK_DARK = QColor(10, 8, 6, 230)


def _hash(x: int, y: int, salt: int = 0) -> float:
    """A tile's own fixed number in [0, 1): the same greebles every paint."""
    n = (x * 374761393 + y * 668265263 + salt * 2246822519) & 0xFFFFFFFF
    n = (n ^ (n >> 13)) * 1274126177 & 0xFFFFFFFF
    return ((n ^ (n >> 16)) & 0xFFFF) / 65536.0


def _marks(area: Area):
    hull = area.hull
    h, w = area.height, area.width

    def mark(x: int, y: int) -> str:
        if 0 <= y < h and 0 <= x < w and y < len(hull) and x < len(hull[y]):
            return hull[y][x]
        return " "

    def space(x: int, y: int) -> bool:
        inside = 0 <= y < h and 0 <= x < w
        return (not inside or area.rows[y][x] == " ") and mark(x, y) == " "

    return mark, space


def hull_layer(base, area: Area, px: int, plate: QBrush, rock: QBrush, upsample, blur):
    """The hull's mass (`Area.hull`) laid over the stars: one silhouette for
    the mass and everything the deck builds, its tile steps smoothed to
    chamfers (the mask upsampled between tile centres), bevelled by light
    from the upper left, in the set's plating or an asteroid's rock. The
    deck's floors and walls are painted over it. `upsample` and `blur` are
    the renderer's own (gui/area_render.py)."""
    np = _np()
    h, w = area.height, area.width
    marks = np.array([list(r.ljust(w)[:w]) for r in area.hull])
    built = np.array([list(r) for r in area.rows]) != " "
    is_rock = (marks == "R").any()
    union = (marks != " ") | built
    W, HX = base.width(), base.height()
    alpha = np.zeros((HX, W), np.float32)
    smooth = upsample(union, px)[: h * px, :W]
    alpha[: smooth.shape[0], : smooth.shape[1]] = np.clip((smooth - 0.5) * 3.0 + 0.5, 0, 1)
    texture = QImage(W, HX, QImage.Format_RGB32)
    tp = QPainter(texture)
    tp.fillRect(texture.rect(), rock if is_rock else plate)
    tp.end()
    from sealedlore.gui.area_render import _arr, _qimg

    tex = _arr(texture)
    soft = blur(alpha, max(2, px // 4), 2)
    gy, gx = np.gradient(soft)
    slope = (gx + gy) * px * 0.9
    lit = np.clip(slope, 0, 1)[..., None]
    shade = np.clip(-slope, 0, 1)[..., None]
    tex = tex * (1 + 0.55 * lit - 0.65 * shade) + lit * 30
    out = _arr(base) * (1 - alpha[..., None]) + tex * alpha[..., None]
    return _qimg(out)


def hull_details(p: QPainter, area: Area, px: int) -> None:
    """What is on the hull's mass: panel seams, vents and hatches on
    plating, craters on rock, and each engine's bell with its nozzle."""
    if not area.hull:
        return
    mark, _space = _marks(area)
    h, w = area.height, area.width
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    seam = QPen(QColor(18, 22, 32, 120), max(1.0, px * 0.05))

    def solid(x: int, y: int) -> bool:
        return mark(x, y) in "HE" or (0 <= y < h and 0 <= x < w and area.rows[y][x] != " ")

    for y in range(h):
        for x in range(w):
            m = mark(x, y)
            X, Y = x * px, y * px
            if m == "H":
                # Panels three tiles a side, staggered; seams only between plates.
                p.setPen(seam)
                if (x + (y // 3)) % 3 == 0 and solid(x - 1, y):
                    p.drawLine(QPointF(X, Y + 2), QPointF(X, Y + px - 2))
                if y % 3 == 0 and solid(x, y - 1):
                    p.drawLine(QPointF(X + 2, Y), QPointF(X + px - 2, Y))
                p.setPen(Qt.NoPen)
                if sum(solid(x + dx, y + dy) for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0))) < 4:
                    continue  # nothing on an edge tile: the bevel is there
                r = _hash(x, y, 1)
                if r < 0.07:  # a vent
                    p.setBrush(QColor(20, 24, 32, 230))
                    vent = QRectF(X + px * 0.2, Y + px * 0.3, px * 0.6, px * 0.4)
                    p.drawRect(vent)
                    p.setPen(QPen(QColor(120, 132, 150, 220), max(1.0, px * 0.04)))
                    for k in range(1, 4):
                        yy = vent.y() + vent.height() * k / 4
                        p.drawLine(QPointF(vent.x() + 2, yy), QPointF(vent.right() - 2, yy))
                    p.setPen(Qt.NoPen)
                elif r < 0.11:  # a hatch
                    p.setBrush(QColor(0, 0, 0, 40))
                    p.setPen(QPen(QColor(30, 34, 44, 220), max(1.0, px * 0.05)))
                    p.drawRect(QRectF(X + px * 0.22, Y + px * 0.22, px * 0.56, px * 0.56))
                    p.setPen(QPen(QColor(170, 180, 196, 120), max(1.0, px * 0.03)))
                    p.drawLine(
                        QPointF(X + px * 0.24, Y + px * 0.24), QPointF(X + px * 0.76, Y + px * 0.24)
                    )
                    p.setPen(Qt.NoPen)
                elif r < 0.14:  # a sensor's dome
                    p.setBrush(QColor(176, 186, 200))
                    p.drawEllipse(QPointF(X + px * 0.5, Y + px * 0.5), px * 0.14, px * 0.14)
                    p.setBrush(QColor(40, 44, 54))
                    p.drawEllipse(QPointF(X + px * 0.55, Y + px * 0.55), px * 0.06, px * 0.06)
            elif m == "R" and _hash(x, y, 2) < 0.09:
                # A crater: a dark bowl, its far rim catching the light.
                rad = px * (0.25 + 0.35 * _hash(x, y, 3))
                c = QPointF(X + px * 0.5, Y + px * 0.5)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(20, 16, 12, 110))
                p.drawEllipse(c, rad, rad)
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(230, 210, 186, 120), max(1.0, px * 0.05)))
                p.drawArc(QRectF(c.x() - rad, c.y() - rad, 2 * rad, 2 * rad), 200 * 16, 160 * 16)
                p.setPen(Qt.NoPen)
    # Engines: each run of "E" a bell, lit from above, its nozzle aglow.
    seen: set[tuple[int, int]] = set()
    for y in range(h):
        for x in range(w):
            if mark(x, y) != "E" or (x, y) in seen:
                continue
            x1 = x
            while mark(x1 + 1, y) == "E":
                x1 += 1
            y1 = y
            while mark(x, y1 + 1) == "E":
                y1 += 1
            for yy in range(y, y1 + 1):
                for xx in range(x, x1 + 1):
                    seen.add((xx, yy))
            bell = QRectF(
                x * px,
                y * px + px * 0.1,
                (x1 - x + 1) * px + px * 0.3,
                (y1 - y + 1) * px - px * 0.2,
            )
            metal = QLinearGradient(bell.topLeft(), bell.bottomLeft())
            metal.setColorAt(0.0, QColor(200, 208, 220))
            metal.setColorAt(0.45, QColor(126, 134, 148))
            metal.setColorAt(1.0, QColor(46, 50, 60))
            p.setPen(QPen(QColor(16, 18, 24), max(1.0, px * 0.05)))
            p.setBrush(QBrush(metal))
            p.drawRoundedRect(bell, px * 0.25, px * 0.25)
            p.setPen(QPen(QColor(30, 34, 42, 160), max(1.0, px * 0.04)))
            for k in range(1, x1 - x + 1):
                p.drawLine(
                    QPointF((x + k) * px, bell.y() + 2), QPointF((x + k) * px, bell.bottom() - 2)
                )
            mouth = QRectF(
                bell.x() - px * 0.12, bell.y() + px * 0.08, px * 0.36, bell.height() - px * 0.16
            )
            p.setPen(QPen(QColor(90, 96, 108), max(1.0, px * 0.06)))
            glow = QLinearGradient(mouth.topLeft(), mouth.topRight())
            glow.setColorAt(0.0, QColor(235, 250, 255))
            glow.setColorAt(1.0, QColor(90, 170, 255))
            p.setBrush(QBrush(glow))
            p.drawEllipse(mouth)
    p.restore()


def glows(image, area: Area, px: int) -> None:
    """What shines in the dark after the deck is lit (the lamps are kept to
    their rooms, so these would be lost): running lights along the hull's
    outline, red to port and green to starboard, white at the extremes, and
    each engine's exhaust out into space."""
    if not area.hull:
        return
    mark, space = _marks(area)
    h, w = area.height, area.width
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    edge = [
        (x, y)
        for y in range(h)
        for x in range(w)
        if mark(x, y) == "H"
        and sum(space(x + dx, y + dy) for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0))) >= 2
    ]
    for x, y in edge:
        if _hash(x, y, 5) > 0.22:
            continue
        colour = QColor(255, 70, 60) if y < h / 2 else QColor(80, 255, 130)
        if x <= min(e[0] for e in edge) + 1 or x >= max(e[0] for e in edge) - 1:
            colour = QColor(255, 255, 240)
        c = QPointF((x + 0.5) * px, (y + 0.5) * px)
        halo = QRadialGradient(c, px * 0.7)
        halo.setColorAt(0, QColor(colour.red(), colour.green(), colour.blue(), 150))
        halo.setColorAt(1, QColor(colour.red(), colour.green(), colour.blue(), 0))
        p.setBrush(QBrush(halo))
        p.drawEllipse(c, px * 0.7, px * 0.7)
        p.setBrush(colour)
        p.drawEllipse(c, px * 0.1, px * 0.1)
    for y in range(h):
        for x in range(w):
            if mark(x, y) != "E" or mark(x - 1, y) == "E":
                continue
            # The plume: hot and white at the nozzle, blue, then nothing.
            length = px * 3.6
            top, height = y * px + px * 0.15, px * 0.7
            if mark(x, y - 1) == "E":
                top -= px * 0.15
            plume = QRadialGradient(QPointF(x * px, top + height / 2), length)
            plume.setColorAt(0.0, QColor(235, 250, 255, 235))
            plume.setColorAt(0.25, QColor(120, 200, 255, 170))
            plume.setColorAt(0.6, QColor(60, 110, 255, 70))
            plume.setColorAt(1.0, QColor(40, 60, 255, 0))
            p.setBrush(QBrush(plume))
            p.drawRect(QRectF(x * px - length, top, length, height))
    p.end()


PIECE_LIGHT = {
    "reactor": (120, 220, 255),
    "drive": (190, 140, 255),
    "shaft": (255, 170, 80),
    "beds": (220, 160, 255),
}


def piece_light(f) -> tuple[int, int, int]:
    if f.piece == "reactor" and "drive" in f.name.lower():
        return PIECE_LIGHT["drive"]
    return PIECE_LIGHT.get(f.piece, (200, 200, 200))


def piece(p: QPainter, f, px: int) -> None:
    """A set piece the program draws (engine/deck_plan.py DRAWN_PIECES), over
    its footprint: a reactor's or drive's glowing core in its well, a mine's
    shaft with its hoist, a garden's planter bed. A broken one is dark."""
    box = QRectF(f.x * px, f.y * px, f.w * px, f.h * px)
    c = box.center()
    r = min(box.width(), box.height()) / 2
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    if f.piece == "reactor":
        core = QColor(*piece_light(f))
        # The well and its housing ring, conduits out to the floor's edge.
        p.setBrush(QColor(30, 34, 42))
        p.drawRect(QRectF(box.x(), c.y() - px * 0.12, box.width(), px * 0.24))
        p.drawRect(QRectF(c.x() - px * 0.12, box.y(), px * 0.24, box.height()))
        p.setBrush(QColor(70, 78, 92))
        p.drawEllipse(c, r * 0.98, r * 0.98)
        p.setBrush(QColor(14, 16, 22))
        p.drawEllipse(c, r * 0.8, r * 0.8)
        if not f.broken:
            glow = QRadialGradient(c, r * 0.78)
            glow.setColorAt(0.0, QColor(255, 255, 255, 255))
            glow.setColorAt(0.35, QColor(core.red(), core.green(), core.blue(), 245))
            glow.setColorAt(1.0, QColor(core.red() // 3, core.green() // 3, core.blue() // 2, 200))
            p.setBrush(QBrush(glow))
            p.drawEllipse(c, r * 0.72, r * 0.72)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(core.red(), core.green(), core.blue(), 200), max(1.5, px * 0.06)))
            for k in (0.45, 0.62):
                p.drawEllipse(c, r * k, r * k)
        p.setPen(QPen(QColor(150, 160, 176), max(1.5, px * 0.07)))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, r * 0.9, r * 0.9)
    elif f.piece == "shaft":
        # A round pit going down, ringed in hazard yellow, a hoist frame over it.
        p.setBrush(QColor(230, 186, 40))
        p.drawEllipse(c, r * 0.98, r * 0.98)
        p.setBrush(QColor(28, 24, 20))
        p.drawEllipse(c, r * 0.84, r * 0.84)
        pit = QRadialGradient(c, r * 0.8)
        pit.setColorAt(0.0, QColor(0, 0, 0, 255))
        pit.setColorAt(0.7, QColor(14, 10, 8, 255))
        pit.setColorAt(1.0, QColor(60, 48, 38, 255))
        p.setBrush(QBrush(pit))
        p.drawEllipse(c, r * 0.8, r * 0.8)
        p.setPen(QPen(QColor(120, 110, 96), max(2.0, px * 0.14)))
        p.drawLine(QPointF(box.x() + px * 0.1, c.y()), QPointF(box.right() - px * 0.1, c.y()))
        p.drawLine(QPointF(c.x(), box.y() + px * 0.1), QPointF(c.x(), box.bottom() - px * 0.1))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 200, 120) if not f.broken else QColor(60, 50, 40))
        p.drawEllipse(c, px * 0.12, px * 0.12)
    elif f.piece == "beds":
        # A planter: a metal rim round dark soil, rows of leaves.
        p.setBrush(QColor(96, 104, 116))
        p.drawRoundedRect(box.adjusted(px * 0.05, px * 0.08, -px * 0.05, -px * 0.08), 3, 3)
        soil = box.adjusted(px * 0.14, px * 0.18, -px * 0.14, -px * 0.18)
        p.setBrush(QColor(54, 40, 30))
        p.drawRect(soil)
        greens = (
            QColor(70, 150, 70),
            QColor(96, 180, 84),
            QColor(54, 124, 64),
            QColor(130, 196, 96),
        )
        if f.broken:
            greens = (QColor(110, 96, 60), QColor(90, 80, 54))
        step = px * 0.32
        n_x = max(1, int(soil.width() / step))
        n_y = max(1, int(soil.height() / step))
        for i in range(n_x):
            for j in range(n_y):
                hx = _hash(f.x * 7 + i, f.y * 7 + j, 9)
                leaf = greens[int(hx * len(greens)) % len(greens)]
                p.setBrush(leaf)
                cx_ = soil.x() + (i + 0.5) * soil.width() / n_x
                cy_ = soil.y() + (j + 0.5) * soil.height() / n_y
                rad = step * (0.45 + 0.25 * hx)
                p.drawEllipse(QPointF(cx_, cy_), rad, rad * 0.85)
    p.restore()
