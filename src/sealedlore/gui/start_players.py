"""The Start dialog's "Played by their own model": which main characters a
model of their own plays from the first passage (the trial, gui/character_roles.py).

The author (Nov 18 2026): a new story must offer ways to make characters
AI controlled. Every new story passes through Start (samples, imports,
drafts, the guide, Advanced setup), so this is where it is offered for all
of them; the card's Played by on the cast board stays the way to change it
later. The character played can't be one: their row is locked while chosen.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from sealedlore.gui.character_roles import (
    OWN,
    ROUND_ORDER_LABEL,
    ModelHelpers,
    apply_played_by,
    played_by,
    round_order_combo,
)
from sealedlore.gui.model_picker import ModelField
from sealedlore.models.character import Character
from sealedlore.models.route import ModelRoute

HEADING = "Played by their own model (a trial)"
ABOUT = (
    "Another model plays them, as another player: it says what they mean to do, and the "
    "storyteller works that in with your turn. Any model your endpoints offer, a local one "
    "too. Change it later under Played by on their card."
)
NEEDS_MODEL = "Name the model that plays {names}, or untick them."
PLAYING_TIP = "You play them (Play as above), so no model of their own does."
ASIDE_TIP = "Set aside in this playthrough (you chose another of them), so they never appear."


class StartPlayers(QWidget):
    """A row a main character: a tick and the model that plays them."""

    changed = Signal()

    def __init__(
        self,
        cast: Sequence[Character],
        browse: Callable[..., str | None] | None = None,
        routes: tuple[Callable, Callable, Callable] | None = None,
        round_order: str = "together",
        qualify: Callable[[str], str] | None = None,
    ) -> None:
        """`routes`: the window's Choose with a route, the endpoint a model
        is on, and a model's hosts (`ModelHelpers.lend`)."""
        super().__init__()
        self.helpers = ModelHelpers()
        browse_route, route_endpoint, hosts_of = routes or (None, None, None)
        self.helpers.lend(
            browse,
            browse_route=browse_route,
            route_endpoint=route_endpoint,
            hosts_of=hosts_of,
            qualify=qualify,
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
        self._was: dict[str, tuple[str, ModelRoute | None]] = {}
        self._names: dict[str, str] = {}
        for row, character in enumerate(cast):
            own = played_by(character) == OWN
            tick = QCheckBox(character.name)
            tick.setChecked(own)
            model = self.helpers.field()
            model.setText(character.own_model or "")
            model.set_route(character.own_route)
            tick.toggled.connect(self._sync)
            model.textChanged.connect(self._sync)
            grid.addWidget(tick, row, 0)
            grid.addWidget(model, row, 1)
            self.rows[character.id] = (tick, model)
            self._was[character.id] = (
                ((character.own_model or "").strip(), character.own_route) if own else ("", None)
            )
            self._names[character.id] = character.name
        # When they answer: the story's setting (Setup → About too).
        order_row = QHBoxLayout()
        order_row.addWidget(QLabel(ROUND_ORDER_LABEL))
        self.round_order = round_order_combo(round_order)
        order_row.addWidget(self.round_order)
        order_row.addStretch(1)
        layout.addLayout(order_row)
        self.problem = QLabel()
        self.problem.setObjectName("hintLabel")
        self.problem.setWordWrap(True)
        layout.addWidget(self.problem)
        self._locked: str | None = None
        self._sync()

    def lock(self, held: str | None, aside: Sequence[str] = ()) -> None:
        """The character played is never their own model's, and nor is one
        set aside for this playthrough ("play one of these"): they never
        appear."""
        self._locked = held
        for character_id, (tick, _model) in self.rows.items():
            blocked = character_id == held or character_id in aside
            if blocked:
                tick.setChecked(False)
            tick.setEnabled(not blocked)
            tick.setToolTip(PLAYING_TIP if character_id == held else ASIDE_TIP if blocked else "")
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

    def order(self) -> str:
        return str(self.round_order.currentData())

    def missing(self) -> list[str]:
        """Ticked with no model named: Start waits for one."""
        return [
            character_id
            for character_id, (tick, model) in self.rows.items()
            if tick.isChecked() and not model.text().strip()
        ]

    def chosen(self) -> dict[str, tuple[str, ModelRoute | None] | None]:
        """What changed: a character id → the model that now plays them and
        its route, or None for one whose own model was taken away."""
        changes: dict[str, tuple[str, ModelRoute | None] | None] = {}
        for character_id, (tick, model) in self.rows.items():
            text = model.text().strip() if tick.isChecked() else ""
            now = (text, model.effective_route()) if text else ("", None)
            if now != self._was[character_id]:
                changes[character_id] = now if text else None
        return changes


def apply_chosen(
    cast: Sequence[Character], chosen: dict[str, tuple[str, ModelRoute | None] | None]
) -> list[Character]:
    """Each change onto its card, as the card's Played by would make it; one
    taken off goes back to "You or the storyteller". The cards changed."""
    changed = []
    for character in cast:
        if character.id not in chosen:
            continue
        choice = chosen[character.id]
        if choice:
            model, route = choice
            apply_played_by(character, OWN, model, route)
        else:
            apply_played_by(character, "either")
        changed.append(character)
    return changed
