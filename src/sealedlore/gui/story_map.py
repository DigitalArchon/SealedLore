"""The story map: every branch of the story on one diagram (Map, above the
transcript; Esc or Back to the story returns).

A timeline of branches, not a tree of messages: one lane per branch, like a
metro map, running top to bottom with the message numbers down the left (the
author's call: a complex tree is easier to follow scrolling down than
across). A branch leaves the lane it split from at the message it follows;
its lane then runs down to its end. Beside its first message a lane carries
its name and how it begins: the message that was rewritten is what tells one
branch from another. Chapters are shaded along each lane, and a dot marks
where you are. Takes are not drawn: they swap in place and are not lines of
the story. A 500-message story with five branches is five lanes, not 500
nodes, so it stays readable (the author asked for it, Sept 2026, to explore
stories that branch a lot).

A click on a lane or its name moves the story to that branch and stays on
the map; a double-click opens the story there. Hovering shows a card that
stays while it is read: the tooltip it replaced went as soon as the pointer
moved.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from html import escape

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.branches import MapLane
from sealedlore.gui import text_size, theme
from sealedlore.gui.wrap_row import WrapRow

STEP = 10.0  # scene units per message, down the page
COLUMN = 212.0  # between lanes, across; the labels sit in the gap
AXIS = 56.0  # the message numbers, down the left
TOP = 56.0  # room for the first lane's name
TICK_EVERY = 50
# Zoom is along the timeline only; these bound it.
ZOOM_MIN, ZOOM_MAX = 0.05, 8.0
ZOOM_STEP = 1.25
# How long the hover card stays once the pointer has left what it describes.
CARD_LINGER_MS = 1500


# A label block's lane, as item data (its index in the view's lanes).
_LANE_KEY = 0


def column_width() -> float:
    """Between lanes, across: the labels sit in the gap, so it grows with
    the text size."""
    return COLUMN * max(1.0, text_size.scale())


def label_width() -> float:
    return column_width() - 34


def x_of(row: int) -> float:
    """A lane's column."""
    return AXIS + 20 + row * column_width()


def y_of(message: int) -> float:
    return TOP + (message - 1) * STEP


def _head(text: str, limit: int) -> str:
    """The first `limit` characters, whitespace folded."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tail(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else "…" + text[-(limit - 1) :].lstrip()


class _HoverCard(QLabel):
    """What the pointer is over, kept up while it is read: it stays while the
    pointer is on the lane or on the card itself, and a moment after."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("mapCard")
        self.setTextFormat(Qt.RichText)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.hide()
        self.shown_for: tuple[int, int | None] | None = None
        self._linger = QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.timeout.connect(self._fade)

    def show_at(self, html: str, where: QPoint, key: tuple[int, int | None]) -> None:
        self._linger.stop()
        if key == self.shown_for and self.isVisible():
            return
        self.shown_for = key
        self.setFixedWidth(round(360 * max(1.0, text_size.scale())))
        self.setText(html)
        self.adjustSize()
        area = self.parentWidget().rect()
        x = min(where.x() + 18, area.right() - self.width() - 4)
        y = where.y() + 18
        if y + self.height() > area.bottom() - 4:
            y = where.y() - self.height() - 12
        self.move(max(4, x), max(4, y))
        self.show()
        self.raise_()

    def linger(self) -> None:
        """The pointer has left what the card is about: go in a moment,
        unless it comes onto the card."""
        if self.isVisible() and not self.underMouse() and not self._linger.isActive():
            self._linger.start(CARD_LINGER_MS)

    def _fade(self) -> None:
        if not self.underMouse():
            self.hide()
            self.shown_for = None

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._linger.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._linger.start(CARD_LINGER_MS)
        super().leaveEvent(event)


