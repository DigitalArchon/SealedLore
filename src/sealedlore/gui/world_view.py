"""The map (the Inspector's first page for a story that keeps one): the
world the story has been through, drawn as it grows (models/world.py).

One map in metres, read at three levels by zooming (the author, Oct 2026:
the player should spend most of their time on the local map, zoom out to
the region to decide where to go next, and look at the world now and then
for the big names):

- **Local**: the area the author's character is in (the farm, not just its
  farmhouse: `engine.world_art.local_area`), its plan drawn in the program's
  tileset (gui/map_tiles.py) where the map has it, buildings seen from
  outside, the marked things lying about, everyone the story has there
  standing in it, the author's character ringed. Zoom in on a building and
  its own plan, the inside, takes the roof's place (See inside takes every
  roof off at once).
- **Region**: the country the story has reached (engine/world_art.py) with
  fog over the rest, each place where the program placed it with its
  picture, scaled with the map but never too small to see, lit where the
  character has been, dimmed where the story only told of it, ringed where
  they are now; a place inside another shows once the map is zoomed in far
  enough to read it.
- **World**: zoomed out, the settlements and regions named large, the small
  places hidden.

Local, Region and World jump to each, centred on the character; the wheel
moves between them. Isometric lays each area's painted view over its plan
(gui/isometric_view.py), and marks the areas that have one while it is off.
Everything here is read from the session; the view never changes the story.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from html import escape

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.world_art import (
    containers,
    decked,
    fixed_in_plan,
    local_area,
    plan_ground,
    plan_rect,
    plan_tile,
    standing_spot,
)
from sealedlore.engine.world_layout import position, size_of
from sealedlore.engine.world_pictures import isometric_state
from sealedlore.gui import text_size
from sealedlore.gui.isometric_view import IsometricBar, IsometricDialog
from sealedlore.gui.map_art import FOG, picture_sprite, sprite_pixmap
from sealedlore.gui.map_render import drawn_box, label_key
from sealedlore.gui.map_tiles import (
    TILE,
    building_boxes,
    person_sprite,
    prop_sprite,
    tiles_pixmap,
)
from sealedlore.gui.ref_images import image_of
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.models.world import MapPicture, PlaceLayout, WorldArt, WorldMap, WorldPlace

LEVELS = ("local", "region", "world")
# A place's picture on the region: scaled with the map, within these (a
# farmhouse at region zoom is small, never invisible).
MIN_PLACE_PX, MAX_PLACE_PX = 16, 96
# On the local map a plan is drawn once it is this wide on screen; smaller,
# its place's picture stands for it. Never on the region or the world.
PLAN_MIN_PX = 96
# A building's inside takes its roof's place once its footprint is this
# wide on screen (or with See inside on).
INTERIOR_SHOWN_AT = 320
# The three levels are three zooms: the local area fitted, the region (the
# known places within REGION_REACH of the character, else all of them)
# fitted and at most half as close as the local, and the world (every known
# place) fitted and at most this much further out than the region. The
# level read now is whichever of the three the zoom is nearest, so it
# changes at fixed zooms and nowhere else.
REGION_REACH = 12_000.0
WORLD_ZOOM_OUT = 2.5
# What the world level names.
WORLD_KINDS = frozenset({"settlement", "region", "site"})
# A place inside another is shown once its parent is this many pixels wide.
CHILD_SHOWN_AT = 160
ZOOM_STEP = 1.25
# Over the map while a private scene is open.
PAUSED_NOTE = (
    "Paused for the private scene. The map shows the story as it was when the scene "
    "began, and nothing from the scene reaches it. It catches up from the scene's "
    "summary once you approve it."
)
# A place's label under its picture: its width at the default text size (it
# grows with the font), and the gap between picture and name.
LABEL_WIDTH = 176
LABEL_GAP = 3
# A scene's people, in art pixels (the tileset's scale) before the view's zoom.
AVATAR_HEIGHT = 26
# A postcard's thumbnail on a place's card.
POSTCARD_THUMB = (176, 100)
# An area's isometric view lies over its plan: above every plan (2 to 2.9)
# and below the places' pictures and names (3 and up).
ISO_Z = 2.95
# A view is held no wider than this (the map shows it no wider); seen whole,
# it is read from its file again.
ISO_HELD_WIDTH = 1600
# What marks an area that has a view, while the views aren't shown.
ISO_MARK = "◆"
ISO_TIP = (
    "Each area's isometric view laid over the map where it is: painted by an image model "
    "from the map when you ask, or a picture of your own. The 2D map stays the story's "
    "truth; a view matches it closely, not exactly."
)
# Places with no picture of their own (land, not a thing on it): a mark only.
_MARK_COLOURS = {
    "region": "#c9b98a",
    "area": "#b8c79a",
    "route": "#b9a27f",
    "room": "#d8c7a5",
    None: "#e6dcc2",
}


def place_card(world: WorldMap, place: WorldPlace) -> str:
    """The hover card: what the story said of a place, who and what is there."""
    lines = [f"<b>{escape(place.name)}</b> <i>({escape(place.kind or 'place')})</i>"]
    if place.state:
        lines.append(f"<b>{escape(place.state)}</b>")
    if place.what:
        lines.append(escape(place.what))
    here = [who for who, pid in world.where.items() if pid == place.id]
    if here:
        lines.append("Here: " + escape(", ".join(here)))
    things = [t for t in world.things.values() if t.at == place.id]
    if things:
        lines.append(
            "Things: "
            + escape("; ".join(t.name + (f" ({t.state})" if t.state else "") for t in things))
        )
    return "<br>".join(lines)


_key = label_key


class _PlaceItem(QGraphicsItem):
    """A place on the region: its picture (or a mark) scaled with the map
    within MIN_PLACE_PX and MAX_PLACE_PX, its name, who is there. Drawn in
    screen pixels at its spot."""

    def __init__(
        self,
        place: WorldPlace,
        picture: Callable[[int], QPixmap | None],
        people: list[str],
        *,
        visited: bool,
        current: bool,
        painted: bool = False,
    ) -> None:
        """`painted`: the place has an isometric view, marked before its name."""
        super().__init__()
        self.place = place
        self._picture = picture
        self.people = people
        self.visited = visited
        self.current = current
        self.ringed = current
        self.pixmap: QPixmap | None = None
        self.pixels = 0
        self.big = False
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setAcceptHoverEvents(True)
        self.setOpacity(1.0 if visited or current else 0.55)
        self.setZValue(4 if current else 3)
        mark = f"{ISO_MARK} " if painted else ""
        self._name = mark + place.name + (f" ({place.state})" if place.state else "")
        self._fonts()
        self.set_pixels(MIN_PLACE_PX)

    def _fonts(self) -> None:
        self._font = QFont()
        self._font.setPointSizeF(text_size.points(11 if self.big else 8.5))
        self._bold = QFont(self._font)
        self._bold.setBold(True)
        # The label's lines are as tall as the font, which View → Text size
        # scales: a fixed 14px strip clipped them to the tops of the letters
        # from 150% up. Its width grows with the font too, and to fit the
        # name; the people are elided to it.
        metrics, bold = QFontMetricsF(self._font), QFontMetricsF(self._bold)
        self._line = max(metrics.height(), bold.height())
        width = LABEL_WIDTH * text_size.scale() * (1.3 if self.big else 1.0)
        self._width = min(max(width, bold.horizontalAdvance(self._name) + 12), 2 * width)
        self._who = metrics.elidedText(", ".join(self.people), Qt.ElideRight, self._width - 4)

    def set_pixels(self, pixels: int) -> None:
        """The picture this many pixels across on screen."""
        if pixels == self.pixels:
            return
        self.prepareGeometryChange()
        self.pixels = pixels
        self.pixmap = self._picture(pixels)
        self.update()

    def set_big(self, big: bool) -> None:
        """Named large, as the world level names its places."""
        if big == self.big:
            return
        self.prepareGeometryChange()
        self.big = big
        self._fonts()
        self.update()

    def set_ringed(self, ringed: bool) -> None:
        """Ringed as where the character is: their own place, or, while that
        is too small to show, the nearest place shown that it is in."""
        if ringed == self.ringed:
            return
        self.ringed = ringed
        self.setZValue(4 if ringed else 3)
        self.update()

    def _half(self) -> float:
        return self.pixels / 2 if self.pixmap is not None else max(7.0, self.pixels / 4)

    def boundingRect(self) -> QRectF:
        half = self._half()
        height = half + 6 + LABEL_GAP + 2 * self._line + 2
        return QRectF(-self._width / 2 - 2, -half - 6, self._width + 4, height)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        half = self._half()
        if self.ringed:
            painter.setPen(QPen(QColor("#f6cf6e"), 3))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(0, 0), half + 5, half + 5)
        if self.pixmap is not None:
            painter.drawPixmap(
                int(-self.pixmap.width() / 2), int(-self.pixmap.height() / 2), self.pixmap
            )
        else:
            colour = QColor(_MARK_COLOURS.get(self.place.kind, _MARK_COLOURS[None]))
            painter.setPen(QPen(QColor("#1b1d2b"), 1.5))
            painter.setBrush(colour)
            painter.drawEllipse(QPointF(0, 0), half, half)
        top = half + LABEL_GAP
        self._text(painter, self._name, top, self._bold)
        if self._who:
            self._text(painter, self._who, top + self._line, self._font)

    def _text(self, painter: QPainter, text: str, top: float, font: QFont) -> None:
        painter.setFont(font)
        rect = QRectF(-self._width / 2, top, self._width, self._line)
        flags = Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextSingleLine
        painter.setPen(QColor(0, 0, 0, 150))
        painter.drawText(rect.translated(1, 1), flags, text)
        painter.setPen(QColor("#f4f1e8"))
        painter.drawText(rect, flags, text)


class _PlanItem(QGraphicsPixmapItem):
    """A place's plan on the map, in metres: the tiles rendered at its tile
    size, at its spot. What stands in it (people, props, names, pictured
    buildings) are its children, in the tileset's pixels."""

    def __init__(
        self, place: WorldPlace, layout: PlaceLayout, rect: tuple[float, float, float, float]
    ) -> None:
        super().__init__()
        self.place = place
        self.layout = layout
        self.west, self.north, self.width_m, self.height_m = rect
        self.tile = plan_tile(place)
        self.rows = tuple(layout.rows)
        self.roofs = True
        self.cleared: tuple[tuple[int, int, int, int], ...] = ()
        # Boxes of the buildings in it drawn open for their changed state,
        # where people at them stand at the front (`_stand`).
        self.fronts: tuple[tuple[int, int, int, int], ...] = ()
        self.ground = ""
        # A building the story changed, its plan drawn in that state: shown
        # open on the local map, in place of its roof, at any zoom.
        self.changed = bool(
            place.kind == "building" and place.state and layout.state == place.state
        )
        # What stands in it, held here as well as by the scene (see WorldView).
        self.dressed: list[QGraphicsItem] = []
        self.setTransformationMode(Qt.FastTransformation)
        self.setShapeMode(QGraphicsPixmapItem.BoundingRectShape)
        self.setScale(self.tile / TILE)
        self.setPos(self.west, -self.north)
        self.setZValue(2)

    def fit_to(self, rect: QRectF) -> None:
        """Lie over `rect` (scene units): its width, centred down it. A
        building's plan goes over the box its parent's plan drew for it,
        whatever size the map gives the building (`_fit_in_parents`)."""
        columns = max((len(r) for r in self.rows), default=1)
        self.tile = rect.width() / columns
        self.width_m = rect.width()
        self.height_m = self.tile * len(self.rows)
        self.west = rect.left()
        self.north = -(rect.top() + (rect.height() - self.height_m) / 2)
        self.setScale(self.tile / TILE)
        self.setPos(self.west, -self.north)

    def render(
        self, ground: str, roofs: bool, cleared: tuple[tuple[int, int, int, int], ...]
    ) -> None:
        self.ground, self.roofs, self.cleared = ground, roofs, cleared
        self.setPixmap(tiles_pixmap(self.drawn_rows(), ground, roofs, cleared))

    def drawn_rows(self) -> tuple[str, ...]:
        """The rows as drawn: a deck's blank between its rooms is floor
        (`world_art.decked`), elsewhere the rows as the model gave them."""
        return decked(self.rows) if self.ground == "metal" else self.rows

    def rect(self) -> QRectF:
        """Where it lies, in scene units (y down)."""
        return QRectF(self.west, -self.north, self.width_m, self.height_m)

    def box_rect(self, box: tuple[int, int, int, int]) -> QRectF:
        """A tile box's place in scene units."""
        x0, x1, y0, y1 = box
        tile = self.tile
        return QRectF(
            self.west + x0 * tile,
            -self.north + y0 * tile,
            (x1 - x0 + 1) * tile,
            (y1 - y0 + 1) * tile,
        )


