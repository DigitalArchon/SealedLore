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

import hashlib
import re
import zlib
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPolygonF, QTransform

from sealedlore.engine.area import BRIDGE, Area, Placement
from sealedlore.engine.deck_plan import DRAWN_PIECES
from sealedlore.engine.takeable import TREES
from sealedlore.engine.tile_sets import (
    DECK_FLOOR_ROLE,
    DECK_TEXTURES,
    GROUND_TINTS,
    ROOF_TINTS,
    TEXTURES,
    TileSetInfo,
    closest_item,
    deck_floor_wanted,
    gives_light,
    ground_role,
    ground_wanted,
    head_noun,
    item_slugs,
    roof_role,
    suitable_item,
)
from sealedlore.gui import deck_look
from sealedlore.gui.image_codec import encoded, image_from_data

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
# The void beyond a pier's edge or a cliff (" " out of doors, off a deck),
# and the solid rock of a cave or a mine (`Area.underground`).
VOID = (14, 15, 20)
SOLID_ROCK = (62, 56, 52)

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
    DECK_FLOOR_ROLE: (104, 110, 118),
}


# A picture's plain background, for cutting a sprite out of it.
NEAR_WHITE = 232
NEAR_CORNER = 40
# Sprites are cut out at this size, then kept at most SPRITE_KEPT tall.
CUT_AT = 320
SPRITE_KEPT = 256


def _flooded(reach, background):
    """`reach` grown through `background` from where it starts: whole runs
    of background along each row and column at once (a run with one reached
    pixel is all reached), a pass per turn of the boundary rather than one
    per pixel of the way in (the one-pixel growth took 26 ms a sprite,
    eighty sprites an open, Oct 19 2026)."""
    np = _np()
    reach = reach & background
    while True:
        before = reach
        for axis in (0, 1):
            bg = np.moveaxis(background, axis, 0)
            got = np.moveaxis(reach, axis, 0)
            n, m = bg.shape
            # Runs of background along the axis: a new id at every break.
            run = np.cumsum(~bg, axis=0)
            key = run + np.arange(m)[None, :] * (n + 1)
            hit = np.bincount(key[got].ravel(), minlength=(n + 1) * m) > 0
            grown = bg & hit[key]
            reach = np.moveaxis(grown | got, 0, axis) if axis else grown | got
        if (reach == before).all():
            return reach


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
    reach = _flooded(reach, background)
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


# A set's abandoned versions of its pictures (`aged/<slug>`, `aged/rug`…),
# drawn as edits of them (SealedLoreDocs tools/visual/draw_aged.py), and the
# debris laid on an abandoned room's floor (`aged/debris0`…).
AGED = "aged"
DEBRIS_ROLES = tuple(f"{AGED}/debris{i}" for i in range(3))


def is_texture(role: str) -> bool:
    return (
        role in TEXTURES
        or role in DECK_TEXTURES
        or role.startswith(("out-roof-", "out-ground-"))
        or role == DECK_FLOOR_ROLE
    )


def sprite_image(data: bytes | None) -> QImage | None:
    """A sprite from its file: as it is when it has its own transparency
    (the shipped sets'), else cut out of its white ground (a picture drawn
    for the story)."""
    if not data:
        return None
    image = image_from_data(data)
    if image.isNull():
        return None
    if image.hasAlphaChannel():
        return image
    return cut_out(image)


# A thing that is a stone, a tree or a bush, with no picture of its own nor
# a thing of the set like it: the set's own rock, tree or bush stands in,
# not a plain box (Oct 18 2026: a boundary stone and its scrub oaks were
# crates while the day's map pictures were spent).
NATURE_STAND_INS: dict[str, str] = {
    **{w: "out-rock" for w in "stone rock boulder cairn menhir monolith outcrop".split()},
    **{w: "out-tree0" for w in ("tree", *TREES)},
    **{w: "out-bush" for w in "bush shrub scrub hedge bramble thicket".split()},
}


