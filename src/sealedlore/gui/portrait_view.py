"""A character's portrait, large, at the top of their card on the Cast page
(the author, Oct 15 2026: with the Stories list put away there is room for
the pages beside the story to be more graphical). The portrait is the
card's first picture; the rest stay small in the strip under it.

Held at the size it can be shown, never whole (as the transcript's pictures
are, `ref_images.pixmap_within`); a click shows the file whole.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.image_dialog import FitPicture
from sealedlore.gui.ref_images import image_of, pixmap_of

# The tallest the portrait is drawn, and the widest it is held at.
PORTRAIT_MAX_HEIGHT = 380
PORTRAIT_HELD_WIDTH = 720


class _Picture(QLabel):
    """The picture scaled to the width it is given, up to a height."""

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("portraitPicture")
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumSize(1, 1)
        self._image = QImage()

    def set_image(self, image: QImage) -> None:
        if not image.isNull() and image.width() > PORTRAIT_HELD_WIDTH:
            image = image.scaledToWidth(PORTRAIT_HELD_WIDTH, Qt.SmoothTransformation)
        self._image = image
        self.setCursor(Qt.PointingHandCursor if not image.isNull() else Qt.ArrowCursor)
        self._rescale()
        self.updateGeometry()

    def _box(self, width: int) -> QSize:
        if self._image.isNull():
            return QSize(0, 0)
        return self._image.size().scaled(
            QSize(max(1, width), PORTRAIT_MAX_HEIGHT), Qt.KeepAspectRatio
        )

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._box(width).height()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(PORTRAIT_MAX_HEIGHT, self.heightForWidth(PORTRAIT_MAX_HEIGHT))

    def _rescale(self) -> None:
        if self._image.isNull():
            self.clear()
            return
        box = self._box(self.width())
        self.setPixmap(
            QPixmap.fromImage(self._image.scaled(box, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._rescale()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and not self._image.isNull():
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class PortraitView(QWidget):
    """The portrait, or a place for one, and the ways to get one."""

    draw_requested = Signal()
    add_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.picture = _Picture()
        self.picture.clicked.connect(self._show_whole)
        self.empty = QLabel("No portrait yet")
        self.empty.setObjectName("portraitEmpty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setMinimumHeight(120)
        self.caption = QLabel()
        self.caption.setObjectName("hintLabel")
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setWordWrap(True)
        self.draw_button = QPushButton("Draw a portrait…")
        self.draw_button.setToolTip(
            "A portrait from their card by an image model; you see the prompt and the price "
            "first. It becomes their first picture."
        )
        self.draw_button.clicked.connect(self.draw_requested)
        self.add_button = QPushButton("Choose a file…")
        self.add_button.setToolTip("A picture from your computer as their portrait")
        self.add_button.clicked.connect(self.add_requested)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addStretch(1)
        buttons.addWidget(self.draw_button)
        buttons.addWidget(self.add_button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(4)
        layout.addWidget(self.picture)
        layout.addWidget(self.empty)
        layout.addWidget(self.caption)
        layout.addLayout(buttons)
        self._data: bytes | None = None
        self._name = ""
        self.show_portrait(None, "", "")

    def show_portrait(self, data: bytes | None, caption: str, name: str) -> None:
        """`data`: the portrait's file, or None for none."""
        self._data, self._name = data, name
        image = image_of(data) if data else QImage()
        has = not image.isNull()
        self.picture.set_image(image)
        self.picture.setVisible(has)
        self.empty.setVisible(not has)
        self.caption.setText(caption if has else "")
        self.caption.setVisible(has and bool(caption))
        self.picture.setToolTip(f"{name}: click to see it whole" if has else "")
        self.draw_button.setText("Draw a new portrait…" if has else "Draw a portrait…")

    def set_offered(self, draw: bool, add: bool) -> None:
        self.draw_button.setVisible(draw)
        self.add_button.setVisible(add)

    def _show_whole(self) -> None:
        if not self._data:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(self._name or "Portrait")
        layout = QVBoxLayout(dialog)
        layout.addWidget(FitPicture(pixmap_of(self._data)))
        dialog.resize(800, 860)
        dialog.exec()
