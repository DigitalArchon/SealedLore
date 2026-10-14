"""The isometric view of the local map (Map → Isometric): a picture an image
model painted from the program's own render of the plan, when the author
asked for it (engine/session_world_art.py `start_isometric`).

The author (Oct 15 2026): the 2D map is the source of truth; the isometric
view matches it closely, not exactly, and says so. It is never painted on
its own: the author presses Paint (with its price), and again once the map
there has changed (a view painted under another state says it is out of
date).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

ZOOM_STEP = 1.25
TRUTH_NOTE = (
    "Painted from the map by {model}. The 2D map is the story's truth: this view matches it "
    "closely, not exactly."
)


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


class IsometricPanel(QWidget):
    """The view, what it is, and the offer to paint it."""

    paint_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.picture = _PictureView()
        self.note = QLabel()
        self.note.setObjectName("hintLabel")
        self.note.setWordWrap(True)
        self.offer = QLabel()
        self.offer.setObjectName("hintLabel")
        self.offer.setWordWrap(True)
        self.paint_button = QPushButton("Paint it")
        self.paint_button.clicked.connect(self.paint_requested.emit)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.offer, 1)
        row.addWidget(self.paint_button, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(row)
        layout.addWidget(self.picture, 1)
        layout.addWidget(self.note)
        self._shown_file: str | None = None

    def show_state(
        self,
        *,
        place: str,
        image: QImage | None,
        file: str | None,
        current: bool,
        painting: bool,
        model: str,
        price: float | None,
        problem: str | None,
    ) -> None:
        """`image` (from `file`) is the latest view of `place`, `current`
        when it was painted for the map as it is now."""
        if file != self._shown_file:
            self.picture.show_image(image)
            self._shown_file = file
        cost = f", ${price:.2f}" if price else ""
        if painting:
            self.offer.setText(f"Painting the isometric view of {place}…")
            self.paint_button.hide()
        elif image is not None and current:
            self.offer.setText("")
            self.paint_button.hide()
        else:
            if image is None:
                said = f"{place} has no isometric view yet."
                self.paint_button.setText(f"Paint it ({model}{cost})")
            else:
                said = f"The map of {place} has changed since this view was painted."
                self.paint_button.setText(f"Paint it again ({model}{cost})")
            self.offer.setText(said + (f" {problem}" if problem else ""))
            self.paint_button.setVisible(True)
            self.paint_button.setEnabled(problem is None)
        self.note.setText(TRUTH_NOTE.format(model=model) if image is not None else "")
        self.note.setVisible(image is not None)
