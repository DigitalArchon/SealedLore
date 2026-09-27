"""Who is there when a story begins: one choice per cast member.

The author's call (Sept 2026): by default the opening places everyone,
here from the start or elsewhere, as the story calls for; the author may
pin someone present or not present. Setup's Starting scene tab and the
Start dialog both show it, since a new story's cast often exists only by
the time it is started.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sealedlore.models.character import Character

DECIDES = "Opening decides"
PRESENT = "Present"
ABSENT = "Not present"
PLAYING = "Playing"
SET_ASIDE = "Not in this playthrough"

HINT = (
    "“Opening decides”: the opening places them, here from the start or elsewhere, as the "
    "story calls for. Choose Present or Not present to decide it yourself. Anyone not "
    "here at the start can still come in later."
)


class StartPresence(QWidget):
    """A row per cast member: the opening decides, present, or not present."""

    def __init__(
        self,
        cast: Sequence[Character],
        present: Collection[str] = (),
        absent: Collection[str] = (),
        parent: QWidget | None = None,
        *,
        max_height: int | None = 200,
    ) -> None:
        """`max_height` caps the list before it scrolls (a dialog of its own);
        None lets it take the room it is given (Setup's tab)."""
        super().__init__(parent)
        rows = QWidget()
        grid = QGridLayout(rows)
        # Room on the right for the scrollbar, which otherwise covers the
        # combos' arrows.
        grid.setContentsMargins(0, 0, 14, 0)
        grid.setColumnStretch(0, 1)
        self._choices: dict[str, QComboBox] = {}
        # What a row said before it was locked (held, or set aside), so
        # unlocking it, or reading it, gives back the author's own choice.
        self._before_lock: dict[str, int] = {}
        for row, character in enumerate(cast):
            combo = QComboBox()
            combo.addItem(DECIDES, None)
            combo.addItem(PRESENT, "present")
            combo.addItem(ABSENT, "absent")
            # By index: PySide6 doesn't find an item whose data is None.
            index = 1 if character.id in present else 2 if character.id in absent else 0
            combo.setCurrentIndex(index)
            grid.addWidget(QLabel(character.name), row, 0)
            grid.addWidget(combo, row, 1)
            self._choices[character.id] = combo

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(rows)
        # A cast of three shouldn't sit in a tall empty box.
        fits = rows.sizeHint().height() + 4
        scroll.setMaximumHeight(fits if max_height is None else min(fits, max_height))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll, 1)
        hint = QLabel(HINT)
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def _lock(self, character_id: str, text: str) -> None:
        combo = self._choices[character_id]
        if character_id not in self._before_lock:
            self._before_lock[character_id] = combo.currentIndex()
        # One item standing for the lock, removed again on unlock.
        combo.addItem(text, "locked")
        combo.setCurrentIndex(combo.count() - 1)
        combo.setEnabled(False)

    def _unlock(self, character_id: str) -> None:
        combo = self._choices[character_id]
        if character_id not in self._before_lock:
            return
        combo.removeItem(combo.findData("locked"))
        combo.setCurrentIndex(self._before_lock.pop(character_id))
        combo.setEnabled(True)

    def set_locked(self, held: str | None, set_aside: Collection[str] = ()) -> None:
        """The one being played is present; those set aside by that choice
        (a "play one of these" story) aren't in this playthrough at all."""
        for character_id in self._choices:
            self._unlock(character_id)
        for character_id in self._choices:
            if character_id == held:
                self._lock(character_id, PLAYING)
            elif character_id in set_aside:
                self._lock(character_id, SET_ASIDE)

    def show_roster(self, present: Collection[str]) -> None:
        """Present for whoever is in `present`, the opening for everyone else."""
        for character_id, combo in self._choices.items():
            if character_id in self._before_lock:
                self._before_lock[character_id] = 1 if character_id in present else 0
            else:
                combo.setCurrentIndex(1 if character_id in present else 0)

    def choice(self, character_id: str) -> str | None:
        combo = self._choices[character_id]
        if character_id in self._before_lock:
            return combo.itemData(self._before_lock[character_id])
        return combo.currentData()

    def pinned(self) -> tuple[list[str], list[str]]:
        """(pinned present, pinned not present), in cast order. A locked row
        gives the author's own choice for it, so being played this time
        doesn't pin anyone for a Restart."""
        present = [i for i in self._choices if self.choice(i) == "present"]
        absent = [i for i in self._choices if self.choice(i) == "absent"]
        return present, absent
