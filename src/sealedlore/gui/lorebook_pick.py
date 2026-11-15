"""A character's own lorebook before it joins a story that exists: every
entry ticked to begin with, the author unticks what doesn't belong and may
change an entry's words. The entries are the story's copies, so nothing here
reaches their card (the author, Nov 15 2026: "tick to bring in the lorebook,
then quickly delete from it stuff that isn't relevant before including it",
edited "only in the story it is brought into")."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.sizing import GrowingText, enter_only_on_buttons, may_discard, size_to_screen
from sealedlore.models.lore import LoreEntry

TITLE = "Their lorebook in this story"
LEAD = (
    "Untick what doesn't belong in this story, and change an entry's words if you like. "
    "What you leave out or change here is this story's alone: {keeps}. What comes in can "
    "still be changed or removed on the Lore page."
)
KEEPS = "their card keeps its lorebook as it is"
KEEP_SEVERAL = "their cards keep their lorebooks as they are"
OFF_ON_THE_CARD = " (switched off on their card: comes in switched off)"
NONE_CHOSEN = "Choose an entry to read it or change its words."


def _plural(count: int) -> str:
    return f"{count} {'entry' if count == 1 else 'entries'}"


class LorebookPickDialog(QDialog):
    """`groups`: (whose, entries) for each character bringing a lorebook,
    the entries the story's own copies. `chosen()`: the entries ticked, per
    group, with any change made here."""

    def __init__(
        self,
        groups: Sequence[tuple[str, Sequence[LoreEntry]]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        size_to_screen(self, 0.55, 0.7, at_least=QSize(820, 560))
        self._groups = [(name, list(entries)) for name, entries in groups]
        self._entry: LoreEntry | None = None
        self._item: QTreeWidgetItem | None = None
        several = len(self._groups) > 1
        lead = QLabel(LEAD.format(keeps=KEEP_SEVERAL if several else KEEPS))
        lead.setWordWrap(True)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Find an entry by its title, keywords or words")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self._filter())

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setTextElideMode(Qt.ElideRight)
        for number, (name, entries) in enumerate(self._groups):
            top = QTreeWidgetItem([f"{name}'s lorebook ({_plural(len(entries))})"])
            top.setFlags(
                Qt.ItemIsEnabled
                | Qt.ItemIsSelectable
                | Qt.ItemIsUserCheckable
                | Qt.ItemIsAutoTristate
            )
            top.setData(0, Qt.UserRole, None)
            for index, entry in enumerate(entries):
                item = QTreeWidgetItem([self._label(entry)])
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked)
                item.setData(0, Qt.UserRole, (number, index))
                top.addChild(item)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
        self.tree.itemChanged.connect(lambda _item, _column: self._sync())
        self.tree.currentItemChanged.connect(lambda item, _before: self._show(item))

        self.title = QLineEdit()
        self.keywords = QLineEdit()
        self.keywords.setPlaceholderText("Separated by commas")
        self.content = GrowingText(lines=8, most=30, fill=True)
        self.none_chosen = QLabel(NONE_CHOSEN)
        self.none_chosen.setObjectName("hintLabel")
        self.none_chosen.setWordWrap(True)
        self.title.textEdited.connect(self._edited)
        self.keywords.textEdited.connect(self._edited)
        self.content.textChanged.connect(self._edited)
        form = QFormLayout()
        form.addRow("Title", self.title)
        form.addRow("Keywords", self.keywords)
        form.addRow("Text", self.content)
        self.editor = QWidget()
        self.editor.setLayout(form)
        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.addWidget(self.none_chosen)
        right_box.addWidget(self.editor, 1)
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.addWidget(self.search)
        left_box.addWidget(self.tree, 1)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)

        self.count = QLabel()
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = self.buttons.button(QDialogButtonBox.Ok)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(lead)
        layout.addWidget(split, 1)
        layout.addWidget(self.count)
        layout.addWidget(self.buttons)
        self._show(None)
        self._sync()
        self._before = self._state()

    # --- what is chosen -----------------------------------------------------------------

    def _children(self):
        for row in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(row)
            for child in range(top.childCount()):
                yield top.child(child)

    def chosen(self) -> list[list[LoreEntry]]:
        """The entries ticked, a list for each group, in the lorebook's order."""
        picked: list[list[LoreEntry]] = [[] for _ in self._groups]
        for item in self._children():
            if item.checkState(0) == Qt.Checked:
                number, index = item.data(0, Qt.UserRole)
                picked[number].append(self._groups[number][1][index])
        return picked

    def _state(self) -> list:
        """Every tick and every entry's words: what Cancel would lose."""
        ticks = [item.checkState(0) == Qt.Checked for item in self._children()]
        words = [
            (entry.title, tuple(entry.keywords), entry.content)
            for _name, entries in self._groups
            for entry in entries
        ]
        return [ticks, words]

    def _sync(self) -> None:
        total = sum(len(entries) for _name, entries in self._groups)
        ticked = sum(len(entries) for entries in self.chosen())
        self.count.setText(f"Bringing {ticked} of {_plural(total)} into this story's lore.")
        if ticked:
            self.ok.setText(f"Bring in {_plural(ticked)}")
        elif len(self._groups) > 1:
            self.ok.setText("Add them without their lorebooks")
        else:
            self.ok.setText("Add without their lorebook")

    # --- one entry ------------------------------------------------------------------------

    @staticmethod
    def _label(entry: LoreEntry) -> str:
        return (entry.title or "Untitled") + ("" if entry.enabled else OFF_ON_THE_CARD)

    def _show(self, item: QTreeWidgetItem | None) -> None:
        where = item.data(0, Qt.UserRole) if item is not None else None
        self._item = item if where is not None else None
        self._entry = self._groups[where[0]][1][where[1]] if where is not None else None
        self.editor.setVisible(self._entry is not None)
        self.none_chosen.setVisible(self._entry is None)
        if self._entry is None:
            return
        entry = self._entry
        self._entry = None  # filling the fields is no edit
        self.title.setText(entry.title)
        self.title.setCursorPosition(0)
        self.keywords.setText(", ".join(entry.keywords))
        self.keywords.setCursorPosition(0)
        self.content.setPlainText(entry.content)
        self._entry = entry

    def _edited(self, *_args) -> None:
        entry = self._entry
        if entry is None:
            return
        entry.title = self.title.text()
        entry.keywords = [word.strip() for word in self.keywords.text().split(",") if word.strip()]
        entry.content = self.content.toPlainText()
        if self._item is not None:
            self._item.setText(0, self._label(entry))

    def _filter(self) -> None:
        words = self.search.text().strip().casefold()
        for row in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(row)
            shown = 0
            for child in range(top.childCount()):
                item = top.child(child)
                number, index = item.data(0, Qt.UserRole)
                entry = self._groups[number][1][index]
                text = " ".join([entry.title, *entry.keywords, entry.content]).casefold()
                hidden = bool(words) and words not in text
                item.setHidden(hidden)
                shown += not hidden
            top.setHidden(bool(words) and not shown)

    # --- leaving ------------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)

    def reject(self) -> None:
        if may_discard(self, self._before, self._state()):
            super().reject()
