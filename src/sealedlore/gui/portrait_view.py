"""A card's first picture, large, at the top of the card on the Cast and
Lore pages: a character's portrait, a lore entry's picture (the author, Oct
15 2026: with the Stories list put away there is room for the pages beside
the story to be more graphical). The rest stay small in the strip under it.

Held at the size it can be shown, never whole (as the transcript's pictures
are), read once the card is shown (`cast_board.Thumbs`) and kept while its
file stands as it is: every choice of a card decoded its picture whole. Its
place is made at once, from the picture's header; a click shows the file
whole.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from sealedlore.gui.cast_board import Thumbs
from sealedlore.gui.expression_set import SET_LABEL, SET_TIP
from sealedlore.gui.image_dialog import show_whole
from sealedlore.gui.picture_cache import WITHIN
from sealedlore.gui.picture_labels import LABELS_LABEL, LABELS_TIP
from sealedlore.gui.picture_words import (
    AGAIN_LABEL,
    DRAW_LABELS,
    EDIT_LABEL,
    FROM_WORDS,
    SCENE_LABEL,
    SENDS_PICTURE,
)
from sealedlore.gui.ref_images import shape_of, story_key
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.storage.picture_store import PictureStore

# The button under the two that holds the rest of what can be done.
MORE_LABEL = "More"

# The tallest the portrait is drawn, and the widest it is held at (its
# height is not bounded: a tall one is as wide as it is shown).
PORTRAIT_MAX_HEIGHT = 380
PORTRAIT_HELD_WIDTH = 720
HELD_HEIGHT = 16 * PORTRAIT_HELD_WIDTH
# Held pictures: a few cards' portraits (one is up to 720 x 1080 x 4 bytes).
HELD_BUDGET = 32 * 1024 * 1024


class _Picture(QLabel):
    """The picture scaled to the width it is given, up to a height."""

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("portraitPicture")
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumSize(1, 1)
        self._pixmap = QPixmap()
        # Its shape while it is being read, from its header (`wait`).
        self._shape = QSize()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """The picture, held no wider than PORTRAIT_HELD_WIDTH."""
        self._pixmap = pixmap
        self._shape = pixmap.size() if not pixmap.isNull() else QSize()
        self.setCursor(Qt.PointingHandCursor if not pixmap.isNull() else Qt.ArrowCursor)
        self._rescale()
        self.updateGeometry()

    def wait(self, shape: QSize) -> None:
        """A picture of `shape` being read: its place, empty, until it is."""
        self.set_pixmap(QPixmap())
        self._shape = shape
        self.updateGeometry()

    def _box(self, width: int) -> QSize:
        if not self._shape.isValid() or self._shape.isEmpty():
            return QSize(0, 0)
        return self._shape.scaled(QSize(max(1, width), PORTRAIT_MAX_HEIGHT), Qt.KeepAspectRatio)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._box(width).height()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(PORTRAIT_MAX_HEIGHT, self.heightForWidth(PORTRAIT_MAX_HEIGHT))

    def _rescale(self) -> None:
        if self._pixmap.isNull():
            self.clear()
            return
        box = self._box(self.width())
        self.setPixmap(self._pixmap.scaled(box, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._rescale()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and not self._pixmap.isNull():
            self.clicked.emit()
        super().mouseReleaseEvent(event)


# Written out whole: tests/test_readme_labels.py finds a control the manual
# names among the GUI's string literals.


class PortraitView(QWidget):
    """A card's first picture (a character's portrait, a lore entry's
    picture), or a place for one, and what can be done with it: draw one,
    draw it again, edit it, (a character) picture them in the scene now,
    take one from a file, remove it."""

    draw_requested = Signal()
    again_requested = Signal()
    edit_requested = Signal()
    scene_requested = Signal()
    add_requested = Signal()
    remove_requested = Signal()
    labels_requested = Signal()
    set_requested = Signal()

    def __init__(self, *, person: bool = True, scene: bool = True) -> None:
        """`scene`: In a scene… is offered (a story's; the author's own
        characters have no scene to picture them in)."""
        super().__init__()
        self._person = person
        self._scene = scene
        self._noun = "portrait" if person else "picture"
        self.picture = _Picture()
        self.picture.clicked.connect(self._show_whole)
        self.empty = QLabel(f"No {self._noun} yet")
        self.empty.setObjectName("portraitEmpty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setMinimumHeight(120)
        self.caption = QLabel()
        self.caption.setObjectName("hintLabel")
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setWordWrap(True)
        whose = "their card" if person else "the entry"
        self.draw_button = self._button(
            DRAW_LABELS[self._noun][0],
            f"A new {self._noun} written from {whose}'s description: no picture is sent, "
            "only words. You see the prompt and the price first. It goes first among the "
            "pictures.",
            self.draw_requested,
        )
        self.edit_button = self._button(
            EDIT_LABEL,
            "This picture is sent to the image model with what you want changed (a scar, "
            "other clothes, another background, years older), so it stays the same "
            f"{'person' if person else 'thing'}. The changed copy is added to the pictures.",
            self.edit_requested,
        )
        self.from_words = QLabel(FROM_WORDS)
        self.from_words.setObjectName("hintLabel")
        self.sends_picture = QLabel(SENDS_PICTURE)
        self.sends_picture.setObjectName("hintLabel")

        # The two most asked for stay buttons, each under what it sends; the
        # rest are in More, grouped the same way (the author, Nov 2026: twelve
        # buttons under a portrait). Fusion draws a menu's sections as bare
        # lines, so the captions are rows of their own.
        menu = QMenu(self)
        self._menu_words = self._caption_row(menu, FROM_WORDS)
        self.again_action = self._action(
            menu,
            AGAIN_LABEL,
            "Another take from this picture's own prompt, by the same model or another. "
            "This picture isn't sent (only any pictures it was itself drawn from, as "
            "before). The new one is added to the pictures; this one stays.",
            self.again_requested,
        )
        self._menu_sends = self._caption_row(menu, SENDS_PICTURE)
        self.scene_action = self._action(
            menu,
            SCENE_LABEL,
            "Their portrait is sent to the image model with a description of them as they "
            "are now in the story (where they are, what they wear and are doing), so it is "
            "the same face. It is added to their pictures.",
            self.scene_requested,
        )
        self.set_action = self._action(menu, SET_LABEL, SET_TIP, self.set_requested)
        menu.addSeparator()
        self.labels_action = self._action(menu, LABELS_LABEL, LABELS_TIP, self.labels_requested)
        self.add_action = self._action(
            menu,
            "Choose a file…",
            f"A picture from your computer as the {self._noun}",
            self.add_requested,
        )
        self.remove_action = self._action(
            menu,
            "Remove",
            f"Take this {self._noun} off; the next picture takes its place",
            self.remove_requested,
        )
        menu.setToolTipsVisible(True)
        self.more_button = QToolButton()
        self.more_button.setText(MORE_LABEL)
        self.more_button.setToolTip(
            "Another take from the same prompt, more pictures of them, labels, a file "
            "from your computer, remove"
        )
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_button.setMenu(menu)

        buttons = QVBoxLayout()
        buttons.setSpacing(4)
        for caption, button in (
            (self.from_words, self.draw_button),
            (self.sends_picture, self.edit_button),
        ):
            buttons.addWidget(caption)
            row = WrapRow(spacing=6, group_spacing=6)
            row.add_group([button])
            buttons.addLayout(row)
        buttons.addSpacing(4)
        more = WrapRow(spacing=6, group_spacing=6)
        more.add_group([self.more_button])
        buttons.addLayout(more)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(4)
        layout.addWidget(self.picture)
        layout.addWidget(self.empty)
        layout.addWidget(self.caption)
        layout.addLayout(buttons)
        self._store: PictureStore | None = None
        self._file = ""
        self._name = ""
        self._caption = ""
        self._offered = False
        self._thumbs = Thumbs(
            lambda: self._store, lambda: None, self, shape=WITHIN, budget=HELD_BUDGET
        )
        self._thumbs_for: object = None
        self.show_portrait(None, "", "", "")

    @staticmethod
    def _button(text: str, tip: str, signal) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tip)
        button.clicked.connect(signal)
        return button

    @staticmethod
    def _action(menu: QMenu, text: str, tip: str, signal) -> QAction:
        # Made with its menu as parent, then added: made by addAction(text),
        # the actions were deleted with the Setup dialog the card had been
        # lent to, though the menu itself came back with it.
        action = QAction(text, menu)
        action.setToolTip(tip)
        action.triggered.connect(signal)
        menu.addAction(action)
        return action

    @staticmethod
    def _caption_row(menu: QMenu, text: str) -> QWidgetAction:
        label = QLabel(text)
        label.setObjectName("menuCaption")
        row = QWidgetAction(menu)
        row.setDefaultWidget(label)
        menu.addAction(row)
        return row

    def show_portrait(
        self, store: PictureStore | None, file: str | None, caption: str, name: str
    ) -> None:
        """`file`: the first picture's, in `store`; None or "" for none. Shown
        at once if it has been read, else read once the event loop runs (its
        place made now from its header, so nothing below it moves)."""
        if story_key(store) != self._thumbs_for:
            self._thumbs.forget()
            self._thumbs_for = story_key(store)
        # What waited for the picture shown before.
        self._thumbs.cancel_fills()
        self._store, self._file = store, file or ""
        self._name, self._caption = name, caption
        if store is None or not file:
            self._show(QPixmap())
            return
        given: list[bool] = []

        def put(pixmap: QPixmap, file: str = file) -> None:
            given.append(True)
            if self._file == file:
                self._show(pixmap)

        self._thumbs.fill(file, PORTRAIT_HELD_WIDTH, HELD_HEIGHT, put)
        if not given:
            shape = shape_of(store.read(file))
            if shape.isValid() and not shape.isEmpty():
                self._show(QPixmap(), waiting=shape)
            else:
                self._show(QPixmap())

    def _show(self, pixmap: QPixmap, waiting: QSize | None = None) -> None:
        """The picture shown (or its place while it is read), or none."""
        has = not pixmap.isNull() or waiting is not None
        if waiting is not None:
            self.picture.wait(waiting)
        else:
            self.picture.set_pixmap(pixmap)
        name, caption = self._name, self._caption
        self.picture.setVisible(has)
        self.empty.setVisible(not has)
        self.caption.setText(caption if has else "")
        self.caption.setVisible(has and bool(caption))
        self.picture.setToolTip(f"{name}: click to see it whole" if has else "")
        self.draw_button.setText(DRAW_LABELS[self._noun][1 if has else 0])
        self._has = has
        self._sync_buttons()

    def set_offered(self, offered: bool, _add: bool = True) -> None:
        """Whether there is a card to act on."""
        self._offered = offered
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        on, has = self._offered, getattr(self, "_has", False)
        self.draw_button.setVisible(on)
        self.from_words.setVisible(on)
        self.more_button.setVisible(on)
        self.add_action.setVisible(on)
        self.edit_button.setVisible(on and has)
        self.sends_picture.setVisible(on and has)
        for action in (self.again_action, self.remove_action):
            action.setVisible(on and has)
        self.labels_action.setVisible(on and has and self._person)
        # Their portrait is what keeps the face: with none, Draw a portrait first.
        self.scene_action.setVisible(on and has and self._person and self._scene)
        self.set_action.setVisible(on and has and self._person)
        self._menu_words.setVisible(self.again_action.isVisible())
        self._menu_sends.setVisible(self.scene_action.isVisible() or self.set_action.isVisible())

    def _show_whole(self) -> None:
        # The one viewer every picture opens in.
        store, file = self._store, self._file
        data = store.read(file) if store is not None and file else None
        show_whole(self, self._name or self._noun.capitalize(), data)