@dataclass
class TileSource:
    """A tile set's pictures, read on demand and kept: textures already made
    to wrap, sprites already cut out of their white ground (the sets are
    stored so; storage/tile_sets.py and the story's own)."""

    info: TileSetInfo
    # Reads an asset's bytes by role ("floor", "decor0", "items/<slug>"),
    # None when there is none.
    read: Callable[[str], bytes | None]
    # Where a story picture's cut-out is kept between launches (the story's
    # cache folder); None keeps nothing. Cutting a picture out of its white
    # ground cost 15-25 ms, eighty things an open (Oct 19 2026).
    cut_dir: Path | None = None
    _images: dict[str, QImage | None] = field(default_factory=dict)
    _stand_ins: dict[tuple[str, bool], str | None] = field(default_factory=dict)

    def image(self, role: str) -> QImage | None:
        """An asset ready to draw: a texture made to wrap if it wasn't (a
        story's own set is kept as drawn, and so is a texture drawn again or
        chosen for a shipped set: the reader's `from_story` says which), a
        sprite cut out if it has no transparency of its own."""
        if role not in self._images:
            data = self.read(role)
            if is_texture(role):
                image = image_from_data(data)
                if not image.isNull() and (self.info.name == "own" or self.from_story(role)):
                    image = tileable(image)
                found = None if image.isNull() else image
            else:
                found = self._sprite(role, data)
            self._images[role] = found
        return self._images[role]

    def _sprite(self, role: str, data: bytes | None) -> QImage | None:
        """`sprite_image`, with a story picture's cut-out kept in `cut_dir`
        under the picture's own hash (a replaced picture is cut afresh)."""
        if not data or self.cut_dir is None or not self.from_story(role):
            return sprite_image(data)
        name = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")[:48]
        path = self.cut_dir / f"{name}-{hashlib.blake2b(data, digest_size=6).hexdigest()}.png"
        if path.is_file():
            kept = image_from_data(path.read_bytes())
            if not kept.isNull():
                return kept
        found = sprite_image(data)
        if found is not None and found.hasAlphaChannel():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(encoded(found, "PNG"))
                tmp.replace(path)
            except OSError:
                pass  # a cache: the sprite is the same without it
        return found

    def from_story(self, role: str) -> bool:
        """Whether an asset was drawn or chosen for the story rather than
        shipped pre-made (the session's reader says; a plain reader doesn't)."""
        asks = getattr(self.read, "from_story", None)
        return bool(asks(role)) if callable(asks) else False

    def item(self, name: str, *, carried: bool = False) -> QImage | None:
        """The thing's own picture, else the set's thing that suits it as it
        is (`suitable_item`: no picture of its own is drawn for it), else the
        set's nearest thing of the same kind (`closest_item`) until its own
        picture comes in (the reader lists what the set has), else None:
        drawn as a plain box. Something a person carries (`carried`) borrows
        only a picture of its very kind, never one that merely shares a
        word with it."""
        role = self._item_role(name, carried)
        return self.image(role) if role else None

    def aged_item(self, name: str, *, carried: bool = False) -> QImage | None:
        """The abandoned version of the picture `item` shows (the set's
        `aged/<slug>`: drawers out, shelves stripped, dust), None when the
        set has none drawn."""
        role = self._item_role(name, carried)
        if role is None or not role.startswith("items/"):
            return None
        return self.image(f"{AGED}/{role.removeprefix('items/')}")

    def worn(self, role: str, abandoned: bool) -> QImage | None:
        """A dressing role (the rug, a decoration) as an abandoned place
        has it when the set has that drawn, else as it is."""
        if abandoned:
            found = self.image(f"{AGED}/{role}")
            if found is not None:
                return found
        return self.image(role)

    def _item_role(self, name: str, carried: bool) -> str | None:
        """The role of the picture `item` shows for this name."""
        for found in item_slugs(name):
            if self.image(f"items/{found}") is not None:
                return f"items/{found}"
        listing = getattr(self.read, "items", None)
        if callable(listing):
            if (name, carried) not in self._stand_ins:
                names = listing()
                self._stand_ins[(name, carried)] = suitable_item(name, names) or closest_item(
                    name, names, same_kind=carried
                )
            found = self._stand_ins[(name, carried)]
            nature = NATURE_STAND_INS.get(head_noun(name))
            # A stone is the set's rock unless the set has a stone of its own.
            if found is not None and not (
                nature and head_noun(found.replace("-", " ")) != head_noun(name)
            ):
                return f"items/{found}"
        return NATURE_STAND_INS.get(head_noun(name))

    def roof(self, material: str, scale_px: float) -> QBrush:
        """A roof of `material`: the set's (or the story's) own texture of it,
        else the set's roof tinted towards it; the set's roof for ""."""
        if material and self.image(roof_role(material)) is not None:
            return _texture_brush(self, roof_role(material), scale_px)
        brush = _texture_brush(self, "out-roof", scale_px)
        if not material or material not in ROOF_TINTS or brush.textureImage().isNull():
            return brush
        key = f"tint:{material}"
        if key not in self._images:
            self._images[key] = _tinted(brush.textureImage(), ROOF_TINTS[material])
        tinted = QBrush(self._images[key])
        tinted.setTransform(brush.transform())
        return tinted

    def ground(self, look: str, scale_px: float) -> QBrush:
        """The outdoor ground in `look`: the set's own where it stands for
        that look, else the ground drawn for the story in it, else the set's
        own tinted towards it until that is drawn (a story keeps its set: new
        ground is drawn in its style, engine/tile_draw.py `ground_jobs`)."""
        if not ground_wanted(self.info, look):
            return _texture_brush(self, "out-ground", scale_px)
        if self.image(ground_role(look)) is not None:
            return _texture_brush(self, ground_role(look), scale_px)
        brush = _texture_brush(self, "out-ground", scale_px)
        if brush.textureImage().isNull():
            return QBrush(QColor(*GROUND_TINTS[look]))
        key = f"tint:ground:{look}"
        if key not in self._images:
            self._images[key] = _recoloured(brush.textureImage(), GROUND_TINTS[look])
        tinted = QBrush(self._images[key])
        tinted.setTransform(brush.transform())
        return tinted

    def floor(self, look: str, scale_px: float) -> QBrush:
        """An indoor floor: a deck's own in a set with no deck (drawn for the
        story; a flat grey until then), else the set's floor."""
        if deck_floor_wanted(self.info, look):
            if self.image(DECK_FLOOR_ROLE) is not None:
                return _texture_brush(self, DECK_FLOOR_ROLE, scale_px)
            return QBrush(QColor(*FALLBACK[DECK_FLOOR_ROLE]))
        return _texture_brush(self, "floor", scale_px)


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


def _tinted(image: QImage, colour: tuple[int, int, int]) -> QImage:
    """`image`'s light and shade in `colour`: its grey, multiplied."""
    out = image.convertToFormat(QImage.Format_Grayscale8).convertToFormat(QImage.Format_RGB32)
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode_Multiply)
    # The grey runs dark; lifted so the colour reads as the material's.
    lift = tuple(min(255, int(c * 1.6)) for c in colour)
    p.fillRect(out.rect(), QColor(*lift))
    p.end()
    return out


def _recoloured(image: QImage, colour: tuple[int, int, int]) -> QImage:
    """`image`'s light and shade around `colour`: its grey scaled so its
    average is the colour, so a snow texture standing in for sand keeps its
    drifts and reads as sand (`_tinted` lifts a dark roof; on near-white
    snow it left the ground white)."""
    np = _np()
    grey = _arr(image.convertToFormat(QImage.Format_RGB32)).mean(axis=2)
    mean = float(grey.mean()) or 1.0
    out = np.clip(grey[..., None] / mean * np.array(colour, np.float32), 0, 255)
    return _qimg(out)


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


def _vignette_factor(h: int, w: int, strength: float, start: float):
    """How much a picture `h` by `w` is darkened towards its corners."""
    np = _np()
    vy = np.linspace(-1, 1, h)[:, None]
    vx = np.linspace(-1, 1, w)[None, :]
    return (1 - strength * np.clip(vx**2 + vy**2 - start, 0, 1)).astype(np.float32)


