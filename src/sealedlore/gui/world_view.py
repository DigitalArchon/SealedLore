"""The map (Map, above the transcript; Esc or Back to the story returns): the
world the story has been through, drawn as it grows (models/world.py).

The land the story has reached is drawn tile by tile (engine/world_art.py);
outside it is fog, the unmapped. Each place stands where the program placed
it, with its picture once drawn: lit where the author's character has been,
dimmed where the story only told of it, ringed where they are now. Who is
there is written under it. A place inside another shows once the map is
zoomed in far enough to read it. Double-click a place to open its ground
plan, with the things and people there; Up returns to the map.

Everything here is read from the session; the view never changes the story.
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.world_layout import extent, position, size_of
from sealedlore.gui import text_size
from sealedlore.gui.map_art import FOG, sprite_pixmap, tile_pixmap
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.models.world import WorldArt, WorldMap, WorldPlace

SPRITE_SIZE = 40  # on screen, whatever the zoom
# A place inside another is shown once its parent is this many pixels wide.
CHILD_SHOWN_AT = 160
ZOOM_STEP = 1.25
TILE_PIXELS = 18  # a ground plan's tile, in scene units
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


class _PlaceItem(QGraphicsItem):
    """A place on the map: its picture (or a mark), its name, who is there.
    Drawn in screen pixels at its spot, whatever the zoom."""

    def __init__(
        self,
        place: WorldPlace,
        pixmap,
        people: list[str],
        *,
        visited: bool,
        current: bool,
    ) -> None:
        super().__init__()
        self.place = place
        self.pixmap = pixmap
        self.people = people
        self.visited = visited
        self.current = current
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setAcceptHoverEvents(True)
        self.setOpacity(1.0 if visited or current else 0.55)
        self.setZValue(3 if current else 2)
        self._font = QFont()
        self._font.setPointSizeF(text_size.points(8.5))
        self._bold = QFont(self._font)
        self._bold.setBold(True)

    def boundingRect(self) -> QRectF:
        half = SPRITE_SIZE / 2
        return QRectF(-90, -half - 6, 180, SPRITE_SIZE + 46)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        half = SPRITE_SIZE / 2
        if self.current:
            painter.setPen(QPen(QColor("#f6cf6e"), 3))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(0, 0), half + 5, half + 5)
        if self.pixmap is not None:
            painter.drawPixmap(int(-half), int(-half), self.pixmap)
        else:
            colour = QColor(_MARK_COLOURS.get(self.place.kind, _MARK_COLOURS[None]))
            painter.setPen(QPen(QColor("#1b1d2b"), 1.5))
            painter.setBrush(colour)
            painter.drawEllipse(QPointF(0, 0), 7, 7)
        name = self.place.name + (f" ({self.place.state})" if self.place.state else "")
        self._text(painter, name, half + 14, self._bold)
        if self.people:
            self._text(painter, ", ".join(self.people), half + 28, self._font)

    def _text(self, painter: QPainter, text: str, y: float, font: QFont) -> None:
        painter.setFont(font)
        rect = QRectF(-88, y - 11, 176, 14)
        painter.setPen(QColor(0, 0, 0, 150))
        painter.drawText(rect.translated(1, 1), Qt.AlignHCenter | Qt.TextSingleLine, text)
        painter.setPen(QColor("#f4f1e8"))
        painter.drawText(rect, Qt.AlignHCenter | Qt.TextSingleLine, text)


class _WorldGraphics(QGraphicsView):
    place_opened = Signal(str)
    zoomed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
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

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt's name
        item = self.itemAt(event.position().toPoint())
        while item is not None and not isinstance(item, _PlaceItem):
            item = item.parentItem()
        if isinstance(item, _PlaceItem):
            self.place_opened.emit(item.place.id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class WorldView(QWidget):
    back_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("worldMap")
        self.title = QLabel("Map")
        self.title.setObjectName("emptyTitle")
        self.hint = QLabel()
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)
        self.up_button = QPushButton("Up to the map")
        self.up_button.setToolTip("Back from this place to the whole map")
        self.up_button.clicked.connect(lambda: self.open_place(None))
        self.zoom_out = self._button("−", "Zoom out (-, or the mouse wheel)")
        self.zoom_in = self._button("+", "Zoom in (+, or the mouse wheel)")
        self.fit_button = self._button("Fit", "Everywhere the story has been, in view")
        self.here_button = self._button("Here", "Where the story is now")
        back = QPushButton("Back to the story")
        back.clicked.connect(self.back_requested)
        header = WrapRow()
        header.add_group([self.title])
        header.add_group([self.zoom_out, self.zoom_in, self.fit_button, self.here_button])
        header.add_group([self.up_button])
        header.add_group([back], right=True)
        self.view = _WorldGraphics()
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self.view.place_opened.connect(self.open_place)
        self.view.zoomed.connect(self._update_detail)
        self.zoom_out.clicked.connect(lambda: self.view.zoom_by(1 / ZOOM_STEP))
        self.zoom_in.clicked.connect(lambda: self.view.zoom_by(ZOOM_STEP))
        self.fit_button.clicked.connect(self.fit)
        self.here_button.clicked.connect(self.go_here)
        # A place opened: its things, people and numbered objects beside it.
        self.side = QLabel()
        self.side.setObjectName("worldMapSide")
        self.side.setWordWrap(True)
        self.side.setTextFormat(Qt.RichText)
        self.side.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.side.setMinimumWidth(180)
        self.side.setMaximumWidth(260)
        self.side.hide()
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(self.view, 1)
        body.addWidget(self.side)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self.hint)
        layout.addLayout(body, 1)
        self.world = WorldMap()
        self.art = WorldArt()
        self.held: str | None = None
        self.place_id: str | None = None
        self._items: list[_PlaceItem] = []
        self._fitted = False
        self.open_place(None)

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
        redraw as art comes in); off, it fits what the story has seen."""
        self.world, self.art, self.held = world, art, held
        if self.place_id is not None and self.place_id not in world.places:
            self.place_id = None
        center = self.view.mapToScene(self.view.viewport().rect().center())
        transform = self.view.transform()
        self._build()
        if keep_view and self._fitted:
            self.view.setTransform(transform)
            self.view.centerOn(center)
            self._update_detail()
        else:
            self.fit()

    def open_place(self, place_id: str | None) -> None:
        """A place's ground plan, or (None) the whole map."""
        self.place_id = place_id if place_id in self.world.places else None
        self._fitted = False
        self._build()
        self.fit()

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
        # Stand somewhere and you see what is in it: the farmhouse in the farm.
        around = {p.id for p in self.world.places.values() if p.inside in been}
        return set(self.world.seen) | been | around

    def _build(self) -> None:
        self.scene.clear()
        self._items = []
        in_place = self.place_id is not None
        self.up_button.setVisible(in_place)
        self.here_button.setVisible(not in_place)
        self.side.setVisible(in_place)
        if in_place:
            self._build_place(self.world.places[self.place_id])
            return
        self.title.setText("Map")
        if not self.world.places:
            self.hint.setText(
                "Nothing on the map yet: it grows as the story goes, from the next passage on."
            )
            return
        self.hint.setText(
            "Where the story has been, drawn as it grows. Lit: where you've been; dimmed: "
            "places the story told of; ringed: where you are now. Scroll to zoom, drag to "
            "move, double-click a place to open it."
        )
        self._build_world()

    def _build_world(self) -> None:
        world = self.world
        for patch in self.art.terrain:
            if not patch.rows:
                continue
            item = QGraphicsPixmapItem(tile_pixmap(tuple(patch.rows), patch.ground))
            item.setTransformationMode(Qt.FastTransformation)
            item.setShapeMode(QGraphicsPixmapItem.BoundingRectShape)
            item.setScale(patch.side / max(len(patch.rows[0]), 1))
            item.setPos(patch.x - patch.side / 2, -(patch.y + patch.side / 2))
            item.setZValue(0)
            self.scene.addItem(item)
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
        visited, here = self._visited(), self.here()
        people: dict[str, list[str]] = {}
        for who, place_id in world.where.items():
            people.setdefault(place_id, []).append(who)
        for place in world.places.values():
            spot = position(world, place.id)
            if spot is None or place.id not in shown:
                continue
            sprite = self.art.sprite_for(place.id, place.state) or self.art.sprite_for(place.id)
            pixmap = sprite_pixmap(sprite.id, sprite.svg, SPRITE_SIZE) if sprite else None
            item = _PlaceItem(
                place,
                pixmap,
                people.get(place.id, []),
                visited=place.id in visited,
                current=place.id == here,
            )
            item.setPos(spot[0], -spot[1])
            item.setToolTip(place_card(world, place))
            self.scene.addItem(item)
            self._items.append(item)
        bounds = extent(world)
        if bounds is not None:
            west, south, east, north = bounds
            pad = max(east - west, north - south, 200.0) * 0.5
            self.scene.setSceneRect(
                QRectF(west - pad, -north - pad, east - west + 2 * pad, north - south + 2 * pad)
            )
        self._update_detail()

    def _update_detail(self) -> None:
        """Show a place inside another only once its parent is big enough on
        screen to hold it; places with nothing above them always show."""
        scale = self.view.transform().m11()
        places = self.world.places
        for item in self._items:
            place = item.place
            shown = True
            parent = places.get(place.inside or "")
            while parent is not None and shown:
                if position(self.world, parent.id) is not None:
                    shown = size_of(parent) * scale >= CHILD_SHOWN_AT
                parent = places.get(parent.inside or "")
            item.setVisible(shown or item.current)

    def _build_place(self, place: WorldPlace) -> None:
        world = self.world
        self.title.setText(place.name + (f" ({place.state})" if place.state else ""))
        layout = self.art.layout_for(place.id, place.state) or self.art.layout_for(place.id)
        side = [place_card(world, place)]
        if layout is None:
            self.hint.setText(
                "Not drawn yet: a place's plan is drawn the first time the story is there."
            )
        else:
            self.hint.setText("Seen from above, north at the top. Numbers are the things marked.")
            item = QGraphicsPixmapItem(
                tile_pixmap(tuple(layout.rows), str(layout.spec.get("ground", "")))
            )
            item.setTransformationMode(Qt.FastTransformation)
            item.setScale(TILE_PIXELS)
            self.scene.addItem(item)
            font = QFont()
            font.setPointSizeF(text_size.points(9))
            font.setBold(True)
            for name, (x, y) in layout.labels.items():
                label = QGraphicsSimpleTextItem(name)
                label.setFont(font)
                label.setBrush(QColor("#1b1d2b"))
                label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
                label.setPos((x + 0.5) * TILE_PIXELS, (y + 0.5) * TILE_PIXELS)
                self.scene.addItem(label)
            if layout.objects:
                side.append(
                    "<b>Marked</b><br>"
                    + "<br>".join(f"{n}. {escape(v)}" for n, v in layout.objects.items())
                )
            width = max((len(r) for r in layout.rows), default=1) * TILE_PIXELS
            self.scene.setSceneRect(QRectF(0, 0, width, len(layout.rows) * TILE_PIXELS))
        inside = [p for p in world.places.values() if p.inside == place.id]
        if inside:
            side.append("<b>In it</b><br>" + "<br>".join(escape(p.name) for p in inside))
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

    # --- moving about ------------------------------------------------------------

    def fit(self) -> None:
        """Everywhere the story has been in view (or the open place)."""
        self._fitted = True
        if self.place_id is not None:
            rect = self.scene.sceneRect()
            if not rect.isEmpty():
                self.view.resetTransform()
                self.view.fitInView(rect, Qt.KeepAspectRatio)
            return
        seen = self._visited() or self._shown()
        spots = [position(self.world, pid) for pid in seen]
        spots = [s for s in spots if s is not None]
        if not spots:
            return
        rect = QRectF()
        for pid in seen:
            spot = position(self.world, pid)
            if spot is None:
                continue
            reach = max(size_of(self.world.places[pid]), 120.0)
            rect = rect.united(QRectF(spot[0] - reach, -spot[1] - reach, 2 * reach, 2 * reach))
        self.view.resetTransform()
        self.view.fitInView(
            rect.adjusted(
                -rect.width() * 0.1, -rect.height() * 0.1, rect.width() * 0.1, rect.height() * 0.1
            ),
            Qt.KeepAspectRatio,
        )
        self._update_detail()

    def go_here(self) -> None:
        here = self.here()
        spot = position(self.world, here) if here else None
        if spot is not None:
            self.view.centerOn(spot[0], -spot[1])

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
    """The places drawn on the map now (for tests)."""
    return sorted(item.place.name for item in view._items if item.isVisible())


__all__ = ["WorldView", "place_card", "visible_names"]
