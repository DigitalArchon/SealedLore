"""Expand character profile… and Generate character profile… in Your
characters (engine/card_fill.py): the author says how, the authoring model
writes, and the author takes what they like of it, field by field. Nothing
on the card changes until they do.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.card_fill import (
    EXPAND,
    GENERATE,
    CardFill,
    FieldChange,
    FillMode,
    proposed_changes,
)
from sealedlore.gui.modal_job import run_with_progress
from sealedlore.gui.sizing import GrowingText, enter_only_on_buttons, may_discard, size_to_screen
from sealedlore.gui.worker import as_job
from sealedlore.models.character import Character

EXPAND_LABEL = "Expand character profile…"
GENERATE_LABEL = "Generate character profile…"
TITLES = {EXPAND: "Expand character profile", GENERATE: "Generate character profile"}
ABOUT = {
    EXPAND: (
        "The model fills in what {name}'s card is missing, building on what it already says, "
        "and keeps what is written unless you ask for a change below."
    ),
    GENERATE: (
        "The model writes {name}'s whole profile anew, taking what the card says as a "
        "starting point where it fits what you ask for below."
    ),
}
# Ready-made wishes, put into the box to change as the author likes.
WISHES = (
    "Keep to what is there, and expand on it.",
    "Make them tougher and more dangerous.",
    "Make them shy and quiet.",
    "A criminal version of themselves.",
    "Ten years older, and changed by it.",
    "Funnier, with a sharp tongue.",
)
PLACEHOLDER = (
    "How should it be written? e.g. 'expand on what's there', 'make them tougher', "
    "'more shy', 'a criminal version of themselves'. Blank: as the card suggests."
)


class CardFillDialog(QDialog):
    """How to write the profile: the author's wishes, and a world the
    character comes from."""

    def __init__(
        self, mode: FillMode, character: Character, model: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.setWindowTitle(TITLES[mode])
        name = character.name or "this character"
        about = QLabel(ABOUT[mode].format(name=name))
        about.setWordWrap(True)
        self.wishes = GrowingText(lines=3, most=10)
        self.wishes.setPlaceholderText(PLACEHOLDER)
        ideas = QToolButton()
        ideas.setText("Ideas")
        ideas.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(ideas)
        for words in WISHES:
            menu.addAction(words).triggered.connect(
                lambda _on=False, w=words: self.wishes.setPlainText(w)
            )
        ideas.setMenu(menu)
        ideas.setToolTip("A few ways to start: each goes in the box, for you to change")
        self.from_world = QCheckBox("They come from an existing world: fill them in from its lore")
        self.world = QLineEdit(character.canon or "")
        self.world.setPlaceholderText(
            "e.g. Shakespeare's Hamlet, the Arthurian legends, Norse myth"
        )
        self.from_world.setChecked(bool(character.canon))
        self.world.setEnabled(self.from_world.isChecked())
        self.from_world.toggled.connect(self.world.setEnabled)
        self.from_world.setToolTip(
            "As Fill with lore characters… does for a story: the model writes them as that "
            "work establishes them (their name on the card says who)"
        )
        written_by = QLabel(f"Written by {model} (Settings → Models → Authoring model).")
        written_by.setObjectName("hintLabel")
        written_by.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Write")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        wishes_row = QHBoxLayout()
        wishes_row.addWidget(self.wishes, 1)
        wishes_row.addWidget(ideas, 0, Qt.AlignTop)
        layout = QVBoxLayout(self)
        layout.addWidget(about)
        layout.addWidget(QLabel("How should it be written?"))
        layout.addLayout(wishes_row)
        layout.addWidget(self.from_world)
        layout.addWidget(self.world)
        layout.addWidget(written_by)
        layout.addStretch(1)
        layout.addWidget(buttons)
        size_to_screen(self, 0.4, 0.4)
        self._opened = self._state()

    def _state(self) -> tuple:
        return self.wishes.toPlainText(), self.from_world.isChecked(), self.world.text()

    def _accept(self) -> None:
        if self.from_world.isChecked() and not self.world.text().strip():
            QMessageBox.information(self, self.windowTitle(), "Name the world they come from.")
            return
        self.accept()

    def reject(self) -> None:
        if not may_discard(self, list(self._opened), list(self._state())):
            return
        super().reject()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)

    def chosen(self) -> tuple[str, str]:
        """(the wishes, the world or "")."""
        world = self.world.text().strip() if self.from_world.isChecked() else ""
        return self.wishes.toPlainText().strip(), world


class CardChangesDialog(QDialog):
    """What the model wrote, field by field, each to take or leave, its text
    yours to change first."""

    def __init__(
        self,
        mode: FillMode,
        character: Character,
        changes: list[FieldChange],
        notes: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLES[mode])
        self._rows: list[tuple[QCheckBox, GrowingText, FieldChange]] = []
        intro = QLabel(
            f"What the model would write on {character.name}'s card. Untick what you don't "
            "want, and change any of it first: nothing is on the card until you press Put on "
            "the card."
        )
        intro.setWordWrap(True)
        form_host = QWidget()
        form = QFormLayout(form_host)
        for change in changes:
            tick = QCheckBox(change.label)
            tick.setChecked(True)
            text = GrowingText(lines=1, most=14)
            text.setPlainText(change.new)
            tick.toggled.connect(text.setEnabled)
            if change.old.strip():
                was = QLabel(f"Was: {_short(change.old)}")
                was.setObjectName("hintLabel")
                was.setWordWrap(True)
                was.setToolTip(change.old)
                box = QWidget()
                column = QVBoxLayout(box)
                column.setContentsMargins(0, 0, 0, 0)
                column.addWidget(text)
                column.addWidget(was)
                form.addRow(tick, box)
            else:
                form.addRow(tick, text)
            self._rows.append((tick, text, change))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_host)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        if notes.strip():
            said = QLabel(f"The model says: {notes.strip()}")
            said.setObjectName("hintLabel")
            said.setWordWrap(True)
            layout.addWidget(said)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Put on the card")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        size_to_screen(self, 0.55, 0.8)

    def reject(self) -> None:
        """What the model wrote was paid for: leaving it is asked about."""
        if not may_discard(self, ["kept"], ["left"]):
            return
        super().reject()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)

    def taken(self) -> list[FieldChange]:
        """The ticked changes, as their text now stands."""
        return [
            FieldChange(change.key, change.label, change.old, text.toPlainText().strip())
            for tick, text, change in self._rows
            if tick.isChecked() and text.toPlainText().strip()
        ]


def _short(text: str, most: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[: most - 1] + "…"


def write_profile(
    parent: QWidget,
    session,
    character: Character,
    mode: FillMode,
) -> list[FieldChange] | None:
    """Asks how, writes, and asks what to take: the changes taken, or None.
    `session`: the one whose authoring model writes (Your characters')."""
    model = session.authoring_model
    ask = CardFillDialog(mode, character, model, parent)
    try:
        if ask.exec() != QDialog.Accepted:
            return None
        wishes, world = ask.chosen()
    finally:
        ask.deleteLater()
    result: dict[str, object] = {}

    @as_job
    def job():
        result["fill"] = session.fill_card(character, mode=mode, wishes=wishes, world=world)

    _run(parent, session, f"{model} is writing {character.name or 'the'}'s profile…", job, result)
    if "error" in result:
        QMessageBox.warning(
            parent, TITLES[mode], f"The profile couldn't be written: {result['error']}"
        )
        return None
    fill = result.get("fill")
    if not isinstance(fill, CardFill):
        return None  # stopped
    changes = proposed_changes(character, fill, mode)
    if not changes:
        QMessageBox.information(
            parent,
            TITLES[mode],
            "The model changed nothing on the card."
            + (f"\n\nIt says: {fill.notes}" if fill.notes else ""),
        )
        return None
    review = CardChangesDialog(mode, character, changes, fill.notes, parent)
    try:
        if review.exec() != QDialog.Accepted:
            return None
        return review.taken()
    finally:
        review.deleteLater()


def _run(parent: QWidget, session, words: str, job: Callable, result: dict) -> None:
    """The call on a worker thread, a progress dialog with Stop meanwhile."""
    run_with_progress(parent, session, words, job, result, title="Writing a profile")
