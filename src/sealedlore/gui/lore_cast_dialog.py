"""Fill with lore characters: which world, and whom the author wants for sure.

The cards themselves are written by the authoring model on the worker
(`StorySession.fill_lore_cast`) and join the story's cards at once, to be
changed, moved or removed like any other.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.fields import set_field_text
from sealedlore.gui.sizing import GrowingText, size_to_screen
from sealedlore.models.config import model_label

FILL_LABEL = "Fill with lore characters…"


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


class LoreCastDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        model: str,
        lore: str = "",
        wanted: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Fill with lore characters")
        layout = QVBoxLayout(self)
        layout.addWidget(
            _hint(
                "Set in a world that already exists? Name it, a book or series, a film, a "
                "game, a myth or a period of history, and the model writes a card for each "
                "of its main and recurring characters. They join your cards like any "
                "other: change, move or remove any of them. Ask again to add people it "
                "missed; it is told who is on file already."
            )
        )
        form = QFormLayout()
        self.lore = QLineEdit()
        self.lore.setPlaceholderText("Shakespeare's Hamlet, the Arthurian legends…")
        set_field_text(self.lore, lore)
        form.addRow("The world", self.lore)
        self.wanted = GrowingText(lines=3, most=8)
        self.wanted.setPlaceholderText(
            "Optional: anyone you want for sure, in plain words. "
            "“Hamlet, Ophelia and the gravedigger”"
        )
        self.wanted.setTabChangesFocus(True)
        self.wanted.setPlainText(wanted)
        form.addRow("Characters you want", self.wanted)
        layout.addLayout(form)
        layout.addWidget(
            _hint(
                f"Written by {model_label(model) if model else 'the story model'} (Settings → "
                "Models → Authoring model). The central figures go into the cast, everyone "
                "else as supporting cards, sent to the storyteller when the story mentions "
                "them. Cards written by the model are marked ✦: look them over."
            )
        )
        layout.addStretch(1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok = self.buttons.addButton("Write the cards", QDialogButtonBox.AcceptRole)
        self.ok.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.lore.textChanged.connect(self._sync)
        self._sync()
        size_to_screen(self, 0.32, 0.36)

    def _sync(self) -> None:
        self.ok.setEnabled(bool(self.lore.text().strip()))

    def values(self) -> tuple[str, str]:
        return self.lore.text().strip(), self.wanted.toPlainText().strip()