def _coarse(a, cell: int):
    """Block means of an (h, w) or (h, w, 3) array, `cell` pixels a block;
    the edge blocks padded with their edge."""
    np = _np()
    h, w = a.shape[:2]
    ph, pw = -(-h // cell) * cell, -(-w // cell) * cell
    pad = ((0, ph - h), (0, pw - w)) + (((0, 0),) if a.ndim == 3 else ())
    a = np.pad(a, pad, mode="edge")
    shape = (ph // cell, cell, pw // cell, cell) + a.shape[2:]
    return a.reshape(shape).mean(axis=(1, 3))


def _sampled(grid, ys, xs):
    """`grid` (rows, cols, 3) at fractional rows `ys` and columns `xs`,
    bilinear, clamped at its edges: (len(ys), len(xs), 3)."""
    np = _np()
    gh, gw = grid.shape[:2]
    y0 = np.clip(np.floor(ys).astype(int), 0, gh - 1)
    x0 = np.clip(np.floor(xs).astype(int), 0, gw - 1)
    y1, x1 = np.clip(y0 + 1, 0, gh - 1), np.clip(x0 + 1, 0, gw - 1)
    fy = np.clip(ys - np.floor(ys), 0, 1)[:, None, None]
    fx = np.clip(xs - np.floor(xs), 0, 1)[None, :, None]
    g = grid.astype(np.float32)
    upper = g[y0][:, x0] * (1 - fx) + g[y0][:, x1] * fx
    lower = g[y1][:, x0] * (1 - fx) + g[y1][:, x1] * fx
    return upper * (1 - fy) + lower * fy


# The light map's step: a quarter of a tile (the light changes over tiles;
# kept at pixels it would be as large as the picture three times over).
LIGHT_CELLS_PER_TILE = 4


@dataclass
class LightMap:
    """How an area's ground was lit, kept so that what stands on it (its
    things; sprites over the ground, gui/area_view.py) is lit the same: a
    pixel's colour times `mul`, plus `add`, each RGB, at `cell` pixels a
    step and bilinear between. The lamps, a deck's colours, the world's
    grade, the exposure and the vignette are all in it. A thing drawn so
    over the ground is the picture it made painted in before the light, to
    rounding (the author, Oct 18 2026: things on their own layer)."""

    mul: Any  # numpy (rows, cols, 3)
    add: Any
    cell: int

    def apply(self, image: QImage, left: int, top: int) -> QImage:
        """`image` as lit standing with its top-left at (`left`, `top`) in
        the area's pixels; its transparency as it was."""
        np = _np()
        src = image.convertToFormat(QImage.Format_ARGB32)
        h, w = src.height(), src.width()
        if not h or not w:
            return src
        raw = np.frombuffer(src.constBits(), np.uint8).reshape(h, src.bytesPerLine())
        a = raw[:, : w * 4].reshape(h, w, 4).astype(np.float32)
        ys = (np.arange(h) + top + 0.5) / self.cell - 0.5
        xs = (np.arange(w) + left + 0.5) / self.cell - 0.5
        rgb = a[..., [2, 1, 0]]  # stored B, G, R, A
        lit = np.clip(rgb * _sampled(self.mul, ys, xs) + _sampled(self.add, ys, xs), 0, 255)
        a[..., [2, 1, 0]] = lit
        out = np.ascontiguousarray(a.astype(np.uint8))
        return QImage(out.data, w, h, w * 4, QImage.Format_ARGB32).copy()


@dataclass
class Ground:
    """An area's picture with nothing standing on it (its things are
    sprites over it: `thing_picture`), and how it was lit."""

    image: QImage
    light: LightMap | None = None


@dataclass
class ThingPicture:
    """A thing as it stands on the play view, on its own: its sprite (a
    plain crate until it is drawn), broken when it is, its shadow at its
    foot. (`dx`, `dy`): its top-left from the middle of its foot, the
    sprite's origin, in the area's pixels."""

    image: QImage
    dx: int
    dy: int


def foot_of(f: Placement, px: int) -> tuple[float, float]:
    """The middle of a thing's foot, in the area's pixels: where it stands."""
    return (f.x + f.w / 2) * px, (f.y + f.h) * px


def _aged(image: QImage) -> QImage:
    """A thing as years of neglect leave it: colour drained a little,
    darker, the alpha kept (an abandoned place's things, Oct 28 2026)."""
    np = _np()
    img = image.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    a = (
        np.frombuffer(img.constBits(), np.uint8)
        .reshape(h, img.bytesPerLine())[:, : w * 4]
        .reshape(h, w, 4)
        .astype(np.float32)
    )
    bgr = a[..., :3]
    grey = bgr[..., 0] * 0.11 + bgr[..., 1] * 0.59 + bgr[..., 2] * 0.30
    a[..., :3] = np.clip((bgr * 0.7 + grey[..., None] * 0.3) * 0.86, 0, 255)
    out = np.ascontiguousarray(a.astype(np.uint8))
    return (
        QImage(out.data, w, h, w * 4, QImage.Format_ARGB32)
        .copy()
        .convertToFormat(QImage.Format_ARGB32_Premultiplied)
    )


def thing_picture(
    source: TileSource, f: Placement, px: int, *, indoor: bool, aged: bool = False
) -> ThingPicture:
    """`f` drawn on its own, exactly as the area's picture drew it; `aged`
    as an abandoned place leaves it (its drawn abandoned version, else its
    picture faded)."""
    sprite = _placement_sprite(source, f, px, indoor=indoor, aged=aged)
    sw, sh = (sprite.width(), sprite.height()) if sprite is not None else (0, 0)
    width = int(max(sw, f.w * px) + 2 * px)
    height = int(max(sh, f.h * px) + px)
    ox, oy = width // 2, height - px // 2
    canvas = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.Antialiasing, True)
    cx, foot = foot_of(f, px)
    p.translate(ox - cx, oy - foot)
    _draw_placement(p, source, f, px, sprite=sprite)
    p.end()
    return ThingPicture(canvas, -ox, -oy)


# The exposure is read from every LUM_STEP-th pixel each way: a mean.
LUM_STEP = 3


def _things_counted(
    image: QImage, things: Sequence[Placement], source: TileSource, px: int, *, indoor: bool
):
    """`image` (the ground before its light) with `things` drawn on it, as
    an array every LUM_STEP-th pixel: what the exposure is read from, so
    that a room full of things is lit as it was when they were painted in
    (read from the ground alone, a dark world came out a seventh brighter)."""
    copy = image.copy()
    p = QPainter(copy)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.Antialiasing, True)
    for f in sorted(things, key=lambda f: f.y + f.h):
        if f.piece not in DRAWN_PIECES:
            sprite = _placement_sprite(source, f, px, indoor=indoor)
            _draw_placement(p, source, f, px, sprite=sprite)
    p.end()
    return _arr(copy)[::LUM_STEP, ::LUM_STEP]


def thing_light(f: Placement, px: int, *, indoor: bool) -> tuple[float, float, float] | None:
    """The light a thing gives where it stands (a lamp, a stove, a brazier),
    by its footprint, not its picture: so moving anything else leaves the
    ground's light as it was. A broken one gives none."""
    if f.broken or not gives_light(f.name):
        return None
    if f.kind == "furniture":
        height = px * (f.h + 1.0)
    else:
        height = px * (2.2 if indoor else 3.2)
    cx, foot = foot_of(f, px)
    return cx, foot - height * 0.5, px * (4.0 if indoor else 6.0)


def compose_things(ground: Ground, area: Area, source: TileSource) -> QImage:
    """The ground with its things drawn on it, back to front, each lit as
    the ground was: the area's whole picture (a painting of the place is
    made from it, `map_render.area_scene_image`)."""
    px = px_for(area)
    image = ground.image.convertToFormat(QImage.Format_RGB32)
    p = QPainter(image)
    for f in sorted(area.placements, key=lambda f: f.y + f.h):
        if f.piece in DRAWN_PIECES:
            continue  # the ground's own (a reactor, a lift shaft)
        picture = thing_picture(source, f, px, indoor=area.indoor)
        cx, foot = foot_of(f, px)
        left, top = int(round(cx + picture.dx)), int(round(foot + picture.dy))
        lit = ground.light.apply(picture.image, left, top) if ground.light else picture.image
        p.drawImage(left, top, lit)
    p.end()
    return image


