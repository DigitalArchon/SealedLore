"""The Start dialog's "Played by their own model": which main characters a
model of their own plays from the first passage (the trial, gui/character_roles.py).

The author (Nov 18 2026): a new story must offer ways to make characters
AI controlled. Every new story passes through Start (samples, imports,
drafts, the guide, Advanced setup), so this is where it is offered for all
of them; the card's Played by on the cast board stays the way to change it
later. The character played can't be one: their row is locked while chosen.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QGridLayout, QLabel, QVBoxLayout, QWidget

from sealedlore.gui.character_roles import OWN, OWN_MODEL_HINT, apply_played_by, played_by
from sealedlore.gui.model_picker import ModelField
from sealedlore.models.character import Character

HEADING = "Played by their own model (a trial)"
ABOUT = (
    "Another model plays them, as another player: after each passage it says what they "
    "mean to do, and the storyteller works that in with your turn. Any model your "
    "endpoints offer, a local one too. Change it later under Played by on their card."
)
NEEDS_MODEL = "Name the model that plays {names}, or untick them."


class StartPlayers(QWidget):
    """A row a main character: a tick and the model that plays them."""

    changed = Signal()

    def __init__(
        self,
        cast: Sequence[Character],
        browse: Callable[..., str | None] | None = None,
    ) -> None:
        super().__init__()
        self._browse = (
            None
            if browse is None
            else weakref.WeakMethod(browse)
            if hasattr(browse, "__self__")
            else (lambda: browse)
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        heading = QLabel(HEADING)
        layout.addWidget(heading)
        about = QLabel(ABOUT)
        about.setObjectName("hintLabel")
        about.setWordWrap(True)
        layout.addWidget(about)
        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        self.rows: dict[str, tuple[QCheckBox, ModelField]] = {}
        self._was: dict[str, str] = {}
        self._names: dict[str, str] = {}
        for row, character in enumerate(cast):
            own = played_by(character) == OWN
            tick = QCheckBox(character.name)
            tick.setChecked(own)
            model = ModelField(character.own_model or "", self._browse_models)
            model.setPlaceholderText(OWN_MODEL_HINT)
            tick.toggled.connect(self._sync)
            model.textChanged.connect(self._sync)
            grid.addWidget(tick, row, 0)
            grid.addWidget(model, row, 1)
            self.rows[character.id] = (tick, model)
            self._was[character.id] = (character.own_model or "").strip() if own else ""
            self._names[character.id] = character.name
        self.problem = QLabel()
        self.problem.setObjectName("hintLabel")
        self.problem.setWordWrap(True)
        layout.addWidget(self.problem)
        self._locked: str | None = None
        self._sync()

    def _browse_models(self, current: str) -> str | None:
        browse = self._browse() if self._browse is not None else None
        return browse(current) if browse is not None else None

    def lock(self, held: str | None) -> None:
        """The character played: never their own model's."""
        self._locked = held
        for character_id, (tick, _model) in self.rows.items():
            if character_id == held:
                tick.setChecked(False)
            tick.setEnabled(character_id != held)
        self._sync()

    def _sync(self) -> None:
        for tick, model in self.rows.values():
            model.setEnabled(tick.isChecked())
        missing = self.missing()
        self.problem.setText(
            NEEDS_MODEL.format(names=", ".join(self._names[i] for i in missing)) if missing else ""
        )
        self.problem.setVisible(bool(missing))
        self.changed.emit()

    def missing(self) -> list[str]:
        """Ticked with no model named: Start waits for one."""
        return [
            character_id
            for character_id, (tick, model) in self.rows.items()
            if tick.isChecked() and not model.text().strip()
        ]

    def chosen(self) -> dict[str, str | None]:
        """What changed: a character id → the model that now plays them,
        or None for one whose own model was taken away."""
        changes: dict[str, str | None] = {}
        for character_id, (tick, model) in self.rows.items():
            now = model.text().strip() if tick.isChecked() else ""
            if now != self._was[character_id]:
                changes[character_id] = now or None
        return changes


def apply_chosen(cast: Sequence[Character], chosen: dict[str, str | None]) -> list[Character]:
    """Each change onto its card, as the card's Played by would make it; one
    taken off goes back to "You or the storyteller". The cards changed."""
    changed = []
    for character in cast:
        if character.id not in chosen:
            continue
        model = chosen[character.id]
        if model:
            apply_played_by(character, OWN, model)
        else:
            apply_played_by(character, "either")
        changed.append(character)
    return changed
