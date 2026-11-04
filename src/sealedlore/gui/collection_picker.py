"""Characters chosen from the author's own (gui/collection_page.py) to put
into a story, or to draft one around: each ticked, any version, found by a
search over their whole card."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.cast_board import Thumbs
from sealedlore.gui.ref_images import StoryPictures
from sealedlore.gui.sizing import size_to_screen
from sealedlore.models.character import Character
from sealedlore.storage import collection

ICON = QSize(40, 60)


class CollectionPicker(QDialog):
    """`chosen()`: the cards ticked, in the list's order. With `placing`, it
    also asks whether they join the cast or the supporting characters, and
    whether their own lorebooks come too."""

    def __init__(
        self,
        characters: Sequence[Character],
        pictures: StoryPictures,
        *,
        title: str,
        lead: str,
        action: str,
        placing: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        size_to_screen(self, 0.35, 0.7, at_least=QSize(560, 600))
        self._characters = list(characters)
        self._thumbs = Thumbs(pictures, lambda: None)
        heading = QLabel(lead)
        heading.setWordWrap(True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search your characters")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self._filter())
        self.list = QListWidget()
        self.list.setIconSize(ICON)
        self.list.setSpacing(2)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.itemChanged.connect(lambda _item: self._sync())
        for main in sorted(collection.mains(self._characters), key=lambda c: c.name.casefold()):
            for card in collection.family_of(main, self._characters):
                words = card.name
                if card is not main:
                    words += f" — {collection.info(card).version or 'a version'}"
                summary = (card.summary or card.full_description or "").strip().split("\n")[0]
                if summary:
                    words += f"\n{summary[:64]}{'…' if len(summary) > 64 else ''}"
                item = QListWidgetItem(words)
                item.setData(Qt.UserRole, card.id)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                first = card.reference_images[0] if card.reference_images else None
                if first is not None:
                    item.setIcon(QIcon(self._thumbs.now(first.file, ICON.width(), ICON.height())))
                self.list.addItem(item)
        self.empty = QLabel(
            "You keep no characters yet: File → Your characters… to make some, or import "
            "SillyTavern cards there."
        )
        self.empty.setObjectName("hintLabel")
        self.empty.setWordWrap(True)
        self.empty.setVisible(not self._characters)

        self.to_cast = QRadioButton("Into the cast: on every turn's roster, and playable")
        self.to_supporting = QRadioButton(
            "As supporting characters: their cards go with the turns that name them"
        )
        self.to_cast.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.to_cast)
        group.addButton(self.to_supporting)
        self.with_lore = QCheckBox("With their own lorebooks, as the story's lore")
        self.with_lore.setChecked(True)
        for widget in (self.to_cast, self.to_supporting, self.with_lore):
            widget.setVisible(placing)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = self.buttons.button(QDialogButtonBox.Ok)
        self.ok.setText(action)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(self.search)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty)
        layout.addWidget(self.to_cast)
        layout.addWidget(self.to_supporting)
        layout.addWidget(self.with_lore)
        layout.addWidget(self.buttons)
        self._sync()

    def _card(self, card_id: str) -> Character | None:
        return next((c for c in self._characters if c.id == card_id), None)

    def _filter(self) -> None:
        words = self.search.text().strip()
        for row in range(self.list.count()):
            item = self.list.item(row)
            card = self._card(item.data(Qt.UserRole))
            item.setHidden(bool(words) and not collection.matches(card, words))

    def check(self, card_id: str) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(Qt.UserRole) == card_id:
                item.setCheckState(Qt.Checked)

    def chosen(self) -> list[Character]:
        found = []
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.checkState() == Qt.Checked:
                card = self._card(item.data(Qt.UserRole))
                if card is not None:
                    found.append(card)
        return found

    def _sync(self) -> None:
        chosen = self.chosen()
        self.ok.setEnabled(bool(chosen))
        books = any(card.tavern and card.tavern.book_entries for card in chosen)
        self.with_lore.setEnabled(books)

    def options(self) -> tuple[bool, bool]:
        """(into the cast, with their lorebooks)."""
        return self.to_cast.isChecked(), self.with_lore.isEnabled() and self.with_lore.isChecked()
