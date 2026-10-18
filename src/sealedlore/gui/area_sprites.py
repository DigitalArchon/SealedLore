"""What stands on the play view as a sprite of its own, over the ground
(gui/area_render.py paints the ground: floor, walls, what grows): the area's
things and its people, each drawn from its foot and in front of whatever
stands north of it.

The author (Oct 18 2026): things on their own layer, not painted into the
area's picture, so moving or adding one shows at once with nothing painted;
and, later, people with pictures for facing and walking each way, and a
thing carried shown going about with whoever carries it. So:

- a person's pictures are `Frames`, by facing and pose; today's figures have
  one picture, which stands in for every facing (mirrored for the other
  side) and every pose, so more pictures drop in without changing what
  draws them;
- a thing carried is a child item of its carrier (`PersonSprite.carry`): Qt
  moves it with them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsItem

from sealedlore.engine.area import Placement
from sealedlore.gui.area_render import LightMap, ThingPicture

# A sprite's place in the depth order: in front of every sprite whose foot
# is on a row north of its own (z is DEPTH plus its foot's row), a person in
# front of a thing whose foot is on their row.
DEPTH = 10
THING_BEHIND = 0.25
GOLD = QColor(246, 207, 110)

FACINGS = ("south", "east", "north", "west")
OTHER_SIDE = {"east": "west", "west": "east"}
# Where a carried thing is held, as shares of the carrier's height up from
# their foot and of their width out to their front.
CARRIED_AT = (0.42, 0.3)


def facing_of(dx: float, dy: float, was: str) -> str:
    """The way a step of (`dx`, `dy`) tiles faces: across before up or down
    (a figure seen from the side reads best), `was` for no step."""
    if dx:
        return "east" if dx > 0 else "west"
    if dy:
        return "south" if dy > 0 else "north"
    return was


@dataclass
class Frames:
    """A figure's pictures, by facing ("south", "east", "north", "west")
    and pose ("stand", "walk": a walk's pictures in order). What a figure
    has no picture for is stood in for: the other side mirrored, a side for
    north or south, standing for a pose, any picture rather than none."""

    pictures: dict[tuple[str, str], list[QPixmap]] = field(default_factory=dict)

    @classmethod
    def single(cls, picture: QPixmap | None) -> Frames:
        """A figure with one picture (every figure today), drawn facing east."""
        return cls({("east", "stand"): [picture]} if picture is not None else {})

    def has(self, pose: str) -> bool:
        return any(p == pose and pics for (_f, p), pics in self.pictures.items())

    def frame(self, facing: str, side: str, pose: str, step: int) -> tuple[QPixmap | None, bool]:
        """The picture to draw and whether it is mirrored. `side`: the way
        across the figure last faced, for a north or south it has no picture
        of; `step`: how far into its pose (a walk's frame)."""
        tries = [(facing, False)]
        if facing in ("north", "south"):
            tries.append((side, False))
        tries += [(OTHER_SIDE[f], True) for f, _m in list(tries) if f in OTHER_SIDE]
        tries += [(f, False) for f in FACINGS]
        for found, mirrored in tries:
            for wanted in (pose, "stand"):
                pictures = self.pictures.get((found, wanted))
                if pictures:
                    return pictures[step % len(pictures)], mirrored
        return None, False


class Sprite(QGraphicsItem):
    """Something standing on the area, drawn from the middle of its foot
    (the item's origin) and ordered by its foot's row."""

    def __init__(self, px: int) -> None:
        super().__init__()
        self.px = px
        self.setAcceptHoverEvents(False)

    def set_row(self, row: float, behind: float = 0.0) -> None:
        self.setZValue(DEPTH + row - behind)


class ThingSprite(Sprite):
    """A thing on the area: its picture (`area_render.thing_picture`), lit
    as the ground under it is (`LightMap`), standing over its footprint."""

    def __init__(self, placement: Placement, picture: ThingPicture, px: int) -> None:
        super().__init__(px)
        self.placement = placement
        self.picture = picture
        self._light: LightMap | None = None
        self._lit_at: tuple[int, int, int] | None = None
        self.pixmap = QPixmap.fromImage(picture.image)
        self.stand_at(placement.x, placement.y)

    def stand_at(self, x: int, y: int) -> None:
        """Its footprint's top-left on tile (`x`, `y`)."""
        f = self.placement
        self.setPos((x + f.w / 2) * self.px, (y + f.h) * self.px)
        self.set_row(y + f.h - 1, THING_BEHIND)
        self._relight()

    def set_light(self, light: LightMap | None) -> None:
        """Lit as the ground painted with `light` (None: as drawn)."""
        if light is not self._light:
            self._light = light
            self._relight()

    def _relight(self) -> None:
        if self._light is None:
            if self._lit_at is not None:
                self.pixmap = QPixmap.fromImage(self.picture.image)
                self._lit_at = None
                self.update()
            return
        left = int(round(self.pos().x() + self.picture.dx))
        top = int(round(self.pos().y() + self.picture.dy))
        at = (left, top, id(self._light))
        if at == self._lit_at:
            return
        self.pixmap = QPixmap.fromImage(self._light.apply(self.picture.image, left, top))
        self._lit_at = at
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt's name
        return QRectF(self.picture.dx, self.picture.dy, self.pixmap.width(), self.pixmap.height())

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        painter.drawPixmap(QPointF(self.picture.dx, self.picture.dy), self.pixmap)


class PersonSprite(Sprite):
    """A person on the map: their picture for the way they face and what
    they are doing (`Frames`), standing on their tile, the author's
    character ringed. A picture is drawn into the height they stand at, so
    every zoom resamples it from the source; one smaller than that is
    scaled up by whole pixels first (the window does: `pixel_scaled`)."""

    def __init__(self, walker, px: int, height: int, frames: Frames) -> None:
        super().__init__(px)
        self.walker = walker
        self.height = height
        self.frames = frames
        picture, _mirrored = frames.frame("south", "east", "stand", 0)
        self.width = (
            height * picture.width() / picture.height()
            if picture is not None and picture.height()
            else height * 0.6
        )
        self.facing, self.side = "south", "east"
        self.pose, self.step = "stand", 0
        # A step's bob while walking, for a figure with no walking pictures.
        self.bob = 0.0
        self.carried: list[Sprite] = []
        self.place_at(walker.x, walker.y)

    @property
    def mirrored(self) -> bool:
        return self.frames.frame(self.facing, self.side, self.pose, self.step)[1]

    def place_at(self, x: float, y: float) -> None:
        self.setPos((x + 0.5) * self.px, (y + 1.0) * self.px)
        self.set_row(y)

    def face(self, dx: float, dy: float) -> None:
        """Facing the way a step of (`dx`, `dy`) goes."""
        facing = facing_of(dx, dy, self.facing)
        side = facing if facing in OTHER_SIDE else self.side
        if (facing, side) != (self.facing, self.side):
            self.facing, self.side = facing, side
            self._place_carried()
            self.update()

    def set_pose(self, pose: str, step: int = 0) -> None:
        """Standing, or walking `step` tiles in (a walk's frame)."""
        if (pose, step) != (self.pose, self.step):
            self.pose, self.step = pose, step
            self.update()

    def set_bob(self, bob: float) -> None:
        if bob != self.bob:
            self.bob = bob
            self.update()

    def carry(self, sprite: Sprite) -> None:
        """`sprite` held by them: it goes where they go, in front of them."""
        if sprite not in self.carried:
            self.carried.append(sprite)
        sprite.setParentItem(self)
        sprite.setZValue(1)
        self._place_carried()

    def put_down(self, sprite: Sprite) -> None:
        """`sprite` no longer theirs: left where they stand, in the scene."""
        if sprite in self.carried:
            self.carried.remove(sprite)
        at = sprite.scenePos()
        sprite.setParentItem(None)
        sprite.setPos(at)

    def _place_carried(self) -> None:
        up, out = CARRIED_AT
        mirrored = self.mirrored
        for sprite in self.carried:
            sprite.setPos((-1 if mirrored else 1) * self.width * out, -self.height * up)

    def _ring(self) -> tuple[QPointF, float, float, float]:
        """The held character's ring: centre, radii, pen width."""
        centre = QPointF(0, -self.px * 0.08)
        return centre, self.width * 0.55, self.px * 0.32, max(2, self.px * 0.08)

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt's name
        # Wide enough for the ring and its pen (it painted outside and
        # smeared while walking), and for the bob.
        centre, rx, ry, pen = self._ring()
        half = max(self.width / 2 + 2, rx + pen + 1)
        bottom = max(4.0, centre.y() + ry + pen + 1)
        top = -self.height - 4 - 1
        return QRectF(-half, top, 2 * half, bottom - top)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 80))
        painter.drawEllipse(QPointF(0, -self.px * 0.08), self.width * 0.4, self.px * 0.22)
        if self.walker.held:
            centre, rx, ry, pen = self._ring()
            painter.setPen(QPen(GOLD, pen))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(centre, rx, ry)
        picture, mirrored = self.frames.frame(self.facing, self.side, self.pose, self.step)
        target = QRectF(-self.width / 2, -self.height - self.bob, self.width, self.height)
        if picture is None:
            painter.setPen(QColor(20, 20, 26))
            painter.setBrush(QColor(90, 110, 170))
            painter.drawRoundedRect(target, 6, 6)
            return
        source = QRectF(picture.rect())
        if mirrored:
            painter.save()
            painter.scale(-1, 1)
            painter.drawPixmap(target, picture, source)
            painter.restore()
        else:
            painter.drawPixmap(target, picture, source)
