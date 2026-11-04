"""Start an unplayed story: choose who to play and who is there, then the opening runs."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.sizing import size_to_screen
from sealedlore.gui.start_presence import StartPresence
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.story import Story
from sealedlore.storage.template_pictures import Kept


def opening_note(story: Story) -> str:
    setup = story.setup
    if not setup.opening_text.strip():
        return "This story has no opening set, so you write the first turn."
    if setup.opening_mode == "as_written":
        return "The opening passage is used as written. No call is made."
    return "The model writes the opening from the setup's notes. Regenerate gives another."


def _count(n: int, one: str, many: str) -> str:
    """ "3 characters", "1 lore entry"; "" for none."""
    return f"{n} {one if n == 1 else many}" if n else ""


class StartDialog(QDialog):
    def __init__(
        self,
        story: Story,
        cast: Sequence[Character],
        parent: QWidget | None = None,
        *,
        without_pictures: tuple[Sequence[Character], Sequence[LoreEntry]] = ((), ()),
        kept: Kept | None = None,
    ) -> None:
        """`without_pictures`: the characters and lore entries the models may
        see that have no picture yet; when there are any, a picture of each
        is offered (a plot file or premise often brings none)."""
        super().__init__(parent)
        self.setWindowTitle(f"Start — {story.title}")
        self.setMinimumWidth(460)
        size_to_screen(self, 0.4, 0.7)
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
                max_height=None,
            )
            # The roster takes the spare height (it scrolled in a small box
            # over an empty half of the dialog).
            layout.addWidget(self.presence, 1)
            self.play_as.currentIndexChanged.connect(self._lock_rows)
            self._lock_rows()
        # A picture of each card (the author, Oct 15 2026): ticked here, the
        # pictures dialog then shows every prompt before anything is drawn,
        # and the opening runs while they are drawn.
        self.pictures: QCheckBox | None = None
        self._without = without_pictures
        people, lore = without_pictures
        if people or lore:
            self.pictures = QCheckBox()
            layout.addWidget(self.pictures)
            self.play_as.currentIndexChanged.connect(self._sync_pictures)
            self._sync_pictures()
        # Pictures the author kept for this template (the cast board's Keep
        # for new stories…): the template as it is, or with them.
        self.kept: QCheckBox | None = None
        if kept is not None and kept.count:
            self.kept = QCheckBox(
                f"Add your {kept.count} pictures for {kept.template} (kept from “{kept.kept_from}”)"
            )
            self.kept.setChecked(True)
            self.kept.setToolTip(
                f"The pictures, labels and sheets you kept for {kept.template}, on its characters "
                "by name. Untick for the pictures it comes with alone."
            )
            layout.addWidget(self.kept)
        note = QLabel(opening_note(story))
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        layout.addWidget(note)
        if self.presence is None:
            layout.addStretch(1)

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

    def _sync_pictures(self) -> None:
        """The cards a picture is offered for, less whoever the choice of
        character sets aside: their portrait was drawn, paid for and lost."""
        if self.pictures is None:
            return
        held = self.play_as.currentData()
        aside = set(self._choose_one_of) - {held} if held in self._choose_one_of else set()
        people = [c for c in self._without[0] if c.id not in aside]
        lore = list(self._without[1])
        counts = [
            _count(len(people), "character", "characters"),
            _count(len(lore), "lore entry", "lore entries"),
        ]
        counts = [count for count in counts if count]
        self.pictures.setText(
            f"Draw pictures for the cards that have none ({' and '.join(counts)})"
        )
        names = [c.name for c in people] + [e.title for e in lore]
        self.pictures.setToolTip(
            f"{', '.join(names)}: drawn from their cards by an image model. You see "
            "every prompt and the price before anything is drawn, and can untick any."
        )
        self.pictures.setVisible(bool(people or lore))

    def pictures_wanted(self) -> bool:
        return (
            self.pictures is not None and self.pictures.isChecked() and not self.pictures.isHidden()
        )

    def held_character_id(self) -> str | None:
        return self.play_as.currentData()

    def pinned(self) -> tuple[list[str], list[str]] | None:
        """(pinned present, pinned not present) at the start; None with no cast."""
        return self.presence.pinned() if self.presence is not None else None

    def kept_wanted(self) -> bool:
        return self.kept is not None and self.kept.isChecked()
