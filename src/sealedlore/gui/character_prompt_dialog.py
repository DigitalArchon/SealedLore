"""What a character's own model is sent, and the author's say in it (the
trial, engine/reactions.py): Played by → What their model is sent… on the
card of a character played by their own model.

The author (Nov 18 2026): their personality should be fixed largely from
what their model is told, with a way to see each one's prompt and history,
and to change it. Three tabs: the request their model would be sent if their
round ran now, read-only and section by section; the author's notes for
their model (`Character.own_notes`, in its system prompt after the card);
and their memory notes, each editable (`Recollection.edited_at`). The rest
is changed where it lives, which the first tab says: the card on the Card
tab, the wording under File → Prompts (advanced)…, the story by editing it.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.sizing import GrowingText, enter_only_on_buttons, may_discard, size_to_screen

TITLE = "What {name}'s model is sent"
SENT_TAB = "Sent next"
NOTES_TAB = "Notes for their model"
MEMORY_TAB = "What they remember"
SENT_ABOUT = (
    "Their next request as it stands, if their round ran now. Their personality comes from "
    "the first part: their card (the Card tab), your notes for their model (the next tab), "
    "and the story's setting. The rules for answering are SealedLore's, changed under File → "
    "Prompts (advanced)…, in “Characters played by their own model (trial)”. Then what they "
    "remember of older scenes (the last tab), and the recent story as they saw it: change "
    "that by editing the story."
)
NOTES_ABOUT = (
    "Your own instructions to the model that plays them, sent after their card in its "
    "system prompt: how to play them, beyond what the card says. The storyteller never "
    "sees these."
)
MEMORY_ABOUT = (
    "What they remember of scenes older than their recent window, in their own words, "
    "written by the summarisation model once that much of the story is behind them. Your "
    "changes stand while the messages a note covers are as they were."
)
NO_MEMORY = "None yet: their recent window still holds everything they have seen."


def as_text(messages) -> str:
    """A request's messages as plain text, each headed by its role."""
    blocks = []
    for message in messages:
        heading = {"system": "SYSTEM", "user": "MESSAGE", "assistant": "REPLY"}.get(
            message.role, message.role.upper()
        )
        text = "\n\n".join(part.text for part in message.parts if part.text.strip())
        blocks.append(f"━━ {heading} ━━\n\n{text}")
    return "\n\n".join(blocks)


def _about(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    label.setTextFormat(Qt.PlainText)
    return label


class CharacterPromptDialog(QDialog):
    def __init__(self, session, character_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.character_id = character_id
        character = session._character(character_id)
        name = character.name if character is not None else "Their"
        self.setWindowTitle(TITLE.format(name=name))
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        sent = QWidget()
        sent_layout = QVBoxLayout(sent)
        sent_layout.addWidget(_about(SENT_ABOUT))
        self.sent = QPlainTextEdit()
        self.sent.setReadOnly(True)
        self.sent.setObjectName("promptView")
        sent_layout.addWidget(self.sent, 1)
        self.tabs.addTab(sent, SENT_TAB)

        notes = QWidget()
        notes_layout = QVBoxLayout(notes)
        notes_layout.addWidget(_about(NOTES_ABOUT))
        self.notes = GrowingText(
            (character.own_notes or "") if character is not None else "", lines=8, fill=True
        )
        self.notes.setPlaceholderText(
            "e.g. Never trusts the constabulary. Answers a question with a question when "
            "cornered. Keeps his word, but chooses it carefully."
        )
        notes_layout.addWidget(self.notes, 1)
        self.tabs.addTab(notes, NOTES_TAB)

        memory = QWidget()
        memory_layout = QVBoxLayout(memory)
        memory_layout.addWidget(_about(MEMORY_ABOUT))
        body = QWidget()
        rows = QVBoxLayout(body)
        self.memories: list[tuple[str, QPlainTextEdit]] = []
        notes_in_force = session.memories_of(character_id)
        if not notes_in_force:
            rows.addWidget(_about(NO_MEMORY))
        for index, note in enumerate(notes_in_force, 1):
            by = "you" if note.edited_at else (note.model or "the summarisation model")
            rows.addWidget(_about(f"{index}. Written by {by}"))
            box = GrowingText(note.content, lines=4)
            rows.addWidget(box)
            self.memories.append((note.id, box))
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        memory_layout.addWidget(scroll, 1)
        self.tabs.addTab(memory, MEMORY_TAB)

        self.problem = _about("")
        self.problem.hide()
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Save).clicked.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_sent()
        self._saved = self._state()
        size_to_screen(self, 0.55, 0.75, at_least=QSize(640, 520))

    def _state(self) -> list[str]:
        return [self.notes.toPlainText(), *(box.toPlainText() for _id, box in self.memories)]

    def _refresh_sent(self) -> None:
        try:
            self.sent.setPlainText(as_text(self.session.character_prompt(self.character_id)))
        except ValueError as error:
            self.sent.setPlainText(str(error))

    def save(self) -> bool:
        """Notes and changed memories onto the story; the first tab then
        shows the request as it now stands."""
        try:
            if self.notes.toPlainText() != self._saved[0]:
                self.session.set_own_notes(self.character_id, self.notes.toPlainText())
            for (memory_id, box), before in zip(self.memories, self._saved[1:], strict=True):
                if box.toPlainText() != before:
                    self.session.edit_memory(memory_id, box.toPlainText())
        except ValueError as error:
            self.problem.setText(str(error))
            self.problem.show()
            return False
        self.problem.hide()
        self._saved = self._state()
        self._refresh_sent()
        return True

    def reject(self) -> None:
        if may_discard(self, self._saved, self._state()):
            super().reject()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)


def show_character_prompt(parent: QWidget, session, character_id: str) -> None:
    dialog = CharacterPromptDialog(session, character_id, parent)
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()  # read, then freed
