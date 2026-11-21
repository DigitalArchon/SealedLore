"""New story from a lorebook (Lorebooks, File → Import… of a lorebook): what
the story is called, who the author plays, and whether anyone comes from Your
characters and the people the lorebook describes become characters, asked
before the story is made (gui/window_lorebooks.py makes it).
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.lore_people import batches, shown_entries
from sealedlore.gui.lore_rules import LoreRulesChoice, settings_in_use
from sealedlore.gui.sizing import enter_only_on_buttons, may_discard, size_to_screen, typed_texts
from sealedlore.models.lorebook import StoredLorebook
from sealedlore.storage import lorebooks


@dataclass
class LorebookStory:
    title: str
    player: str
    about_player: str
    from_collection: bool
    find_people: bool
    lore_firing: str = "sillytavern"


class LorebookStoryDialog(QDialog):
    """`kept`: how many characters Your characters holds; `can_ask`: a model
    is set up to find the people with."""

    def __init__(
        self, book: StoredLorebook, *, kept: int, can_ask: bool, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"A new story from {book.name}")
        size_to_screen(self, 0.4, 0.6, at_least=QSize(620, 560))
        found = lorebooks.stats(book.book)
        lead = QLabel(
            f"A story set in the world of {book.name}. Its {found.entries} entries "
            f"({found.words:,} words) become the story's lore, a copy of its own: "
            + (f"{found.always} of them always sent, " if found.always else "")
            + "the rest when the story mentions them. Setup opens next, to look everything "
            "over before the story begins."
        )
        lead.setWordWrap(True)
        self.title = QLineEdit(book.name)
        self.player = QLineEdit()
        self.player.setPlaceholderText("Blank: choose someone from the cast when it begins")
        self.about = QLineEdit()
        self.about.setPlaceholderText("Who they are, in a line (optional)")
        self.collection = QCheckBox("Bring in characters from Your characters (asked next)")
        self.collection.setEnabled(kept > 0)
        if not kept:
            self.collection.setToolTip("Your characters has nobody in it yet")
        runs = len(batches(shown_entries(lorebooks.lore_of(book).entries)))
        self.people = QCheckBox("Find the people its lore describes, to make them characters")
        self.people.setChecked(can_ask and found.entries > 0)
        self.people.setEnabled(can_ask and found.entries > 0)
        people_note = QLabel(
            f"The authoring model reads its entries ({runs} call{'s' if runs != 1 else ''}, "
            "more for an entry about several people); you then choose who is the cast and "
            "who supporting, and the instructions for the storyteller it finds are pointed out."
            if can_ask
            else "Set up a model in Settings to find the people in it."
        )
        people_note.setObjectName("hintLabel")
        people_note.setWordWrap(True)
        self.lore_rules = LoreRulesChoice(uses=settings_in_use(lorebooks.lore_of(book)))
        form = QFormLayout()
        form.addRow("Title", self.title)
        form.addRow("You play", self.player)
        form.addRow("", self.about)
        form.addRow("", self.collection)
        form.addRow("", self.people)
        form.addRow("", people_note)
        form.addRow("Lore fires by", self.lore_rules)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Make the story")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(lead)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addWidget(buttons)
        self._typed = typed_texts(self)

    def _accept(self) -> None:
        if not self.title.text().strip():
            self.title.setFocus()
            return
        self.accept()

    def reject(self) -> None:
        if may_discard(self, self._typed):
            super().reject()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)

    def chosen(self) -> LorebookStory:
        return LorebookStory(
            title=self.title.text().strip(),
            player=self.player.text().strip(),
            about_player=self.about.text().strip(),
            from_collection=self.collection.isChecked() and self.collection.isEnabled(),
            find_people=self.people.isChecked() and self.people.isEnabled(),
            lore_firing=self.lore_rules.value(),
        )
