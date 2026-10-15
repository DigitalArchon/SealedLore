"""The isometric views on the map (Map → Isometric): each local area's own
painting, laid over the area it shows on the 2D map (gui/world_view.py),
and what can be done for the area in view.

The author (Oct 15 2026): the 2D map is the source of truth; an isometric
view matches it closely, not exactly, and says so. Each area has its own
view, the map marks the areas that have one before Isometric is pressed,
and a view lies over the map where its area is, so it is plain what it
shows and where that is. A view is never painted on its own: the author
presses Paint it (with its price), and again once the map there has
changed (a view painted under another state says it is out of date), or
brings in a picture of their own.

This module holds the bar over the map while the views are shown, and the
view seen whole (`IsometricDialog`); the map draws the views themselves.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.wrap_row import WrapRow

ZOOM_STEP = 1.25
TRUTH_NOTE = (
    "Painted from the map by {model}. The 2D map is the story's truth: this view matches it "
    "closely, not exactly."
)
OWN_NOTE = (
    "A picture of your own. The 2D map is the story's truth: the storyteller never sees this view."
)
NO_VIEW_NOTE = (
    "A view is painted from this area's map by an image model when you press Paint it, or is "
    "a picture of your own; it lies over the map where the area is."
)
# How much of the map shows through the views, at most: some of the view
# always shows, or the slider would hide what it is for.
MIN_OPACITY = 20


class _PictureView(QGraphicsView):
    """The painting, fitted to the view; the wheel zooms, a drag moves it."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("isometricView")
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self._item: QGraphicsPixmapItem | None = None
        self._fitted = True

    def show_image(self, image: QImage | None) -> None:
        self.scene().clear()
        self._item = None
        if image is None or image.isNull():
            return
        self._item = self.scene().addPixmap(QPixmap.fromImage(image))
        self.scene().setSceneRect(self._item.boundingRect())
        self._fitted = True
        self._fit()

    def _fit(self) -> None:
        if self._item is not None:
            self.fitInView(self._item, Qt.KeepAspectRatio)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt's name
        step = ZOOM_STEP if event.angleDelta().y() > 0 else 1 / ZOOM_STEP
        self._fitted = False
        self.scale(step, step)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        if self._fitted:
            self._fit()


def view_note(painted_by: str) -> str:
    """What a view is: painted from the map by a model, or the author's own."""
    return TRUTH_NOTE.format(model=painted_by) if painted_by else OWN_NOTE


class IsometricDialog(QDialog):
    """A view seen whole: the wheel zooms, a drag moves it."""

    def __init__(self, place: str, image: QImage, painted_by: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{place}: the isometric view")
        self.picture = _PictureView()
        note = QLabel(view_note(painted_by))
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(note, 1)
        row.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.picture, 1)
        layout.addLayout(row)
        self.resize(1100, 720)
        self.picture.show_image(image)


class IsometricBar(QWidget):
    """Over the map while the isometric views are shown, one row where it
    fits: what the area in view has (its view, an out-of-date one, or none),
    the offer to paint it or bring a picture in, its view whole, and how much
    of the map shows through the views. What the view is (`note`) goes in
    the map's hint line."""

    paint_requested = Signal()
    # The author's own picture as the view (a file chosen and cropped).
    own_requested = Signal()
    whole_requested = Signal()
    # 0-1: how opaque the views are drawn over the map.
    opacity_changed = Signal(float)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("isometricBar")
        self.offer = QLabel()
        # What the view is, for the map's hint line ("" with no view).
        self.note = ""
        self.paint_button = QPushButton("Paint it")
        self.paint_button.clicked.connect(self.paint_requested.emit)
        self.own_button = QPushButton("Use a picture of your own…")
        self.own_button.setToolTip(
            "Bring in a picture from disk as this area's isometric view, cropped to its shape"
        )
        self.own_button.clicked.connect(self.own_requested.emit)
        self.whole_button = QPushButton("See it whole")
        self.whole_button.setToolTip("This area's view on its own, to zoom into")
        self.whole_button.clicked.connect(self.whole_requested.emit)
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(MIN_OPACITY, 100)
        self.opacity.setValue(100)
        self.opacity.setFixedWidth(110)  # a group of its own width: the row wraps, not squeezes
        self.opacity.setToolTip("Fade the views to see the 2D map under them")
        self.opacity.valueChanged.connect(lambda value: self.opacity_changed.emit(value / 100))
        fade = QLabel("Fade")
        fade.setObjectName("hintLabel")
        fade.setToolTip(self.opacity.toolTip())
        row = WrapRow(spacing=6, group_spacing=10)
        row.add_group([self.offer])
        row.add_group([self.paint_button])
        row.add_group([self.own_button])
        row.add_group([self.whole_button])
        row.add_group([fade, self.opacity])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)

    def show_state(
        self,
        *,
        place: str,
        has_view: bool,
        current: bool,
        painting: bool,
        model: str,
        price: float | None,
        problem: str | None,
        painted_by: str = "",
    ) -> None:
        """The area in view (`place`): whether it `has_view`, `current` when
        that was painted for the map as it is now, and the offer to paint it
        (`model`, `price`, or the `problem` that stops it)."""
        cost = f", ${price:.2f}" if price else ""
        if painting:
            said = f"Painting the view of {place}…"
        elif not has_view:
            said = f"{place} has no isometric view yet."
        elif not current:
            said = f"{place}: the map has changed since this view was painted."
        else:
            said = f"{place}: its isometric view."
        self.offer.setText(said)
        again = "Paint it again" if has_view else "Paint it"
        self.paint_button.setText(f"{again} ({model}{cost})" if model else again)
        self.paint_button.setVisible(not painting)
        self.paint_button.setEnabled(problem is None)
        self.paint_button.setToolTip(
            problem
            or "An image model paints this area from its map, now; the price is listed by the "
            "endpoint"
        )
        self.own_button.setEnabled(not painting)
        self.whole_button.setVisible(has_view)
        self.note = view_note(painted_by) if has_view else NO_VIEW_NOTE
        if problem and not painting:
            self.note = f"{problem} {self.note}"