class _MapView(QGraphicsView):
    """Pan by dragging, scroll down it, zoom with Ctrl+wheel, + and -. A click
    (not a drag) on a lane or its name picks that branch; a double-click opens
    the story there. Zoom is along the timeline only: lanes keep their spacing
    and labels their size, whatever the zoom."""

    # (lane, message, or None for a click on its name)
    clicked = Signal(object, object)
    double_clicked = Signal(object, object)
    context = Signal(object, object)  # lane, global position
    zoomed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setMouseTracking(True)
        self.setObjectName("storyMapView")
        self.lanes: list[MapLane] = []
        self.text_of: Callable[[str], str] = lambda _node_id: ""
        self.openings: Mapping[str | None, str] = {}
        self.card = _HoverCard(self)
        self._pressed_at: QPointF | None = None
        # A click waits out the double-click interval: the first click of a
        # double-click would otherwise act before it arrived.
        self._click = QTimer(self)
        self._click.setSingleShot(True)
        self._click.timeout.connect(self._clicked)
        self._pending: tuple[MapLane, int | None] | None = None

    # --- what is where -------------------------------------------------------

    def hit(self, scene_point: QPointF) -> tuple[MapLane, int] | None:
        """The lane and message under a point of the scene, if any."""
        for lane in self.lanes:
            if abs(scene_point.x() - x_of(lane.column)) > 14:
                continue
            message = round((scene_point.y() - TOP) / STEP) + 1
            last = lane.branch.length
            if lane.first - 1 <= message <= last + 1:
                return lane, max(lane.first, min(message, last))
        return None

    def hit_at(self, view_point: QPoint) -> tuple[MapLane, int | None] | None:
        """A message on a lane, or a lane's name and opening (message None)."""
        found = self.hit(self.mapToScene(view_point))
        if found is not None:
            return found
        for item in self.items(view_point):
            while item is not None:
                index = item.data(_LANE_KEY)
                if isinstance(index, int) and 0 <= index < len(self.lanes):
                    return self.lanes[index], None
                item = item.parentItem()
        return None

    def point_for(self, lane: MapLane, message: int) -> QPoint:
        """Where a message of a lane is drawn, in the view's own coordinates."""
        return self.mapFromScene(QPointF(x_of(lane.column), y_of(message)))

    def label_point(self, lane: MapLane) -> QPoint:
        """A point on a lane's name, in the view's own coordinates."""
        return self.point_for(lane, lane.first) + QPoint(40, 0)

    # --- zoom ------------------------------------------------------------------

    def zoom(self) -> float:
        return self.transform().m22()

    def zoom_by(self, factor: float, *, under_mouse: bool = False) -> None:
        target = max(ZOOM_MIN, min(ZOOM_MAX, self.zoom() * factor))
        if abs(target - self.zoom()) < 1e-9:
            return
        anchor = self.transformationAnchor()
        if not under_mouse:
            self.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
        self.scale(1.0, target / self.zoom())
        self.setTransformationAnchor(anchor)
        self.card.hide()
        self.zoomed.emit()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.modifiers() & Qt.ControlModifier:
            self.zoom_by(1.15 if event.angleDelta().y() > 0 else 1 / 1.15, under_mouse=True)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Plain + and -: with Ctrl they are the app's text size.
        if not event.modifiers() & Qt.ControlModifier:
            if event.key() in (Qt.Key_Plus, Qt.Key_Equal):
                self.zoom_by(ZOOM_STEP)
                return
            if event.key() == Qt.Key_Minus:
                self.zoom_by(1 / ZOOM_STEP)
                return
        super().keyPressEvent(event)

    # --- the pointer -----------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._pressed_at = event.position()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().mouseReleaseEvent(event)
        pressed, self._pressed_at = self._pressed_at, None
        if event.button() != Qt.LeftButton or pressed is None:
            return
        if (event.position() - pressed).manhattanLength() > 4:
            return  # a drag, not a click
        found = self.hit_at(event.position().toPoint())
        if found is not None:
            self._pending = found
            self._click.start(QApplication.doubleClickInterval())

    def _clicked(self) -> None:
        found, self._pending = self._pending, None
        if found is not None:
            self.clicked.emit(*found)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._click.stop()
        self._pending = None
        found = self.hit_at(event.position().toPoint())
        if found is not None:
            self.double_clicked.emit(*found)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().mouseMoveEvent(event)
        if event.buttons():
            return
        point = event.position().toPoint()
        found = self.hit_at(point)
        if found is None:
            self.card.linger()
            return
        lane, message = found
        self.card.show_at(self.card_text(lane, message), point, (self.lanes.index(lane), message))

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.card.linger()
        super().leaveEvent(event)

    def card_text(self, lane: MapLane, message: int | None) -> str:
        """The hover card: the branch, how it begins, the message under the
        pointer, how it ends, and what a click does."""
        branch = lane.branch
        dim = theme.colour("map_dim").name()
        parts = [
            f"<b>{escape(branch.name)}</b>" + (" · you are here" if branch.is_current else ""),
            f"<span style='color:{dim}'>{escape(_where(lane))}</span>",
        ]
        opening = self.openings.get(branch.start_id, "")
        if opening:
            parts.append(f"Begins: “{escape(_head(opening, 240))}”")
        node_id = lane.node_at(message) if message is not None else None
        if node_id is not None and message != lane.first:
            parts.append(f"Message {message}: “{escape(_head(self.text_of(node_id), 280))}”")
        ending = self.text_of(branch.end_id)
        if ending and branch.length > lane.first:
            parts.append(f"Ends: “{escape(_tail(ending, 200))}”")
        action = (
            "Double-click to open the story here"
            if branch.is_current
            else "Click to move to this branch; double-click to open the story here"
        )
        parts.append(f"<span style='color:{dim}'>{action}</span>")
        return "<br>".join(parts)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt naming
        found = self.hit_at(event.pos())
        if found is not None:
            self.context.emit(found[0], event.globalPos())


