"""The stage (View → Characters above the box you type in): who is in the
scene now, large, in the look each is shown with (engine/looks.py), as a
strip between the story and the box the author types in. The author's call
(Nov 2 2026): optional, since it costs room, but it turns a story into
something like a visual novel. The page shows; the window fills it."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

# The tallest a picture on the stage is drawn.
STAGE_HEIGHT = 180


class _Face(QWidget):
    def __init__(
        self,
        name: str,
        pixmap: QPixmap,
        parent: QWidget,
        opened: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        # A click shows the picture whole (the window's viewer).
        self._opened = opened
        if opened is not None:
            self.setCursor(Qt.PointingHandCursor)
        picture = QLabel(self)
        picture.setPixmap(pixmap)
        picture.setAlignment(Qt.AlignCenter)
        label = QLabel(name, self)
        label.setObjectName("stageName")
        label.setAlignment(Qt.AlignCenter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(picture)
        layout.addWidget(label)
        self.setToolTip(f"{name}: click to see the picture full size" if opened else name)
        self.name = name

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self._opened is not None:
            self._opened()
            return
        super().mouseReleaseEvent(event)


class Stage(QFrame):
    """A row of faces, centred, scrolling never: as many as fit are shown."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("stage")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(8, 6, 8, 6)
        self._row.setSpacing(16)
        self.faces: list[_Face] = []

    def set_people(self, people: Sequence[tuple]) -> None:
        """(name, picture at STAGE_HEIGHT[, what opens it whole]), the one
        played first."""
        while self._row.count():
            item = self._row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.faces = []
        self._row.addStretch(1)
        for name, pixmap, *opened in people:
            face = _Face(name, pixmap, self, opened[0] if opened else None)
            self._row.addWidget(face)
            self.faces.append(face)
        self._row.addStretch(1)