def px_for(area: Area) -> int:
    return INDOOR_PX if area.indoor else OUTDOOR_PX


def render_area(area: Area, source: TileSource, *, dress: bool = True) -> QImage:
    """The area drawn in the tile set, `px_for(area)` pixels a tile, its
    things on it: the whole picture."""
    return compose_things(render_ground(area, source, dress=dress), area, source)


def render_ground(
    area: Area,
    source: TileSource,
    *,
    dress: bool = True,
    avoid: Sequence[Placement] | None = None,
    lit: Collection[str] | None = None,
    abandoned: bool = False,
) -> Ground:
    """The area's ground (floor, walls, what grows and what is built), with
    no thing on it: they are sprites over it (`thing_picture`), so moving
    or adding one paints nothing. Lit by the things that give light where
    they stand. `avoid`: the footprints a room's dressing (a rug, what
    stands along the walls) keeps clear of: the things as the plan put them,
    so the ground stays as it is while they are moved (default: as they are
    now). `lit`: outdoors, the buildings (place ids) whose windows show a
    light; None lights every window (as every one was, until a deserted
    farmstead was played "lights on", Oct 28 2026). `abandoned`: a place
    nobody has lived in for years: no lamp, stove or fire gives light, and
    indoors the rug and what stands along the walls are their abandoned
    versions (the set's `aged/` pictures), with debris on the floors."""
    if area.indoor:
        return _render_indoor(area, source, dress=dress, avoid=avoid, abandoned=abandoned)
    return _render_outdoor(area, source, lit=lit, abandoned=abandoned)


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


def _hull_brushes(source: TileSource, px: int, seed: int) -> tuple[QBrush, QBrush]:
    """The plating and the rock of a deck's hull mass: the set's own
    textures when it has them, else its wall top tinted dark and cool, and
    rock made here from noise."""
    if source.image("hull") is not None:
        plate = _texture_brush(source, "hull", px * 4)
    else:
        cap = source.image("cap")
        if cap is None:
            plate = QBrush(QColor(72, 80, 94))
        else:
            if "tint:hull" not in source._images:
                source._images["tint:hull"] = _tinted(cap, (132, 142, 160))
            image = source._images["tint:hull"]
            plate = QBrush(image)
            plate.setTransform(QTransform.fromScale(px * 2 / image.width(), px * 2 / image.width()))
    if source.image("rock") is not None:
        rock = _texture_brush(source, "rock", px * 5)
    else:
        np = _np()
        n = _noise(256, 256, 48.0, seed, ((1, 0.55), (2.7, 0.3), (7, 0.15)))
        base = np.array((150, 134, 118), np.float32)
        image = tileable(_qimg(base * (0.6 + 0.75 * n[..., None])))
        rock = QBrush(image)
        rock.setTransform(QTransform.fromScale(px * 5 / 256, px * 5 / 256))
    return plate, rock