class StoryMap(QWidget):
    back_requested = Signal()
    # (branch start id or None, message id or None for its end): move the
    # story to that branch, staying on the map.
    select_requested = Signal(object, object)
    # The same, and back to the story, scrolled to the message.
    open_requested = Signal(object, object)
    rename_requested = Signal(object)
    delete_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("storyMap")
        title = QLabel("Story map")
        title.setObjectName("emptyTitle")
        hint = QLabel(
            "Each lane is a branch, the story running down the page. Click a lane or its "
            "name to move to that branch, double-click to open the story there, right-click "
            "to rename or delete it. Drag or scroll to move. Shaded: summarised into chapters."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        self.zoom_out = self._zoom_button("−", "Zoom out (-)")
        self.zoom_in = self._zoom_button("+", "Zoom in (+, or Ctrl and the mouse wheel)")
        self.fit_button = self._zoom_button("Fit", "The whole story in view")
        back = QPushButton("Back to the story")
        back.clicked.connect(self.back_requested)
        # Wraps under a large text size rather than widening the window; the
        # hint has a line of its own below.
        header = WrapRow()
        header.add_group([title])
        header.add_group([self.zoom_out, self.zoom_in, self.fit_button])
        header.add_group([back], right=True)
        self.view = _MapView()
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self.view.clicked.connect(
            lambda lane, message: self.select_requested.emit(
                lane.branch.start_id, lane.node_at(message) if message else None
            )
        )
        self.view.double_clicked.connect(
            lambda lane, message: self.open_requested.emit(
                lane.branch.start_id, lane.node_at(message) if message else None
            )
        )
        self.view.context.connect(self._context_menu)
        self.view.zoomed.connect(self._fit_scene_rect)
        self.zoom_out.clicked.connect(lambda: self.view.zoom_by(1 / ZOOM_STEP))
        self.zoom_in.clicked.connect(lambda: self.view.zoom_by(ZOOM_STEP))
        self.fit_button.clicked.connect(self.fit)
        self._longest = 0
        self._right = 0.0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(hint)
        layout.addWidget(self.view, 1)

    @staticmethod
    def _zoom_button(text: str, tip: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("viewToggleButton")
        button.setText(text)
        button.setToolTip(tip)
        return button

    def fit_zoom(self) -> float:
        """The zoom that puts the whole story down the view."""
        height = self.view.viewport().height() or 600
        return max(ZOOM_MIN, min(2.5, (height - TOP - 60) / max(STEP * self._longest, 1)))

    def fit(self) -> None:
        if self._longest:
            self.view.zoom_by(self.fit_zoom() / self.view.zoom())
            self.view.verticalScrollBar().setValue(0)

    def _fit_scene_rect(self) -> None:
        if self._longest:
            # Room below the last lane for its labels, in pixels at any zoom.
            bottom = y_of(self._longest) + 90 / max(self.view.zoom(), ZOOM_MIN)
            self.scene.setSceneRect(QRectF(0, 0, self._right, bottom))

    def set_lanes(
        self,
        lanes: Sequence[MapLane],
        *,
        here: int | None,
        text_of: Callable[[str], str],
        openings: Mapping[str | None, str],
        keep_view: bool = False,
    ) -> None:
        """`here`: the message the story is at, on the current branch's lane.
        `openings`: how each branch begins, by start id (None: the first line).
        `keep_view`: redrawn where it was (a branch picked on the map), not
        re-centred on where the story is."""
        view = self.view
        view.lanes = list(lanes)
        view.text_of = text_of
        view.openings = dict(openings)
        view.card.hide()
        view.card.shown_for = None
        kept = (
            view.transform(),
            view.horizontalScrollBar().value(),
            view.verticalScrollBar().value(),
        )
        scene = self.scene
        scene.clear()
        if not lanes:
            self._longest = 0
            return
        self._longest = longest = max(lane.branch.length for lane in lanes)
        self._right = right = x_of(max(lane.column for lane in lanes)) + column_width()
        small = _font(8)
        name_font, dim_font = _font(9.5, bold=True), _font(9.5)

        # The axis: every TICK_EVERY messages, a faint line and its number.
        for message in [1, *range(TICK_EVERY, longest + 1, TICK_EVERY)]:
            scene.addLine(
                AXIS,
                y_of(message),
                right,
                y_of(message),
                _cosmetic(QPen(theme.colour("map_grid"), 1)),
            )
            _text_block(
                scene, [(str(message), small, theme.colour("map_dim"))], 8, y_of(message), top=-7
            )

        columns = {lane.column for lane in lanes}
        for index, lane in enumerate(lanes):
            current = lane.branch.is_current
            colour = theme.colour("map_current" if current else "map_lane")
            x = x_of(lane.column)
            first, last = lane.first, lane.branch.length
            # Chapters, under the line, only along the lane's own part.
            for start, end in lane.chapters:
                a, b = max(start, first), min(end, last)
                if a <= b:
                    band = QRectF(x - 8, y_of(a) - STEP / 2, 16, (b - a + 1) * STEP)
                    scene.addRect(band, QPen(Qt.NoPen), QBrush(theme.colour("map_chapter", 170)))
            pen = _cosmetic(QPen(colour, 6 if current else 4, Qt.SolidLine, Qt.RoundCap))
            if lane.parent_column is not None and lane.parent_column in columns:
                # Leaving the parent's lane at the message it follows.
                path = QPainterPath(QPointF(x_of(lane.parent_column), y_of(first - 1)))
                middle = (y_of(first - 1) + y_of(first)) / 2
                path.cubicTo(
                    QPointF(x_of(lane.parent_column), middle),
                    QPointF(x, middle),
                    QPointF(x, y_of(first)),
                )
                scene.addPath(path, _cosmetic(QPen(colour, 3, Qt.SolidLine, Qt.RoundCap)))
            if last > first:
                scene.addLine(x, y_of(first), x, y_of(last), pen)
            else:
                _dot(scene, x, y_of(first), 4, colour)

            # Its name, where it comes from and how it begins, beside its
            # first message, in the gap before the next lane.
            lines = [
                (
                    lane.branch.name,
                    name_font if current else dim_font,
                    theme.colour("map_text" if current else "map_dim"),
                ),
                (_where(lane), small, theme.colour("map_dim")),
            ]
            opening = " ".join(openings.get(lane.branch.start_id, "").split())
            if opening:
                lines += [
                    (text, small, theme.colour("map_dim"))
                    for text in _wrap(f"“{opening}”", small, 2)
                ]
            _text_block(scene, lines, x + 12, y_of(first), top=-8).setData(_LANE_KEY, index)

            if current and here is not None:
                _dot(scene, x, y_of(here), 7, theme.colour("map_here"))

        if keep_view:
            view.setTransform(kept[0])
        else:
            view.resetTransform()
            # The whole story down the view where it fits; long ones scroll.
            view.scale(1.0, max(0.35, self.fit_zoom()))
        self._fit_scene_rect()
        if keep_view:
            view.horizontalScrollBar().setValue(kept[1])
            view.verticalScrollBar().setValue(kept[2])
            return
        current_lane = next((lane for lane in lanes if lane.branch.is_current), lanes[0])
        view.centerOn(x_of(current_lane.column), y_of(here or current_lane.branch.length))
        # From the left edge when the current lane is in view from there, so
        # the first line is not cut off.
        if x_of(current_lane.column) + column_width() <= view.viewport().width():
            view.horizontalScrollBar().setValue(0)

    def _context_menu(self, lane: MapLane, where) -> None:
        menu = QMenu(self)
        rename = menu.addAction("Rename this branch…")
        rename.triggered.connect(lambda: self.rename_requested.emit(lane.branch.start_id))
        delete = menu.addAction("Delete this branch…")
        delete.setEnabled(not lane.branch.is_main)
        delete.triggered.connect(lambda: self.delete_requested.emit(lane.branch.start_id))
        menu.exec(where)


def _dot(scene: QGraphicsScene, x: float, y: float, radius: float, colour: QColor) -> None:
    """A round dot, round at any zoom."""
    dot = scene.addEllipse(-radius, -radius, 2 * radius, 2 * radius, QPen(colour), QBrush(colour))
    dot.setPos(x, y)
    _fixed(dot)


def _fixed(item: QGraphicsItem) -> QGraphicsItem:
    """Drawn in pixels, at any zoom."""
    item.setFlag(QGraphicsItem.ItemIgnoresTransformations)
    return item


def _cosmetic(pen: QPen) -> QPen:
    """A line as wide on screen at any zoom."""
    pen.setCosmetic(True)
    return pen


def _font(points: float, *, bold: bool = False) -> QFont:
    font = QFont()
    font.setBold(bold)
    font.setPointSizeF(text_size.points(points))
    return font


def _where(lane: MapLane) -> str:
    branch = lane.branch
    where = "the start" if branch.start_at <= 1 else f"message {branch.start_at}"
    return f"from {where} · {branch.length} messages"


def _wrap(text: str, font: QFont, lines: int) -> list[str]:
    """Up to `lines` lines of `text` that fit the gap between lanes, the last
    cut with "…" if there is more."""
    metrics = QFontMetrics(font)
    width = int(label_width())
    words = text.split()
    out: list[str] = []
    while words and len(out) < lines:
        line = words.pop(0)
        while words and metrics.horizontalAdvance(f"{line} {words[0]}") <= width:
            line += " " + words.pop(0)
        out.append(line)
    if words:
        out[-1] = f"{out[-1]} {' '.join(words)}"
    return [metrics.elidedText(line, Qt.ElideRight, width) for line in out]


def _text_block(
    scene: QGraphicsScene,
    lines: Sequence[tuple[str, QFont, QColor]],
    x: float,
    y: float,
    *,
    top: float,
) -> QGraphicsRectItem:
    """Lines of text, each cut to the gap between lanes, the first `top`
    pixels from `y`. One item drawn in pixels at (x, y), so its lines keep
    their spacing at any zoom; its rectangle is what a click on it hits."""
    block = QGraphicsRectItem()
    block.setPen(QPen(Qt.NoPen))
    _fixed(block)
    offset, widest = top, 0.0
    for text, font, colour in lines:
        fitted = QFontMetrics(font).elidedText(text, Qt.ElideRight, int(label_width()))
        item = QGraphicsSimpleTextItem(fitted, block)
        item.setFont(font)
        item.setBrush(colour)
        item.setPos(0, offset)
        offset += item.boundingRect().height()
        widest = max(widest, item.boundingRect().width())
    block.setRect(QRectF(0, top, widest, offset - top))
    block.setPos(x, y)
    scene.addItem(block)
    return block
