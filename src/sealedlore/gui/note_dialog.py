"""A few words from the author for one passage: what a new take should do
differently (⋯ → Regenerate with a note…), or what should change in the one
there is (⋯ → Change this passage…).

One box and two buttons. The words go to the model with the request that
follows, so Enter in the box is a new line and never sends them (gui/sizing.py
`enter_only_on_buttons`), and closing with something typed asks first.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.sizing import GrowingText, enter_only_on_buttons, may_discard, size_to_screen

RETAKE_TITLE = "Regenerate with a note"
RETAKE_ASKS = "What should the new take do differently?"
RETAKE_BUTTON = "Regenerate"
RETAKE_HINT = (
    "Your note goes to the storyteller with this take only, as an instruction no character "
    "hears. The take you have now is kept (‹ ›) and isn't shown to the storyteller. Length "
    "and Outcome below the story apply as for Regenerate."
)
CHAT_RETAKE_HINT = (
    "Your note goes to the model with this reply only, apart from your message. The reply "
    "you have now is kept (‹ ›) and isn't shown to the model."
)
RETAKE_PLACEHOLDER = "She should refuse this time, and say why."

# Change this passage… in a story's words and a chat's (False, True).
CHANGE_TITLES = {False: "Change this passage", True: "Change this reply"}
CHANGE_ASKS = "What should change?"
CHANGE_BUTTON = "Change"
CHANGE_HINTS = {
    False: (
        "The storyteller writes this passage again with your change made, keeping the rest "
        "in the same words wherever it can. The result is a new take in this one's place; "
        "this one is kept (‹ ›)."
    ),
    True: (
        "The model writes this reply again with your change made, keeping the rest in the "
        "same words wherever it can. The result is a new take in this one's place; this one "
        "is kept (‹ ›)."
    ),
}
CHANGE_PLACEHOLDER = "It was raining. And she calls him by his first name."


class NoteDialog(QDialog):
    """`asks` over the box, `hint` under it, and `button` to go on, which
    waits for something to be written."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str,
        asks: str,
        button: str,
        hint: str = "",
        placeholder: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        question = QLabel(asks)
        question.setWordWrap(True)
        layout.addWidget(question)
        self.text = GrowingText(lines=4, most=14, fill=True)
        self.text.setObjectName("noteText")
        self.text.setPlaceholderText(placeholder)
        question.setBuddy(self.text)
        layout.addWidget(self.text, 1)
        self.hint = QLabel(hint)
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        if not hint:
            self.hint.hide()
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.go = self.buttons.addButton(button, QDialogButtonBox.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.text.textChanged.connect(self._sync)
        self._sync()
        size_to_screen(self, 0.3, 0.28, at_least=QSize(520, 300))
        self.text.setFocus()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Enter never sends the note (a paid call): it presses only a push
        button that has the focus."""
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)

    def _sync(self) -> None:
        self.go.setEnabled(bool(self.note()))

    def note(self) -> str:
        return self.text.toPlainText().strip()

    def accept(self) -> None:
        if self.note():
            super().accept()

    def reject(self) -> None:
        """Cancel, Esc or the window's close: asked first when something
        was written."""
        if may_discard(self, [""], [self.note()]):
            super().reject()


__all__ = [
    "CHANGE_ASKS",
    "CHANGE_BUTTON",
    "CHANGE_HINTS",
    "CHANGE_PLACEHOLDER",
    "CHANGE_TITLES",
    "CHAT_RETAKE_HINT",
    "RETAKE_ASKS",
    "RETAKE_BUTTON",
    "RETAKE_HINT",
    "RETAKE_PLACEHOLDER",
    "RETAKE_TITLE",
    "NoteDialog",
]