def _render_indoor(
    area: Area,
    source: TileSource,
    *,
    dress: bool,
    avoid: Sequence[Placement] | None = None,
    abandoned: bool = False,
) -> Ground:
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
    if area.hull:
        brushes = _hull_brushes(source, px, _seed(area.id))
        base = deck_look.hull_layer(base, area, px, *brushes, _upsample, _blur)
    p = QPainter(base)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    floor = source.floor(area.look, px * 4.8)
    pool = _texture_brush(source, "out-water", px * 8)
    for y in range(h):
        for x in range(w):
            if not space[y, x]:
                p.fillRect(x * px, y * px, px, px, pool if water[y, x] else floor)
    if area.hull:  # a deck laid out by the program: its hull's mass round it
        deck_look.hull_details(p, area, px)
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
    # A deck's rooms each drawn for their use (gui/deck_look.py).
    zoned = deck and any(room.use for room in area.rooms)
    zones = deck_look.zone_of_tiles(area) if zoned else []
    if zoned:
        img = deck_look.grade(img, area, px, zones)
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
            elif c == "&":
                if zoned:
                    deck_look.lift_pad(p, x, y, px)
                else:
                    _stairs(p, x, y, px, deck=deck)
    if zoned:
        deck_look.marks(p, area, px, zones)
    # 5. dressing: a rug in some rooms, decorations along walls, clear of
    # doorways and furniture
    near_door = np.zeros((h, w), bool)
    for d in area.doorways:
        near_door[max(0, d.y0 - 2) : d.y1 + 3, max(0, d.x0 - 2) : d.x1 + 3] = True
    taken = np.isin(grid, list("*:~"))
    for f in area.placements if avoid is None else avoid:
        taken[max(0, f.y - 1) : f.y + f.h + 1, max(0, f.x - 1) : f.x + f.w + 1] = True
    decor = [source.worn(f"decor{i}", abandoned) for i in range(3)]
    debris = (
        [d for d in (source.image(r) for r in DEBRIS_ROLES) if d is not None] if abandoned else []
    )
    decor_lit = list(info.decor_lights) + [False] * 3
    for room in area.rooms if dress else []:
        x0, y0, x1, y1 = room.box
        rw, rh = x1 - x0 + 1, y1 - y0 + 1
        rr0 = np.random.default_rng(500 + room.index + _seed(area.id) % 1000)
        if rw >= 6 and rh >= 4 and room.cells >= 30 and rr0.random() < 0.55:
            rug = _scaled(
                source.worn("rug", abandoned), int(min(rh * rr0.uniform(0.35, 0.5), rw * 0.3) * px)
            )
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
            if decor_lit[which] and not abandoned:
                lights.append(((x + 0.5) * px, (y + 0.4) * px, px * 3.0))
        if debris:
            # Nobody has lived here for years: papers, plaster and leaves on
            # the floor, flat on the ground under everything that stands.
            floor = [
                (x, y)
                for y in range(y0, y1 + 1)
                for x in range(x0, x1 + 1)
                if area.room_of[y][x] == room.index
                and grid[y, x] == "_"
                and not near_door[y, x]
                and not taken[y, x]
            ]
            rd = np.random.default_rng(1500 + room.index + _seed(area.id) % 1000)
            rd.shuffle(floor)
            for k, (x, y) in enumerate(floor[: max(1, min(3, room.cells // 25))]):
                piece = _scaled(debris[(k + room.index) % len(debris)], int(px * 1.4))
                if piece is None:
                    continue
                taken[max(0, y - 1) : y + 2, max(0, x - 1) : x + 2] = True
                p.setOpacity(0.9)
                p.drawImage(
                    int((x + 0.5) * px - piece.width() / 2),
                    int((y + 0.5) * px - piece.height() / 2),
                    piece,
                )
                p.setOpacity(1.0)
    # 6. walls, back to front: the cap on the tile, the face below wherever
    # no wall stands south (floor or the void: the south outer wall has a
    # face too), darker than the cap, the cap's north edge lit, so a wall
    # reads as a wall and not a flat band
    cap = _texture_brush(source, "cap", px * 2)
    face = _texture_brush(source, "face", face_h * 1.6)
    edge = QColor(14, 12, 12)
    stars = _stars(W, HX, _seed(area.id) + 1) if zoned else None
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
            if stars is not None and " " in (
                at(x, y - 1),
                at(x, y + 1),
                at(x - 1, y),
                at(x + 1, y),
            ):
                # A viewport in the hull: glass onto the stars.
                deck_look.viewport(p, stars, X, top, px, face_h if faced else 0.0)
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
    if zoned:
        deck_look.hull(p, area, px, face_h)
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
    # 8. a deck's set pieces the program draws (a reactor, a lift shaft):
    # the ground's; every other thing stands on it as a sprite of its own
    # (`thing_picture`), giving its light here. The flames over the rest.
    for f in sorted(area.placements, key=lambda f: f.y + f.h):
        if f.piece in DRAWN_PIECES:
            deck_look.piece(p, f, px)
        elif not abandoned and (light := thing_light(f, px, indoor=True)) is not None:
            lights.append(light)
    for sprite, X, Y in flames:
        p.drawImage(X, Y, sprite)
    # ceiling lamps in the larger rooms
    for room in area.rooms:
        if room.cells >= 40 and not zoned:
            lights.append(((room.cx + 0.5) * px, (room.cy + 0.5) * px, px * 6.0))
    p.end()
    # 9. light: a glow from each source, kept to its room, the world's
    # grade, a vignette
    img = _arr(out)
    things = area.placements if avoid is None else avoid
    counted = _things_counted(out, things, source, px, indoor=True)
    masks = _room_masks(area, px, HX, W)
    lc = np.array(info.light, np.float32) / 255
    cell = max(1, px // LIGHT_CELLS_PER_TILE)
    if zoned:
        # A deck: darker between its lamps, each lamp its room's colour.
        coloured = [(x, y, r, tuple(int(c * 255) for c in lc)) for x, y, r in lights]
        coloured += deck_look.lights(area, px, zones)
        glow3 = deck_look.light_rgb(coloured, HX, W, masks)
        img = img * (deck_look.AMBIENT + 0.6 * glow3) + glow3 * 26
        few = glow3[::LUM_STEP, ::LUM_STEP]
        counted = counted * (deck_look.AMBIENT + 0.6 * few) + few * 26
        coarse = _coarse(glow3, cell)
        mul, add = deck_look.AMBIENT + 0.6 * coarse, coarse * 26
    else:
        glow = _light(lights, HX, W, masks)
        img = img * (0.78 + 0.32 * glow[..., None] * lc) + glow[..., None] * lc * 18
        few = glow[::LUM_STEP, ::LUM_STEP, None]
        counted = counted * (0.78 + 0.32 * few * lc) + few * lc * 18
        coarse = _coarse(glow, cell)[..., None]
        mul, add = 0.78 + 0.32 * coarse * lc, coarse * lc * 18
    grade = np.array(info.grade, np.float32)
    img *= grade
    counted *= grade
    scale, shift = grade, np.zeros(3, np.float32)
    seen = inside[::LUM_STEP, ::LUM_STEP]
    if seen.any():
        lum = float(counted[seen].mean())
        floor_lum = 78 if zoned else 95
        if lum < floor_lum:
            k = (floor_lum / max(lum, 1.0)) ** 0.7
            img = img * k
            scale = scale * k
        elif lum > 135:  # a pale world loses its depth: pulled down, with contrast
            mean = counted[seen].mean(0)
            img = (img - mean) * 1.25 + mean * (135 / lum)
            scale, shift = scale * 1.25, mean * (135 / lum - 1.25)
    shade = _vignette_factor(HX, W, 0.18, 0.3)
    final = _qimg(img * shade[..., None])
    if zoned:
        deck_look.glows(final, area, px)
    light = LightMap(
        (mul * scale) * _coarse(shade, cell)[..., None],
        (add * scale + shift) * _coarse(shade, cell)[..., None],
        cell,
    )
    return Ground(final, light)


def _stairs(p: QPainter, x: int, y: int, px: int, *, deck: bool = False) -> None:
    """A stairs tile (engine/levels.py: the door between a plan's levels),
    drawn on the floor's texture: a flight of four treads running up the
    tile, each a lit edge over a shaded riser, in a dark well; on a deck a
    grey steel ladder of rungs instead."""
    X, Y = x * px, y * px
    inset = max(1.0, px * 0.08)
    well = QRectF(X + inset, Y + inset, px - 2 * inset, px - 2 * inset)
    p.fillRect(well, QColor(10, 9, 9, 150))
    treads = 4
    step = well.height() / treads
    for i in range(treads):
        top = well.y() + i * step
        shade = 70 + i * 28  # the lower treads nearer the eye, lighter
        tread = (
            QColor(shade + 20, shade + 10, shade - 10)
            if not deck
            else QColor(shade, shade + 5, shade + 15)
        )
        p.fillRect(QRectF(well.x(), top, well.width(), step), tread)
        p.fillRect(
            QRectF(well.x(), top, well.width(), max(1.0, step * 0.25)), QColor(255, 255, 255, 90)
        )
        p.fillRect(
            QRectF(well.x(), top + step - max(1.0, step * 0.2), well.width(), max(1.0, step * 0.2)),
            QColor(0, 0, 0, 110),
        )
    rail = max(1.0, px * 0.06)
    p.fillRect(QRectF(well.x(), well.y(), rail, well.height()), QColor(0, 0, 0, 140))
    p.fillRect(QRectF(well.right() - rail, well.y(), rail, well.height()), QColor(0, 0, 0, 140))


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


# Something a person carries, by its height in tiles (a person is drawn
# 2.1 tiles tall indoors, 2.6 out: gui/area_view.PERSON_TILES), and its
# width at most. Drawn as tall as anything else on the floor (2.2 tiles), a
# tin of beans stood as high as the player (the author, Oct 26 2026).
CARRIED_TILES = {True: 0.85, False: 1.1}
CARRIED_WIDEST = 1.6


def carried(f: Placement) -> bool:
    """Whether a thing is something a person carries: a thing (not a piece
    of furniture or a vehicle) that can be taken."""
    return f.kind == "object" and "take" in f.verbs


def _thing_image(source: TileSource, name: str, *, carried: bool, aged: bool) -> QImage | None:
    """A thing's picture as found: in an abandoned place (`aged`) its drawn
    abandoned version, else its picture faded (`_aged`)."""
    if aged:
        drawn = source.aged_item(name, carried=carried)
        if drawn is not None:
            return drawn
        plain = source.item(name, carried=carried)
        return _aged(plain) if plain is not None else None
    return source.item(name, carried=carried)


def _placement_sprite(
    source: TileSource, f: Placement, px: int, *, indoor: bool, aged: bool = False
) -> QImage | None:
    """A thing's sprite at the size it stands at, None until it is drawn."""
    if carried(f):
        picture = _thing_image(source, f.name, carried=True, aged=aged)
        sprite = _scaled(picture, int(px * CARRIED_TILES[indoor]))
        if sprite is not None and sprite.width() > px * CARRIED_WIDEST:
            sprite = sprite.scaledToWidth(int(px * CARRIED_WIDEST), Qt.SmoothTransformation)
        return sprite
    sprite = _thing_image(source, f.name, carried=False, aged=aged)
    if sprite is None:
        return None
    if f.kind == "vehicle":
        # Moored: as long as its footprint, never taller than its beam allows.
        sprite = sprite.scaledToWidth(max(int(f.w * px), 1), Qt.SmoothTransformation)
        most = int(px * (f.h + 2.0))
        if sprite.height() > most:
            sprite = sprite.scaledToHeight(most, Qt.SmoothTransformation)
        return sprite
    if f.kind == "furniture" or indoor:
        height = int(px * (f.h + 1.0)) if f.kind == "furniture" else int(px * 2.2)
        sprite = _scaled(sprite, height)
        if sprite is not None and sprite.width() > f.w * px * 1.15 and f.kind == "furniture":
            sprite = sprite.scaledToWidth(int(f.w * px * 1.15), Qt.SmoothTransformation)
        return sprite
    return _scaled(sprite, int(px * 3.2))


def _draw_placement(
    p: QPainter, source: TileSource, f: Placement, px: int, *, sprite: QImage | None
) -> None:
    """A thing over its footprint, its foot on its last row: its sprite
    (`_placement_sprite`) with its shadow, broken when it is; a plain crate
    while it has none."""
    cx, foot = foot_of(f, px)
    if sprite is None and carried(f):
        _bundle(p, cx, foot, px, broken=f.broken)
        return
    if sprite is None:
        # Not drawn yet: a plain crate of the thing's footprint, so it is
        # seen to be there (its sprite comes in the background).
        p.setPen(QColor(30, 24, 20))
        p.setBrush(QColor(90, 70, 50) if f.broken else QColor(150, 120, 80))
        box = QRectF(f.x * px + 3, f.y * px + 3, f.w * px - 6, f.h * px - 6)
        p.drawRoundedRect(box, 4, 4)
        if f.broken:
            _cracks(p, box, px)
            _rubble_at(p, source, cx, foot, px)
        return
    if f.broken:
        sprite = broken_sprite(sprite)
    for x in _run_middles(f, px, sprite.width()):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 60))
        p.drawEllipse(QPointF(x, foot - px * 0.1), sprite.width() * 0.42, px * 0.25)
        left, top = int(x - sprite.width() / 2), int(foot - sprite.height())
        p.drawImage(left, top, sprite)
        if f.broken:
            _cracks(p, QRectF(left, top, sprite.width(), sprite.height()), px)
    if f.broken:
        _rubble_at(p, source, cx, foot, px)


def _run_middles(f: Placement, px: int, width: float) -> list[float]:
    """Where a thing's picture stands along its footprint: once in its middle,
    or, for a long run of furniture its picture is much narrower than (a row
    of shelves, a pew, a fence a world kit laid six tiles long), as many
    times as fit, side by side. Drawn once, a run showed one shelf in the
    middle of six tiles of floor."""
    cx = (f.x + f.w / 2) * px
    span = f.w * px
    if f.kind != "furniture" or width <= 0 or width * 1.6 > span:
        return [cx]
    n = max(1, round(span / width))
    step = span / n
    return [f.x * px + step * (i + 0.5) for i in range(n)]


def _bundle(p: QPainter, cx: float, foot: float, px: int, *, broken: bool = False) -> None:
    """Something a person carries that has no picture of its kind: a small
    cloth bundle tied at the neck, so it reads as a thing lying there to be
    picked up, at its size (its name is in its menu), not as a crate."""
    w, h = px * 0.62, px * 0.5
    cloth = QColor(118, 98, 74) if not broken else QColor(84, 72, 60)
    p.save()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 60))
    p.drawEllipse(QPointF(cx, foot - px * 0.06), w * 0.55, px * 0.14)
    body = QRectF(cx - w / 2, foot - h, w, h * 0.92)
    p.setPen(QPen(QColor(40, 30, 22), max(1.0, px / 24)))
    p.setBrush(cloth)
    p.drawRoundedRect(body, w * 0.32, h * 0.38)
    neck = QPolygonF(
        [
            QPointF(cx - w * 0.16, body.top() + h * 0.08),
            QPointF(cx - w * 0.24, body.top() - h * 0.26),
            QPointF(cx + w * 0.24, body.top() - h * 0.26),
            QPointF(cx + w * 0.16, body.top() + h * 0.08),
        ]
    )
    p.drawPolygon(neck)
    p.setPen(QPen(QColor(64, 46, 30), max(1.0, px / 18)))
    p.drawLine(
        QPointF(cx - w * 0.2, body.top() + h * 0.02), QPointF(cx + w * 0.2, body.top() + h * 0.02)
    )
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 240, 210, 70))
    p.drawEllipse(QPointF(cx - w * 0.16, body.top() + h * 0.34), w * 0.14, h * 0.18)
    p.restore()


