"""The area the author's character is in, to play on (map-first play;
SealedLoreDocs design/map-first.md §3): the area drawn in its tile set
(gui/area_render.py), everyone standing where the map has them, and the
author's character walking where the author clicks, with no model call.

Clicking a person offers Talk, Look and Attack; a thing, what can be done
with it (Use, Take, Eat, Destroy). A choice fills the composer with the
turn it means (the author's ruling: so the prompts are easy to test), and
the author's character walks up first, unless they are only watching. A
door is walked through by walking into it; Travel… sets off for another
place, the way's length on foot. The view only shows and asks: the window
(gui/window_area.py) does the moving through the session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QLabel,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.area import Area, Placement, seen_tiles, sees
from sealedlore.gui.area_render import px_for
from sealedlore.gui.wrap_row import WrapRow

# Tiles a second the author's character walks on screen.
STEPS_PER_SECOND = 9
# A person's sprite, in tiles tall, indoors and out.
PERSON_TILES = {True: 2.1, False: 2.6}
VERB_LABELS = {
    "talk": "Talk",
    "look": "Look (stay out of sight and watch)",
    "attack": "Attack",
    "use": "Use",
    "take": "Take",
    "eat": "Eat",
    "destroy": "Destroy",
}
ZOOM_STEP = 1.25
WALK_KEYS = {
    Qt.Key_Left: (-1, 0),
    Qt.Key_A: (-1, 0),
    Qt.Key_Right: (1, 0),
    Qt.Key_D: (1, 0),
    Qt.Key_Up: (0, -1),
    Qt.Key_W: (0, -1),
    Qt.Key_Down: (0, 1),
    Qt.Key_S: (0, 1),
    Qt.Key_Plus: None,
    Qt.Key_Equal: None,
    Qt.Key_Minus: None,
    Qt.Key_Home: None,
}


@dataclass
class Walker:
    """Someone standing in the area."""

    name: str
    x: int
    y: int
    image: QImage | None
    held: bool = False


class _Graphics(QGraphicsView):
    clicked = Signal(QPointF, QPoint)
    # A right-click: what can be done at a spot (mark it, walk there).
    context = Signal(QPointF, QPoint)
    hovered = Signal(QPointF)
    zoomed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setBackgroundBrush(QColor(12, 12, 16))
        self._press: QPoint | None = None
        self._dragging = False

    resized = Signal()

    def zoom_by(self, factor: float) -> None:
        scale = self.transform().m11() * factor
        if 0.15 <= scale <= 6.0:
            self.scale(factor, factor)
            self.zoomed.emit()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self.resized.emit()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt's name
        self.zoom_by(ZOOM_STEP if event.angleDelta().y() > 0 else 1 / ZOOM_STEP)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if event.button() in (Qt.LeftButton, Qt.RightButton):
            self._press = event.position().toPoint()
            self._dragging = False
            self._button = event.button()
        if event.button() == Qt.MiddleButton:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt's name
        self.hovered.emit(self.mapToScene(event.position().toPoint()))
        if self._press is not None and event.buttons() & Qt.LeftButton:
            delta = event.position().toPoint() - self._press
            if self._dragging or delta.manhattanLength() > 8:
                # Dragging moves the view, as on the region map.
                self._dragging = True
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
                self._press = event.position().toPoint()
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt's name
        # Walking and zoom keys are the play view's, not the scroll bars'.
        if event.key() in WALK_KEYS:
            event.ignore()
            return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if self._press is not None and not self._dragging:
            signal = self.context if event.button() == Qt.RightButton else self.clicked
            signal.emit(
                self.mapToScene(event.position().toPoint()), event.globalPosition().toPoint()
            )
        self._press = None
        self._dragging = False
        self.setDragMode(QGraphicsView.NoDrag)
        super().mouseReleaseEvent(event)


class _Person(QGraphicsItem):
    """A person on the map: their sprite, standing on their tile, their name
    over them."""

    def __init__(self, walker: Walker, px: int, indoor: bool) -> None:
        super().__init__()
        self.walker = walker
        self.px = px
        height = int(px * PERSON_TILES[indoor])
        image = walker.image
        self.pixmap = (
            QPixmap.fromImage(image.scaledToHeight(height, Qt.SmoothTransformation))
            if image is not None and not image.isNull()
            else None
        )
        self.height = height
        self.width = self.pixmap.width() if self.pixmap else int(height * 0.6)
        self.setZValue(10 + walker.y)
        self.setAcceptHoverEvents(False)
        self.place_at(walker.x, walker.y)

    def place_at(self, x: float, y: float) -> None:
        self.setPos((x + 0.5) * self.px, (y + 1.0) * self.px)
        self.setZValue(10 + y)

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt's name
        return QRectF(-self.width / 2 - 2, -self.height - 4, self.width + 4, self.height + 8)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 80))
        painter.drawEllipse(QPointF(0, -self.px * 0.08), self.width * 0.4, self.px * 0.22)
        if self.walker.held:
            painter.setPen(QPen(QColor(246, 207, 110), max(2, self.px * 0.08)))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(0, -self.px * 0.08), self.width * 0.55, self.px * 0.32)
        if self.pixmap is not None:
            painter.drawPixmap(int(-self.width / 2), int(-self.height), self.pixmap)
        else:
            painter.setPen(QColor(20, 20, 26))
            painter.setBrush(QColor(90, 110, 170))
            painter.drawRoundedRect(
                QRectF(-self.width / 2, -self.height, self.width, self.height), 6, 6
            )


class _Tag(QGraphicsSimpleTextItem):
    """A name over a person, the same size on screen at any zoom."""

    def __init__(self, text: str, held: bool) -> None:
        super().__init__(text)
        font = QFont()
        font.setPointSizeF(9.0)
        font.setBold(held)
        self.setFont(font)
        self.setBrush(QColor(255, 236, 170) if held else QColor(244, 241, 232))
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setZValue(100)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.boundingRect().adjusted(-3, -1, 3, 1)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(15, 15, 20, 170))
        painter.drawRoundedRect(rect, 3, 3)
        super().paint(painter, option, widget)


class _Pin(QGraphicsItem):
    """A spot marked in the turn being written (⌖): the same size on screen
    at any zoom."""

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setZValue(150)

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt's name
        return QRectF(-12, -30, 24 + 8 * len(self.label), 34)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        painter.setPen(QPen(QColor(20, 16, 8), 2))
        painter.setBrush(QColor(246, 207, 110))
        painter.drawEllipse(QPointF(0, -18), 8, 8)
        painter.drawLine(QPointF(0, -10), QPointF(0, 0))
        painter.setPen(QColor(20, 16, 8))
        font = QFont()
        font.setPointSizeF(7.5)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(-8, -26, 16, 16), Qt.AlignCenter, self.label)


class AreaView(QWidget):
    # Walk to a tile (beside it, for a person or a thing).
    walk_requested = Signal(int, int, bool)
    # A menu choice: the verb, who or what, its id ("" for a person).
    action_chosen = Signal(str, str, str)
    # Walking shown is done: the tiles walked.
    walk_finished = Signal(list)
    # Travel… is about to show: the window fills its menu (`set_travel`).
    travel_wanted = Signal()
    travel_chosen = Signal(str)
    # Look ▾ is about to show (`set_looks`); a set chosen; a set asked for.
    looks_wanted = Signal()
    look_chosen = Signal(str)
    own_look_requested = Signal()
    edit_look_requested = Signal()
    # Mark this tile in the turn being written (⌖).
    mark_requested = Signal(int, int)
    # At an outdoor area's edge: where on past it (`set_edge_choices`), and
    # the choice (the side, "area" or a place id).
    edge_wanted = Signal(str)
    edge_chosen = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("areaView")
        self.title = QLabel()
        self.title.setObjectName("emptyTitle")
        self.clock = QLabel()
        self.clock.setObjectName("hintLabel")
        self.clock.setToolTip(
            "The map's clock: walking and travel move it on. Shown to you; the storyteller "
            "is told only how far you walked."
        )
        self.travel_button = QToolButton()
        self.travel_button.setObjectName("viewToggleButton")
        self.travel_button.setText("Travel…")
        self.travel_button.setToolTip(
            "Set off for another place on the map: the way's length on foot moves the clock, "
            "and the turn goes in the composer"
        )
        self.travel_button.setPopupMode(QToolButton.InstantPopup)
        self.travel_menu = QMenu(self)
        self.travel_menu.aboutToShow.connect(self.travel_wanted.emit)
        self.travel_button.setMenu(self.travel_menu)
        self.look_button = QToolButton()
        self.look_button.setObjectName("viewToggleButton")
        self.look_button.setText("Look")
        self.look_button.setToolTip(
            "The tile set the map is drawn in: one of those that come with SealedLore, or "
            "one drawn for this story from its world"
        )
        self.look_button.setPopupMode(QToolButton.InstantPopup)
        self.look_menu = QMenu(self)
        self.look_menu.aboutToShow.connect(self.looks_wanted.emit)
        self.look_button.setMenu(self.look_menu)
        self.zoom_out = QToolButton()
        self.zoom_out.setObjectName("viewToggleButton")
        self.zoom_out.setText("−")
        self.zoom_out.setToolTip("Zoom out (-, or the mouse wheel)")
        self.zoom_in = QToolButton()
        self.zoom_in.setObjectName("viewToggleButton")
        self.zoom_in.setText("+")
        self.zoom_in.setToolTip("Zoom in (+, or the mouse wheel)")
        self.edge_button = QToolButton()
        self.edge_button.setObjectName("viewToggleButton")
        self.edge_button.setToolTip(
            "You are at the edge of this area: walk on into the next, or set off for a place "
            "that lies this way"
        )
        self.edge_button.setPopupMode(QToolButton.InstantPopup)
        self.edge_menu = QMenu(self)
        self.edge_menu.aboutToShow.connect(lambda: self.edge_wanted.emit(self._edge or ""))
        self.edge_button.setMenu(self.edge_menu)
        self.edge_button.hide()
        self._edge: str | None = None
        self.me_button = QToolButton()
        self.me_button.setObjectName("viewToggleButton")
        self.me_button.setText("◎ Me")
        self.me_button.setToolTip("Centre the view on your character (Home)")
        self.hint = QLabel()
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)
        # The map's coordinate under the pointer, as a mark is written.
        self.coords = QLabel()
        self.coords.setObjectName("hintLabel")
        self.coords.setToolTip(
            "Where the pointer is on the map, in metres east and north. Right-click a spot "
            "to mark it in your turn."
        )
        header = WrapRow()
        header.add_group([self.title])
        header.add_group([self.clock])
        header.add_group(
            [
                self.edge_button,
                self.travel_button,
                self.look_button,
                self.me_button,
                self.zoom_out,
                self.zoom_in,
            ]
        )
        header.add_group([self.coords])
        self.view = _Graphics()
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self.view.clicked.connect(self._clicked)
        self.view.context.connect(self._context)
        self.view.hovered.connect(self._hovered)
        self.view.zoomed.connect(self._zoomed)
        self.view.resized.connect(self._resized)
        # Fitted to the area until the author zooms (then it is theirs).
        self._fitted = True
        self.zoom_out.clicked.connect(lambda: self.view.zoom_by(1 / ZOOM_STEP))
        self.zoom_in.clicked.connect(lambda: self.view.zoom_by(ZOOM_STEP))
        self.me_button.clicked.connect(self.centre_on_me)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.hint)
        layout.addWidget(self.view, 1)
        self.area: Area | None = None
        self.walkers: list[Walker] = []
        self.people: dict[str, _Person] = {}
        self._tags: dict[str, _Tag] = {}
        self._background: QGraphicsPixmapItem | None = None
        self._hover: QGraphicsRectItem | None = None
        self._key: object = None
        self.locked = False
        self._steps: list[tuple[int, int]] = []
        self._walked: list[tuple[int, int]] = []
        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / STEPS_PER_SECOND))
        self._timer.timeout.connect(self._step)
        self._fitted_for: str | None = None
        # The map's point (metres east, north) at a tile of the area shown,
        # or None (the window sets it).
        self.point_of: Callable[[int, int], tuple[float, float] | None] = lambda _x, _y: None
        # Tiles marked in the turn being written, and their pins.
        self._marks: list[tuple[int, int]] = []
        self._pins: list[_Pin] = []
        # Indoors, what the author's character can't see is shaded.
        self._shade: QGraphicsPixmapItem | None = None
        self._shade_from: tuple[int, int] | None = None
        self.set_message("No area here yet.")

    # --- what is shown -----------------------------------------------------------

    def set_message(self, text: str) -> None:
        """Nothing to play on: a word in the view's place."""
        self.area = None
        self._key = None
        self._stop_walking()
        self.scene.clear()
        self.people, self._tags, self._background, self._hover = {}, {}, None, None
        self._pins = []
        self._shade, self._shade_from = None, None
        label = self.scene.addSimpleText(text)
        label.setBrush(QColor(200, 200, 210))
        self.title.setText("")
        self.hint.setText(text)

    def set_area(
        self,
        area: Area,
        image: QImage | None,
        walkers: list[Walker],
        *,
        key: object,
        clock: str = "",
        hint: str = "",
    ) -> None:
        """Show this area with these people (the window calls it whenever the
        session's state changes, and again once the picture is drawn)."""
        if self._timer.isActive():
            return  # mid-walk: the walk's end refreshes it
        px = px_for(area)
        rebuild = (
            key != self._key
            or self.area is None
            or self.area.place_id != area.place_id
            or (image is not None and self._background is None)
        )
        self.area, self.walkers = area, walkers
        self.title.setText(area.name)
        self.clock.setText(clock)
        self.hint.setText(hint or self._hint())
        if rebuild:
            centre = self.view.mapToScene(self.view.viewport().rect().center())
            same_place = self._fitted_for == area.place_id
            self.scene.clear()
            self.people, self._tags, self._hover = {}, {}, None
            self._pins = []
            self._shade, self._shade_from = None, None
            self._key = key
            if image is not None:
                self._background = self.scene.addPixmap(QPixmap.fromImage(image))
                self._background.setZValue(0)
            else:
                self._background = None
                rect = self.scene.addRect(
                    QRectF(0, 0, area.width * px, area.height * px),
                    QPen(Qt.NoPen),
                    QColor(24, 26, 32),
                )
                rect.setZValue(0)
                note = self.scene.addSimpleText(f"Drawing {area.name}…")
                note.setBrush(QColor(210, 210, 220))
                note.setPos(px, px)
            self.scene.setSceneRect(QRectF(0, 0, area.width * px, area.height * px))
            self._hover = QGraphicsRectItem()
            self._hover.setPen(QPen(QColor(246, 207, 110, 200), 2))
            self._hover.setZValue(200)
            self._hover.hide()
            self.scene.addItem(self._hover)
            if not same_place:
                self._fit(area)
            else:
                self.view.centerOn(centre)
        self._place_people(area, walkers, px)
        self._place_pins()

    def set_marks(self, tiles: list[tuple[int, int]]) -> None:
        """Pins on the tiles marked in the turn being written, numbered in
        the order the turn has them."""
        self._marks = list(tiles)
        self._place_pins()

    def _place_pins(self) -> None:
        for pin in self._pins:
            self.scene.removeItem(pin)
        self._pins = []
        if self.area is None:
            return
        px = px_for(self.area)
        for number, (x, y) in enumerate(self._marks, start=1):
            pin = _Pin(str(number))
            pin.setPos((x + 0.5) * px, (y + 0.5) * px)
            self.scene.addItem(pin)
            self._pins.append(pin)

    def centre_on_me(self) -> None:
        """The view on the author's character."""
        held = self.held()
        if self.area is None or held is None:
            return
        px = px_for(self.area)
        self.view.centerOn((held.x + 0.5) * px, (held.y + 0.5) * px)

    def _place_people(self, area: Area, walkers: list[Walker], px: int) -> None:
        names = {w.name for w in walkers}
        for name in list(self.people):
            if name not in names:
                self.scene.removeItem(self.people.pop(name))
                self.scene.removeItem(self._tags.pop(name))
        for walker in walkers:
            person = self.people.get(walker.name)
            if person is None or person.walker.image is not walker.image:
                if person is not None:
                    self.scene.removeItem(person)
                    self.scene.removeItem(self._tags.pop(walker.name))
                person = _Person(walker, px, area.indoor)
                self.scene.addItem(person)
                self.people[walker.name] = person
                tag = _Tag(walker.name, walker.held)
                self.scene.addItem(tag)
                self._tags[walker.name] = tag
            person.walker = walker
            person.place_at(walker.x, walker.y)
            self._tag_at(walker.name, walker.x, walker.y)
        held = self.held()
        if held is not None:
            self._follow(held.x, held.y)
        self._sync_sight()

    def _sync_sight(self) -> None:
        """Line of sight from the author's character: indoors the tiles they
        can't see are shaded; anyone out of sight is drawn faint."""
        held, area = self.held(), self.area
        if held is None or area is None:
            return
        here = (held.x, held.y)
        for name, person in self.people.items():
            walker = person.walker
            seen = walker.held or sees(area, here, (walker.x, walker.y))
            person.setOpacity(1.0 if seen else 0.45)
            tag = self._tags.get(name)
            if tag is not None:
                tag.setOpacity(1.0 if seen else 0.6)
        if not area.indoor or self._shade_from == here:
            return
        self._shade_from = here
        grid = seen_tiles(area, here)
        image = QImage(area.width, area.height, QImage.Format_ARGB32)
        image.fill(QColor(0, 0, 0, 0))
        dark = QColor(8, 8, 14, 150)
        for y, row in enumerate(grid):
            for x, seen in enumerate(row):
                if not seen:
                    image.setPixelColor(x, y, dark)
        px = px_for(area)
        pixmap = QPixmap.fromImage(image)
        if self._shade is None:
            self._shade = self.scene.addPixmap(pixmap)
            self._shade.setZValue(5)
            self._shade.setTransformationMode(Qt.FastTransformation)
            self._shade.setScale(px)
        else:
            self._shade.setPixmap(pixmap)

    def _tag_at(self, name: str, x: float, y: float) -> None:
        person, tag = self.people.get(name), self._tags.get(name)
        if person is None or tag is None:
            return
        px = person.px
        top = (y + 1.0) * px - person.height
        tag.setPos((x + 0.5) * px, top)
        width = tag.boundingRect().width() / max(self.view.transform().m11(), 0.01)
        tag.setPos((x + 0.5) * px - width / 2, top - 16 / max(self.view.transform().m11(), 0.01))

    def _fit(self, area: Area) -> None:
        """Opened on an area: indoors the whole of it; outdoors close enough
        to see who is who, on the author's character."""
        self._fitted_for = area.place_id
        self._fitted = True
        px = px_for(area)
        port = self.view.viewport().rect()
        if area.indoor:
            scale = min(port.width() / (area.width * px), port.height() / (area.height * px))
        else:
            scale = port.width() / (min(area.width, 48) * px)
        self.view.resetTransform()
        scale = max(0.15, min(scale, 6.0))
        self.view.blockSignals(True)
        self.view.scale(scale, scale)
        self.view.blockSignals(False)
        held = self.held()
        if held is not None:
            self.view.centerOn((held.x + 0.5) * px, (held.y + 0.5) * px)

    def _zoomed(self) -> None:
        self._fitted = False

    def _resized(self) -> None:
        if self._fitted and self.area is not None:
            self._fit(self.area)

    def _follow(self, x: float, y: float) -> None:
        if self.area is None:
            return
        px = px_for(self.area)
        port = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        margin = port.width() * 0.25
        point = QPointF((x + 0.5) * px, (y + 0.5) * px)
        inner = port.adjusted(margin, margin * 0.6, -margin, -margin * 0.6)
        if not inner.contains(point):
            self.view.centerOn(point)

    def held(self) -> Walker | None:
        return next((w for w in self.walkers if w.held), None)

    def _hint(self) -> str:
        if self.locked:
            return "The story is being written: the map waits for it."
        return (
            "Click to walk there; click a person or a thing for what you can do. Walk into a "
            "door to go through it. Right-click a spot to mark it in your turn. Travel… for "
            "another place. Drag or wheel to look around; ◎ Me (Home) finds you."
        )

    def set_locked(self, locked: bool) -> None:
        self.locked = locked
        self.hint.setText(self._hint())

    def set_travel(self, choices: list[tuple[str, str, str]]) -> None:
        """Travel…'s entries: (place id, name, how long)."""
        self.travel_menu.clear()
        if not choices:
            action = self.travel_menu.addAction("Nowhere else known yet")
            action.setEnabled(False)
            return
        for place_id, name, how_long in choices:
            action = self.travel_menu.addAction(f"{name} — {how_long}")
            action.setEnabled(not self.locked)
            action.triggered.connect(lambda _c=False, pid=place_id: self.travel_chosen.emit(pid))

    def set_edge(self, side: str | None) -> None:
        """The author's character stands on this side's edge (None: not on one)."""
        self._edge = side
        self.edge_button.setText(f"Leave {side} ▾" if side else "")
        self.edge_button.setVisible(side is not None)

    def set_edge_choices(self, side: str, choices: list[tuple[str, str]]) -> None:
        """The edge menu's entries: (an id, what it says)."""
        self.edge_menu.clear()
        if not choices:
            action = self.edge_menu.addAction(f"Nothing known lies {side} of here")
            action.setEnabled(False)
            return
        for choice_id, label in choices:
            action = self.edge_menu.addAction(label)
            action.setEnabled(not self.locked)
            action.triggered.connect(lambda _c=False, c=choice_id: self.edge_chosen.emit(side, c))

    def _off_the_edge(self, x: int, y: int) -> str | None:
        area = self.area
        if area is None or area.indoor:
            return None
        if y < 0:
            return "north"
        if y >= area.height:
            return "south"
        if x < 0:
            return "west"
        if x >= area.width:
            return "east"
        return None

    def set_looks(self, choices: list[tuple[str, str]], current: str, chosen: str) -> None:
        """Look ▾'s entries: (name, title) of each set, the one drawn in now
        ticked, and whether the author chose it or the program did."""
        self.look_menu.clear()
        auto = self.look_menu.addAction("Chosen for the setting")
        auto.setCheckable(True)
        auto.setChecked(not chosen)
        auto.triggered.connect(lambda: self.look_chosen.emit(""))
        self.look_menu.addSeparator()
        for name, title in choices:
            action = self.look_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(name == current)
            action.triggered.connect(lambda _c=False, n=name: self.look_chosen.emit(n))
        self.look_menu.addSeparator()
        own = self.look_menu.addAction("Draw a tile set for this story…")
        own.setEnabled(not self.locked)
        own.triggered.connect(self.own_look_requested.emit)
        edit = self.look_menu.addAction("Edit the tile set…")
        edit.setEnabled(not self.locked)
        edit.triggered.connect(self.edit_look_requested.emit)

    # --- walking -------------------------------------------------------------------

    def walk(self, steps: list[tuple[int, int]]) -> None:
        """Show the author's character walking these tiles; `walk_finished`
        once they are there."""
        self._stop_walking()
        if len(steps) < 2:
            self.walk_finished.emit(list(steps))
            return
        self._steps = list(steps[1:])
        self._walked = [steps[0]]
        self._timer.start()

    def walking(self) -> bool:
        return self._timer.isActive()

    def _step(self) -> None:
        held = self.held()
        if not self._steps or held is None:
            self._finish()
            return
        x, y = self._steps.pop(0)
        self._walked.append((x, y))
        held.x, held.y = x, y
        person = self.people.get(held.name)
        if person is not None:
            person.place_at(x, y)
            self._tag_at(held.name, x, y)
        self._follow(x, y)
        self._sync_sight()
        if not self._steps:
            self._finish()

    def _finish(self) -> None:
        self._timer.stop()
        walked, self._walked, self._steps = self._walked, [], []
        self.walk_finished.emit(walked)

    def _stop_walking(self) -> None:
        if self._timer.isActive():
            self._finish()

    # --- clicks and keys -------------------------------------------------------------

    def tile_at(self, point: QPointF) -> tuple[int, int] | None:
        if self.area is None:
            return None
        px = px_for(self.area)
        x, y = int(point.x() // px), int(point.y() // px)
        if 0 <= x < self.area.width and 0 <= y < self.area.height:
            return x, y
        return None

    def person_at(self, point: QPointF) -> Walker | None:
        for item in self.scene.items(point):
            if isinstance(item, _Person):
                return item.walker
        if self.area is None:
            return None
        tile = self.tile_at(point)
        return next((w for w in self.walkers if (w.x, w.y) == tile), None)

    def _clicked(self, point: QPointF, global_pos: QPoint) -> None:
        if self.area is None:
            return
        if self.locked:
            self.hint.setText(self._hint())
            return
        person = self.person_at(point)
        tile = self.tile_at(point)
        if person is not None and not person.held:
            self._person_menu(person, global_pos)
            return
        thing = self.area.placement_at(*tile) if tile is not None else None
        if thing is not None:
            self._thing_menu(thing, global_pos)
            return
        if tile is not None:
            self.walk_requested.emit(tile[0], tile[1], False)

    def _hovered(self, point: QPointF) -> None:
        tile = self.tile_at(point)
        spot = self.point_of(*tile) if tile is not None else None
        self.coords.setText(f"⌖({round(spot[0])}, {round(spot[1])})" if spot else "")

    def _context(self, point: QPointF, global_pos: QPoint) -> None:
        """A right-click: mark the spot in the turn, or walk there; on a
        person or a thing, what can be done with them too."""
        tile = self.tile_at(point)
        if self.area is None or tile is None:
            return
        person = self.person_at(point)
        if person is not None and not person.held and not self.locked:
            self._person_menu(person, global_pos, mark=(person.x, person.y))
            return
        thing = self.area.placement_at(*tile)
        if thing is not None and not self.locked:
            self._thing_menu(thing, global_pos, mark=tile)
            return
        menu = QMenu(self)
        spot = self.point_of(*tile)
        if spot is not None:
            title = menu.addAction(f"⌖({round(spot[0])}, {round(spot[1])})")
            title.setEnabled(False)
        mark = menu.addAction("Mark this spot in your turn")
        mark.setEnabled(spot is not None)
        mark.triggered.connect(lambda: self.mark_requested.emit(*tile))
        walk = menu.addAction("Walk here")
        walk.setEnabled(not self.locked and self.area.walkable(*tile))
        walk.triggered.connect(lambda: self.walk_requested.emit(tile[0], tile[1], False))
        menu.exec(global_pos)
        menu.deleteLater()

    def _add_mark(self, menu: QMenu, tile: tuple[int, int] | None) -> None:
        if tile is None:
            return
        menu.addSeparator()
        mark = menu.addAction("Mark this spot in your turn")
        mark.setEnabled(self.point_of(*tile) is not None)
        mark.triggered.connect(lambda: self.mark_requested.emit(*tile))

    def _person_menu(self, person: Walker, at: QPoint, mark=None) -> None:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        title = menu.addAction(person.name)
        title.setEnabled(False)
        for verb in ("talk", "look", "attack"):
            action = menu.addAction(VERB_LABELS[verb])
            action.triggered.connect(
                lambda _c=False, v=verb: self.action_chosen.emit(v, person.name, "")
            )
        menu.addSeparator()
        walk = menu.addAction("Walk over")
        walk.triggered.connect(lambda: self.walk_requested.emit(person.x, person.y, True))
        self._add_mark(menu, mark)
        menu.exec(at)
        menu.deleteLater()

    def _thing_menu(self, thing: Placement, at: QPoint, mark=None) -> None:
        menu = QMenu(self)
        title = menu.addAction(thing.name[0].upper() + thing.name[1:])
        title.setEnabled(False)
        for verb in thing.verbs:
            action = menu.addAction(VERB_LABELS.get(verb, verb.capitalize()))
            action.triggered.connect(
                lambda _c=False, v=verb: self.action_chosen.emit(v, thing.name, thing.id)
            )
        menu.addSeparator()
        walk = menu.addAction("Walk over")
        cx, cy = thing.centre
        walk.triggered.connect(lambda: self.walk_requested.emit(int(cx), int(cy), True))
        self._add_mark(menu, mark)
        menu.exec(at)
        menu.deleteLater()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt's name
        held = self.held()
        step = WALK_KEYS.get(event.key())
        if step is not None and held is not None and not self.locked and not self.walking():
            dx, dy = step
            side = self._off_the_edge(held.x + dx, held.y + dy)
            if side is not None:
                # Walking on past the edge: where to.
                self.set_edge(side)
                self.edge_wanted.emit(side)
                person = self.people.get(held.name)
                point = person.scenePos() if person is not None else QPointF()
                at = self.view.viewport().mapToGlobal(self.view.mapFromScene(point))
                self.edge_menu.popup(at)
                return
            self.walk_requested.emit(held.x + dx, held.y + dy, False)
            return
        if event.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self.view.zoom_by(ZOOM_STEP)
            return
        if event.key() == Qt.Key_Minus:
            self.view.zoom_by(1 / ZOOM_STEP)
            return
        if event.key() == Qt.Key_Home:
            self.centre_on_me()
            return
        super().keyPressEvent(event)
