"""Start an unplayed story: choose who to play and who is there, then the opening runs."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.start_presence import StartPresence
from sealedlore.models.character import Character
from sealedlore.models.story import Story


def opening_note(story: Story) -> str:
    setup = story.setup
    if not setup.opening_text.strip():
        return "This story has no opening set, so you write the first turn."
    if setup.opening_mode == "as_written":
        return "The opening passage is used as written. No call is made."
    return "The model writes the opening from the setup's notes. Regenerate gives another."


class StartDialog(QDialog):
    def __init__(
        self, story: Story, cast: Sequence[Character], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Start — {story.title}")
        self.setMinimumWidth(460)
        self._choose_one_of = list(story.setup.choose_one_of)

        self.play_as = QComboBox()
        self.play_as.addItem("Nobody — I'll direct", None)
        # "Play one of these": only they can start; the rest are met in play.
        choices = story.setup.choose_one_of
        for character in cast:
            if character.id in choices if choices else character.is_player_available:
                self.play_as.addItem(character.name, character.id)
        index = self.play_as.findData(story.setup.suggested_character_id)
        self.play_as.setCurrentIndex(index if index >= 0 else min(1, self.play_as.count() - 1))

        form = QFormLayout()
        form.addRow("Play as", self.play_as)

        layout = QVBoxLayout(self)
        if story.setup.description:
            description = QLabel(story.setup.description)
            description.setWordWrap(True)
            layout.addWidget(description)
        layout.addLayout(form)
        # Here as well as in Setup: a new story's cast is often made after
        # its Setup, so this is the first place it can be chosen.
        self.presence: StartPresence | None = None
        if cast:
            layout.addWidget(QLabel("Who is there at the start"))
            self.presence = StartPresence(
                cast,
                story.setup.starting_scene.present_character_ids,
                story.setup.absent_at_start,
            )
            layout.addWidget(self.presence)
            self.play_as.currentIndexChanged.connect(self._lock_rows)
            self._lock_rows()
        note = QLabel(opening_note(story))
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        start = buttons.addButton("Start", QDialogButtonBox.AcceptRole)
        start.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _lock_rows(self) -> None:
        if self.presence is None:
            return
        held = self.play_as.currentData()
        # Picking one of a "play one of these" choice sets the others aside.
        aside = [i for i in self._choose_one_of if i != held] if held in self._choose_one_of else []
        self.presence.set_locked(held, aside)

    def held_character_id(self) -> str | None:
        return self.play_as.currentData()

    def pinned(self) -> tuple[list[str], list[str]] | None:
        """(pinned present, pinned not present) at the start; None with no cast."""
        return self.presence.pinned() if self.presence is not None else None