def broken_sprite(sprite: QImage) -> QImage:
    """The sprite as a broken thing: darkened and greyed within its own
    outline (the author's ruling: something broken is visibly broken), and
    its top third cut away at a slant, as if it had come apart."""
    out = sprite.convertToFormat(QImage.Format_ARGB32)
    w, h = out.width(), out.height()
    q = QPainter(out)
    q.setCompositionMode(QPainter.CompositionMode_SourceAtop)
    q.fillRect(0, 0, w, h, QColor(24, 20, 18, 150))
    q.setCompositionMode(QPainter.CompositionMode_Clear)
    # A jagged piece gone from the top, so the silhouette reads as broken.
    gone = QPolygonF(
        [
            QPointF(w * 0.15, 0),
            QPointF(w, 0),
            QPointF(w, h * 0.22),
            QPointF(w * 0.7, h * 0.3),
            QPointF(w * 0.55, h * 0.12),
            QPointF(w * 0.4, h * 0.34),
            QPointF(w * 0.15, 0),
        ]
    )
    q.setPen(Qt.NoPen)
    q.setBrush(QColor(0, 0, 0))
    q.drawPolygon(gone)
    q.end()
    return out


def _cracks(p: QPainter, box: QRectF, px: int) -> None:
    """Two dark cracks across a broken thing."""
    p.setPen(QPen(QColor(12, 10, 10, 230), max(1.0, px / 14)))
    x0, y0, w, h = box.x(), box.y(), box.width(), box.height()
    p.drawPolyline(
        QPolygonF(
            [
                QPointF(x0 + w * 0.3, y0 + h * 0.35),
                QPointF(x0 + w * 0.5, y0 + h * 0.55),
                QPointF(x0 + w * 0.42, y0 + h * 0.7),
                QPointF(x0 + w * 0.62, y0 + h * 0.95),
            ]
        )
    )
    p.drawPolyline(
        QPolygonF(
            [
                QPointF(x0 + w * 0.5, y0 + h * 0.55),
                QPointF(x0 + w * 0.72, y0 + h * 0.5),
                QPointF(x0 + w * 0.85, y0 + h * 0.62),
            ]
        )
    )


