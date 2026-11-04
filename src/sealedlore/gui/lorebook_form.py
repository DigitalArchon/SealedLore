"""Your characters' Lorebook tab: a card's own lorebook, every entry of it to
read, change, add or take off, for the card to go out again as changed.

The book is kept as the card had it (TavernFields.character_book), in the
spec's shape or SillyTavern's own, and edited in place through
storage/tavern_lore.py, so an edit changes only what was edited. What a
story takes from an entry (its keywords, text, always sent and on) has a
field each, as do the keywords' other switches a card's apps share; the
rest of an entry (its order, its place in the prompt, SillyTavern's own
settings) and of the book is JSON under a heading, so nothing on it is out
of reach (the author, Nov 2026).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.fields import FormScroll, set_field_text
from sealedlore.gui.json_field import JsonField
from sealedlore.gui.sizing import GrowingText
from sealedlore.models.character import Character
from sealedlore.models.tavern import TavernFields
from sealedlore.storage import tavern_lore as lore

# As many as fit beside the entry being edited at 1080p; more scroll.
LIST_ROWS = 12
INTRO = (
    "Their own lore, offered to a story's lore when they join one, and part of their card "
    "when it is exported. A story uses an entry's keywords, its text and whether it is "
    "always sent or switched off; the rest is kept for SillyTavern and other apps."
)


def _keys_text(keys: list[str]) -> str:
    return ", ".join(keys)


def _keys_of(text: str) -> list[str]:
    return [key.strip() for key in text.split(",") if key.strip()]


class LorebookForm(QWidget):
    changed = Signal()
    # "import", "export" or "remove": the whole book, for the window.
    lorebook_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._character: Character | None = None
        self._entries: list[dict[str, Any]] = []
        self._loading = False

        intro = QLabel(INTRO)
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)
        self.book_name = QLineEdit()
        self.book_name.setPlaceholderText("The lorebook's own name, if it has one")
        self.book_name.textChanged.connect(self._book_edited)
        self.book_description = GrowingText(lines=2, most=10)
        self.book_description.setPlaceholderText("What the lorebook is about, for its readers.")
        self.book_description.textChanged.connect(self._book_edited)
        self.book_rest = JsonField(
            "The book's own settings (JSON)",
            tip="How deep SillyTavern looks, its budget, recursion, other apps' settings",
        )
        self.book_rest.edited.connect(self._book_rest_edited)

        self.find = QLineEdit()
        self.find.setPlaceholderText("Find an entry by its name, keywords or text")
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(self._filter)
        self.list = QListWidget()
        self.list.setObjectName("cardBook")
        self.list.currentRowChanged.connect(self._chosen)
        remove_key = QShortcut(QKeySequence(QKeySequence.Delete), self.list)
        remove_key.setContext(Qt.WidgetShortcut)
        remove_key.activated.connect(self._remove)
        self.add_button = QPushButton("Add an entry")
        self.add_button.clicked.connect(self._add)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip("Another entry like this one, to change")
        self.copy_button.clicked.connect(self._copy)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        entry_buttons = QHBoxLayout()
        entry_buttons.setContentsMargins(0, 0, 0, 0)
        for button in (self.add_button, self.copy_button, self.remove_button):
            entry_buttons.addWidget(button)
        entry_buttons.addStretch(1)

        self.title = QLineEdit()
        self.title.setPlaceholderText("What the entry is about (its name in the list)")
        self.title.textChanged.connect(lambda: self._entry_edited("title"))
        self.keys = QLineEdit()
        self.keys.setPlaceholderText("comma-separated: the entry is sent when one is mentioned")
        self.keys.textChanged.connect(lambda: self._entry_edited("keys"))
        self.content = GrowingText(lines=4, most=24)
        self.content.setPlaceholderText("What the storyteller is told.")
        self.content.textChanged.connect(lambda: self._entry_edited("content"))
        self.order = QSpinBox()
        self.order.setRange(-100000, 100000)
        self.order.setMaximumWidth(120)
        self.order.setToolTip(
            "SillyTavern's insertion order: of the entries it sends, a higher number goes "
            "nearer the end of the prompt"
        )
        self.order.valueChanged.connect(lambda _value: self._entry_edited("order"))
        self.enabled = QCheckBox("On")
        self.enabled.setToolTip("Switched off, the entry is kept but never sent")
        self.constant = QCheckBox("Always sent")
        self.constant.setToolTip("Sent whatever is mentioned, keywords or not")
        self.use_regex = QCheckBox("Keywords are regular expressions")
        self.use_regex.setToolTip(
            "A keyword written /like this/ is a pattern whatever this says. SillyTavern ticks "
            "it on every entry of a card it makes; a plain word finds the same either way."
        )
        self.case_sensitive = QCheckBox("Keywords match only in the same case")
        for box in (self.enabled, self.constant, self.use_regex, self.case_sensitive):
            box.toggled.connect(lambda _on, box=box: self._entry_edited(box))
        switches = QVBoxLayout()
        switches.setContentsMargins(0, 0, 0, 0)
        switches.setSpacing(2)
        for box in (self.enabled, self.constant, self.use_regex, self.case_sensitive):
            switches.addWidget(box)
        self.secondary = QLineEdit()
        self.secondary.setPlaceholderText("comma-separated: SillyTavern's second keywords")
        self.secondary.textChanged.connect(lambda: self._entry_edited("secondary"))
        self.selective = QCheckBox("Sent only when one of these is mentioned too")
        self.selective.toggled.connect(lambda _on: self._entry_edited("selective"))
        self.entry_rest = JsonField(
            "Everything else on this entry (JSON)",
            tip="Its order and place in the prompt, its chance, groups, timing: SillyTavern's "
            "own settings, kept for it",
        )
        self.entry_rest.edited.connect(self._entry_rest_edited)
        self.entry_box = QWidget()
        entry_form = QFormLayout(self.entry_box)
        entry_form.setContentsMargins(0, 6, 0, 0)
        entry_form.addRow("Name", self.title)
        entry_form.addRow("Keywords", self.keys)
        entry_form.addRow("Text", self.content)
        entry_form.addRow("", switches)
        entry_form.addRow("Order", self.order)
        entry_form.addRow("Second keywords", self.secondary)
        entry_form.addRow("", self.selective)
        entry_form.addRow("", self.entry_rest)

        self.empty = QLabel("No lorebook yet: add an entry, or import a lorebook.")
        self.empty.setWordWrap(True)
        self.import_button = QPushButton("Import a lorebook…")
        self.import_button.setToolTip(
            "A SillyTavern lorebook, or another card's, in place of this one"
        )
        self.import_button.clicked.connect(lambda: self.lorebook_requested.emit("import"))
        self.export_button = QPushButton("Export…")
        self.export_button.setToolTip("The entries as a SillyTavern lorebook file")
        self.export_button.clicked.connect(lambda: self.lorebook_requested.emit("export"))
        self.remove_book = QPushButton("Remove the lorebook")
        self.remove_book.clicked.connect(lambda: self.lorebook_requested.emit("remove"))
        book_buttons = QHBoxLayout()
        book_buttons.setContentsMargins(0, 0, 0, 0)
        for button in (self.import_button, self.export_button, self.remove_book):
            book_buttons.addWidget(button)
        book_buttons.addStretch(1)

        entries = QWidget()
        # As tall as it is: spare height went to this row, a gap under it.
        entries.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        entries_layout = QVBoxLayout(entries)
        entries_layout.setContentsMargins(0, 0, 0, 0)
        entries_layout.setSpacing(4)
        entries_layout.addWidget(self.empty)
        entries_layout.addWidget(self.find)
        entries_layout.addWidget(self.list)
        entries_layout.addLayout(entry_buttons)
        entries_layout.addWidget(self.entry_box)

        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(10, 10, 10, 0)
        form.addRow(intro)
        form.addRow("", book_buttons)
        form.addRow("Book's name", self.book_name)
        form.addRow("Description", self.book_description)
        form.addRow("", self.book_rest)
        form.addRow("Entries", entries)
        self._book_rows = (self.book_name, self.book_description, self.book_rest)
        self._form = form
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(FormScroll(content))
        self.load(None)

    # --- the book ---------------------------------------------------------------------

    @property
    def _tavern(self) -> TavernFields | None:
        character = self._character
        if character is None:
            return None
        if character.tavern is None:
            character.tavern = TavernFields()
        return character.tavern

    @property
    def _book(self) -> dict[str, Any] | None:
        tavern = self._tavern
        return tavern.character_book if tavern is not None else None

    def load(self, character: Character | None, *, row: int = 0) -> None:
        self._character = character
        self.setEnabled(character is not None)
        book = self._book
        self._entries = lore.book_entries(book) if book else []
        self._loading = True
        set_field_text(self.book_name, str(book.get("name") or "") if book else "")
        self.book_description.setPlainText(str(book.get("description") or "") if book else "")
        self.book_rest.set_value(lore.book_rest(book) if book else {})
        self._loading = False
        for widget in self._book_rows:
            self._form.setRowVisible(widget, book is not None)
        self.export_button.setEnabled(bool(self._entries))
        self.remove_book.setEnabled(book is not None)
        self.empty.setVisible(book is None)
        self._fill(row)

    def _line(self, raw: dict[str, Any]) -> str:
        marks = (["always sent"] if raw.get("constant") else []) + (
            ["switched off"] if not lore.entry_enabled(raw) else []
        )
        keys = lore.entry_keys(raw)
        title = lore.entry_title(raw) or (keys[0] if keys else "(no name)")
        return " · ".join([title, *marks])

    def _fill(self, row: int) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for raw in self._entries:
            item = QListWidgetItem(self._line(raw))
            item.setToolTip(_keys_text(lore.entry_keys(raw)) or "No keywords")
            self.list.addItem(item)
        shown = bool(self._entries)
        self.list.setVisible(shown)
        self.find.setVisible(len(self._entries) > LIST_ROWS or bool(self.find.text()))
        if shown:
            rows = min(len(self._entries), LIST_ROWS)
            frame = 2 * self.list.frameWidth()
            self.list.setFixedHeight(rows * self.list.sizeHintForRow(0) + frame + 4)
        row = min(max(row, 0), len(self._entries) - 1)
        if row >= 0:
            self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        self._filter(self.find.text())
        self._chosen(self.list.currentRow())

    def _filter(self, words: str) -> None:
        """Only the entries with every word in their name, keywords or text."""
        wanted = words.casefold().split()
        for row, raw in enumerate(self._entries):
            text = " ".join(
                [lore.entry_title(raw), *lore.entry_keys(raw), str(raw.get("content") or "")]
            ).casefold()
            self.list.setRowHidden(row, not all(word in text for word in wanted))

    def _book_edited(self) -> None:
        book = self._book
        if self._loading or book is None:
            return
        book["name"] = self.book_name.text()
        book["description"] = self.book_description.toPlainText()
        self.changed.emit()

    def _book_rest_edited(self, rest: dict[str, Any]) -> None:
        book = self._book
        if book is not None:
            lore.set_book_rest(book, rest)
            self.changed.emit()

    # --- the entries ------------------------------------------------------------------

    def _current(self) -> dict[str, Any] | None:
        row = self.list.currentRow()
        return self._entries[row] if 0 <= row < len(self._entries) else None

    def _chosen(self, row: int) -> None:
        raw = self._entries[row] if 0 <= row < len(self._entries) else None
        self.entry_box.setVisible(raw is not None)
        self.copy_button.setEnabled(raw is not None)
        self.remove_button.setEnabled(raw is not None)
        if raw is None:
            return
        self._loading = True
        set_field_text(self.title, lore.entry_title(raw))
        set_field_text(self.keys, _keys_text(lore.entry_keys(raw)))
        self.content.setPlainText(str(raw.get("content") or ""))
        self.enabled.setChecked(lore.entry_enabled(raw))
        self.constant.setChecked(bool(raw.get("constant")))
        self.use_regex.setChecked(bool(raw.get("use_regex")))
        self.case_sensitive.setChecked(lore.entry_case_sensitive(raw))
        order = lore.entry_get(raw, "insertion_order")
        self.order.setValue(order if isinstance(order, int) else 100)
        set_field_text(self.secondary, _keys_text(lore.entry_keys(raw, "secondary_keys")))
        self.selective.setChecked(bool(raw.get("selective")))
        self.entry_rest.set_value(lore.entry_rest(raw))
        self._loading = False

    def _entry_edited(self, what: object) -> None:
        raw = self._current()
        if self._loading or raw is None:
            return
        if what == "title":
            lore.set_entry_title(raw, self.title.text())
        elif what == "keys":
            lore.entry_set(raw, "keys", _keys_of(self.keys.text()))
        elif what == "content":
            raw["content"] = self.content.toPlainText()
        elif what is self.enabled:
            lore.set_entry_enabled(raw, self.enabled.isChecked())
        elif what is self.constant:
            raw["constant"] = self.constant.isChecked()
        elif what is self.use_regex:
            raw["use_regex"] = self.use_regex.isChecked()
        elif what is self.case_sensitive:
            lore.set_entry_case_sensitive(raw, self.case_sensitive.isChecked())
        elif what == "secondary":
            lore.entry_set(raw, "secondary_keys", _keys_of(self.secondary.text()))
        elif what == "selective":
            raw["selective"] = self.selective.isChecked()
        elif what == "order":
            lore.entry_set(raw, "insertion_order", self.order.value())
        item = self.list.currentItem()
        if item is not None:
            item.setText(self._line(raw))
            item.setToolTip(_keys_text(lore.entry_keys(raw)) or "No keywords")
        self.changed.emit()

    def _entry_rest_edited(self, rest: dict[str, Any]) -> None:
        raw = self._current()
        if raw is not None:
            lore.set_entry_rest(raw, rest)
            self.changed.emit()

    def _add(self) -> None:
        tavern = self._tavern
        if tavern is None:
            return
        if tavern.character_book is None:
            tavern.character_book = lore.new_book()
        lore.add_entry(tavern.character_book)
        self.load(self._character, row=len(lore.book_entries(tavern.character_book)) - 1)
        self.title.setFocus()
        self.changed.emit()

    def _copy(self) -> None:
        raw, book = self._current(), self._book
        if raw is None or book is None:
            return
        new = lore.copy_entry(book, raw)
        rows = lore.book_entries(book)
        self.load(self._character, row=next(i for i, e in enumerate(rows) if e is new))
        self.changed.emit()

    def _remove(self) -> None:
        raw, book = self._current(), self._book
        if raw is None or book is None or not self.remove_button.isEnabled():
            return
        if str(raw.get("content") or "").strip():
            answer = QMessageBox.question(
                self,
                "Remove this entry?",
                f"Take “{self._line(raw)}” out of their lorebook?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        row = self.list.currentRow()
        lore.remove_entry(book, raw)
        self.load(self._character, row=row)
        self.changed.emit()
