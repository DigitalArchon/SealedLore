"""The overmap of a visual story (models/overmap.py, engine/session_overmap.py;
the author, Oct 26 2026): the wider world as a painted map, its regions,
roads, rivers and the rail line, the places found so far, the country
explored, and where the player is. A place (or anywhere at all, to explore)
is chosen on it; the journey is told before it is made: how long, the
nights out, the regions crossed and how dangerous they are, the food,
water and warmth it takes. Setting out is the window's (gui/visual_window.py).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.overmap import (
    KM,
    cell_key,
    cell_rect,
    clamped_cell,
    region_at,
    size,
)
from sealedlore.gui import text_size
from sealedlore.gui.map_pan import MiddlePan
from sealedlore.gui.sizing import size_to_screen
from sealedlore.models.overmap import Overmap, OverRegion

# Pixels a kilometre on the painted map.
PX_PER_KM = 20
PAPER = QColor("#e9e6dc")
FILLS = {
    "fields": QColor("#eeede6"),
    "plains": QColor("#ebe4d3"),
    "woods": QColor("#b7c3b3"),
    "hills": QColor("#d4cbbc"),
    "marsh": QColor("#c5d0cd"),
    "ruins": QColor("#b8aca4"),
    "scrub": QColor("#dcd7c6"),
    "lake": QColor("#c3d6e1"),
}
GLYPHS = {
    "woods": QColor("#6b7d6c"),
    "hills": QColor("#8a7e6e"),
    "marsh": QColor("#76897f"),
    "ruins": QColor("#665a52"),
    "scrub": QColor("#9a9480"),
    "plains": QColor("#b2a68a"),
    "fields": QColor("#c2bfb2"),
}
WATER = QColor("#5b84a2")
ROAD = QColor("#6a5948")
ROAD_CASING = QColor("#f7f3e8")
RAIL = QColor("#4b4b4b")
DANGER = QColor(176, 52, 40)
EXPLORED = QColor(240, 196, 92, 70)
HERE = QColor("#e2b23f")
INK = QColor("#2f2c28")


def danger_words(region: OverRegion | None) -> str:
    """How dangerous a region is, in a word, by its chance an hour and
    its share of danger."""
    if region is None:
        return ""
    risk = region.chance * region.split[0] / 100
    if risk < 1.5:
        return "quiet"
    if risk < 3:
        return "uneasy"
    if risk < 6:
        return "risky"
    if risk < 25:
        return "dangerous"
    return "deadly"


def duration(seconds: int) -> str:
    """A journey's length in words: "about 40 minutes", "about 9 hours",
    "about a day and a half"."""
    minutes = round(seconds / 60)
    if minutes < 10:
        return "a few minutes"
    if minutes < 60:
        return f"about {minutes} minutes"
    hours = seconds / 3600
    if hours < 20:
        whole = round(hours * 2) / 2
        text = f"{whole:g}"
        return f"about {text} hour{'s' if whole != 1 else ''}"
    days = round(hours / 24 * 2) / 2
    if days <= 1:
        return "about a day"
    if days == 1.5:
        return "about a day and a half"
    return f"about {days:g} days"


@dataclass(frozen=True)
class Painted:
    image: QImage
    px_per_km: float


def paint_overmap(om: Overmap, seed: str = "") -> Painted:
    """The overmap as a picture: the regions' ground, a scatter of marks for
    what each is (trees, hills, reeds, ruins), their borders, dangerous
    country hatched, then the rivers, rail line and roads."""
    width = int((om.east - om.west) * PX_PER_KM)
    height = int((om.north - om.south) * PX_PER_KM)
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(PAPER)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)

    def to_px(x_km: float, y_km: float) -> QPointF:
        return QPointF((x_km - om.west) * PX_PER_KM, (om.north - y_km) * PX_PER_KM)

    # The ground: each region's boxes, last first, so the first in the list
    # (which wins where they overlap) is drawn over the rest.
    for region in reversed(om.regions):
        colour = FILLS.get(region.terrain, PAPER)
        for west, east, south, north in region.boxes:
            a, b = to_px(west, north), to_px(east, south)
            painter.fillRect(QRectF(a, b), colour)
    cols, rows = size(om)
    # What each region is, as marks scattered where it is the region.
    rng = random.Random(f"{seed}:overmap")
    step = 0.9  # km between marks
    y = om.south + step / 2
    while y < om.north:
        x = om.west + step / 2
        while x < om.east:
            jx, jy = x + rng.uniform(-0.35, 0.35), y + rng.uniform(-0.35, 0.35)
            region = region_at(om, jx * KM, jy * KM)
            if region is not None:
                _mark(painter, region.terrain, to_px(jx, jy), rng)
            x += step
        y += step
    # Borders between regions, and dangerous country hatched.
    border = QPen(QColor(90, 80, 70, 110), 1.2, Qt.DashLine)
    painter.setPen(border)
    cw, ch = om.cell_w * PX_PER_KM, om.cell_h * PX_PER_KM
    keys = [[_region_key(om, (i, j)) for i in range(cols)] for j in range(rows)]
    for j in range(rows):
        for i in range(cols):
            here = keys[j][i]
            if i + 1 < cols and keys[j][i + 1] != here:
                painter.drawLine(QPointF((i + 1) * cw, j * ch), QPointF((i + 1) * cw, (j + 1) * ch))
            if j + 1 < rows and keys[j + 1][i] != here:
                painter.drawLine(QPointF(i * cw, (j + 1) * ch), QPointF((i + 1) * cw, (j + 1) * ch))
    for region in om.regions:
        risk = region.chance * region.split[0] / 100
        if risk < 6:
            continue
        alpha = 40 if risk < 15 else 70
        painter.setPen(QPen(QColor(DANGER.red(), DANGER.green(), DANGER.blue(), alpha), 1.4))
        for west, east, south, north in region.boxes:
            box = QRectF(to_px(west, north), to_px(east, south))
            painter.save()
            clip = QPainterPath()
            clip.addRect(box)
            painter.setClipPath(clip)
            gap = 9.0
            span = box.width() + box.height()
            k = 0.0
            while k < span:
                painter.drawLine(
                    QPointF(box.left() + k, box.top()),
                    QPointF(box.left() + k - box.height(), box.bottom()),
                )
                k += gap
            painter.restore()
    # Water, then the rail line, then the roads on top.
    for line in om.lines:
        if line.kind not in ("river", "creek"):
            continue
        pen = QPen(WATER, 4.0 if line.kind == "river" else 2.0)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(QPolygonF([to_px(x, y) for x, y in line.points]))
    for line in om.lines:
        if line.kind != "rail":
            continue
        points = [to_px(x, y) for x, y in line.points]
        painter.setPen(QPen(RAIL, 1.6))
        painter.drawPolyline(QPolygonF(points))
        tie = QPen(RAIL, 1.0)
        painter.setPen(tie)
        for a, b in zip(points, points[1:], strict=False):
            length = math.hypot(b.x() - a.x(), b.y() - a.y())
            if not length:
                continue
            nx, ny = -(b.y() - a.y()) / length * 3, (b.x() - a.x()) / length * 3
            t = 0.0
            while t < length:
                px = a.x() + (b.x() - a.x()) * t / length
                py = a.y() + (b.y() - a.y()) * t / length
                painter.drawLine(QPointF(px - nx, py - ny), QPointF(px + nx, py + ny))
                t += 7.0
    for line in om.lines:
        if line.kind not in ("road", "track"):
            continue
        points = QPolygonF([to_px(x, y) for x, y in line.points])
        if line.kind == "road":
            casing = QPen(ROAD_CASING, 5.5)
            casing.setCapStyle(Qt.RoundCap)
            casing.setJoinStyle(Qt.RoundJoin)
            painter.setPen(casing)
            painter.drawPolyline(points)
            core = QPen(ROAD, 2.6 if "highway" in line.name else 2.0)
        else:
            core = QPen(ROAD, 1.4, Qt.DashLine)
        core.setCapStyle(Qt.RoundCap)
        core.setJoinStyle(Qt.RoundJoin)
        painter.setPen(core)
        painter.drawPolyline(points)
    painter.end()
    return Painted(image=image, px_per_km=PX_PER_KM)


def region_middles(om: Overmap) -> dict[str, tuple[float, float]]:
    """Where each region's name goes: the middle of the ground that is
    its own (a region's box may lie mostly under another's), on a km grid."""
    sums: dict[str, list[float]] = {}
    y = om.south + 0.5
    while y < om.north:
        x = om.west + 0.5
        while x < om.east:
            region = region_at(om, x * KM, y * KM)
            if region is not None:
                acc = sums.setdefault(region.key, [0.0, 0.0, 0.0])
                acc[0] += x
                acc[1] += y
                acc[2] += 1
            x += 1.0
        y += 1.0
    return {key: (acc[0] / acc[2] * KM, acc[1] / acc[2] * KM) for key, acc in sums.items()}


def _region_key(om: Overmap, cell: tuple[int, int]) -> str:
    west, south, east, north = cell_rect(om, cell)
    region = region_at(om, (west + east) / 2, (south + north) / 2)
    return region.key if region is not None else ""


def _mark(painter: QPainter, terrain: str, at: QPointF, rng: random.Random) -> None:
    colour = GLYPHS.get(terrain)
    if colour is None:
        return
    painter.setPen(QPen(colour, 1.1))
    x, y = at.x(), at.y()
    if terrain == "woods":
        painter.setBrush(QBrush(colour))
        for dx in (-3.5, 3.5) if rng.random() < 0.6 else (0,):
            tree = QPolygonF(
                [QPointF(x + dx, y - 6), QPointF(x + dx - 3.5, y + 2), QPointF(x + dx + 3.5, y + 2)]
            )
            painter.drawPolygon(tree)
        painter.setBrush(Qt.NoBrush)
    elif terrain == "hills":
        path = QPainterPath(QPointF(x - 6, y + 2))
        path.quadTo(QPointF(x, y - 6), QPointF(x + 6, y + 2))
        painter.drawPath(path)
    elif terrain == "marsh":
        for dx in (-3, 0, 3):
            painter.drawLine(QPointF(x + dx, y + 2), QPointF(x + dx * 1.4, y - 4))
        painter.drawLine(QPointF(x - 5, y + 2), QPointF(x + 5, y + 2))
    elif terrain == "ruins":
        painter.drawRect(QRectF(x - 3, y - 3, 5, 4))
        if rng.random() < 0.5:
            painter.drawRect(QRectF(x + 3, y - 1, 3, 3))
    elif terrain == "plains":
        painter.drawLine(QPointF(x - 5, y), QPointF(x + 5, y))
    elif terrain in ("scrub", "fields"):
        if rng.random() < 0.5:
            painter.drawPoint(QPointF(x, y))
            painter.drawPoint(QPointF(x + 2.5, y + 1))


class OvermapView(QGraphicsView):
    """The painted map, zoomed by the wheel, panned with the middle button
    or by dragging, a click choosing a point (metres)."""

    clicked = Signal(float, float)

    def __init__(self, om: Overmap) -> None:
        super().__init__()
        self.om = om
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(PAPER))
        self.setMouseTracking(True)
        self._pan = MiddlePan(self)
        self._pressed: QPointF | None = None
        self.items_kept: list[QGraphicsItem] = []

    def to_scene(self, x: float, y: float) -> QPointF:
        return QPointF((x / KM - self.om.west) * PX_PER_KM, (self.om.north - y / KM) * PX_PER_KM)

    def to_metres(self, point: QPointF) -> tuple[float, float]:
        return (
            (point.x() / PX_PER_KM + self.om.west) * KM,
            (self.om.north - point.y() / PX_PER_KM) * KM,
        )

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt's name
        notches = event.angleDelta().y() / 120
        factor = 1.25**notches
        scale = self.transform().m11() * factor
        if 0.2 <= scale <= 12:
            self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if self._pan.press(event):
            return
        if event.button() == Qt.LeftButton:
            self._pressed = event.position()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if self._pan.move(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if self._pan.release(event):
            return
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton and self._pressed is not None:
            moved = event.position() - self._pressed
            self._pressed = None
            if abs(moved.x()) + abs(moved.y()) < 6:
                x, y = self.to_metres(self.mapToScene(event.position().toPoint()))
                self.clicked.emit(x, y)


class OvermapDialog(QDialog):
    """The overmap, a list of the places found, and what a journey to the
    one chosen would take. `chosen` is (x, y, name, vehicle) once Set out is
    pressed (vehicle: the record id of the one driven, or None on foot)."""

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("The map")
        self.session = session
        self.om: Overmap = session.overmap()
        self.chosen: tuple[float, float, str, str | None] | None = None
        self._target: tuple[float, float, str] | None = None
        self._described = None
        self.view = OvermapView(self.om)
        self.view.clicked.connect(self._clicked)
        painted = paint_overmap(self.om, session.visual_seed())
        scene = self.view.scene()
        self._base = QGraphicsPixmapItem(QPixmap.fromImage(painted.image))
        scene.addItem(self._base)
        scene.setSceneRect(QRectF(painted.image.rect()))
        self._marker: QGraphicsEllipseItem | None = None
        self._dress()

        self.places = QListWidget()
        self.places.itemClicked.connect(self._picked)
        self.title = QLabel()
        self.title.setObjectName("emptyTitle")
        self.title.setWordWrap(True)
        self.region = QLabel()
        self.region.setWordWrap(True)
        self.journey = QLabel()
        self.journey.setWordWrap(True)
        self.carrying = QLabel()
        self.carrying.setObjectName("hintLabel")
        self.carrying.setWordWrap(True)
        self.how = QComboBox()
        self.how.addItem("On foot", None)
        for record_id, name, km in session.overmap_vehicles():
            self.how.addItem(f"Drive the {name} (fuel for about {round(km)} km)", record_id)
        self.how.setVisible(self.how.count() > 1)
        self.how.currentIndexChanged.connect(lambda _i: self._describe(self._described))
        self.set_out = QPushButton("Set out")
        self.set_out.setEnabled(False)
        self.set_out.clicked.connect(self._set_out)
        self.centre = QPushButton("◎ Where I am")
        self.centre.clicked.connect(self._centre_on_me)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(8, 0, 0, 0)
        found = QLabel("Places you know")
        found.setObjectName("emptyTitle")
        side_layout.addWidget(found)
        side_layout.addWidget(self.places, 2)
        side_layout.addWidget(self.title)
        side_layout.addWidget(self.region)
        side_layout.addWidget(self.journey)
        side_layout.addWidget(self.carrying)
        side_layout.addWidget(self.how)
        side_layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addWidget(self.centre)
        buttons.addStretch(1)
        buttons.addWidget(self.set_out)
        side_layout.addLayout(buttons)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        side_layout.addWidget(close)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.view)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        side.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)
        hint = QLabel(
            "Click a place you know, or anywhere at all to go and explore it. Drag to move the "
            "map, the wheel to zoom."
        )
        hint.setObjectName("hintLabel")
        layout.addWidget(hint)
        size_to_screen(self, 0.8, 0.8)
        self._fill_places()
        self._show_carrying()
        self._describe(None)

    # --- what is drawn over the map -----------------------------------------------

    def _dress(self) -> None:
        scene = self.view.scene()
        state = self.session.area_state()
        om = self.om
        cw, ch = om.cell_w * PX_PER_KM, om.cell_h * PX_PER_KM
        explored = QColor(EXPLORED)
        for key in state.overmap.explored:
            try:
                i, j = (int(p) for p in key.split(","))
            except ValueError:
                continue
            rect = scene.addRect(QRectF(i * cw, j * ch, cw, ch), QPen(Qt.NoPen), QBrush(explored))
            self.view.items_kept.append(rect)
        label_font = QFont()
        label_font.setItalic(True)
        label_font.setPointSizeF(text_size.points(13))
        label_font.setLetterSpacing(QFont.PercentageSpacing, 110)
        middles = region_middles(om)
        for region in om.regions:
            if region.key not in middles:
                continue
            mx, my = middles[region.key]
            label = QGraphicsSimpleTextItem(region.name)
            label.setFont(label_font)
            label.setBrush(QBrush(QColor(60, 52, 44, 120)))
            middle = self.view.to_scene(mx, my)
            box = label.boundingRect()
            label.setPos(middle.x() - box.width() / 2, middle.y() - box.height() / 2)
            scene.addItem(label)
            self.view.items_kept.append(label)
        place_font = QFont()
        place_font.setPointSizeF(text_size.points(10))
        place_font.setBold(True)
        for place in self.session.overmap_known(state):
            at = self.view.to_scene(place.x * KM, place.y * KM)
            dot = scene.addEllipse(
                QRectF(at.x() - 4, at.y() - 4, 8, 8), QPen(QColor("#f7f3e8"), 1.5), QBrush(INK)
            )
            dot.setToolTip(place.name)
            label = QGraphicsSimpleTextItem(place.name)
            label.setFont(place_font)
            label.setBrush(QBrush(INK))
            label.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            label.setPos(at.x() + 9, at.y() - 8)
            label.setZValue(12)  # over the "you are here" ring, which covered its first letter
            scene.addItem(label)
            self.view.items_kept += [dot, label]
        me = self.session.overmap_point(state)
        if me is not None:
            at = self.view.to_scene(*me)
            ring = scene.addEllipse(
                QRectF(at.x() - 7, at.y() - 7, 14, 14), QPen(INK, 2), QBrush(HERE)
            )
            ring.setZValue(10)
            ring.setToolTip("You are here")
            self.view.items_kept.append(ring)
            self._me = at
        else:
            self._me = None

    def showEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().showEvent(event)
        if not getattr(self, "_fitted", False):
            self._fitted = True
            self.view.fitInView(self.view.scene().sceneRect(), Qt.KeepAspectRatio)
            self.view.scale(2.2, 2.2)
            self._centre_on_me()

    def _centre_on_me(self) -> None:
        if self._me is not None:
            self.view.centerOn(self._me)

    # --- choosing ---------------------------------------------------------------------

    def _fill_places(self) -> None:
        self.places.clear()
        for choice in self.session.overmap_choices():
            words = "you are here" if choice.here else duration(choice.walking) + " on foot"
            item = QListWidgetItem(f"{choice.name} — {words}")
            item.setData(Qt.UserRole, (choice.x, choice.y, choice.name, choice.here))
            self.places.addItem(item)

    def _picked(self, item: QListWidgetItem) -> None:
        x, y, name, here = item.data(Qt.UserRole)
        self._choose(x, y, name, here=here)
        self.view.centerOn(self.view.to_scene(x, y))

    def _clicked(self, x: float, y: float) -> None:
        """A click: the known place near it, else the country there."""
        reach = 600.0 / max(self.view.transform().m11(), 0.2)
        best = None
        for choice in self.session.overmap_choices():
            d = math.hypot(choice.x - x, choice.y - y)
            if d <= reach and (best is None or d < best[0]):
                best = (d, choice)
        if best is not None:
            choice = best[1]
            self._choose(choice.x, choice.y, choice.name, here=choice.here)
            return
        cell = clamped_cell(self.om, x, y)
        west, south, east, north = cell_rect(self.om, cell)
        self._choose((west + east) / 2, (south + north) / 2, "", here=False, cell=cell)

    def _choose(self, x: float, y: float, name: str, *, here: bool, cell=None) -> None:
        self._target = (x, y, name)
        scene = self.view.scene()
        if self._marker is not None:
            scene.removeItem(self._marker)
        at = self.view.to_scene(x, y)
        self._marker = scene.addEllipse(
            QRectF(at.x() - 9, at.y() - 9, 18, 18), QPen(DANGER, 2.4), QBrush(Qt.NoBrush)
        )
        self._marker.setZValue(11)
        self._describe((x, y, name, here, cell))

    def _describe(self, target) -> None:
        self._described = target
        if target is None:
            region = self.session.overmap_region()
            self.title.setText("Choose where to go")
            self.region.setText(
                f"You are in {region.name}: {danger_words(region)}." if region else ""
            )
            self.journey.setText("")
            self.set_out.setEnabled(False)
            return
        x, y, name, here, cell = target
        region = region_at(self.om, x, y)
        if not name:
            explored = (
                cell is not None and cell_key(cell) in self.session.area_state().overmap.explored
            )
            name = (
                self.session.cell_name(cell)
                if explored
                else f"Unexplored: {region.name if region else 'open country'}"
            )
        self.title.setText(name)
        if region is not None:
            words = f" {region.words}" if region.words else ""
            self.region.setText(f"{region.name}: {danger_words(region)}.{words}")
        else:
            self.region.setText("")
        if here:
            self.journey.setText("You are here.")
            self.set_out.setEnabled(False)
            return
        vehicle = self.how.currentData()
        estimate = self.session.overmap_estimate(x, y, vehicle=vehicle)
        if estimate is None:
            self.journey.setText(
                "There is no way there by road from here."
                if vehicle
                else "There is no way there from here."
            )
            self.set_out.setEnabled(False)
            return
        parts = [f"{duration(estimate.walking).capitalize()} {'driving' if vehicle else 'on foot'}"]
        if estimate.road_share >= 0.6:
            parts[0] += ", most of it by road"
        elif estimate.road_share <= 0.2:
            parts[0] += ", across country"
        if estimate.nights:
            parts.append(
                "one night out" if estimate.nights == 1 else f"{estimate.nights} nights out"
            )
        lines = ["; ".join(parts) + "."]
        if estimate.regions:
            crossed = []
            for region_name in estimate.regions:
                found = next((r for r in self.om.regions if r.name == region_name), None)
                crossed.append(f"{region_name} ({danger_words(found)})")
            lines.append("Through " + ", ".join(crossed) + ".")
        needs = []
        if estimate.food:
            needs.append(f"{estimate.food} food")
        if estimate.drink:
            needs.append(f"{estimate.drink} drink")
        if needs:
            lines.append("It will take " + " and ".join(needs) + ".")
        if estimate.warm > 1:
            lines.append(f"The coldest of it needs you dressed warm ({estimate.warm}).")
        self.journey.setText("\n".join(lines))
        self.set_out.setEnabled(True)

    def _show_carrying(self) -> None:
        session = self.session
        food, drink = session.carried_provisions()
        warm = session.visual_warmth()
        short = session.visual_needs()
        text = f"You carry {food} food and {drink} drink, and are dressed warm ({warm})."
        if short:
            text += " You are " + ", ".join(short) + "."
        self.carrying.setText(text)

    def _set_out(self) -> None:
        if self._target is None:
            return
        x, y, name = self._target
        self.chosen = (x, y, name, self.how.currentData())
        self.accept()