def _rubble_at(p: QPainter, source: TileSource, cx: float, foot: float, px: int) -> None:
    """The set's rubble at a broken thing's foot (a scatter of dark pieces
    when the set has none)."""
    rubble = _scaled(source.image("out-rubble"), int(px * 0.7))
    if rubble is not None:
        p.drawImage(int(cx - rubble.width() * 0.8), int(foot - rubble.height() * 0.8), rubble)
        p.drawImage(int(cx + rubble.width() * 0.1), int(foot - rubble.height() * 0.6), rubble)
        return
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(*RUBBLE_TINT))
    for dx, dy, r in ((-0.35, -0.12, 0.14), (0.1, -0.08, 0.1), (0.3, -0.16, 0.12)):
        p.drawEllipse(QPointF(cx + dx * px, foot + dy * px), r * px, r * px * 0.7)


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
    # A town's wall is wider than 40 tiles or taller than 30: not a building.
    # The plan's own limit (16) is for its coarser tiles: at a lane's 1.5 m a
    # tile, a 27 m shop was drawn roofless, grass inside (Oct 18 2026).
    for x0, x1, y0, y1 in building_boxes(area.rows, most=(39, 29)):
        boxes.append((x0, y0, x1, y1))
    return boxes


def _bridges(p: QPainter, area: Area, source: TileSource, px: int) -> None:
    """A bridge's deck over the water (engine/area.py `BRIDGE`): the set's
    paving, a parapet along each side over water, its shadow on the water
    south of it."""
    rows = area.rows
    tiles = [(x, y) for y, row in enumerate(rows) for x, ch in enumerate(row) if ch == BRIDGE]
    if not tiles:
        return
    deck = _texture_brush(source, "out-pave", px * 12)
    rail, shade = QColor(88, 80, 70), QColor(0, 0, 0, 70)
    t = max(2, round(px * 0.16))

    def water(x: int, y: int) -> bool:
        return 0 <= y < len(rows) and 0 <= x < len(rows[y]) and rows[y][x] == "~"

    for x, y in tiles:
        X, Y = x * px, y * px
        if water(x, y + 1):
            p.fillRect(X, Y + px, px, t * 2, shade)
        p.fillRect(X, Y, px, px, deck)
        p.fillRect(X, Y, px, px, QColor(255, 255, 255, 18))
        if water(x, y - 1):
            p.fillRect(X, Y, px, t, rail)
        if water(x, y + 1):
            p.fillRect(X, Y + px - t, px, t, rail)
        if water(x - 1, y):
            p.fillRect(X, Y, t, px, rail)
        if water(x + 1, y):
            p.fillRect(X + px - t, Y, t, px, rail)


def _side_door(
    p: QPainter, x: int, y: int, px: int, *, on_east: bool = False, on_north: bool = False
) -> None:
    """A building's door in its east, west or north wall, where the roof
    hides the wall itself: a door panel standing proud of the roof's edge at
    the door's tile, framed light, with a lit knob, and a stone step on the
    ground outside it, so the way in reads at a glance."""
    frame = QColor(230, 200, 140)
    panel = QColor(74, 47, 28)
    knob = QColor(246, 207, 110)
    step = QColor(150, 140, 125, 220)
    if on_north:
        # Above the roof's far edge: the top of the door and its step beyond.
        door = QRectF(x * px + px * 0.2, y * px - px * 0.75, px * 0.6, px * 0.55)
        p.fillRect(QRectF(x * px + px * 0.1, y * px - px * 1.05, px * 0.8, px * 0.3), step)
    elif on_east:
        door = QRectF((x + 1) * px - px * 0.1, y * px + px * 0.1, px * 0.5, px * 0.85)
        p.fillRect(QRectF((x + 1) * px + px * 0.35, y * px + px * 0.2, px * 0.4, px * 0.7), step)
    else:
        door = QRectF(x * px - px * 0.4, y * px + px * 0.1, px * 0.5, px * 0.85)
        p.fillRect(QRectF(x * px - px * 0.75, y * px + px * 0.2, px * 0.4, px * 0.7), step)
    p.fillRect(door.adjusted(-1.5, -1.5, 1.5, 1.5), frame)
    p.fillRect(door, panel)
    p.fillRect(
        QRectF(
            door.x() + door.width() * 0.2, door.y() + door.height() * 0.15, 1.5, door.height() * 0.7
        ),
        QColor(93, 60, 37),
    )
    p.fillRect(QRectF(door.right() - door.width() * 0.3, door.center().y() - 1, 2, 2), knob)


def _lit_boxes(
    area: Area, boxes: Sequence[tuple[int, int, int, int]], lit: Collection[str]
) -> set[tuple[int, int, int, int]]:
    """The drawn boxes (walled rings the picture roofs) of the buildings
    in `lit`: the ring whose box holds the building's middle."""
    centres = [
        ((x0 + x1) / 2, (y0 + y1) / 2)
        for pid, (x0, y0, x1, y1) in area.buildings.items()
        if pid in lit
    ]
    return {
        box
        for box in boxes
        if any(box[0] <= cx <= box[2] and box[1] <= cy <= box[3] for cx, cy in centres)
    }


