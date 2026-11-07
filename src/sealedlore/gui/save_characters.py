"""A story's characters chosen to be saved to Your characters (the Cast
page's and the board's Save to Your characters…, and Your characters'
Import → Characters from the open story…): each ticked, everyone at once with
All. One already there (linked to a card that is still kept) is listed but
can't be ticked: Changes… on the Cast page saves what this story changed of
them, and a second card of them would be one more to keep in step."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.cast_board import SAVE_LABEL, Thumbs
from sealedlore.gui.ref_images import StoryPictures
from sealedlore.gui.sizing import size_to_screen
from sealedlore.models.character import Character

ICON = QSize(40, 60)
ALREADY = "already in Your characters"
SAVE_BUTTON = "Save to Your characters"


class SaveCharactersDialog(QDialog):
    """`chosen()`: the characters ticked, in the list's order (the cast, then
    the supporting characters)."""

    def __init__(
        self,
        characters: Sequence[Character],
        pictures: StoryPictures,
        *,
        supporting: Collection[str] = (),
        already: Collection[str] = (),
        ticked: Collection[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(SAVE_LABEL.rstrip("…"))
        size_to_screen(self, 0.3, 0.6, at_least=QSize(520, 520))
        self._characters = list(characters)
        self._thumbs = Thumbs(pictures, lambda: None, self)
        heading = QLabel(
            "Who is kept in Your characters? Each goes there with their pictures, and can "
            "join any story from then on. This story's character stays as it is, linked to "
            "the card made: what either side changes later can go to the other (Changes… on "
            "their card on the cast board)."
        )
        heading.setWordWrap(True)
        self.list = QListWidget()
        self.list.setIconSize(ICON)
        self.list.setSpacing(2)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.ElideRight)
        for character in self._characters:
            words = character.name or "(unnamed)"
            if character.id in supporting:
                words += " · supporting"
            here = character.id in already
            if here:
                words += f" — {ALREADY}"
            summary = (character.summary or character.full_description or "").strip()
            summary = summary.split("\n")[0]
            if summary:
                words += f"\n{summary[:64]}{'…' if len(summary) > 64 else ''}"
            item = QListWidgetItem(words)
            item.setData(Qt.UserRole, character.id)
            if here:
                # Listed, so nobody is missing from the list, but not offered
                # (an item is checkable by default: All would tick them).
                item.setFlags(item.flags() & ~(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable))
                item.setToolTip(
                    "Already in Your characters, linked to this story's: Changes… on their "
                    "card on the cast board saves what this story changed of them"
                )
            else:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if character.id in ticked else Qt.Unchecked)
            first = character.reference_images[0] if character.reference_images else None
            if first is not None:
                item.setIcon(QIcon(self._thumbs.now(first.file, ICON.width(), ICON.height())))
            self.list.addItem(item)
        self.list.itemChanged.connect(lambda _item: self._sync())
        # A double-click ticks or unticks, as a click on the box does.
        self.list.itemDoubleClicked.connect(self._toggle)
        self.empty = QLabel("Everyone in this story is in Your characters already.")
        self.empty.setObjectName("hintLabel")
        self.empty.setWordWrap(True)
        self.empty.setVisible(not self._offered())

        self.all_button = QPushButton("All")
        self.all_button.setToolTip("Tick everyone not in Your characters yet")
        self.all_button.clicked.connect(lambda: self._tick_all(True))
        self.none_button = QPushButton("None")
        self.none_button.clicked.connect(lambda: self._tick_all(False))
        ticks = QHBoxLayout()
        ticks.setContentsMargins(0, 0, 0, 0)
        ticks.addWidget(self.all_button)
        ticks.addWidget(self.none_button)
        ticks.addStretch(1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = self.buttons.button(QDialogButtonBox.Ok)
        self.ok.setText(SAVE_BUTTON)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addLayout(ticks)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty)
        layout.addWidget(self.buttons)
        self._sync()

    def _offered(self) -> list[QListWidgetItem]:
        items = (self.list.item(row) for row in range(self.list.count()))
        return [item for item in items if item.flags() & Qt.ItemIsUserCheckable]

    def _toggle(self, item: QListWidgetItem) -> None:
        if item.flags() & Qt.ItemIsUserCheckable:
            checked = item.checkState() == Qt.Checked
            item.setCheckState(Qt.Unchecked if checked else Qt.Checked)

    def _tick_all(self, on: bool) -> None:
        for item in self._offered():
            item.setCheckState(Qt.Checked if on else Qt.Unchecked)

    def chosen(self) -> list[Character]:
        ids = {
            item.data(Qt.UserRole) for item in self._offered() if item.checkState() == Qt.Checked
        }
        return [character for character in self._characters if character.id in ids]

    def _sync(self) -> None:
        offered = self._offered()
        count = len(self.chosen())
        self.ok.setEnabled(bool(count))
        self.ok.setText(f"{SAVE_BUTTON} ({count})" if count > 1 else SAVE_BUTTON)
        self.all_button.setEnabled(bool(offered))
        self.none_button.setEnabled(bool(offered))
