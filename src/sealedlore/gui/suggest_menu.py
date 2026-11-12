"""The options Suggest… came back with, in a menu at its button.

Each option is a sentence or two, more than a menu's one line holds, so the
menu carries them as a short list whose rows wrap: read whole, chosen by a
click or by the arrow keys and Enter, left with Esc or a click elsewhere.
Choosing one only hands its words to the window, which puts them in the box
the author writes in; nothing is ever sent from here.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QItemSelectionModel, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QWidget,
    QWidgetAction,
)

from sealedlore.gui import text_size

CAPTION = "Pick one to edit and send as your own, or Esc for none"
CHAT_CAPTION = "Pick one to edit and send, or Esc for none"
# The list's width at 100% text size, and the room around each row's words.
WIDTH = 520
ROW_MARGIN = 10


class SuggestMenu(QMenu):
    # The words of the option the author chose.
    chosen = Signal(str)

    def __init__(
        self, options: Sequence[str], parent: QWidget | None = None, *, chat: bool = False
    ) -> None:
        super().__init__(parent)
        self.setObjectName("suggestMenu")
        caption = QLabel(CHAT_CAPTION if chat else CAPTION)
        caption.setObjectName("menuCaption")
        heading = QWidgetAction(self)
        heading.setDefaultWidget(caption)
        self.addAction(heading)

        self.list = QListWidget()
        self.list.setObjectName("suggestList")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        # The row under the pointer is the one chosen, as in any menu.
        self.list.setMouseTracking(True)
        self.list.itemEntered.connect(self.list.setCurrentItem)
        self._labels: list[QLabel] = []
        for option in options:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, option)
            self.list.addItem(item)
            # A label a row, since a label knows how tall its words are at a
            # width; plain text, as words from a model always are.
            label = QLabel(option)
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setObjectName("suggestOption")
            label.setContentsMargins(ROW_MARGIN, ROW_MARGIN, ROW_MARGIN, ROW_MARGIN)
            # Clicks and the pointer are the list's.
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            self.list.setItemWidget(item, label)
            self._labels.append(label)
        rows = QWidgetAction(self)
        rows.setDefaultWidget(self.list)
        self.addAction(rows)
        self._fit()
        # A click (on its release, so no press is left behind on a window
        # that has gone) or Enter on the row the arrow keys chose.
        self.list.itemClicked.connect(self._choose)
        self.list.itemActivated.connect(self._choose)
        self._done = False

    def options(self) -> list[str]:
        return [self.list.item(row).data(Qt.UserRole) for row in range(self.list.count())]

    def _fit(self) -> None:
        """Each row as tall as its words wrapped at the list's width, and
        the list as tall as its rows: nothing to scroll. Measured in the
        fonts the stylesheet gives them."""
        self.list.ensurePolished()
        width = int(WIDTH * max(1.0, text_size.scale()))
        frame = 2 * self.list.frameWidth() + 2
        inner = width - frame
        total = 0
        for row, label in enumerate(self._labels):
            label.ensurePolished()
            # A pixel more for the line the list draws under each row.
            tall = label.heightForWidth(inner) + 1
            self.list.item(row).setSizeHint(QSize(inner, tall))
            total += tall
        self.list.setFixedSize(width, total + frame)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # The keyboard goes to the options, the first marked as a menu's
        # is: Down moves on and Enter chooses.
        self.list.setFocus(Qt.PopupFocusReason)
        if self.list.count() and not self.list.selectedItems():
            self.list.setCurrentRow(0, QItemSelectionModel.ClearAndSelect)

    def choose(self, row: int) -> None:
        """Take the option at `row` (what a click on it does)."""
        self._choose(self.list.item(row))

    def _choose(self, item: QListWidgetItem | None) -> None:
        if item is None or self._done:
            return
        self._done = True  # a double click chooses once
        self.close()
        self.chosen.emit(item.data(Qt.UserRole))


__all__ = ["CAPTION", "CHAT_CAPTION", "SuggestMenu"]
