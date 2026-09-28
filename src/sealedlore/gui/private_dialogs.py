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

# A simple chat has private parts on a story's terms, so every message about
# one is written once, of a story, and said of a chat by these. In order: the
# longer phrase first, or "the story's model" would become "the chat's model"
# by way of "the chat".
_CHAT_WORDS = (
    ("Private scene", "Private part"),
    ("private scene", "private part"),
    ("Back to the scene", "Back to the private part"),
    ("Keep playing the private scene", "Stay in the private part"),
    ("Discard the scene", "Discard the private part"),
    ("before this scene", "before this part"),
    ("the scene itself", "the part itself"),
    ("The scene", "The private part"),
    ("the scene", "the private part"),
    ("joins the story as a passage", "joins the chat as a message"),
    ("The story's own model", "The chat's own model"),
    ("the story's own model", "the chat's own model"),
    ("the story's model", "the chat's model"),
    ("The story so far", "The chat so far"),
    ("the story so far", "the chat so far"),
    ("Summarise the story", "Summarise the chat"),
    ("The story", "The chat"),
    ("This story", "This chat"),
    ("the story", "the chat"),
    ("oldest passages", "oldest messages"),
    ("Keep playing", "Keep going"),
    ("keep playing", "keep going"),
    (
        "check especially who it says saw or heard what: a private model can lose track of "
        "who left the room",
        "check that it says no more than you want known",
    ),
)


def chat_words(text: str, chat: bool = True) -> str:
    """`text`, written of a story's private scene, as it is said in a chat."""
    if not chat:
        return text
    for story, said in _CHAT_WORDS:
        text = text.replace(story, said)
    return text


class FitDialog(QDialog):
    """The story is bigger than the private model's budget."""

    SUMMARISE, RECENT, CANCEL = "summarise", "recent", "cancel"

    def __init__(
        self,
        fit: PrivateFit,
        main_model: str,
        parent: QWidget | None = None,
        *,
        chat: bool = False,
    ) -> None:
        super().__init__(parent)
        self.chat = chat
        self.setWindowTitle(self._say("The story is too long for the private model"))
        self.choice = self.CANCEL
        text = QLabel(
            f"The story so far would need about {fit.total:,} tokens; your private model's "
            f"budget is {fit.budget:,}, which leaves room for only the last "
            f"{fit.kept_messages} messages.\n\n"
            f"The story's own model ({main_model}) can condense the story so far to fit. "
            "It reads only what came before this scene, never the scene itself."
        )
        text.setText(self._say(text.text()))
        text.setWordWrap(True)
        summarise = QPushButton(self._say("Summarise the story to fit"))
        summarise.setObjectName("sendButton")
        summarise.clicked.connect(lambda: self._done(self.SUMMARISE))
        recent = QPushButton("Just use the most recent")
        recent.setToolTip(
            self._say("The oldest passages simply don't fit, as when a budget is full")
        )
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

    def _say(self, text: str) -> str:
        return chat_words(text, self.chat)

    def _done(self, choice: str) -> None:
        self.choice = choice
        self.accept() if choice != self.CANCEL else self.reject()


class PrivateSummaryDialog(QDialog):
    """The private model's summary of the scene, for the author to approve.

    Whatever is approved is all the story's model will ever know of the scene.
    """

    APPROVE, AGAIN, BACK, DISCARD = "approve", "again", "back", "discard"

    def __init__(
        self,
        summary: str,
        parent: QWidget | None = None,
        *,
        note: str | None = None,
        chat: bool = False,
    ) -> None:
        super().__init__(parent)
        say = lambda text: chat_words(text, chat)  # noqa: E731
        self.setWindowTitle(say("Private scene summary"))
        self.choice = self.BACK
        intro = QLabel(
            "The private model's summary of the scene. Once approved it joins the story as "
            "a passage, and it is all the story's model, or any other, will know of the "
            "scene. Change anything you like, and check especially who it says saw or "
            "heard what: a private model can lose track of who left the room."
            + (f"\n\n⚠ {note}" if note else "")
        )
        intro.setText(say(intro.text()))
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
        back = QPushButton(say("Back to the scene"))
        back.setToolTip(say("Keep playing the private scene; nothing is added"))
        back.clicked.connect(lambda: self._done(self.BACK))
        discard = QPushButton(say("Discard the scene"))
        discard.setToolTip(
            say("End the scene with nothing carried over: the story goes on from where it began")
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