def _render_outdoor(
    area: Area, source: TileSource, *, lit: Collection[str] | None = None, abandoned: bool = False
) -> Ground:
    np = _np()
    info = source.info
    rows = area.rows
    h, w = area.height, area.width
    px = OUTDOOR_PX
    W, H = w * px, h * px
    g = np.array([list(r) for r in rows])
    boxes = _building_boxes(area)
    lit_boxes = None if lit is None else _lit_boxes(area, boxes, lit)
    roofed = np.zeros((h, w), bool)
    for x0, y0, x1, y1 in boxes:
        roofed[y0 : y1 + 1, x0 : x1 + 1] = True

    def at(x: int, y: int) -> str:
        return rows[y][x] if 0 <= x < w and 0 <= y < h else ""

    # 1. terrain: ground, then pavement, rock, forest floor, water, road.
    # Texture scales are in tiles (a texture every 16 tiles of ground), so
    # the picture is the same whatever `px` is.
    ground = QImage(W, H, QImage.Format_RGB32)
    painter = QPainter(ground)
    painter.fillRect(0, 0, W, H, source.ground(area.look, px * 16))
    painter.end()
    img = _arr(ground)
    trees_mask = g == "T"
    layers = [
        ("out-pave", np.isin(g, list("_+")) & ~roofed, 0.2, px * 12, False),
        (
            "out-canopy",
            np.isin(g, ["^"]) if not area.underground else np.zeros_like(roofed),
            0.45,
            px * 16,
            False,
        ),
        ("out-canopy", trees_mask if not info.lava else np.zeros_like(roofed), 0.4, px * 16, True),
        ("out-water", np.isin(g, ["~", BRIDGE]), 0.3, px * 16, False),
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
    ruined = g == ":"
    if ruined.any():  # rubble's grey-brown on the ground, as indoors: a burnt
        # barn's floor was bare snow with planks strewn over it (Oct 17 2026)
        ash = _soft(ruined, 41, px, 0.3)[..., None]
        img = img * (1 - 0.6 * ash) + np.array(RUBBLE_TINT, np.float32) * 0.6 * ash
    img *= (0.9 + 0.16 * _noise(H, W, px * 19, 7))[..., None]
    if area.underground:  # the rock mass round a cave's passages: solid, dark
        mass = _soft(g == "^", 43, px, 0.25)[..., None]
        rock = np.array(SOLID_ROCK, np.float32) * (0.75 + 0.5 * _noise(H, W, px * 5, 11))[..., None]
        img = img * (1 - mass) + rock * mass
    void = g == " "
    if void.any():  # off a deck a blank tile is the void: the drop off a pier, a chasm
        gone = _soft(void, 47, px, 0.15)[..., None]
        img = img * (1 - gone) + np.array(VOID, np.float32) * gone
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
    # Stairs on the ground (a cellar's hatch, steps down into a dungeon):
    # on the terrain, under whatever stands.
    for y in range(h):
        for x in range(w):
            if rows[y][x] == "&":
                _stairs(p, x, y, px)
    _bridges(p, area, source, px)
    # 3. everything that stands, back to front by its foot
    stand: list[tuple[float, str, object]] = []
    # Things are sprites over the ground (`thing_picture`): here only their
    # light, and their tiles keep what the ground has under them.
    for f in area.placements:
        if not abandoned and (light := thing_light(f, px, indoor=False)) is not None:
            lights.append(light)
    for y in range(h):
        for x in range(w):
            c = rows[y][x]
            r = rng.random()
            if c == "T" and r < 0.42:
                stand.append(((y + 1) * px, "tree", (x, y)))
            elif c == "," and r < 0.55:
                stand.append(((y + 1) * px, "bush", (x, y)))
            elif c == "^" and r < 0.18 and not area.underground:
                stand.append(((y + 1) * px, "rock", (x, y)))
            elif c == ":" and r < 0.22:
                stand.append(((y + 1) * px, "rubble", (x, y)))
            elif c == "%":
                stand.append(((y + 1) * px, "fence", (x, y)))
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

    def roof_brush(x0: int, y0: int, x1: int, y1: int) -> QBrush:
        """The roof over this box: the material its building's words name
        (`Area.roofs`), the set's roof otherwise."""
        for pid, (bx0, by0, bx1, by1) in area.buildings.items():
            if bx0 <= x1 and x0 <= bx1 and by0 <= y1 and y0 <= by1 and area.roofs.get(pid):
                return source.roof(area.roofs[pid], px * 12)
        return roofb

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
            p.fillRect(roof, roof_brush(x0, y0, x1, y1))
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
                    if lit_boxes is None or (x0, y0, x1, y1) in lit_boxes:
                        p.fillRect(glass, QColor(255, 210, 130))
                        lights.append(((x + 0.5) * px, Y1 + px * 0.6, px * 3.5))
                    else:
                        # Nobody in: dark glass with the sky in it.
                        p.fillRect(glass, QColor(54, 62, 78))
                        p.fillRect(
                            QRectF(glass.x() + 1, glass.y() + 1.5, glass.width() * 0.4, 1.2),
                            QColor(150, 165, 190, 120),
                        )
                    p.fillRect(QRectF(glass.x(), glass.y(), glass.width(), 1.5), edge)
            # Doors in the other walls: a door drawn on that wall, with a lit
            # knob and a step out of the roof's shadow. They were a faint
            # translucent step alone, and the author (Oct 23 2026) could not
            # see where Brasshaven's buildings were entered.
            for y in range(y0, y1):
                for x in (x0, x1):
                    if at(x, y) == "+":
                        _side_door(p, x, y, px, on_east=x == x1)
            for x in range(x0, x1 + 1):
                if at(x, y0) == "+":
                    _side_door(p, x, y0, px, on_north=True)
    for sprite, X, Y in flames:
        p.drawImage(X, Y, sprite)
    p.end()
    # 4. light: lava, fire, lamps and windows; a grade. No vignette outdoors:
    # it made an area's edge look like the end of the world, and Leave ▾
    # carries on past it (indoors it stays).
    img = _arr(out)
    counted = _things_counted(out, area.placements, source, px, indoor=False)
    glow = _light(lights, H, W)
    if info.lava and (g == "~").any():
        glow = glow + _blur(_soft(g == "~", 5, px), px, 3) * 1.1
    lc = np.array(info.light, np.float32) / 255
    img = img * (0.92 + 0.25 * glow[..., None] * lc) + glow[..., None] * lc * 22
    few = glow[::LUM_STEP, ::LUM_STEP, None]
    counted = counted * (0.92 + 0.25 * few * lc) + few * lc * 22
    grade = np.array(info.grade, np.float32)
    img *= grade
    scale = grade
    lum = float((counted * grade).mean())
    if lum < 85:  # a dark world lifted, as indoors
        k = (85 / max(lum, 1.0)) ** 0.75
        img = img * k
        scale = scale * k
    cell = max(1, px // LIGHT_CELLS_PER_TILE)
    coarse = _coarse(glow, cell)[..., None]
    light = LightMap((0.92 + 0.25 * coarse * lc) * scale, coarse * lc * 22 * scale, cell)
    return Ground(_qimg(img), light)
