"""A row's or a column's heading on the cast board's sheet, and the words it
is drawn with (engine/look_sheet.py `wording`): Add a look…, Add an
expression…, and a heading's Heading and wording….

The author (Nov 7 2026): a column should have a heading, "happy", and the
words a picture is drawn with should say more, "a happy expression and a
bright smile", or for someone else "happy but reserved, a small smile and
bright eyes": the same emotion, drawn differently. The heading names the row
or column on the sheet and labels its pictures; the words are what an image
model is asked for, the usual ones until the author writes their own.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.look_sheet import tidy, usual_wording
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.gui.sizing import GrowingText, may_discard, size_to_screen

HEADING_AND_WORDING = "Heading and wording…"
USUAL_LABEL = "Use the usual words"


class SheetLabelDialog(QDialog):
    """See the module. `look`: a row (clothes, armour, an injury) rather than
    a column (an expression or pose). `offered`: headings to choose from
    (the common ones the sheet hasn't got); `taken`: those it has, which a
    new heading may not repeat."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        title: str,
        about: str,
        look: bool,
        heading: str = "",
        words: str = "",
        offered: Sequence[str] = (),
        taken: Sequence[str] = (),
        texts: PromptTexts = DEFAULT_TEXTS,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        size_to_screen(self, 0.32, 0.3, at_least=QSize(520, 300))
        self._look = look
        self._texts = texts
        self._taken = {tidy(x).lower() for x in taken if tidy(x).lower() != tidy(heading).lower()}
        # The words follow the heading until the author writes their own.
        self._words_by_hand = bool(words) and words != usual_wording(
            heading, look=look, texts=texts
        )

        intro = QLabel(about)
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)
        self.heading = QComboBox()
        self.heading.setEditable(True)
        self.heading.addItems([heading] if heading else ["", *offered])
        self.heading.setCurrentText(heading)
        self.heading.lineEdit().setPlaceholderText(
            "combat armour, formal clothes" if look else "happy, laughing, in a fighting stance"
        )
        self.heading.currentTextChanged.connect(self._heading_changed)
        self.words = GrowingText(lines=2, most=5)
        self.words.setPlaceholderText(
            "a dented steel breastplate over a red tunic"
            if look
            else "a happy expression with a bright smile"
        )
        self.words.setPlainText(words or usual_wording(heading, look=look, texts=texts))
        self.words.textChanged.connect(self._words_typed)
        self.usual = QPushButton(USUAL_LABEL)
        self.usual.setToolTip(
            "The words SealedLore draws this with when a card has none of its own (for most, "
            "the heading itself)"
        )
        self.usual.clicked.connect(self._use_usual)
        heading_hint = QLabel(
            "Names the row on the sheet and labels its pictures"
            if look
            else "Names the column on the sheet and labels its pictures"
        )
        heading_hint.setObjectName("hintLabel")
        heading_hint.setWordWrap(True)
        words_hint = QLabel(
            "What an image model is asked for when a picture is drawn for it. Only this "
            "character's: another can draw the same heading differently."
        )
        words_hint.setObjectName("hintLabel")
        words_hint.setWordWrap(True)
        self.problem = QLabel()
        self.problem.setObjectName("hintLabel")
        self.problem.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Heading", self.heading)
        form.addRow("", heading_hint)
        form.addRow("Drawn as", self.words)
        form.addRow("", words_hint)
        # As wide as its words: across the form it read as a field.
        usual_row = QHBoxLayout()
        usual_row.setContentsMargins(0, 0, 0, 0)
        usual_row.addWidget(self.usual)
        usual_row.addStretch(1)
        form.addRow("", usual_row)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.problem)
        layout.addStretch(1)
        layout.addWidget(self.buttons)
        self._sync()
        # The words it opened with: Cancel asks only when the author wrote
        # words of their own since (a heading picked from the list asked).
        self._opened_words = tidy(self.words.toPlainText())

    def values(self) -> tuple[str, str]:
        """(heading, words), tidied; the words "" when they are the usual."""
        heading = tidy(self.heading.currentText())
        words = tidy(self.words.toPlainText())
        if words.rstrip(" .") == usual_wording(heading, look=self._look, texts=self._texts):
            words = ""
        return heading, words

    def _heading_changed(self, text: str) -> None:
        if not self._words_by_hand:
            self.words.blockSignals(True)
            self.words.setPlainText(usual_wording(text, look=self._look, texts=self._texts))
            self.words.blockSignals(False)
        self._sync()

    def _words_typed(self) -> None:
        self._words_by_hand = True
        self._sync()

    def _use_usual(self) -> None:
        self._words_by_hand = False
        self._heading_changed(self.heading.currentText())

    def _sync(self) -> None:
        heading = tidy(self.heading.currentText())
        taken = heading.lower() in self._taken
        self.problem.setText(f"The sheet has “{heading}” already." if taken else "")
        self.problem.setVisible(taken)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(heading) and not taken)
        self.usual.setEnabled(
            tidy(self.words.toPlainText()).rstrip(" .")
            != usual_wording(heading, look=self._look, texts=self._texts)
        )

    def reject(self) -> None:  # noqa: D102
        written = self._words_by_hand and tidy(self.words.toPlainText()) != self._opened_words
        if not written or may_discard(self, [], [True]):
            super().reject()


def ask_sheet_label(parent: QWidget | None, **kwargs) -> tuple[str, str] | None:
    """The heading and words asked for (see `SheetLabelDialog`), or None."""
    dialog = SheetLabelDialog(parent, **kwargs)
    accepted = dialog.exec() == QDialog.Accepted
    values = dialog.values()
    dialog.deleteLater()
    return values if accepted else None