class _WorldGraphics(QGraphicsView):
    place_opened = Signal(str)
    # A double-click on an area's isometric view: see it whole.
    view_opened = Signal(str)
    zoomed = Signal()
    zoom_at = Signal(QPointF)
    dragged = Signal()
    resized = Signal()
    scrolled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        # The middle stays where it is when the map's room changes (the
        # isometric bar over it, the card under it): what is in view stays.
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor(FOG)))
        self.setObjectName("worldMapView")

    def zoom_by(self, factor: float) -> None:
        scale = self.transform().m11() * factor
        if 1e-7 < scale < 400:
            self.scale(factor, factor)
            self.zoomed.emit()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt's name
        steps = event.angleDelta().y() / 120
        if steps:
            self.zoom_by(ZOOM_STEP**steps)
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self.resized.emit()  # the card under the map showing or hiding changes it

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802 - Qt's name
        super().scrollContentsBy(dx, dy)
        self.scrolled.emit()  # a plan dragged into view is drawn then

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if event.button() == Qt.LeftButton:
            self.dragged.emit()  # the author moves the map: no more refitting
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt's name
        item = self.itemAt(event.position().toPoint())
        while item is not None and not isinstance(item, (_PlaceItem, _IsoItem)):
            item = item.parentItem()
        if isinstance(item, _IsoItem):
            self.view_opened.emit(item.area.id)
        elif isinstance(item, _PlaceItem):
            self.place_opened.emit(item.place.id)
        else:
            self.zoom_at.emit(self.mapToScene(event.position().toPoint()))
        event.accept()


