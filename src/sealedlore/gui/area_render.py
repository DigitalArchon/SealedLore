"""Drawing an area for map-first play (engine/area.py) in a tile set's
textures and sprites (engine/tile_sets.py): the program lays every one from
the law layer, so the picture is the map, tile for tile.

Ported from SealedLoreDocs tools/bakeoff/tiles_v2.py (indoors) and
tiles_out.py (outdoors), the look the author chose (Oct 16 2026): walls in
three-quarter view with a lit top edge and a face, soft occlusion and
shadows, doorways as the floor running through between jambs, a rug and a
few decorations in some rooms, furniture and things each over its own
footprint, light from windows, lamps and fire, a grade per world and a
vignette. Outdoors: terrain layers blended with soft noisy edges,
shorelines, forests as overlapping trees, buildings roofed with their front
wall, door and lit windows.

QImage and numpy only (no QPixmap), so it runs on a worker thread; people
are drawn by the view over it, as they move.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QTransform

from sealedlore.engine.area import Area, Placement
from sealedlore.engine.tile_sets import LIGHT_WORDS, TEXTURES, TileSetInfo, item_slugs

# Pixels a tile, indoors and out: the view scales the picture to the zoom.
# Outdoors 24: at 16 the default fit on a 1080p screen scaled the art up 1.2x
# and a wheel step in was mush (the farm takes 3.5 s and 20 MB on its thread
# instead of 1.6 s and 9 MB, measured Oct 16 2026). Everything drawn is sized
# from `px`, never in pixels of its own.
INDOOR_PX = 40
OUTDOOR_PX = 24
# Scorched ground under fire, and rubble's grey-brown, as tints over the floor.
SCORCH = (28, 16, 10)
RUBBLE_TINT = (96, 84, 72)

# Flat colours for a texture a set lacks.
FALLBACK = {
    "floor": (120, 98, 72),
    "cap": (70, 58, 50),
    "face": (96, 80, 66),
    "out-ground": (96, 128, 72),
    "out-road": (136, 118, 92),
    "out-water": (60, 100, 150),
    "out-pave": (130, 126, 118),
    "out-canopy": (40, 80, 44),
    "out-roof": (120, 64, 52),
    "out-wall": (150, 130, 110),
}


# A picture's plain background, for cutting a sprite out of it.
NEAR_WHITE = 232
NEAR_CORNER = 40
# Sprites are cut out at this size, then kept at most SPRITE_KEPT tall.
CUT_AT = 320
SPRITE_KEPT = 256


def cut_out(image: QImage) -> QImage | None:
    """A sprite out of its plain background: near-white or the corner's
    colour, flooded in from the edges (numpy: the app's per-pixel
    `map_art._cut_out` took seconds on a picture this size), cropped to what
    is left. None when nothing is left."""
    np = _np()
    work = image
    if max(image.width(), image.height()) > CUT_AT:
        work = image.scaled(CUT_AT, CUT_AT, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    work = work.convertToFormat(QImage.Format_RGBA8888)
    h, w = work.height(), work.width()
    a = np.frombuffer(work.constBits(), np.uint8).reshape(h, work.bytesPerLine())[:, : w * 4]
    a = a.reshape(h, w, 4).astype(np.int16)
    corner = a[1, 1, :3]
    background = (a[..., :3] > NEAR_WHITE).all(-1) | (
        np.abs(a[..., :3] - corner).sum(-1) < NEAR_CORNER
    )
    reach = np.zeros((h, w), bool)
    reach[0, :], reach[-1, :], reach[:, 0], reach[:, -1] = (
        background[0, :],
        background[-1, :],
        background[:, 0],
        background[:, -1],
    )
    while True:
        grown = reach.copy()
        grown[1:] |= reach[:-1]
        grown[:-1] |= reach[1:]
        grown[:, 1:] |= reach[:, :-1]
        grown[:, :-1] |= reach[:, 1:]
        grown &= background
        if (grown == reach).all():
            break
        reach = grown
    out = a.astype(np.uint8).copy()
    out[reach, 3] = 0
    ys, xs = np.nonzero(~reach)
    if len(xs) == 0:
        return None
    out = np.ascontiguousarray(out[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1])
    cut = QImage(out.data, out.shape[1], out.shape[0], out.shape[1] * 4, QImage.Format_RGBA8888)
    cut = cut.copy()
    if cut.height() > SPRITE_KEPT:
        cut = cut.scaledToHeight(SPRITE_KEPT, Qt.SmoothTransformation)
    return cut


def tileable(image: QImage) -> QImage:
    """A texture blended with its half-offset copy near the edges, so it
    wraps without a seam (tools/bakeoff/tiles_ai.py)."""
    np = _np()
    img = image.convertToFormat(QImage.Format_RGB888)
    h, w = img.height(), img.width()
    a = np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine())[:, : w * 3]
    a = a.reshape(h, w, 3).astype(np.float32)
    b = np.roll(np.roll(a, h // 2, 0), w // 2, 1)
    yy = np.abs(np.linspace(-1, 1, h))[:, None]
    xx = np.abs(np.linspace(-1, 1, w))[None, :]
    m = np.clip(np.maximum(yy, xx) * 1.6 - 0.6, 0, 1)[..., None]
    out = np.ascontiguousarray((a * (1 - m) + b * m).astype(np.uint8))
    return QImage(out.data, w, h, w * 3, QImage.Format_RGB888).copy()


def is_texture(role: str) -> bool:
    return role in TEXTURES


def sprite_image(data: bytes | None) -> QImage | None:
    """A sprite from its file: as it is when it has its own transparency
    (the shipped sets'), else cut out of its white ground (a picture drawn
    for the story)."""
    if not data:
        return None
    image = QImage.fromData(data)
    if image.isNull():
        return None
    if image.hasAlphaChannel():
        return image
    return cut_out(image)


@dataclass
class TileSource:
    """A tile set's pictures, read on demand and kept: textures already made
    to wrap, sprites already cut out of their white ground (the sets are
    stored so; storage/tile_sets.py and the story's own)."""

    info: TileSetInfo
    # Reads an asset's bytes by role ("floor", "decor0", "items/<slug>"),
    # None when there is none.
    read: Callable[[str], bytes | None]
    _images: dict[str, QImage | None] = field(default_factory=dict)

    def image(self, role: str) -> QImage | None:
        """An asset ready to draw: a texture made to wrap if it wasn't (a
        story's own set is kept as drawn, and so is a texture drawn again or
        chosen for a shipped set: the reader's `from_story` says which), a
        sprite cut out if it has no transparency of its own."""
        if role not in self._images:
            data = self.read(role)
            if is_texture(role):
                image = QImage.fromData(data) if data else QImage()
                if not image.isNull() and (self.info.name == "own" or self.from_story(role)):
                    image = tileable(image)
                found = None if image.isNull() else image
            else:
                found = sprite_image(data)
            self._images[role] = found
        return self._images[role]

    def from_story(self, role: str) -> bool:
        """Whether an asset was drawn or chosen for the story rather than
        shipped pre-made (the session's reader says; a plain reader doesn't)."""
        asks = getattr(self.read, "from_story", None)
        return bool(asks(role)) if callable(asks) else False

    def item(self, name: str) -> QImage | None:
        for found in item_slugs(name):
            image = self.image(f"items/{found}")
            if image is not None:
                return image
        return None


def _np():
    import numpy as np

    return np


def _arr(img: QImage):
    np = _np()
    i = img.convertToFormat(QImage.Format_RGB888)
    a = np.frombuffer(i.constBits(), np.uint8).reshape(i.height(), i.bytesPerLine())
    return a[:, : i.width() * 3].reshape(i.height(), i.width(), 3).astype(np.float32)


def _qimg(a) -> QImage:
    np = _np()
    a = np.ascontiguousarray(np.clip(a, 0, 255).astype(np.uint8))
    return QImage(a.data, a.shape[1], a.shape[0], a.shape[1] * 3, QImage.Format_RGB888).copy()


def _box_blur(a, r: int):
    np = _np()
    if r <= 0:
        return a
    pad = np.pad(a, ((r + 1, r), (r + 1, r)), mode="edge")
    c = pad.cumsum(0)
    a1 = (c[2 * r + 1 :] - c[: -2 * r - 1]) / (2 * r + 1)
    c = a1.cumsum(1)
    return (c[:, 2 * r + 1 :] - c[:, : -2 * r - 1]) / (2 * r + 1)


def _blur(a, r: int, passes: int = 3):
    for _ in range(passes):
        a = _box_blur(a, r)
    return a


def _noise(h: int, w: int, cell: float, seed: int, octaves=((1, 1.0),)):
    np = _np()
    out = np.zeros((h, w), np.float32)
    total = 0.0
    for mult, weight in octaves:
        c = max(cell / mult, 1.0)
        gh, gw = int(h / c) + 2, int(w / c) + 2
        grid = np.random.default_rng(seed + int(mult * 101)).random((gh, gw)).astype(np.float32)
        ys = (np.arange(h, dtype=np.float32) / c)[:, None]
        xs = (np.arange(w, dtype=np.float32) / c)[None, :]
        y0, x0 = ys.astype(int), xs.astype(int)
        fy, fx = ys - y0, xs - x0
        fy, fx = fy * fy * (3 - 2 * fy), fx * fx * (3 - 2 * fx)
        a, b = grid[y0, x0], grid[y0, x0 + 1]
        cc, d = grid[y0 + 1, x0], grid[y0 + 1, x0 + 1]
        out += weight * ((a * (1 - fx) + b * fx) * (1 - fy) + (cc * (1 - fx) + d * fx) * fy)
        total += weight
    return out / total


def _upsample(mask, px: int):
    """A tile grid to pixels, bilinear between tile centres."""
    np = _np()
    h, w = mask.shape
    ys = (np.arange(h * px) + 0.5) / px - 0.5
    xs = (np.arange(w * px) + 0.5) / px - 0.5
    y0 = np.clip(np.floor(ys).astype(int), 0, h - 1)
    x0 = np.clip(np.floor(xs).astype(int), 0, w - 1)
    y1, x1 = np.clip(y0 + 1, 0, h - 1), np.clip(x0 + 1, 0, w - 1)
    fy = np.clip(ys - np.floor(ys), 0, 1)[:, None]
    fx = np.clip(xs - np.floor(xs), 0, 1)[None, :]
    m = mask.astype(np.float32)
    return (
        m[y0][:, x0] * (1 - fx) * (1 - fy)
        + m[y0][:, x1] * fx * (1 - fy)
        + m[y1][:, x0] * (1 - fx) * fy
        + m[y1][:, x1] * fx * fy
    )


def _seed(text: str) -> int:
    return zlib.crc32(text.encode())


def _texture_brush(source: TileSource, role: str, scale_px: float) -> QBrush:
    image = source.image(role)
    if image is None:
        return QBrush(QColor(*FALLBACK.get(role, (110, 110, 110))))
    brush = QBrush(image)
    brush.setTransform(QTransform.fromScale(scale_px / image.width(), scale_px / image.width()))
    return brush


def _texture_layer(source: TileSource, role: str, w: int, h: int, scale_px: float):
    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    p.fillRect(0, 0, w, h, _texture_brush(source, role, scale_px))
    p.end()
    return _arr(img)


def _scaled(image: QImage | None, height: int) -> QImage | None:
    if image is None or height <= 0:
        return None
    return image.scaledToHeight(max(1, height), Qt.SmoothTransformation)


def _light(lights, h: int, w: int, mask_at: Callable[[float, float], object] | None = None):
    """The glow of every light summed: a radial falloff each, kept within
    its room by `mask_at` (a pixel mask for a light's spot, None for no
    limit, False for a light nowhere: nothing). Summed with no occlusion, a
    lamp lit the room next door through the wall."""
    np = _np()
    glow = np.zeros((h, w), np.float32)
    for lx, ly, rad in lights:
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
        glow[y0:y1, x0:x1] += part
    return np.clip(glow, 0, 1.4)


def _room_masks(area: Area, px: int, height: int, width: int):
    """`mask_at` for `_light` indoors: a light's room (the tile under it, or
    the one south or north of it: a wall-mounted light's glow is centred on
    the wall), dilated by one tile so the doorways and the walls catch its
    light, as a soft pixel mask; None with no rooms to keep to."""
    np = _np()
    if not area.rooms or not area.room_of:
        return None
    rooms = np.array(area.room_of)
    cache: dict[int, object] = {}

    def mask_at(lx: float, ly: float):
        tx, ty = int(lx // px), int(ly // px)
        room = area.room_at(tx, ty) or area.room_at(tx, ty + 1) or area.room_at(tx, ty - 1)
        if room is None:
            return False
        if room.index not in cache:
            cells = rooms == room.index
            grown = cells.copy()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    shifted = np.roll(np.roll(cells, dy, 0), dx, 1)
                    if dy == -1:
                        shifted[-1, :] = False
                    elif dy == 1:
                        shifted[0, :] = False
                    if dx == -1:
                        shifted[:, -1] = False
                    elif dx == 1:
                        shifted[:, 0] = False
                    grown |= shifted
            mask = np.zeros((height, width), np.float32)
            tiles = np.repeat(np.repeat(grown.astype(np.float32), px, 0), px, 1)
            mask[: tiles.shape[0], : tiles.shape[1]] = tiles[:height, :width]
            cache[room.index] = _blur(mask, max(1, px // 5), 1)
        return cache[room.index]

    return mask_at


def _vignette(img, strength: float, start: float):
    np = _np()
    h, w = img.shape[:2]
    vy = np.linspace(-1, 1, h)[:, None]
    vx = np.linspace(-1, 1, w)[None, :]
    return img * (1 - strength * np.clip(vx**2 + vy**2 - start, 0, 1))[..., None]


def px_for(area: Area) -> int:
    return INDOOR_PX if area.indoor else OUTDOOR_PX


def render_area(area: Area, source: TileSource, *, dress: bool = True) -> QImage:
    """The area drawn in the tile set, `px_for(area)` pixels a tile."""
    if area.indoor:
        return _render_indoor(area, source, dress=dress)
    return _render_outdoor(area, source)


def _stars(w: int, h: int, seed: int) -> QImage:
    image = QImage(w, h, QImage.Format_RGB32)
    image.fill(QColor(8, 10, 20))
    p = QPainter(image)
    import random

    rng = random.Random(seed)
    for _ in range(w * h // 900):
        shade = rng.randint(120, 255)
        p.setPen(QColor(shade, shade, min(255, shade + 20)))
        p.drawPoint(rng.randrange(w), rng.randrange(h))
    p.end()
    return image


def _render_indoor(area: Area, source: TileSource, *, dress: bool) -> QImage:
    np = _np()
    info = source.info
    rows = area.rows
    h, w = area.height, area.width
    px = INDOOR_PX
    face_h = px * 0.7
    W, H = w * px, h * px
    # The picture is a wall's face taller than the grid, so the south outer
    # wall gets a face over the void too (it ended flat at the edge).
    HX = H + int(round(face_h))
    grid = np.array([list(r) for r in rows])
    wall = np.isin(grid, list("#W"))
    space = grid == " "
    water = grid == "~"
    deck = info.deck or area.look == "space"

    def at(x: int, y: int) -> str:
        return rows[y][x] if 0 <= x < w and 0 <= y < h else " "

    # 1. the ground: stars where a deck's void is, else dark; then the floor,
    # and water where the plan has it (a flooded cellar, a pool)
    if deck:
        base = _stars(W, HX, _seed(area.id))
    else:
        base = QImage(W, HX, QImage.Format_RGB32)
        base.fill(QColor(20, 18, 16))
    p = QPainter(base)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    floor = _texture_brush(source, "floor", px * 4.8)
    pool = _texture_brush(source, "out-water", px * 8)
    for y in range(h):
        for x in range(w):
            if not space[y, x]:
                p.fillRect(x * px, y * px, px, px, pool if water[y, x] else floor)
    p.end()
    img = _arr(base)
    # 2. large soft variation, so the texture doesn't read as a repeat
    var = _noise(HX, W, 220.0, 7)
    inside = np.zeros((HX, W), bool)
    inside[:H] = np.repeat(np.repeat(~space, px, 0), px, 1)
    img[inside] *= (0.92 + 0.12 * var[inside])[..., None]
    # 3. occlusion: the floor darkens near walls; a shadow cast south-east
    wm = np.zeros((HX, W), np.float32)
    wm[:H] = np.repeat(np.repeat(wall.astype(np.float32), px, 0), px, 1)
    ao = _blur(wm, max(2, px // 3), 2)
    sh = np.zeros_like(wm)
    off = max(2, px // 3)
    sh[off:, off // 2 :] = wm[:-off, : W - off // 2]
    sh = _blur(sh, max(2, px // 5), 2)
    dark = np.clip(ao * 0.45 + sh * 0.35, 0, 0.7) * inside * (1 - wm)
    img *= (1 - dark)[..., None]
    out = _qimg(img)
    p = QPainter(out)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.Antialiasing, True)
    lights: list[tuple[float, float, float]] = []
    late: list[tuple[QImage, int, int]] = []
    # 4. what the story did to the place: scorched ground with embers under
    # fire, rubble's grey-brown with the set's rubble scattered on it (the
    # author's rule: a barn blown up is drawn blown up; the play view used
    # to show its floor as new)
    rubble = source.image("out-rubble")
    flames: list[tuple[QImage, int, int]] = []
    hot = np.random.default_rng(_seed(area.id) + 3)
    for y in range(h):
        for x in range(w):
            c = at(x, y)
            if c == "*":
                _burning(p, x, y, px, hot, flames)
                lights.append(((x + 0.5) * px, (y + 0.5) * px, px * 4.5))
            elif c == ":":
                p.fillRect(x * px, y * px, px, px, QColor(*RUBBLE_TINT, 150))
                for _ in range(int(hot.integers(1, 3))):
                    sprite = _scaled(rubble, int(px * hot.uniform(0.55, 0.9)))
                    if sprite is None:
                        break
                    late.append(
                        (
                            sprite,
                            int((x + hot.uniform(0.1, 0.9)) * px - sprite.width() / 2),
                            int((y + hot.uniform(0.5, 1.0)) * px - sprite.height()),
                        )
                    )
            elif c == "~" and info.lava:
                lights.append(((x + 0.5) * px, (y + 0.5) * px, px * 3.0))
    # 5. dressing: a rug in some rooms, decorations along walls, clear of
    # doorways and furniture
    near_door = np.zeros((h, w), bool)
    for d in area.doorways:
        near_door[max(0, d.y0 - 2) : d.y1 + 3, max(0, d.x0 - 2) : d.x1 + 3] = True
    taken = np.isin(grid, list("*:~"))
    for f in area.placements:
        taken[max(0, f.y - 1) : f.y + f.h + 1, max(0, f.x - 1) : f.x + f.w + 1] = True
    decor = [source.image(f"decor{i}") for i in range(3)]
    decor_lit = list(info.decor_lights) + [False] * 3
    for room in area.rooms if dress else []:
        x0, y0, x1, y1 = room.box
        rw, rh = x1 - x0 + 1, y1 - y0 + 1
        rr0 = np.random.default_rng(500 + room.index + _seed(area.id) % 1000)
        if rw >= 6 and rh >= 4 and room.cells >= 30 and rr0.random() < 0.55:
            rug = _scaled(source.image("rug"), int(min(rh * rr0.uniform(0.35, 0.5), rw * 0.3) * px))
            if rug is not None:
                cx, cy = room.cx + rr0.uniform(-0.15, 0.15) * rw, room.cy
                gx0 = int(cx + 0.5 - rug.width() / px / 2)
                gy0 = int(cy + 0.5 - rug.height() / px / 2)
                gx1 = int(cx + 0.5 + rug.width() / px / 2)
                gy1 = int(cy + 0.5 + rug.height() / px / 2)
                foot = grid[max(0, gy0) : gy1 + 1, max(0, gx0) : gx1 + 1]
                clear = not taken[max(0, gy0) : gy1 + 1, max(0, gx0) : gx1 + 1].any()
                if foot.size and np.isin(foot, ["_"]).all() and clear:
                    p.setOpacity(0.92)
                    p.drawImage(
                        int((cx + 0.5) * px - rug.width() / 2),
                        int((cy + 0.5) * px - rug.height() / 2),
                        rug,
                    )
                    p.setOpacity(1.0)
        rr = np.random.default_rng(1000 + room.index + _seed(area.id) % 1000)
        spots = [
            (x, y)
            for y in range(y0, y1 + 1)
            for x in range(x0, x1 + 1)
            if area.room_of[y][x] == room.index
            and at(x, y - 1) in "#W"
            and not near_door[y, x]
            and not taken[y, x]
            and x % 2 == 0
        ]
        rr.shuffle(spots)
        for k, (x, y) in enumerate(spots[: max(1, min(3, rw // 5))]):
            which = k % 3
            sprite = _scaled(decor[which], int(px * 1.3))
            if sprite is None:
                continue
            taken[y, max(0, x - 1) : x + 2] = True
            late.append(
                (
                    sprite,
                    int((x + 0.5) * px - sprite.width() / 2),
                    int((y + 1.0) * px - sprite.height()),
                )
            )
            if decor_lit[which]:
                lights.append(((x + 0.5) * px, (y + 0.4) * px, px * 3.0))
    # 6. walls, back to front: the cap on the tile, the face below wherever
    # no wall stands south (floor or the void: the south outer wall has a
    # face too), darker than the cap, the cap's north edge lit, so a wall
    # reads as a wall and not a flat band
    cap = _texture_brush(source, "cap", px * 2)
    face = _texture_brush(source, "face", face_h * 1.6)
    edge = QColor(14, 12, 12)
    glass_colour = QColor(20, 30, 60) if deck else QColor(170, 205, 235)
    sill_colour = QColor(40, 60, 100) if deck else QColor(222, 232, 242)
    for y in range(h):
        for x in range(w):
            c = at(x, y)
            if c not in "#W":
                continue
            X, Y = x * px, y * px
            top = Y - face_h * 0.35
            p.fillRect(QRectF(X, top, px, px), cap)
            if at(x, y - 1) not in "#W":
                p.fillRect(QRectF(X, top, px, 3), QColor(255, 255, 255, 120))
            faced = at(x, y + 1) not in "#W"
            if faced:
                p.fillRect(QRectF(X, top + px, px, face_h), face)
                p.fillRect(QRectF(X, top + px, px, face_h), QColor(0, 0, 0, 64))
                p.fillRect(QRectF(X, top + px, px, max(1, px * 0.04)), QColor(0, 0, 0, 120))
                p.fillRect(
                    QRectF(X, top + px + face_h - max(1, px * 0.05), px, max(1, px * 0.05)), edge
                )
            for dx, dy, rect in (
                (-1, 0, (X, top, max(1, px * 0.05), px)),
                (1, 0, (X + px - max(1, px * 0.05), top, max(1, px * 0.05), px)),
                (0, -1, (X, top, px, max(1, px * 0.05))),
            ):
                if at(x + dx, y + dy) not in "#W":
                    p.fillRect(QRectF(*rect), edge)
            if c != "W":
                continue
            if faced and at(x, y + 1) != " ":
                # A window in a wall facing us: glass on the face, its light
                # on the floor south of it.
                glass = QRectF(X + px * 0.2, top + px + face_h * 0.15, px * 0.6, face_h * 0.5)
                p.fillRect(glass, glass_colour)
                p.fillRect(
                    QRectF(glass.x(), glass.y(), glass.width(), max(1, glass.height() * 0.15)),
                    QColor(255, 255, 255, 140),
                )
                if not deck:
                    lights.append((X + px / 2, Y + px * 1.4, px * 4.0))
                continue
            # A window in a side wall: a pale sill strip along the room's
            # edge of the cap (a square on the cap read as a hatch), its
            # light into that room.
            sides = [
                (X, -px * 0.6, at(x - 1, y) not in "#W "),
                (X + px - px * 0.16, px * 1.6, at(x + 1, y) not in "#W "),
            ]
            for sx, light_x, open_ in sides:
                if not open_:
                    continue
                strip = QRectF(sx, top + px * 0.18, px * 0.16, px * 0.64)
                p.fillRect(strip, sill_colour)
                p.fillRect(
                    QRectF(sx, strip.y(), px * 0.16, max(1, px * 0.06)), QColor(255, 255, 255, 160)
                )
                if not deck:
                    lights.append((X + light_x, Y + px * 0.5, px * 4.0))
    # 7. doorways: the floor runs through between dark jambs
    jamb = QColor(210, 170, 40) if deck else QColor(30, 22, 18)
    j = max(2, px * 0.14)
    for d in area.doorways:
        if d.way == "h":
            for X in (d.x0 * px - j / 2, (d.x1 + 1) * px - j / 2):
                p.fillRect(QRectF(X, d.y0 * px - face_h * 0.35, j, px + face_h * 0.6), jamb)
        else:
            for Y in (d.y0 * px - j / 2, (d.y1 + 1) * px - j / 2):
                p.fillRect(QRectF(d.x0 * px, Y - face_h * 0.35, px, j), jamb)
    for sprite, X, Y in late:
        p.drawImage(X, Y, sprite)
    # 8. furniture and things, each over its footprint, its foot on its
    # last row, back to front; the flames over everything
    for f in sorted(area.placements, key=lambda f: f.y + f.h):
        _draw_placement(p, source, f, px, lights, indoor=True)
    for sprite, X, Y in flames:
        p.drawImage(X, Y, sprite)
    # ceiling lamps in the larger rooms
    for room in area.rooms:
        if room.cells >= 40:
            lights.append(((room.cx + 0.5) * px, (room.cy + 0.5) * px, px * 6.0))
    p.end()
    # 9. light: a glow from each source, kept to its room, the world's
    # grade, a vignette
    img = _arr(out)
    glow = _light(lights, HX, W, _room_masks(area, px, HX, W))
    lc = np.array(info.light, np.float32) / 255
    img = img * (0.78 + 0.32 * glow[..., None] * lc) + glow[..., None] * lc * 18
    img *= np.array(info.grade, np.float32)
    if inside.any():
        lum = float(img[inside].mean())
        if lum < 95:
            img = img * (95 / max(lum, 1.0)) ** 0.7
        elif lum > 135:  # a pale world loses its depth: pulled down, with contrast
            mean = img[inside].mean(0)
            img = (img - mean) * 1.25 + mean * (135 / lum)
    return _qimg(_vignette(img, 0.18, 0.3))


def _burning(
    p: QPainter, x: int, y: int, px: int, rng, flames: list, *, scorch: bool = True
) -> None:
    """A tile on fire: the ground scorched (unless the terrain already is)
    with embers in it, and two or three tongues of flame
    (gui/map_tiles.flame_sprite) standing on it, drawn over whatever stands
    there once everything else is in."""
    from sealedlore.gui.map_tiles import FLAME, flame_sprite

    if scorch:
        p.fillRect(x * px, y * px, px, px, QColor(*SCORCH, 190))
    for _ in range(4):
        p.fillRect(
            QRectF(
                (x + rng.uniform(0.05, 0.85)) * px,
                (y + rng.uniform(0.05, 0.85)) * px,
                max(2.0, px * 0.08),
                max(2.0, px * 0.08),
            ),
            QColor(FLAME[2] if rng.random() < 0.4 else FLAME[1]),
        )
    for _ in range(int(rng.integers(2, 4))):
        sprite = flame_sprite(int(rng.integers(3))).scaledToHeight(
            int(px * rng.uniform(0.9, 1.3)), Qt.SmoothTransformation
        )
        flames.append(
            (
                sprite,
                int((x + rng.uniform(0.15, 0.85)) * px - sprite.width() / 2),
                int((y + rng.uniform(0.6, 1.0)) * px - sprite.height()),
            )
        )


def _draw_placement(
    p: QPainter,
    source: TileSource,
    f: Placement,
    px: int,
    lights: list[tuple[float, float, float]],
    *,
    indoor: bool,
) -> None:
    sprite = source.item(f.name)
    cx, foot = (f.x + f.w / 2) * px, (f.y + f.h) * px
    if sprite is None:
        # Not drawn yet: a plain crate of the thing's footprint, so it is
        # seen to be there (its sprite comes in the background).
        p.setPen(QColor(30, 24, 20))
        p.setBrush(QColor(150, 120, 80))
        p.drawRoundedRect(QRectF(f.x * px + 3, f.y * px + 3, f.w * px - 6, f.h * px - 6), 4, 4)
        return
    if f.kind == "furniture" or indoor:
        height = int(px * (f.h + 1.0)) if f.kind == "furniture" else int(px * 2.2)
        sprite = _scaled(sprite, height)
        if sprite is not None and sprite.width() > f.w * px * 1.15 and f.kind == "furniture":
            sprite = sprite.scaledToWidth(int(f.w * px * 1.15), Qt.SmoothTransformation)
    else:
        sprite = _scaled(sprite, int(px * 3.2))
    if sprite is None:
        return
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 60))
    p.drawEllipse(QPointF(cx, foot - px * 0.1), sprite.width() * 0.42, px * 0.25)
    p.drawImage(int(cx - sprite.width() / 2), int(foot - sprite.height()), sprite)
    if any(word in f.name.lower() for word in LIGHT_WORDS):
        lights.append((cx, foot - sprite.height() * 0.5, px * (4.0 if indoor else 6.0)))


def _soft(mask, seed: int, px: int, rough: float = 0.35):
    """A tile mask to pixels with a soft, noisy edge."""
    np = _np()
    m = _upsample(mask.astype(np.float32), px)
    m = _blur(m, max(1, px // 4), 2)
    n = _noise(m.shape[0], m.shape[1], px * 2.5, seed, ((1, 1.0), (2, 0.5)))
    return np.clip((m - 0.5 + (n - 0.5) * rough) * 6 + 0.5, 0, 1)


def _building_boxes(area: Area) -> list[tuple[int, int, int, int]]:
    from sealedlore.gui.map_tiles import building_boxes

    boxes = []
    for x0, x1, y0, y1 in building_boxes(area.rows):
        if x1 - x0 + 1 > 40 or y1 - y0 + 1 > 30:
            continue  # a town's wall, not a building
        boxes.append((x0, y0, x1, y1))
    return boxes


def _render_outdoor(area: Area, source: TileSource) -> QImage:
    np = _np()
    info = source.info
    rows = area.rows
    h, w = area.height, area.width
    px = OUTDOOR_PX
    W, H = w * px, h * px
    g = np.array([list(r) for r in rows])
    boxes = _building_boxes(area)
    roofed = np.zeros((h, w), bool)
    for x0, y0, x1, y1 in boxes:
        roofed[y0 : y1 + 1, x0 : x1 + 1] = True

    def at(x: int, y: int) -> str:
        return rows[y][x] if 0 <= x < w and 0 <= y < h else ""

    # 1. terrain: ground, then pavement, rock, forest floor, water, road.
    # Texture scales are in tiles (a texture every 16 tiles of ground), so
    # the picture is the same whatever `px` is.
    img = _texture_layer(source, "out-ground", W, H, px * 16)
    trees_mask = g == "T"
    layers = [
        ("out-pave", np.isin(g, list("_+")) & ~roofed, 0.2, px * 12, False),
        ("out-canopy", np.isin(g, ["^"]), 0.45, px * 16, False),
        ("out-canopy", trees_mask if not info.lava else np.zeros_like(roofed), 0.4, px * 16, True),
        ("out-water", g == "~", 0.3, px * 16, False),
        ("out-road", g == "=", 0.15, px * 18, False),
    ]
    water_px = None
    for i, (role, mask, rough, scale, shade) in enumerate(layers):
        if not mask.any():
            continue
        m = _soft(mask, 31 + i, px, rough)
        tex = _texture_layer(source, role, W, H, scale)
        if shade:
            tex *= 0.55  # the floor under the trees, in their shade
        img = img * (1 - m[..., None]) + tex * m[..., None]
        if role == "out-water":
            water_px = m
    if water_px is not None:  # shorelines: a dark rim on the land, foam on water
        edge = _blur(water_px, 2, 2)
        rim = np.clip((edge - water_px) * 3, 0, 1)
        img *= (1 - 0.35 * rim)[..., None]
        if not info.lava:
            foam = np.clip((water_px - _blur(water_px, 2, 2)) * -4, 0, 1) * water_px
            img += foam[..., None] * 60
    burning = g == "*"
    if burning.any():  # the ground scorched under fire, soft-edged like the rest
        scorch = _soft(burning, 37, px, 0.3)[..., None]
        img = img * (1 - 0.8 * scorch) + np.array(SCORCH, np.float32) * 0.8 * scorch
    img *= (0.9 + 0.16 * _noise(H, W, px * 19, 7))[..., None]
    # 2. shadows of buildings, walls and trees, cast south-east
    solid = (np.isin(g, list("#W+")) | roofed).astype(np.float32)
    occl = _upsample(np.maximum(solid, trees_mask.astype(np.float32) * 0.7), px)
    sh = np.zeros_like(occl)
    o = px // 2
    sh[o:, o // 2 :] = occl[:-o, : W - o // 2]
    sh = _blur(sh, px // 3, 2)
    img *= (1 - 0.4 * np.clip(sh - occl * 0.5, 0, 1))[..., None]
    out = _qimg(img)
    p = QPainter(out)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.Antialiasing, True)
    lights: list[tuple[float, float, float]] = []
    rng = np.random.default_rng(_seed(area.id))
    # 3. everything that stands, back to front by its foot
    stand: list[tuple[float, str, object]] = []
    placed = {(f.x, f.y): f for f in area.placements}
    for y in range(h):
        for x in range(w):
            c = rows[y][x]
            r = rng.random()
            if c == "T" and r < 0.42:
                stand.append(((y + 1) * px, "tree", (x, y)))
            elif c == "," and r < 0.55:
                stand.append(((y + 1) * px, "bush", (x, y)))
            elif c == "^" and r < 0.18:
                stand.append(((y + 1) * px, "rock", (x, y)))
            elif c == ":" and r < 0.5:
                stand.append(((y + 1) * px, "rubble", (x, y)))
            elif c == "%":
                stand.append(((y + 1) * px, "fence", (x, y)))
            elif (x, y) in placed:
                stand.append(((y + 1) * px + 1, "obj", placed[(x, y)]))
            elif c in "#W+" and not roofed[y, x]:
                stand.append(((y + 1) * px, "wall", (x, y)))
            elif c == "*":
                stand.append(((y + 1) * px, "fire", (x, y)))
                # Closer in than a lamp's: a burning building's twenty tiles
                # summed bleached the snow five tiles round it to white.
                lights.append(((x + 0.5) * px, (y + 0.5) * px, px * 3.5))
    for b in boxes:
        stand.append(((b[3] + 1) * px, "building", b))
    stand.sort(key=lambda s: s[0])
    flames: list[tuple[QImage, int, int]] = []
    trees = [t for t in (source.image("out-tree0"), source.image("out-tree1")) if t is not None]
    nature = {
        "bush": source.image("out-bush"),
        "rock": source.image("out-rock"),
        "rubble": source.image("out-rubble"),
    }
    sizes = {"tree": (3.0, 4.2), "bush": (1.2, 1.7), "rock": (1.6, 2.6), "rubble": (1.0, 1.5)}
    cache: dict[tuple[int, int], QImage | None] = {}

    def sized(image: QImage | None, height: int) -> QImage | None:
        if image is None:
            return None
        k = (id(image), height // 2)
        if k not in cache:
            cache[k] = _scaled(image, height)
        return cache[k]

    cap = _texture_brush(source, "cap", px * 2)
    wallb = _texture_brush(source, "out-wall", px * 6)
    roofb = _texture_brush(source, "out-roof", px * 12)
    fence = source.image("out-fence")
    door = source.image("out-door")
    edge = QColor(14, 12, 12)
    for foot, kind, data in stand:
        if kind in ("tree", "bush", "rock", "rubble"):
            x, y = data  # type: ignore[misc]
            if kind == "tree":
                image = trees[int(rng.integers(len(trees)))] if trees else None
            else:
                image = nature[kind]
            low, high = sizes[kind]
            sprite = sized(image, int(px * rng.uniform(low, high)))
            if sprite is None:
                if kind == "tree":  # no tree sprite: a dark crown
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(30, 70, 36))
                    p.drawEllipse(QPointF((x + 0.5) * px, foot - px), px * 0.9, px * 0.9)
                continue
            jx, jy = rng.uniform(-0.3, 0.3) * px, rng.uniform(-0.2, 0.2) * px
            p.drawImage(
                int((x + 0.5) * px - sprite.width() / 2 + jx),
                int(foot - sprite.height() + jy),
                sprite,
            )
        elif kind == "fence":
            x, y = data  # type: ignore[misc]
            horiz = at(x - 1, y) == "%" or at(x + 1, y) == "%"
            sprite = sized(fence, int(px * 1.1))
            if sprite is None:
                p.fillRect(QRectF(x * px, y * px + px * 0.4, px, px * 0.2), QColor(110, 80, 50))
                continue
            if horiz:
                part = sprite.scaled(
                    px + 1, sprite.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation
                )
                p.drawImage(x * px, int(foot - part.height()), part)
            else:
                post = sprite.copy(0, 0, max(2, sprite.width() // 6), sprite.height())
                p.drawImage(int((x + 0.5) * px - post.width() / 2), int(foot - post.height()), post)
        elif kind == "obj":
            _draw_placement(p, source, data, px, lights, indoor=False)  # type: ignore[arg-type]
        elif kind == "fire":
            # The ground is scorched already (the terrain); embers and the
            # flames stand on it, drawn over everything once the rest is in.
            x, y = data  # type: ignore[misc]
            _burning(p, x, y, px, rng, flames, scorch=False)
        elif kind == "wall":
            x, y = data  # type: ignore[misc]
            X, Y = x * px, y * px
            face_h = px * 0.8
            p.fillRect(QRectF(X, Y - face_h * 0.4, px, px), cap)
            if at(x, y + 1) not in ("#", "W", "+"):
                p.fillRect(QRectF(X, Y + px - face_h * 0.4, px, face_h), wallb)
                p.fillRect(QRectF(X, Y + px - face_h * 0.4 + face_h - 1, px, 1.5), edge)
        elif kind == "building":
            x0, y0, x1, y1 = data  # type: ignore[misc]
            X0, Y0, X1, Y1 = x0 * px, y0 * px, (x1 + 1) * px, (y1 + 1) * px
            face_h = min(px * 1.6, (Y1 - Y0) * 0.35)
            roof = QRectF(X0 - 2, Y0 - px * 0.4, X1 - X0 + 4, Y1 - Y0 - face_h + px * 0.4)
            p.fillRect(roof, roofb)
            ridge = roof.y() + roof.height() * 0.45
            p.fillRect(
                QRectF(roof.x(), ridge, roof.width(), roof.bottom() - ridge), QColor(0, 0, 0, 45)
            )
            p.fillRect(QRectF(roof.x(), ridge - 1.5, roof.width(), 3), QColor(255, 255, 255, 50))
            p.fillRect(QRectF(roof.x(), roof.bottom() - 2, roof.width(), 2), QColor(0, 0, 0, 120))
            for rect in (
                (roof.x(), roof.y(), 1.5, roof.height()),
                (roof.right() - 1.5, roof.y(), 1.5, roof.height()),
                (roof.x(), roof.y(), roof.width(), 1.5),
            ):
                p.fillRect(QRectF(*rect), edge)
            front = QRectF(X0, Y1 - face_h, X1 - X0, face_h)
            p.fillRect(front, wallb)
            p.fillRect(QRectF(X0, Y1 - 1.5, X1 - X0, 1.5), edge)
            for x in range(x0, x1 + 1):
                c = at(x, y1)
                if c == "+":
                    sprite = sized(door, int(face_h * 0.95))
                    if sprite is not None:
                        p.drawImage(
                            int((x + 0.5) * px - sprite.width() / 2),
                            int(Y1 - sprite.height()),
                            sprite,
                        )
                    else:
                        p.fillRect(
                            QRectF(x * px + 2, Y1 - face_h * 0.8, px - 4, face_h * 0.8),
                            QColor(70, 46, 30),
                        )
                elif c == "W":
                    glass = QRectF(x * px + px * 0.15, Y1 - face_h * 0.75, px * 0.7, face_h * 0.4)
                    p.fillRect(glass, QColor(255, 210, 130))
                    p.fillRect(QRectF(glass.x(), glass.y(), glass.width(), 1.5), edge)
                    lights.append(((x + 0.5) * px, Y1 + px * 0.6, px * 3.5))
            # Doors in the other walls: a step out of the roof's shadow.
            for y in range(y0, y1):
                for x in (x0, x1):
                    if at(x, y) == "+":
                        dx = -px * 0.3 if x == x0 else px * 0.6
                        p.fillRect(
                            QRectF(x * px + dx, y * px, px * 0.7, px), QColor(60, 50, 40, 160)
                        )
            for x in range(x0, x1 + 1):
                if at(x, y0) == "+":
                    p.fillRect(QRectF(x * px, Y0 - px * 0.6, px, px * 0.3), QColor(60, 50, 40, 160))
    for sprite, X, Y in flames:
        p.drawImage(X, Y, sprite)
    p.end()
    # 4. light: lava, fire, lamps and windows; a grade. No vignette outdoors:
    # it made an area's edge look like the end of the world, and Leave ▾
    # carries on past it (indoors it stays).
    img = _arr(out)
    glow = _light(lights, H, W)
    if info.lava and (g == "~").any():
        glow = glow + _blur(_soft(g == "~", 5, px), px, 3) * 1.1
    lc = np.array(info.light, np.float32) / 255
    img = img * (0.92 + 0.25 * glow[..., None] * lc) + glow[..., None] * lc * 22
    img *= np.array(info.grade, np.float32)
    lum = float(img.mean())
    if lum < 85:  # a dark world lifted, as indoors
        img = img * (85 / max(lum, 1.0)) ** 0.75
    return _qimg(img)
