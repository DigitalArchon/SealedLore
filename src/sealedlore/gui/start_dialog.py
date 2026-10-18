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


# The Map tiles row's first and last choices.
AI_CHOOSES = "The AI chooses the closest set"
DESIGN_ONE = "Design a new set for this world…"
DESIGN = "design"


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
        tile_sets: Sequence[tuple[str, str]] = (),
    ) -> None:
        """`without_pictures`: the characters and lore entries the models may
        see that have no picture yet; when there are any, a picture of each
        is offered (a plot file or premise often brings none). `tile_sets`:
        (name, title) of each set a new world's map may be drawn in; when
        there are any, the look of the map is chosen here, before anything
        is drawn (the author, Oct 18 2026: by hand, by the AI, or a new set
        designed for the world)."""
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
        self.tile_set: QComboBox | None = None
        if tile_sets:
            self.tile_set = QComboBox()
            self.tile_set.addItem(AI_CHOOSES, "")
            for name, title in tile_sets:
                self.tile_set.addItem(title, name)
            self.tile_set.addItem(DESIGN_ONE, DESIGN)
            self.tile_set.setToolTip(
                "The pixel art the map is drawn in. The AI reads this world and chooses the "
                "closest of the sets; or choose one yourself; or have a new set designed for "
                "this world, from your words and pictures of it, before the story starts. "
                "Look ▾ → Tile sets… changes it later."
            )
            form.addRow("Map tiles", self.tile_set)

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

    def tile_set_choice(self) -> str | None:
        """None: not asked; "": the AI chooses; DESIGN: a new set designed
        first; else the name of the set chosen."""
        return self.tile_set.currentData() if self.tile_set is not None else None

    def held_character_id(self) -> str | None:
        return self.play_as.currentData()

    def pinned(self) -> tuple[list[str], list[str]] | None:
        """(pinned present, pinned not present) at the start; None with no cast."""
        return self.presence.pinned() if self.presence is not None else None