class _PersonItem(QGraphicsItem):
    """Someone standing in a scene: their figure (in the scene's pixels, so
    it scales with the ground) and their name over it, the author's
    character ringed at their feet."""

    def __init__(self, name: str, image, *, held: bool) -> None:
        super().__init__()
        self.name = name
        self.image = image
        self.held = held
        self.setZValue(5 if held else 4)
        self.setToolTip(name)

    def boundingRect(self) -> QRectF:
        w, h = self.image.width(), self.image.height()
        return QRectF(-w / 2 - 4, -h - 2, w + 8, h + 6)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        w, h = self.image.width(), self.image.height()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(20, 24, 40, 80))
        painter.drawEllipse(QPointF(0, 0), w / 2, 2)
        if self.held:
            painter.setPen(QPen(QColor("#f6cf6e"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(0, 0), w / 2 + 2, 3)
        painter.drawImage(int(-w / 2), int(-h), self.image)


class _TagItem(QGraphicsSimpleTextItem):
    """A name over a scene: the same size on screen whatever the zoom."""

    def __init__(self, text: str, *, bold: bool = True, colour: str = "#f4f1e8") -> None:
        super().__init__(text)
        font = QFont()
        font.setPointSizeF(text_size.points(8.5))
        font.setBold(bold)
        self.setFont(font)
        self.setBrush(QColor(colour))
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setZValue(6)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.boundingRect().adjusted(-3, -1, 3, 1)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(20, 24, 40, 150))
        painter.drawRoundedRect(rect, 3, 3)
        super().paint(painter, option, widget)

    def centre_on(self, x: float, y: float) -> None:
        """Its middle on (x, y) in its parent's units, wherever the zoom puts the text."""
        self.setPos(x, y)
        rect = self.boundingRect()
        self.setTransformOriginPoint(rect.center())
        self.setTransform(QTransform.fromTranslate(-rect.width() / 2, -rect.height() / 2))


class _IsoItem(QGraphicsItem):
    """An area's isometric view over its plan, in metres: the painting
    covers the plan's rectangle, cut to it where their shapes differ (a view
    is painted near the plan's 5:3, so little is), framed so the edge of the
    area shows: gold while it is of the map as it is, grey once out of date."""

    def __init__(
        self, area: WorldPlace, rect: QRectF, image: QImage, *, current: bool, painted_by: str
    ) -> None:
        super().__init__()
        self.area = area
        self.current = current
        self._rect = rect
        self.pixmap = QPixmap.fromImage(image)
        self.setZValue(ISO_Z)
        said = f"painted from the map by {painted_by}" if painted_by else "a picture of your own"
        old = "" if current else " The map here has changed since it was painted."
        self.setToolTip(
            f"{area.name}: its isometric view, {said}.{old} Double-click to see it whole."
        )

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt's name
        return self._rect

    def source(self) -> QRectF:
        """The part of the painting over the plan: all of it, less what is
        wider (at the sides) or taller (top and bottom) than the plan."""
        width, height = self.pixmap.width(), self.pixmap.height()
        wanted = self._rect.width() / self._rect.height()
        if height and width / height > wanted:
            cut = width - height * wanted
            return QRectF(cut / 2, 0, width - cut, height)
        cut = height - width / wanted
        return QRectF(0, cut / 2, width, height - cut)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(self._rect, self.pixmap, self.source())
        pen = QPen(QColor("#f6cf6e" if self.current else "#9aa0b4"), 2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self._rect)


class _BadgeItem(QGraphicsItem):
    """The mark on an area that has an isometric view, at its plan's top
    left while the views aren't shown, the same size whatever the zoom: so
    the author can see which areas have one before pressing Isometric. A
    click shows them, centred on this area."""

    def __init__(self, area_id: str, text: str, on_click: Callable[[str], None]) -> None:
        super().__init__()
        self.area_id = area_id
        self.text = text
        self._on_click = on_click
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setZValue(ISO_Z)
        self.setCursor(Qt.PointingHandCursor)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setToolTip("This area has an isometric view: click to lay it over the map")
        self._font = QFont()
        self._font.setPointSizeF(text_size.points(8.5))
        self._font.setBold(True)
        metrics = QFontMetricsF(self._font)
        # Inside the plan's corner, a few pixels in.
        self._box = QRectF(6, 6, metrics.horizontalAdvance(text) + 14, metrics.height() + 6)

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt's name
        return self._box.adjusted(-1, -1, 1, 1)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        painter.setPen(QPen(QColor("#f6cf6e"), 1))
        painter.setBrush(QColor(20, 24, 40, 210))
        painter.drawRoundedRect(self._box, 4, 4)
        painter.setFont(self._font)
        painter.setPen(QColor("#f6cf6e"))
        painter.drawText(self._box, Qt.AlignCenter, self.text)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt's name
        event.accept()  # a click on the mark, not the start of a drag

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if self._box.contains(event.pos()):
            self._on_click(self.area_id)
        event.accept()


class WorldView(QWidget):
    back_requested = Signal()
    # The author asked for the isometric view of this area to be painted.
    isometric_requested = Signal(str)
    # A picture of the author's own as this area's isometric view.
    own_isometric_requested = Signal(str)
    # The author asked for this area's plan to be drawn again.
    redraw_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("worldMap")
        self.title = QLabel("Map")
        self.title.setObjectName("emptyTitle")
        self.hint = QLabel()
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)
        self.local_button = self._button("Local", "The place the story is now, close up")
        self.region_button = self._button("Region", "The country around it: where to go next")
        self.world_button = self._button("World", "The whole map: its regions and settlements")
        for button in (self.local_button, self.region_button, self.world_button):
            button.setCheckable(True)
        self.zoom_out = self._button("−", "Zoom out (-, or the mouse wheel)")
        self.zoom_in = self._button("+", "Zoom in (+, or the mouse wheel)")
        self.inside_button = self._button("See inside", "Take every roof off, to see inside")
        self.inside_button.setCheckable(True)
        self.iso_button = self._button("Isometric", ISO_TIP)
        self.iso_button.setCheckable(True)
        self.redraw_button = self._button(
            "Redraw plan…",
            "Draw the plan of the place you are in again, if it came out wrong. You are "
            "asked first; the map art model draws it in the background.",
        )
        self.redraw_button.clicked.connect(self._redraw_requested)
        header = WrapRow()
        header.add_group([self.title])
        header.add_group([self.local_button, self.region_button, self.world_button])
        header.add_group([self.zoom_out, self.zoom_in])
        header.add_group([self.inside_button, self.iso_button, self.redraw_button])
        # How far the drawing has got (the author: the hint's small text said
        # neither that it was under way nor that it was done).
        self.progress_label = QLabel()
        self.progress_label.setObjectName("hintLabel")
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumWidth(220)
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.addWidget(self.progress_label)
        progress_row.addWidget(self.progress, 1)
        progress_row.addStretch(1)
        self.progress_label.hide()
        self.progress.hide()
        # Paused for a private scene (`set_paused`).
        self.pause_note = QLabel(PAUSED_NOTE)
        self.pause_note.setObjectName("mapPaused")
        self.pause_note.setWordWrap(True)
        self.pause_note.hide()
        self.paused = False
        self._drew = False  # a run has finished since this map was shown
        self.view = _WorldGraphics()
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self.view.place_opened.connect(self.go_to)
        self.view.zoom_at.connect(self._zoom_in_at)
        self.view.zoomed.connect(self._zoomed)
        self.view.dragged.connect(self._dragged)
        self.view.resized.connect(self._resized)
        self.view.scrolled.connect(self._scrolled)
        self.zoom_out.clicked.connect(lambda: self.view.zoom_by(1 / ZOOM_STEP))
        self.zoom_in.clicked.connect(lambda: self.view.zoom_by(ZOOM_STEP))
        self.local_button.clicked.connect(lambda: self.go_local())
        self.region_button.clicked.connect(lambda: self.go_region())
        self.world_button.clicked.connect(lambda: self.go_world())
        self.inside_button.toggled.connect(lambda _on: self._update_detail())
        # Isometric lays each area's view over the map; the bar over the map
        # is for the area in view (`area_in_view`).
        self.iso_bar = IsometricBar()
        self.iso_bar.paint_requested.connect(self._paint_requested)
        self.iso_bar.own_requested.connect(self._own_requested)
        self.iso_bar.whole_requested.connect(self._whole_requested)
        self.iso_bar.opacity_changed.connect(self._set_iso_opacity)
        self.iso_bar.hide()
        self.iso_button.toggled.connect(self._iso_toggled)
        self.view.view_opened.connect(self.show_view_whole)
        # A place's card, under the map: what the story said of it, who and
        # what is there. Shown on the local map, for the place in view.
        self.side = QLabel()
        self.side.setObjectName("worldMapSide")
        self.side.setWordWrap(True)
        self.side.setTextFormat(Qt.RichText)
        self.side.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.side.setContentsMargins(0, 0, 6, 0)
        # The place's postcard, when one is drawn: click it to see it whole.
        self.postcard_button = QToolButton()
        self.postcard_button.setObjectName("postcardButton")
        self.postcard_button.setAutoRaise(True)
        self.postcard_button.setToolTip("The place's postcard: click to see it whole")
        self.postcard_button.clicked.connect(self._show_postcard)
        self.postcard_button.hide()
        self._postcard: tuple[str, bytes] | None = None
        inner = QWidget()
        row = QHBoxLayout(inner)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self.postcard_button, 0, Qt.AlignTop)
        row.addWidget(self.side, 1)
        self.card = QScrollArea()
        self.card.setObjectName("worldMapCard")
        self.card.setWidget(inner)
        self.card.setWidgetResizable(True)
        self.card.setFrameShape(QFrame.NoFrame)
        self.card.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.card.setMaximumHeight(130)
        self.card.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(progress_row)
        layout.addWidget(self.pause_note)
        layout.addWidget(self.hint)
        layout.addWidget(self.iso_bar)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.card)
        self.world = WorldMap()
        self.art = WorldArt()
        self.held: str | None = None
        # Reads a map picture's file from the story (the window sets it).
        self.picture_reader: Callable[[str], bytes | None] | None = None
        # Whether the map is being drawn (Settings → Models → Draw the map;
        # the window sets it): off, a plan "not drawn yet" never will be.
        self.art_on = True
        # Whether art is on its way now (the window sets it): said in the hint.
        self.drawing = False
        # The isometric view's model, price and why it can't be painted now
        # (`session.isometric_offer`), and the area being painted (the window
        # sets them).
        self.isometric_offer: tuple[str, float | None, str | None] = ("", None, None)
        self.isometric_painting: str | None = None
        # Each area's view over its plan, the marks on those that have one,
        # and the views read from their files (held scaled, by picture id).
        self._isos: dict[str, _IsoItem] = {}
        self._badges: dict[str, _BadgeItem] = {}
        self._iso_images: dict[str, QImage] = {}
        self._iso_opacity = 1.0
        # The area the isometric bar is about now (`area_in_view`).
        self._focus: str | None = None
        # Every item the scene holds is held here too: PySide lets a scene
        # item go with its Python wrapper, so the land vanished once built.
        self._items: list[_PlaceItem] = []
        self._plans: dict[str, _PlanItem] = {}
        # Plans shown only for a changed state (see `_PlanItem.changed`).
        self._changed_only: set[str] = set()
        # Building plan -> (its parent's plan, the box drawn for it there).
        self._boxes: dict[str, tuple[str, tuple[int, int, int, int]]] = {}
        self._links: list[QGraphicsLineItem] = []
        self._land: list[QGraphicsPixmapItem] = []
        self.people: list[_PersonItem] = []
        self._card_place: WorldPlace | None = None
        # The level last fitted (refitted when the view is resized), None
        # once the author has zoomed or dragged.
        self._fitted: str | None = None
        self._built = False
        self._updating = False
        self._fitting = False
        # The places out of the fog, whose plans may be drawn (`_build`).
        self._open: set[str] = set()
        self._build()

    @staticmethod
    def _button(text: str, tip: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("viewToggleButton")
        button.setText(text)
        button.setToolTip(tip)
        return button

    # --- what is shown ---------------------------------------------------------

    def set_world(
        self, world: WorldMap, art: WorldArt, held: str | None, *, keep_view: bool = True
    ) -> None:
        """Show this map. `keep_view` holds the zoom and place in view (a
        redraw as art comes in, a page flip); off, it opens on the local
        map where the story is now."""
        self.world, self.art, self.held = world, art, held
        center = self.view.mapToScene(self.view.viewport().rect().center())
        transform = self.view.transform()
        fitted = self._fitted
        self._build()
        if keep_view and self._built and fitted is None:
            self.view.setTransform(transform)
            self.view.centerOn(center)
            self._update_detail()
        elif keep_view and self._built:
            # Still at a level the author chose (not zoomed or dragged since):
            # fitted again with what is drawn now, so the local map opens on
            # its plan the moment the plan arrives.
            self._go(fitted)
        else:
            self.go_local()
        self._built = True
        self.sync_isometric()

    def here(self) -> str | None:
        """Where the author's character is: as the story last put them, or an
        imported map's start before it has."""
        return self.world.where.get(self.held or "") or self.world.start

    def _visited(self) -> set[str]:
        visited = set(self.world.visited.get(self.held or "", []))
        if self.world.start is not None and not visited:
            visited.add(self.world.start)
        return visited

    def _shown(self) -> set[str]:
        """What shows through the fog: what the story has seen or told of,
        and where the character has been."""
        been = self._visited() | ({self.here()} - {None})
        # Stand somewhere and you know what it is in and what is in it: the
        # farm from its farmhouse, the farmhouse from the farm.
        been |= containers(self.world, been)
        around = {p.id for p in self.world.places.values() if p.inside in been}
        return set(self.world.seen) | been | around

    def _out_of_the_fog(self) -> set[str]:
        """The places whose plans may be drawn: those shown (`_shown`) and
        what is inside them. A plot file's map can come with plans of places
        the story hasn't reached (a sample's are drawn whole), and showing
        one would lift the fog off it: The Ashen Reach's vox relay showed,
        plan and all, to anyone who moved the map over it."""
        shown = self._shown()
        return {
            pid
            for pid in self.world.places
            if pid in shown or containers(self.world, [pid]) & shown
        }

    def level(self) -> str:
        """Which of LEVELS the zoom is nearest now (see REGION_REACH)."""
        scales = self._fit_scales()
        if scales is None:
            return "region"
        local, region, world = scales
        scale = self.view.transform().m11()
        if scale >= (local * region) ** 0.5:
            return "local"
        if scale <= (region * world) ** 0.5:
            return "world"
        return "region"

    def _around(self, ids) -> QRectF:
        rect = QRectF()
        for pid in ids:
            spot = position(self.world, pid)
            if spot is None:
                continue
            reach = max(size_of(self.world.places[pid]), 120.0)
            rect = rect.united(QRectF(spot[0] - reach, -spot[1] - reach, 2 * reach, 2 * reach))
        return rect

    def _local_plan(self) -> _PlanItem | None:
        area = local_area(self.world, self.here())
        return self._plans.get(area.id) if area is not None else None

    def _rects(self) -> tuple[QRectF, QRectF, QRectF]:
        """What each level fits, in scene units: the local area's plan (or
        a little country round the character), the known places within
        REGION_REACH of them (all of them when that is fewer than two), and
        every known place."""
        here = self.here()
        plan = self._local_plan()
        if plan is not None:
            local = plan.rect()
        else:
            spot = position(self.world, here) if here else None
            if spot is not None:
                reach = max(size_of(self.world.places[here]), 100.0) * 1.5  # type: ignore[index]
                local = QRectF(spot[0] - reach, -spot[1] - reach, 2 * reach, 2 * reach)
            else:
                local = QRectF()
        shown = self._shown()
        world = self._around(shown)
        spot = position(self.world, here) if here else None
        region = world
        if spot is not None:
            near = set()
            for pid in shown:
                there = position(self.world, pid)
                if (
                    there is not None
                    and math.hypot(there[0] - spot[0], there[1] - spot[1]) <= REGION_REACH
                ):
                    near.add(pid)
            if len(near) >= 2:
                region = self._around(near)
        if local.isEmpty():
            local = region
        return local, region, world

    def _scale_for(self, rect: QRectF, pad: float) -> float | None:
        port = self.view.viewport().rect()
        if rect.isEmpty() or port.isEmpty():
            return None
        return min(
            port.width() / (rect.width() * (1 + 2 * pad)),
            port.height() / (rect.height() * (1 + 2 * pad)),
        )

    def _fit_scales(self) -> tuple[float, float, float] | None:
        """The zoom each level fits at: local, region (at most half as
        close), world (at most WORLD_ZOOM_OUT further out than the region)."""
        local_rect, region_rect, world_rect = self._rects()
        local = self._scale_for(local_rect, 0.04)
        region = self._scale_for(region_rect, 0.1)
        world = self._scale_for(world_rect, 0.1)
        if local is None or region is None or world is None:
            return None
        region = min(region, local / 2)
        world = min(world, region / WORLD_ZOOM_OUT)
        return local, region, world

    # --- building the scene ------------------------------------------------------

    def _build(self) -> None:
        # Forget the old items before the scene deletes them: clearing moves
        # the scrollbars, and the scroll handler (`_scrolled`) walked plans
        # Qt had already deleted.
        self._items = []
        self._plans = {}
        self._links = []
        self._land = []
        self._isos = {}
        self._badges = {}
        self.people = []
        self.scene.clear()
        self.scene.setSceneRect(QRectF())  # set below from what is drawn
        if not self.world.places:
            self.title.setText("Map")
            self.hint.setText(
                "Nothing on the map yet: it grows as the story goes, from the next passage on."
            )
            self._sync_buttons()
            return
        self._open = self._out_of_the_fog()
        self._build_land()
        self._build_links()
        self._build_plans()
        self._build_isometric()
        self._build_places()
        bounds = QRectF()
        for item in self.scene.items():
            if (
                isinstance(item, (_PlaceItem, _PlanItem, QGraphicsPixmapItem))
                and item.parentItem() is None
            ):
                bounds = bounds.united(item.sceneBoundingRect())
        if not bounds.isEmpty():
            pad = max(bounds.width(), bounds.height(), 200.0) * 0.5
            self.scene.setSceneRect(bounds.adjusted(-pad, -pad, pad, pad))
        self._update_detail()

    def _build_land(self) -> None:
        # Coarse land first, finer on top: a town's six kilometres under the
        # road's patches.
        for patch in sorted(self.art.terrain, key=lambda p: -p.side):
            if not patch.rows:
                continue
            item = QGraphicsPixmapItem(tiles_pixmap(tuple(patch.rows), patch.ground))
            item.setTransformationMode(Qt.FastTransformation)
            item.setShapeMode(QGraphicsPixmapItem.BoundingRectShape)
            item.setScale(patch.side / (max(len(patch.rows[0]), 1) * TILE))
            item.setPos(patch.x - patch.side / 2, -(patch.y + patch.side / 2))
            item.setZValue(0)
            self.scene.addItem(item)
            self._land.append(item)

    def _build_links(self) -> None:
        world = self.world
        pen = QPen(QColor("#e8dfc4"), 1.5, Qt.DashLine)
        pen.setCosmetic(True)
        shown = self._shown()
        for link in world.links:
            a, b = position(world, link.a), position(world, link.b)
            if a is None or b is None or link.a not in shown or link.b not in shown:
                continue
            line = QGraphicsLineItem(a[0], -a[1], b[0], -b[1])
            line.setPen(pen)
            line.setZValue(1)
            words = ", ".join(w for w in (link.direction, link.distance) if w)
            if words:
                line.setToolTip(
                    f"{world.places[link.a].name} to {world.places[link.b].name}: {words}"
                )
            self.scene.addItem(line)
            self._links.append(line)

    def _build_plans(self) -> None:
        """Every plan drawn, where its place is; its people, props and names
        are added when it is first shown (`_dress_plan`)."""
        for place in self.world.places.values():
            layout = self.art.layout_for(place.id, place.state) or self.art.layout_for(place.id)
            rect = plan_rect(self.world, place) if layout is not None else None
            if layout is None or rect is None or not layout.rows:
                continue
            plan = _PlanItem(place, layout, rect)
            # Inner plans over outer ones: a building's over its farm's.
            plan.setZValue(2 + 0.1 * min(len(containers(self.world, [place.id])), 9))
            plan.setVisible(False)
            self.scene.addItem(plan)
            self._plans[place.id] = plan
        self._fit_in_parents()

    def _area_plans(self) -> dict[str, _PlanItem]:
        """The plans of local areas (the farm, the town: `local_area`), the
        places an isometric view is of."""
        out = {}
        for pid, plan in self._plans.items():
            area = local_area(self.world, pid)
            if area is not None and area.id == pid and pid in self._open:
                out[pid] = plan
        return out

    def _build_isometric(self) -> None:
        """Each local area's latest isometric view over its plan, shown while
        Isometric is on, and the mark saying it has one, shown while it is
        off (`_update_detail_now`)."""
        held: dict[str, QImage] = {}
        for pid, plan in self._area_plans().items():
            picture = self.art.picture_for("isometric", pid)
            image = self._iso_image(picture) if picture is not None else None
            if picture is None or image is None:
                continue
            held[picture.id] = image
            now = self.art.picture_for("isometric", pid, isometric_state(self.world, pid))
            current = now is picture
            view = _IsoItem(
                plan.place, plan.rect(), image, current=current, painted_by=picture.model
            )
            view.setOpacity(self._iso_opacity)
            view.setVisible(False)
            self.scene.addItem(view)
            self._isos[pid] = view
            mark = f"{ISO_MARK} Isometric view" + ("" if current else " · out of date")
            badge = _BadgeItem(pid, mark, self._badge_clicked)
            badge.setPos(plan.rect().topLeft())
            badge.setVisible(False)
            self.scene.addItem(badge)
            self._badges[pid] = badge
        # Only the views in use stay held: one painted again replaces its old one.
        self._iso_images = held

    def _iso_image(self, picture: MapPicture) -> QImage | None:
        """An isometric view's picture, held no wider than ISO_HELD_WIDTH;
        None when its file can't be read."""
        image = self._iso_images.get(picture.id)
        if image is None:
            data = self.picture_reader(picture.file) if self.picture_reader else None
            image = image_of(data)
            if image.width() > ISO_HELD_WIDTH:
                image = image.scaledToWidth(ISO_HELD_WIDTH, Qt.SmoothTransformation)
            self._iso_images[picture.id] = image
        return None if image.isNull() else image

    def _fit_in_parents(self) -> None:
        """Each building's plan over the box its parent's plan drew for it
        (found by its name there), and that box kept as the one to open
        (`_boxes`). Live, a farm plan drawn when a building's box was at
        least 5 tiles wide left a strip of roof beside the barn once the
        program asked for 4: the box drawn is the one that counts."""
        self._boxes = {}
        world = self.world
        depth = {pid: len(containers(world, [pid])) for pid in self._plans}
        for pid in sorted(self._plans, key=lambda p: depth[p]):
            plan = self._plans[pid]
            parent = self._plans.get(plan.place.inside or "")
            if parent is None or plan.place.kind != "building":
                continue
            box = drawn_box(parent.layout, plan.place)
            if box is None:
                continue
            plan.fit_to(parent.box_rect(box))
            self._boxes[pid] = (parent.place.id, box)

    def _build_places(self) -> None:
        world = self.world
        shown = self._shown()
        visited, here = self._visited(), self.here()
        # A place names everyone in it, or in anything inside it: the farm
        # says "John" while its farmhouse is too small to show.
        people: dict[str, list[str]] = {}
        for who, place_id in world.where.items():
            for container in (place_id, *containers(world, [place_id])):
                people.setdefault(container, []).append(who)
        for place in world.places.values():
            spot = position(world, place.id)
            if spot is None or place.id not in shown:
                continue
            painted = place.id in self._isos
            item = _PlaceItem(
                place,
                self._picture_for(place),
                people.get(place.id, []),
                visited=place.id in visited,
                current=place.id == here,
                painted=painted,
            )
            item.setPos(spot[0], -spot[1])
            item.setToolTip(
                place_card(world, place)
                + (
                    f"<br><br>{ISO_MARK} It has an isometric view: Isometric, close up."
                    if painted
                    else ""
                )
            )
            self.scene.addItem(item)
            self._items.append(item)

    def _picture_for(self, place: WorldPlace) -> Callable[[int], QPixmap | None]:
        """How to get a place's picture at a size: the image model's, else
        its sprite, else nothing (a mark)."""

        def picture(pixels: int) -> QPixmap | None:
            pixmap = self._picture_pixmap("place", place.id, place.state, pixels)
            if pixmap is not None:
                return pixmap
            sprite = self.art.sprite_for(place.id, place.state) or self.art.sprite_for(place.id)
            return sprite_pixmap(sprite.id, sprite.svg, pixels) if sprite else None

        return picture

    # --- the level of detail -----------------------------------------------------

    def _zoomed(self) -> None:
        self._fitted = None
        self._update_detail()

    def _update_detail(self) -> None:
        """What is drawn at this zoom: plans once wide enough on screen, a
        building's inside once its footprint is, places as pictures where no
        plan carries them, and, zoomed out past the region, the big names."""
        if self._updating:
            return  # centerOn inside a fit scrolls, which lands here again
        self._updating = True
        try:
            self._update_detail_now()
        finally:
            self._updating = False

    def _update_detail_now(self) -> None:
        scale = self.view.transform().m11()
        inside = self.inside_button.isChecked()
        places = self.world.places
        level = self.level()
        # Only plans near the view are drawn (and dressed): the town's,
        # five miles off, is not, however the zoom would read it.
        port = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        near = port.adjusted(-port.width(), -port.height(), port.width(), port.height())
        shown_plans: set[str] = set()
        # Shown only because the story changed them: nobody stands inside.
        self._changed_only: set[str] = set()
        for pid, plan in self._plans.items():
            if level != "local" or not plan.rect().intersects(near):
                continue  # the region and the world draw places, not plans
            if pid not in self._open:
                continue  # still in the fog
            on_screen = plan.width_m * scale
            is_inside = plan.place.kind == "building"
            if is_inside:
                parent = self._plans.get(plan.place.inside or "")
                parent_shown = parent is not None and parent.width_m * scale >= PLAN_MIN_PX
                show = on_screen >= INTERIOR_SHOWN_AT or (
                    inside and (parent_shown or on_screen >= PLAN_MIN_PX)
                )
                if not show and plan.changed and parent_shown:
                    show = True
                    self._changed_only.add(pid)
            else:
                show = on_screen >= PLAN_MIN_PX
            if show:
                shown_plans.add(pid)
        # An area's roofs come off where an inside is shown; the picture of
        # a building standing on it goes with the roof.
        for pid, plan in self._plans.items():
            show = pid in shown_plans
            if show:
                ground = self._ground_for(plan.place, str(plan.layout.spec.get("ground", "")))
                # A deck has no roofs: its rooms are under the one hull.
                roofs = not inside and plan.place.kind != "building" and ground != "metal"
                fixed = {
                    f["place_id"]: (f["x"], f["x"] + f["w"] - 1, f["y"], f["y"] + f["h"] - 1)
                    for f in fixed_in_plan(self.world, plan.place)
                }
                # The boxes this plan actually drew win (`_fit_in_parents`).
                fixed.update({c: box for c, (par, box) in self._boxes.items() if par == pid})
                opened = tuple(sorted(box for child, box in fixed.items() if child in shown_plans))
                fronts = tuple(
                    sorted(box for child, box in fixed.items() if child in self._changed_only)
                )
                dressed = plan.childItems()
                if (
                    not dressed
                    or plan.roofs != roofs
                    or plan.cleared != opened
                    or plan.fronts != fronts
                    or plan.ground != ground
                ):
                    self._dress_plan(plan, ground, roofs, opened, fronts)
            plan.setVisible(show)
        # Each area's view over its plan with Isometric on; its mark with it off.
        iso_on = self.iso_button.isChecked()
        for pid, view in self._isos.items():
            view.setVisible(iso_on and pid in shown_plans)
        for pid, badge in self._badges.items():
            badge.setVisible(not iso_on and pid in shown_plans)
        here = self.here()
        for item in self._items:
            place = item.place
            carried = place.id in shown_plans or (place.inside or "") in shown_plans
            if level == "world":
                visible = place.kind in WORLD_KINDS and not carried
            else:
                visible = not carried
                parent = places.get(place.inside or "")
                while parent is not None and visible:
                    if position(self.world, parent.id) is not None:
                        visible = size_of(parent) * scale >= CHILD_SHOWN_AT
                    parent = places.get(parent.inside or "")
            item.setVisible(visible)
            item.set_pixels(int(min(max(size_of(place) * scale, MIN_PLACE_PX), MAX_PLACE_PX)))
            item.set_big(level == "world")
        # The ring: on the character's place, or the nearest shown place it is in.
        shown_ids = {item.place.id for item in self._items if item.isVisible()}
        ringed: str | None = None
        current = here
        seen: set[str] = set()
        while current is not None and current not in seen:
            seen.add(current)
            if current in shown_ids:
                ringed = current
                break
            current = places[current].inside if current in places else None
        if ringed is None and here is not None:  # nothing above it shown: itself, then
            own = next((item for item in self._items if item.place.id == here), None)
            in_plan = here in shown_plans or (places[here].inside or "") in shown_plans
            if own is not None and not in_plan:  # a plan carrying it stands for it
                own.setVisible(True)
                ringed = here
        for item in self._items:
            item.set_ringed(item.place.id == ringed)
        self._sync_buttons()
        self._sync_card(level, shown_plans)
        focus = self.area_in_view(level)
        if (focus.id if focus is not None else None) != self._focus:
            self.sync_isometric()  # the bar follows the area in view

    def _dress_plan(
        self,
        plan: _PlanItem,
        ground: str,
        roofs: bool,
        opened: tuple[tuple[int, int, int, int], ...],
        fronts: tuple[tuple[int, int, int, int], ...] = (),
    ) -> None:
        """Render a plan and stand everything in it: pictured buildings on
        cleared ground, the marked things, names, and the people."""
        plan.fronts = fronts
        for child in list(plan.childItems()):
            self.scene.removeItem(child)
        plan.dressed = []
        self.people = [p for p in self.people if p.parentItem() is not plan]
        layout = plan.layout
        plan.ground = ground
        rows = plan.drawn_rows()
        pictured = self._pictured_buildings(rows, layout.labels, opened) if roofs else {}
        cleared = tuple(sorted({*opened, *pictured}))
        plan.render(ground, roofs, cleared)
        for (x0, x1, _y0, y1), image in pictured.items():
            building = QGraphicsPixmapItem(QPixmap.fromImage(image), plan)
            building.setTransformationMode(Qt.FastTransformation)
            building.setPos(
                (x0 + x1 + 1) * TILE / 2 - image.width() / 2, (y1 + 1) * TILE - image.height()
            )
            building.setZValue(2)
            plan.dressed.append(building)
        houses = building_boxes(rows)
        roofed = [b for b in houses if b not in opened] if roofs else []
        self._marked(plan, rows, layout.objects, roofed)
        spots = {_key(name): (x, y) for name, (x, y) in layout.labels.items()}
        stood = self._stand(plan, self._people_in(plan.place.id), spots, [*roofed, *fronts], rows)
        own = {_key(n) for n in (plan.place.name, *plan.place.aliases)}
        for name, (x, y) in layout.labels.items():
            if _key(name) in own:
                continue  # the plan's own place: its parent or the card names it
            tag = _TagItem(name.split("(")[0].strip())
            tag.setParentItem(plan)
            plan.dressed.append(tag)
            box = next((b for b in houses if b[0] <= x <= b[1] and b[2] <= y <= b[3]), None)
            if box is not None:  # a building's name on its roof, or its far wall
                x, y = (box[0] + box[1]) / 2, box[2] + (0.6 if box in roofed else -0.4)
            elif (x, y) in stood:  # people stand here: the name goes under their feet
                y += 1.6
            tag.centre_on(x * TILE + TILE / 2, y * TILE)

    def _ground_for(self, place: WorldPlace, ground: str) -> str:
        """The look a plan is drawn in (`world_art.plan_ground`)."""
        return plan_ground(self.world, self.art, place, ground)

    def _people_in(self, place_id: str) -> list[tuple[str, str]]:
        """(who, where) for everyone the story has at this place or somewhere
        inside it, but not in a place inside it whose own plan is shown (they
        stand in that). Before a passage has placed the author's character,
        they are at an imported map's start, as `here()` has them. Nobody
        stands in a building drawn open only for its changed state: they are
        at its front, in the plan around it."""
        if place_id in self._changed_only:
            return []
        where = dict(self.world.where)
        here = self.here()
        if self.held and self.held not in where and here is not None:
            where[self.held] = here
        out = []
        for who, at in where.items():
            seen: set[str] = set()
            spot = at
            while spot is not None and spot not in seen:
                if spot == place_id:
                    out.append((who, at))
                    break
                seen.add(spot)
                plan = self._plans.get(spot)
                if (
                    plan is not None
                    and plan.isVisible()
                    and spot != at
                    and spot not in self._changed_only
                ):
                    break  # stands in that plan, drawn closer
                spot = self.world.places[spot].inside if spot in self.world.places else None
        return out

    def _marked(
        self,
        plan: _PlanItem,
        rows: tuple[str, ...],
        objects: dict[str, str],
        roofed: list[tuple[int, int, int, int]],
    ) -> None:
        """Each numbered thing in a plan as a small prop, its name on hover;
        not what is under a roof (See inside shows it)."""
        for y, row in enumerate(rows):
            for x, ch in enumerate(row):
                if not ch.isdigit():
                    continue
                if any(b[0] <= x <= b[1] and b[2] <= y <= b[3] for b in roofed):
                    continue
                prop = QGraphicsPixmapItem(QPixmap.fromImage(prop_sprite(int(ch))), plan)
                prop.setTransformationMode(Qt.FastTransformation)
                prop.setPos(x * TILE + 2, y * TILE + 2)
                prop.setZValue(3)
                prop.setToolTip(objects.get(ch, "something marked here"))
                plan.dressed.append(prop)

    def _stand(
        self,
        plan: _PlanItem,
        people: list[tuple[str, str]],
        spots: dict[str, tuple[int, int]],
        roofed: list[tuple[int, int, int, int]],
        rows: tuple[str, ...],
    ) -> set[tuple[int, int]]:
        """Everyone where the plan marks their place; at a roofed building,
        or one drawn open for its changed state (`roofed` has both), at its
        front; otherwise in the middle. Several in one spot stand side by
        side."""
        width = max((len(r) for r in rows), default=1)
        groups: dict[tuple[float, float], list[str]] = {}
        # The labelled tiles people stand on, for `_dress_plan` to move the
        # place's name off them.
        standing: dict[tuple[int, int], list[str]] = {}
        for who, at in people:
            place = self.world.places.get(at)
            names = [place.name, *place.aliases] if place is not None else []
            spot = next((spots[_key(n)] for n in names if _key(n) in spots), None)
            if spot is None:
                x, y = width * TILE / 2, len(rows) * TILE / 2
            else:
                tx, ty = spot
                box = next((b for b in roofed if b[0] <= tx <= b[1] and b[2] <= ty <= b[3]), None)
                if box is not None:  # roofed over: they stand at its front
                    ty = box[3] + 1
                if box is None:
                    standing.setdefault(spot, []).append(who)
                    # A label on space or a wall would stand them there.
                    tx, ty = standing_spot(rows, (tx, ty))
                x, y = tx * TILE + TILE / 2, ty * TILE + TILE - 2
            groups.setdefault((x, y), []).append(who)
        for (x, y), names in groups.items():
            names.sort(key=lambda n: n != self.held)
            tags = [
                _TagItem(name.split()[0] if name.split() else name, bold=name == self.held)
                for name in names
            ]
            # Side by side by their names' width: live, two officers' names
            # ran into each other at 14px apart.
            step = max(14.0, *(tag.boundingRect().width() + 4 for tag in tags))
            for i, (name, tag) in enumerate(zip(names, tags, strict=True)):
                offset = (i - (len(names) - 1) / 2) * step
                avatar = self._picture_image("person", name, None, AVATAR_HEIGHT)
                figure = _PersonItem(name, avatar or person_sprite(name), held=name == self.held)
                figure.setParentItem(plan)
                figure.setPos(x + offset, y)
                self.people.append(figure)
                plan.dressed.append(figure)
                tag.setParentItem(plan)
                tag.centre_on(x + offset, y - figure.image.height() - 6)
                plan.dressed.append(tag)
        return {spot for spot, _names in standing.items()}

    # --- the card --------------------------------------------------------------------

    def _sync_card(self, level: str, shown_plans: set[str]) -> None:
        """On the local map, the innermost plan under the middle of the view
        (else the character's) has its card shown; otherwise none."""
        place: WorldPlace | None = None
        if level == "local":
            centre = self.view.mapToScene(self.view.viewport().rect().center())
            inner = [
                self._plans[pid] for pid in shown_plans if self._plans[pid].rect().contains(centre)
            ]
            if inner:
                place = min(inner, key=lambda p: p.width_m).place
            else:
                area = local_area(self.world, self.here())
                place = area if area is not None and area.id in shown_plans else None
        if place is None:
            self.title.setText("Map")
            self.card.hide()
            self.postcard_button.hide()
            self._card_place = None
            return
        if (
            self._card_place is not None
            and self._card_place.id == place.id
            and self.card.isVisible()
        ):
            return
        self._card_place = place
        world = self.world
        self.title.setText(place.name + (f" ({place.state})" if place.state else ""))
        side = [place_card(world, place)]
        layout = self.art.layout_for(place.id, place.state) or self.art.layout_for(place.id)
        if layout is not None and layout.objects:
            side.append(
                "<b>Marked</b><br>"
                + "<br>".join(f"{n}. {escape(v)}" for n, v in layout.objects.items())
            )
        inside = [p for p in world.places.values() if p.inside == place.id]
        if inside:
            side.append("<b>In it</b><br>" + ", ".join(escape(p.name) for p in inside))
        carried = [
            t
            for t in world.things.values()
            if t.carried_by and world.where.get(t.carried_by) == place.id
        ]
        if carried:
            side.append(
                "<b>Carried here</b><br>"
                + "<br>".join(escape(f"{t.name}, by {t.carried_by}") for t in carried)
            )
        self.side.setText("<br><br>".join(side))
        self._set_postcard(place)
        self.card.show()

    def _set_postcard(self, place: WorldPlace) -> None:
        picture = self.art.picture_for("postcard", place.id, place.state) or self.art.picture_for(
            "postcard", place.id
        )
        data = self.picture_reader(picture.file) if picture and self.picture_reader else None
        image = QImage.fromData(data) if data else QImage()
        if image.isNull():
            self._postcard = None
            self.postcard_button.hide()
            return
        self._postcard = (place.name, data)
        width, height = POSTCARD_THUMB
        thumb = image.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.postcard_button.setIcon(QPixmap.fromImage(thumb))
        self.postcard_button.setIconSize(thumb.size())
        self.postcard_button.show()

    def _show_postcard(self) -> None:
        if self._postcard is None:
            return
        name, data = self._postcard
        dialog = QDialog(self)
        dialog.setWindowTitle(name)
        label = QLabel()
        image = QImage.fromData(data)
        screen = self.screen()
        if screen is not None:
            room = screen.availableGeometry()
            image = image.scaled(
                int(room.width() * 0.8),
                int(room.height() * 0.8),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        label.setPixmap(QPixmap.fromImage(image))
        layout = QVBoxLayout(dialog)
        layout.addWidget(label)
        dialog.exec()

    # --- pictures ---------------------------------------------------------------------

    def _picture_image(self, subject: str, subject_id: str, state: str | None, height: int):
        """A map picture, cut out and shrunk to `height`: in this state, else
        the latest. None when there is none (or its file can't be read)."""
        picture = self.art.picture_for(subject, subject_id, state) or self.art.picture_for(
            subject, subject_id
        )
        if picture is None or self.picture_reader is None:
            return None
        reader, file = self.picture_reader, picture.file
        return picture_sprite(picture.id, height, lambda: reader(file))

    def _picture_pixmap(self, subject: str, subject_id: str, state: str, size: int):
        image = self._picture_image(subject, subject_id, state, size)
        if image is None:
            return None
        if image.width() > size:  # a wide building: it fits the same square
            image = image.scaledToWidth(size, Qt.SmoothTransformation)
        return QPixmap.fromImage(image)

    def _place_named(self, name: str) -> WorldPlace | None:
        key = _key(name)
        for place in self.world.places.values():
            if key in {_key(n) for n in (place.name, *place.aliases)}:
                return place
        return None

    def _pictured_buildings(
        self, rows: tuple[str, ...], labels: dict, opened: tuple[tuple[int, int, int, int], ...]
    ) -> dict:
        """Each building in the plan the story's place has a picture of: its
        box, and the picture shrunk to stand on it. Not one whose inside is
        shown."""
        found: dict = {}
        boxes = building_boxes(rows)
        for name, (x, y) in labels.items():
            place = self._place_named(name)
            box = next((b for b in boxes if b[0] <= x <= b[1] and b[2] <= y <= b[3]), None)
            if place is None or box is None or box in found or box in opened:
                continue
            x0, x1, y0, y1 = box
            height = (y1 - y0 + 2) * TILE
            image = self._picture_image("place", place.id, place.state, height)
            if image is None:
                continue
            widest = (x1 - x0 + 3) * TILE
            if image.width() > widest:
                image = image.scaledToWidth(widest, Qt.SmoothTransformation)
            found[box] = image
        return found

    # --- moving about ------------------------------------------------------------

    def _sync_buttons(self) -> None:
        level = self.level() if self.world.places else "region"
        for name, button in (
            ("local", self.local_button),
            ("region", self.region_button),
            ("world", self.world_button),
        ):
            button.setChecked(level == name)
        self.inside_button.setVisible(level == "local")
        area = self.area_in_view(level)
        self.iso_button.setVisible(self.iso_button.isChecked() or area is not None)
        if area is not None:
            has = area.id in self._isos
            self.iso_button.setToolTip(
                ISO_TIP
                + (
                    f" {area.name} has one ({ISO_MARK} on the map marks each area that does)."
                    if has
                    else f" {area.name} has none yet: paint one, or use a picture of your own."
                )
            )
        self.redraw_button.setVisible(
            area is not None and not self.iso_button.isChecked() and not self.paused
        )
        self.redraw_button.setEnabled(not self.drawing)
        self.hint.setText(self._hint_for(level))

    # --- the isometric views ---------------------------------------------------------

    def area_in_view(self, level: str | None = None) -> WorldPlace | None:
        """The local area the isometric bar and Redraw plan act on, on the
        local map: the one whose plan is under the middle of the view, else
        the nearest whose plan is in sight. None on the region or the world,
        or with no area's plan in sight. (It was always the character's own:
        looking at the Ashcombe Works, Isometric showed Tinker's Row.)"""
        if not self.world.places or (level or self.level()) != "local":
            return None
        port = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        centre = port.center()
        nearest: tuple[float, WorldPlace] | None = None
        for plan in self._area_plans().values():
            rect = plan.rect()
            if rect.contains(centre):
                return plan.place
            if not rect.intersects(port):
                continue
            middle = rect.center()
            distance = math.hypot(middle.x() - centre.x(), middle.y() - centre.y())
            if nearest is None or distance < nearest[0]:
                nearest = (distance, plan.place)
        return nearest[1] if nearest is not None else None

    def sync_isometric(self) -> None:
        """The bar for the area in view, while the views are shown: its view
        and whether it is of the map as it is now, and the offer."""
        on = self.iso_button.isChecked()
        area = self.area_in_view()
        self._focus = area.id if area is not None else None
        self.inside_button.setEnabled(not on)
        self.iso_bar.setVisible(on and area is not None)
        if not on or area is None:
            if self.world.places:
                self.hint.setText(self._hint_for(self.level()))
            return
        latest = self.art.picture_for("isometric", area.id)
        has_view = latest is not None and self._iso_image(latest) is not None
        now = self.art.picture_for("isometric", area.id, isometric_state(self.world, area.id))
        model, price, problem = self.isometric_offer
        self.iso_bar.show_state(
            place=area.name,
            has_view=has_view,
            current=now is not None and now is latest,
            painting=self.isometric_painting == area.id,
            model=model,
            price=price,
            problem=problem,
            painted_by=latest.model if latest is not None else "",
        )
        self.hint.setText(self._hint_for("local"))

    def leave_isometric(self) -> bool:
        """The views off, back to the 2D map; False when they weren't on."""
        if not self.iso_button.isChecked():
            return False
        self.iso_button.setChecked(False)
        return True

    def _iso_toggled(self, _on: bool) -> None:
        self._update_detail()
        self.sync_isometric()

    def _set_iso_opacity(self, opacity: float) -> None:
        self._iso_opacity = opacity
        for view in self._isos.values():
            view.setOpacity(opacity)

    def _badge_clicked(self, area_id: str) -> None:
        """A mark clicked: that area, with the views over the map."""
        self.go_to(area_id)
        self.iso_button.setChecked(True)

    def show_view_whole(self, area_id: str) -> None:
        """An area's view on its own, read whole from its file."""
        area = self.world.places.get(area_id)
        picture = self.art.picture_for("isometric", area_id)
        if area is None or picture is None or self.picture_reader is None:
            return
        image = image_of(self.picture_reader(picture.file))
        if image.isNull():
            return
        IsometricDialog(area.name, image, picture.model, self).exec()

    def _whole_requested(self) -> None:
        area = self.area_in_view()
        if area is not None:
            self.show_view_whole(area.id)

    def _redraw_requested(self) -> None:
        area = self.area_in_view()
        if area is not None:
            self.redraw_requested.emit(area.id)

    def _paint_requested(self) -> None:
        area = self.area_in_view()
        if area is not None:
            self.isometric_requested.emit(area.id)

    def _own_requested(self) -> None:
        area = self.area_in_view()
        if area is not None:
            self.own_isometric_requested.emit(area.id)

    def _hint_for(self, level: str) -> str:
        if not self.world.places:
            return "Nothing on the map yet: it grows as the story goes, from the next passage on."
        if level == "local" and self.iso_button.isChecked() and self.iso_bar.isVisibleTo(self):
            return self.iso_bar.note  # what the view in sight is
        if level == "local":
            return (
                "North at the top. Zoom in on a building to see inside it; hover a marked "
                "thing for what it is. Zoom out for the region."
            )
        if level == "world":
            return "The whole map: its settlements and regions. Zoom in for the country."
        hint = (
            "The country the story has reached. Lit: where you've been; dimmed: places the "
            "story told of; ringed: where you are now. Double-click a place to go to it; "
            "zoom in for the local map."
        )
        if self._local_plan() is None:
            if self.drawing:
                hint += " The local map is being drawn; it opens when its plan is in."
            elif self.art_on:
                hint += " The local map isn't drawn yet: it is, the first time the story is there."
            else:
                hint += " The local map isn't drawn: Draw the map is off (Settings → Models)."
        return hint

    def set_paused(self, paused: bool) -> None:
        """A private scene is open: the map stands as the story was when it
        began, and says so."""
        self.pause_note.setVisible(paused)
        self.paused = paused
        if self.world.places:
            self._sync_buttons()

    def set_progress(self, done: int, total: int) -> None:
        """The drawing under way: `done` of `total` pieces (art and
        pictures); nothing under way when `total` is 0, said as drawn once a
        run has finished. Cheap: nothing is redrawn."""
        drawing = total > 0
        if drawing:
            self._drew = True
            self.progress_label.setText(f"Drawing the map: {done} of {total}")
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        else:
            self.progress_label.setText("✓ The map is drawn")
        self.progress.setVisible(drawing)
        self.progress_label.setVisible(drawing or (self._drew and self.art_on))
        self.redraw_button.setEnabled(not drawing)
        if drawing != self.drawing:
            self.drawing = drawing
            if self.world.places:
                self.hint.setText(self._hint_for(self.level()))

    def _show_at(self, centre: QPointF, scale: float) -> None:
        """The map at `scale` (screen pixels a metre), `centre` in the middle.
        One transform, set once: changing it moves the scrollbars, which
        resizes the viewport, which refits (`_resized`) from inside this very
        call; reset-then-scale left the view scaled twice over."""
        if self._fitting:
            return
        self._fitting = True
        try:
            self.view.setTransform(QTransform.fromScale(scale, scale))
            self.view.centerOn(centre)
        finally:
            self._fitting = False
        self._update_detail()

    def _fit(self, rect: QRectF, pad: float = 0.1) -> None:
        scale = self._scale_for(rect, pad)
        if scale is None:
            return
        self._show_at(rect.center(), scale)

    def _go(self, level: str | None) -> None:
        if level == "local":
            self.go_local()
        elif level == "region":
            self.go_region()
        elif level == "world":
            self.go_world()

    def go_local(self) -> None:
        """The local map where the story is now: the area's plan fitted. With
        no plan drawn yet there is nothing to see close up, so the region
        shows until it is (the hint says so), and then this opens on it."""
        self._fitted = "local"
        scales = self._fit_scales()
        if scales is None:
            return
        local_rect, region_rect, _world = self._rects()
        plan = self._local_plan()
        if plan is None:
            self._show_at(region_rect.center(), scales[1])
            return
        scale = scales[0]
        # Crisp: a whole number of screen pixels to each pixel of the art.
        k = scale * plan.tile / TILE
        if k >= 2:
            scale = int(k) * TILE / plan.tile
        self._show_at(local_rect.center(), scale)

    def go_region(self) -> None:
        """The country round the character: where to go next."""
        self._fitted = "region"
        scales = self._fit_scales()
        if scales is None:
            return
        _local, region_rect, _world = self._rects()
        self._show_at(region_rect.center(), scales[1])

    def go_world(self) -> None:
        """The whole map, zoomed out past the region so only the big names show."""
        self._fitted = "world"
        scales = self._fit_scales()
        if scales is None:
            return
        _local, _region, world_rect = self._rects()
        self._show_at(world_rect.center(), scales[2])

    def go_to(self, place_id: str) -> None:
        """A place in view: its plan when it has one, else close around it."""
        self._fitted = None
        plan = self._plans.get(place_id)
        if plan is not None:
            self._fit(plan.rect(), 0.04)
            return
        spot = position(self.world, place_id)
        if spot is None:
            return
        reach = max(size_of(self.world.places[place_id]), 60.0) * 1.5
        self._fit(QRectF(spot[0] - reach, -spot[1] - reach, 2 * reach, 2 * reach), 0.0)

    def _dragged(self) -> None:
        self._fitted = None

    def _scrolled(self) -> None:
        if self._built and self.world.places and not self._updating:
            self._update_detail()

    def _resized(self) -> None:
        """The map's own size changed (the window, or the card under it):
        a level fitted is fitted again to it."""
        if self._fitted is not None and self._built and not self._fitting:
            self._go(self._fitted)

    def _zoom_in_at(self, point: QPointF) -> None:
        self._fitted = None
        self.view.zoom_by(2.0)
        self.view.centerOn(point)
        self._update_detail()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if (
            event.key() in (Qt.Key_Plus, Qt.Key_Equal)
            and not event.modifiers() & Qt.ControlModifier
        ):
            self.view.zoom_by(ZOOM_STEP)
        elif event.key() == Qt.Key_Minus and not event.modifiers() & Qt.ControlModifier:
            self.view.zoom_by(1 / ZOOM_STEP)
        else:
            super().keyPressEvent(event)


def visible_names(view: WorldView) -> list[str]:
    """The places drawn as pictures on the map now (for tests)."""
    return sorted(item.place.name for item in view._items if item.isVisible())


def shown_plans(view: WorldView) -> list[str]:
    """The plans drawn now, by place name (for tests)."""
    return sorted(plan.place.name for plan in view._plans.values() if plan.isVisible())


__all__ = ["WorldView", "place_card", "shown_plans", "visible_names"]
