"""Dialogs for private scenes: fitting the story to the private model, and
approving the summary that goes back into the story."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.session_private import PrivateFit


class FitDialog(QDialog):
    """The story is bigger than the private model's budget."""

    SUMMARISE, RECENT, CANCEL = "summarise", "recent", "cancel"

    def __init__(self, fit: PrivateFit, main_model: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("The story is too long for the private model")
        self.choice = self.CANCEL
        text = QLabel(
            f"The story so far would need about {fit.total:,} tokens; your private model's "
            f"budget is {fit.budget:,}, which leaves room for only the last "
            f"{fit.kept_messages} messages.\n\n"
            f"The story's own model ({main_model}) can condense the story so far to fit. "
            "It reads only what came before this scene, never the scene itself."
        )
        text.setWordWrap(True)
        summarise = QPushButton("Summarise the story to fit")
        summarise.setObjectName("sendButton")
        summarise.clicked.connect(lambda: self._done(self.SUMMARISE))
        recent = QPushButton("Just use the most recent")
        recent.setToolTip("The oldest passages simply don't fit, as when a budget is full")
        recent.clicked.connect(lambda: self._done(self.RECENT))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(lambda: self._done(self.CANCEL))
        row = QHBoxLayout()
        row.addStretch(1)
        for button in (cancel, recent, summarise):
            row.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addLayout(row)
        self.setMinimumWidth(520)

    def _done(self, choice: str) -> None:
        self.choice = choice
        self.accept() if choice != self.CANCEL else self.reject()


class PrivateSummaryDialog(QDialog):
    """The private model's summary of the scene, for the author to approve.

    Whatever is approved is all the story's model will ever know of the scene.
    """

    APPROVE, AGAIN, BACK, DISCARD = "approve", "again", "back", "discard"

    def __init__(
        self, summary: str, parent: QWidget | None = None, *, note: str | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Private scene summary")
        self.choice = self.BACK
        intro = QLabel(
            "The private model's summary of the scene. Once approved it joins the story as "
            "a passage, and it is all the story's model, or any other, will know of the "
            "scene. Change anything you like, and check especially who it says saw or "
            "heard what: a private model can lose track of who left the room."
            + (f"\n\n⚠ {note}" if note else "")
        )
        intro.setWordWrap(True)
        intro.setObjectName("hintLabel")
        self.editor = QPlainTextEdit(summary)
        self.editor.setMinimumHeight(220)
        self.approve = QPushButton("Approve")
        self.approve.setObjectName("sendButton")
        self.approve.clicked.connect(lambda: self._done(self.APPROVE))
        self.editor.textChanged.connect(
            lambda: self.approve.setEnabled(bool(self.editor.toPlainText().strip()))
        )
        again = QPushButton("Write it again")
        again.clicked.connect(lambda: self._done(self.AGAIN))
        back = QPushButton("Back to the scene")
        back.setToolTip("Keep playing the private scene; nothing is added")
        back.clicked.connect(lambda: self._done(self.BACK))
        discard = QPushButton("Discard the scene")
        discard.setToolTip(
            "End the scene with nothing carried over: the story goes on from where it began"
        )
        discard.clicked.connect(lambda: self._done(self.DISCARD))
        row = QHBoxLayout()
        row.addWidget(discard)
        row.addStretch(1)
        row.addWidget(back)
        row.addWidget(again)
        row.addWidget(self.approve)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.editor, 1)
        layout.addLayout(row)
        self.resize(640, 420)

    def text(self) -> str:
        return self.editor.toPlainText()

    def _done(self, choice: str) -> None:
        self.choice = choice
        self.accept()
