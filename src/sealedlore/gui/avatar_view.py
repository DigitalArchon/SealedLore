"""A character's avatar on the map, on their card under the portrait (the
author, Oct 16 2026: see it full size, change it and draw it again as with
the portrait, and draw a new one from a new portrait). Shown only while the
story is played on a map; the window answers its requests through the
session's tile-asset methods (engine/session_area.py), so a change here is
the same as one made in Edit the tile set….

The sprite is pixel art: scaled by whole pixels when shown larger than it
is (`pixel_scaled`), never smoothed into a blur.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.wrap_row import WrapRow

# The avatar is shown this tall on the card, and about this tall whole.
AVATAR_HEIGHT = 112
WHOLE_HEIGHT = 560

# Written out whole: tests/test_readme_labels.py finds a control the manual
# names among the GUI's string literals.
DRAW_LABELS = ("Draw the avatar…", "Draw the avatar again…")
SOURCES = {
    "": "A stand-in figure walks for them until one is drawn",
    "drawn for this story": "Drawn for this story",
    "changed for this story": "Chosen or drawn again for this story",
    "the set's": "One of the tile set's own figures",
}


def pixel_scaled(image: QImage, height: int) -> QImage:
    """The sprite at about `height`: by a whole number of pixels each way
    when shown larger (crisp), smoothly when shown smaller."""
    if image.isNull() or image.height() <= 0:
        return image
    if image.height() >= height:
        return image.scaledToHeight(height, Qt.SmoothTransformation)
    factor = max(1, height // image.height())
    return image.scaled(
        image.width() * factor, image.height() * factor, Qt.KeepAspectRatio, Qt.FastTransformation
    )


class _Sprite(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self.pixmap() is not None:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class AvatarView(QWidget):
    """The avatar, where it came from, and what can be done with it: draw
    one (from the portrait when there is one, else from the card), choose a
    file, go back to the set's own."""

    draw_requested = Signal()
    # Drawn from words the author gives (not the portrait).
    draw_with_words_requested = Signal()
    # One of the avatars kept for them (a map picture's id) walks for them.
    choose_requested = Signal(str)
    add_requested = Signal()
    reset_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("avatarView")
        self.title = QLabel("On the map")
        self.title.setObjectName("composerCaption")
        self.picture = _Sprite()
        self.picture.setObjectName("avatarPicture")
        self.picture.setAlignment(Qt.AlignCenter)
        self.picture.setMinimumHeight(AVATAR_HEIGHT)
        self.picture.setToolTip("Click to see it whole")
        self.picture.clicked.connect(self.show_whole)
        self.source = QLabel()
        self.source.setObjectName("hintLabel")
        self.source.setWordWrap(True)
        self.draw_button = QPushButton(DRAW_LABELS[0])
        self.draw_button.setToolTip(
            "A pixel-art figure for the map, from the portrait when there is one, else from "
            "what the card says of them; you see the model and the price first. It counts "
            "against today's map pictures."
        )
        self.draw_button.clicked.connect(self.draw_requested)
        self.words_button = QPushButton("Draw with instructions…")
        self.words_button.setToolTip(
            "Say what the avatar should look like, in a few words, and it is drawn from them "
            "(not the portrait); the model and price are shown first. Every avatar drawn is "
            "kept: choose among them below, or in Media."
        )
        self.words_button.clicked.connect(self.draw_with_words_requested)
        self.choose_button = QToolButton()
        self.choose_button.setText("Choose a drawn avatar ▾")
        self.choose_button.setToolTip("The avatars drawn or chosen for them so far: pick one")
        self.choose_button.setPopupMode(QToolButton.InstantPopup)
        self.choose_menu = QMenu(self.choose_button)
        self.choose_button.setMenu(self.choose_menu)
        self.choose_button.setVisible(False)
        self.add_button = QPushButton("Choose a file…")
        self.add_button.setToolTip(
            "A picture from your computer as the avatar: a small figure on a plain white or "
            "transparent ground"
        )
        self.add_button.clicked.connect(self.add_requested)
        self.reset_button = QPushButton("Back to the set's own")
        self.reset_button.setToolTip("One of the tile set's own figures walks for them again")
        self.reset_button.clicked.connect(self.reset_requested)
        buttons = WrapRow(spacing=6, group_spacing=6)
        for button in (
            self.draw_button,
            self.words_button,
            self.choose_button,
            self.add_button,
            self.reset_button,
        ):
            buttons.add_group([button])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(4)
        layout.addWidget(self.title)
        layout.addWidget(self.picture)
        layout.addWidget(self.source)
        layout.addLayout(buttons)
        self._image = QImage()
        self._name = ""
        self._source = ""
        self._drawing = False
        self.show_avatar(None, "", "")

    def show_avatar(self, image: QImage | None, source: str, name: str) -> None:
        """`image`: the figure the map walks for them (a drawn avatar, the
        set's figure or the program's stand-in); `source` as
        `session.walker_asset` says it ("" for a stand-in)."""
        self._image = image if image is not None else QImage()
        self._name = name
        self._source = source
        has = source in ("drawn for this story", "changed for this story")
        if self._image.isNull():
            self.picture.clear()
            self.picture.setCursor(Qt.ArrowCursor)
        else:
            self.picture.setPixmap(QPixmap.fromImage(pixel_scaled(self._image, AVATAR_HEIGHT)))
            self.picture.setCursor(Qt.PointingHandCursor)
        self.draw_button.setText(DRAW_LABELS[1 if has else 0])
        self.reset_button.setVisible(source == "changed for this story")
        self.set_drawing(self._drawing)

    def set_choices(self, choices: list[tuple[str, QImage | None, str, bool]]) -> None:
        """The avatars kept for them: (picture id, the figure, how it came to
        be, whether it walks for them now); the menu offers each, the one in
        use ticked. Hidden with none."""
        self.choose_menu.clear()
        for picture_id, image, how, in_use in choices:
            action = self.choose_menu.addAction(how + (" (in use)" if in_use else ""))
            if image is not None and not image.isNull():
                action.setIcon(QIcon(QPixmap.fromImage(pixel_scaled(image, 48))))
            action.setCheckable(True)
            action.setChecked(in_use)
            action.setData(picture_id)
            action.triggered.connect(
                lambda _c=False, pid=picture_id: self.choose_requested.emit(pid)
            )
        self.choose_button.setVisible(bool(choices))

    def set_drawing(self, drawing: bool) -> None:
        """Whether this avatar is being drawn now (the buttons wait)."""
        self._drawing = drawing
        self.draw_button.setEnabled(not drawing)
        self.words_button.setEnabled(not drawing)
        self.source.setText(
            "Drawing the avatar…" if drawing else SOURCES.get(self._source, self._source)
        )

    def show_whole(self) -> None:
        """The avatar large, by whole pixels, in a dialog of its own."""
        if self._image.isNull():
            return
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setWindowTitle(f"{self._name} on the map" if self._name else "Avatar")
        layout = QVBoxLayout(dialog)
        big = pixel_scaled(self._image, WHOLE_HEIGHT)
        whole = QLabel()
        whole.setAlignment(Qt.AlignCenter)
        whole.setPixmap(QPixmap.fromImage(big))  # as it is: whole pixels, never smoothed
        layout.addWidget(whole)
        size = QSize(big.width(), big.height()).expandedTo(QSize(240, 240))
        dialog.resize(size.width() + 40, size.height() + 40)
        dialog.exec()
