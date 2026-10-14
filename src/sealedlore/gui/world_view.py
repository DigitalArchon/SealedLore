"""The map (the Inspector's first page for a story that keeps one): the
world the story has been through, drawn as it grows (models/world.py).

Two views. **Here** is the place the author's character is in, at walking
scale: its plan drawn in the program's tileset (gui/map_tiles.py), buildings
seen from outside (See inside takes the roofs off), the marked things lying
about, and everyone the story has there standing in it, the author's
character ringed. **Region** is the country: the land the story has reached
(engine/world_art.py) with fog over the rest, each place where the program
placed it with its picture, lit where the character has been, dimmed where
the story only told of it, ringed where they are now; a place inside another
shows once the map is zoomed in far enough to read it. Double-click a place
to stand in its scene; Region goes back out.

Everything here is read from the session; the view never changes the story.
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QLabel,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.world_layout import extent, position, size_of
from sealedlore.gui import text_size
from sealedlore.gui.map_art import FOG, picture_sprite, sprite_pixmap
from sealedlore.gui.map_tiles import (
    TILE,
    building_boxes,
    person_sprite,
    prop_sprite,
    tiles_pixmap,
)
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.models.world import WorldArt, WorldMap, WorldPlace

SPRITE_SIZE = 48  # on screen, whatever the zoom
# A place inside another is shown once its parent is this many pixels wide.
CHILD_SHOWN_AT = 160
ZOOM_STEP = 1.25
# Ground a plan may share with the land around it (`_ground_for`).
_OPEN_GROUND = frozenset({"snow", "grass", "dirt", "sand"})
# A scene's people, in art pixels (the tileset's scale) before the view's zoom.
PERSON_HEIGHT = 20
AVATAR_HEIGHT = 26
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
        """Its middle on (x, y) in scene units, wherever the zoom puts the text."""
        self.setPos(x, y)
        rect = self.boundingRect()
        self.setTransformOriginPoint(rect.center())
        self.setTransform(QTransform.fromTranslate(-rect.width() / 2, -rect.height() / 2))


def _key(name: str) -> str:
    """A place's name as a plan's label might give it: no case, no "the",
    nothing in brackets ("barn (blown up, burning)" is the barn)."""
    name = name.lower().split("(")[0].strip()
    return name[4:] if name.startswith("the ") else name


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
        self.here_button = self._button("Here", "The place the story is now, close up")
        self.region_button = self._button("Region", "The whole country the story has reached")
        for button in (self.here_button, self.region_button):
            button.setCheckable(True)
        self.zoom_out = self._button("−", "Zoom out (-, or the mouse wheel)")
        self.zoom_in = self._button("+", "Zoom in (+, or the mouse wheel)")
        self.fit_button = self._button("Fit", "All of it in view")
        self.inside_button = self._button("See inside", "Take the roofs off, to see inside")
        self.inside_button.setCheckable(True)
        # Kept for the window's Esc: up from a place to the region.
        self.up_button = self.region_button
        header = WrapRow()
        header.add_group([self.title])
        header.add_group([self.here_button, self.region_button])
        header.add_group([self.zoom_out, self.zoom_in, self.fit_button])
        header.add_group([self.inside_button])
        self.view = _WorldGraphics()
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self.view.place_opened.connect(self.open_place)
        self.view.zoomed.connect(self._update_detail)
        self.zoom_out.clicked.connect(lambda: self.view.zoom_by(1 / ZOOM_STEP))
        self.zoom_in.clicked.connect(lambda: self.view.zoom_by(ZOOM_STEP))
        self.fit_button.clicked.connect(self.fit)
        self.here_button.clicked.connect(lambda: self.go_here())
        self.region_button.clicked.connect(lambda: self.open_place(None))
        self.inside_button.toggled.connect(lambda _on: self._rebuild_scene())
        # A place's card, under its scene: what the story said of it, who and
        # what is there.
        self.side = QLabel()
        self.side.setObjectName("worldMapSide")
        self.side.setWordWrap(True)
        self.side.setTextFormat(Qt.RichText)
        self.side.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.side.setContentsMargins(0, 0, 6, 0)
        self.card = QScrollArea()
        self.card.setObjectName("worldMapCard")
        self.card.setWidget(self.side)
        self.card.setWidgetResizable(True)
        self.card.setFrameShape(QFrame.NoFrame)
        self.card.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.card.setMaximumHeight(120)
        self.card.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self.hint)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.card)
        self.world = WorldMap()
        self.art = WorldArt()
        self.held: str | None = None
        # Reads a map picture's file from the story (the window sets it).
        self.picture_reader: Callable[[str], bytes | None] | None = None
        self.place_id: str | None = None
        self._items: list[_PlaceItem] = []
        self.people: list[_PersonItem] = []
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
        redraw as art comes in); off, it opens on the scene where the story
        is now, or the region when there is none."""
        self.world, self.art, self.held = world, art, held
        if not keep_view:
            self.place_id = self.scene_for(self.here())
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
        """A place's scene, or (None) the whole region."""
        self.place_id = place_id if place_id in self.world.places else None
        self._fitted = False
        self._build()
        self.fit()

    def go_here(self) -> None:
        """The scene where the story is now (or, with none drawn, the region
        centred on it)."""
        scene = self.scene_for(self.here())
        if scene is not None:
            self.open_place(scene)
            return
        self.open_place(None)
        here = self.here()
        spot = position(self.world, here) if here else None
        if spot is not None:
            self.view.centerOn(spot[0], -spot[1])
        self._sync_buttons()

    def here(self) -> str | None:
        """Where the author's character is: as the story last put them, or an
        imported map's start before it has."""
        return self.world.where.get(self.held or "") or self.world.start

    def scene_for(self, place_id: str | None) -> str | None:
        """The place whose plan shows `place_id` close up: its own, or the
        nearest place it is in that has one."""
        seen: set[str] = set()
        while place_id is not None and place_id in self.world.places and place_id not in seen:
            seen.add(place_id)
            if self.art.layout_for(place_id):
                return place_id
            place_id = self.world.places[place_id].inside
        return None

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

    def _sync_buttons(self) -> None:
        in_place = self.place_id is not None
        here_scene = self.scene_for(self.here())
        self.here_button.setChecked(in_place and self.place_id == here_scene)
        self.region_button.setChecked(not in_place)
        self.inside_button.setVisible(in_place)
        self.card.setVisible(in_place)

    def _rebuild_scene(self) -> None:
        if self.place_id is not None:
            self._build()

    def _build(self) -> None:
        self.scene.clear()
        self._items = []
        self.people = []
        self._sync_buttons()
        if self.place_id is not None:
            self._build_place(self.world.places[self.place_id])
            return
        self.title.setText("Map")
        if not self.world.places:
            self.hint.setText(
                "Nothing on the map yet: it grows as the story goes, from the next passage on."
            )
            return
        self.hint.setText(
            "The country the story has reached. Lit: where you've been; dimmed: places the "
            "story told of; ringed: where you are now. Double-click a place to stand in it."
        )
        self._build_world()

    def _build_world(self) -> None:
        world = self.world
        for patch in self.art.terrain:
            if not patch.rows:
                continue
            item = QGraphicsPixmapItem(tiles_pixmap(tuple(patch.rows), patch.ground))
            item.setTransformationMode(Qt.FastTransformation)
            item.setShapeMode(QGraphicsPixmapItem.BoundingRectShape)
            item.setScale(patch.side / (max(len(patch.rows[0]), 1) * TILE))
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
            pixmap = self._picture_pixmap("place", place.id, place.state, SPRITE_SIZE)
            if pixmap is None:
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
        people = self._people_in(place.id)
        if layout is None:
            self.hint.setText(
                "Not drawn yet: a place's plan is drawn the first time the story is there."
            )
        else:
            self.hint.setText(
                "North at the top. Hover the things marked; double-click a place on the "
                "Region to stand in it."
            )
            rows = tuple(layout.rows)
            ground = self._ground_for(place, str(layout.spec.get("ground", "")))
            roofs = not self.inside_button.isChecked()
            # A building with a picture stands there as drawn, on cleared ground.
            pictured = self._pictured_buildings(rows, layout.labels) if roofs else {}
            item = QGraphicsPixmapItem(tiles_pixmap(rows, ground, roofs, tuple(pictured)))
            item.setTransformationMode(Qt.FastTransformation)
            item.setShapeMode(QGraphicsPixmapItem.BoundingRectShape)
            self.scene.addItem(item)
            for (x0, x1, _y0, y1), image in pictured.items():
                building = QGraphicsPixmapItem(QPixmap.fromImage(image))
                building.setTransformationMode(Qt.FastTransformation)
                building.setPos(
                    (x0 + x1 + 1) * TILE / 2 - image.width() / 2, (y1 + 1) * TILE - image.height()
                )
                building.setZValue(2)
                self.scene.addItem(building)
            boxes = building_boxes(rows) if roofs else []
            self._marked(rows, layout.objects, boxes)
            spots = {_key(name): (x, y) for name, (x, y) in layout.labels.items()}
            houses = building_boxes(rows)
            for name, (x, y) in layout.labels.items():
                tag = _TagItem(name.split("(")[0].strip())
                box = next((b for b in houses if b[0] <= x <= b[1] and b[2] <= y <= b[3]), None)
                if box is not None:  # a building's name on its roof, or its far wall
                    x, y = (box[0] + box[1]) / 2, box[2] + (0.6 if roofs else -0.4)
                tag.centre_on(x * TILE + TILE / 2, y * TILE)
                self.scene.addItem(tag)
            self._stand(people, spots, boxes, layout.rows)
            if layout.objects:
                side.append(
                    "<b>Marked</b><br>"
                    + "<br>".join(f"{n}. {escape(v)}" for n, v in layout.objects.items())
                )
            width = max((len(r) for r in rows), default=1) * TILE
            self.scene.setSceneRect(QRectF(0, 0, width, len(rows) * TILE))
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

    def _picture_image(self, subject: str, subject_id: str, state: str | None, height: int):
        """A map picture, cut out and shrunk to `height`: in this state, else
        the latest. None when there is none (or its file can't be read)."""
        picture = self.art.picture_for(subject, subject_id, state) or self.art.picture_for(
            subject, subject_id
        )
        if picture is None or self.picture_reader is None:
            return None
        data = self.picture_reader(picture.file)
        return picture_sprite(picture.id, data, height) if data else None

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

    def _pictured_buildings(self, rows: tuple[str, ...], labels: dict) -> dict:
        """Each building in the plan the story's place has a picture of: its
        box, and the picture shrunk to stand on it."""
        found: dict = {}
        boxes = building_boxes(rows)
        for name, (x, y) in labels.items():
            place = self._place_named(name)
            box = next((b for b in boxes if b[0] <= x <= b[1] and b[2] <= y <= b[3]), None)
            if place is None or box is None or box in found:
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

    def _ground_for(self, place: WorldPlace, ground: str) -> str:
        """A plan's open ground as the land it stands on has it: live, a barn's
        plan came back on summer grass in a snowbound country. A floor, rock
        or space is the plan's own."""
        spot = position(self.world, place.id)
        if spot is None or (ground and ground not in _OPEN_GROUND):
            return ground
        for patch in self.art.terrain:
            half = patch.side / 2
            if abs(spot[0] - patch.x) <= half and abs(spot[1] - patch.y) <= half:
                return patch.ground if patch.ground in _OPEN_GROUND else ground
        return ground

    def _people_in(self, place_id: str) -> list[tuple[str, str]]:
        """(who, where) for everyone the story has at this place or somewhere
        inside it."""
        out = []
        for who, at in self.world.where.items():
            seen: set[str] = set()
            spot = at
            while spot is not None and spot not in seen:
                if spot == place_id:
                    out.append((who, at))
                    break
                seen.add(spot)
                spot = self.world.places[spot].inside if spot in self.world.places else None
        return out

    def _marked(
        self,
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
                prop = QGraphicsPixmapItem(QPixmap.fromImage(prop_sprite(int(ch))))
                prop.setTransformationMode(Qt.FastTransformation)
                prop.setPos(x * TILE + 2, y * TILE + 2)
                prop.setZValue(3)
                prop.setToolTip(objects.get(ch, "something marked here"))
                self.scene.addItem(prop)

    def _stand(
        self,
        people: list[tuple[str, str]],
        spots: dict[str, tuple[int, int]],
        boxes: list[tuple[int, int, int, int]],
        rows: list[str],
    ) -> None:
        """Everyone where the plan marks their place; outside a roofed
        building, at its front; otherwise in the middle. Several in one spot
        stand side by side."""
        width = max((len(r) for r in rows), default=1)
        groups: dict[tuple[float, float], list[str]] = {}
        for who, at in people:
            place = self.world.places.get(at)
            names = [place.name, *place.aliases] if place is not None else []
            spot = next((spots[_key(n)] for n in names if _key(n) in spots), None)
            if spot is None:
                x, y = width * TILE / 2, len(rows) * TILE / 2
            else:
                tx, ty = spot
                box = next((b for b in boxes if b[0] <= tx <= b[1] and b[2] <= ty <= b[3]), None)
                if box is not None:  # roofed over: they stand at its front
                    ty = box[3] + 1
                x, y = tx * TILE + TILE / 2, ty * TILE + TILE - 2
            groups.setdefault((x, y), []).append(who)
        for (x, y), names in groups.items():
            names.sort(key=lambda n: n != self.held)
            for i, name in enumerate(names):
                offset = (i - (len(names) - 1) / 2) * 14
                avatar = self._picture_image("person", name, None, AVATAR_HEIGHT)
                figure = _PersonItem(name, avatar or person_sprite(name), held=name == self.held)
                figure.setPos(x + offset, y)
                self.scene.addItem(figure)
                self.people.append(figure)
                tag = _TagItem(name.split()[0], bold=name == self.held)
                tag.centre_on(x + offset, y - figure.image.height() - 6)
                self.scene.addItem(tag)

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

    # --- moving about ------------------------------------------------------------

    def fit(self) -> None:
        """All of a scene in view, at a whole number of screen pixels to its
        pixels where it fits (crisp); in the region, everywhere the story has
        been."""
        self._fitted = True
        if self.place_id is not None:
            rect = self.scene.sceneRect()
            if rect.isEmpty():
                return
            port = self.view.viewport().rect()
            scale = min(port.width() / rect.width(), port.height() / rect.height())
            self.view.resetTransform()
            if scale >= 2:  # crisp: a whole number of screen pixels to each
                scale = int(scale)
            self.view.scale(scale, scale)
            self.view.centerOn(rect.center())
            return
        seen = self._visited() or self._shown()
        rect = QRectF()
        for pid in seen:
            spot = position(self.world, pid)
            if spot is None:
                continue
            reach = max(size_of(self.world.places[pid]), 120.0)
            rect = rect.united(QRectF(spot[0] - reach, -spot[1] - reach, 2 * reach, 2 * reach))
        if rect.isEmpty():
            return
        self.view.resetTransform()
        self.view.fitInView(
            rect.adjusted(
                -rect.width() * 0.1, -rect.height() * 0.1, rect.width() * 0.1, rect.height() * 0.1
            ),
            Qt.KeepAspectRatio,
        )
        self._update_detail()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        if self.place_id is not None and self._fitted:
            self.fit()

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
