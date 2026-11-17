"""Main and supporting characters, and who plays a main character, in the
words the window uses for them (the author, Nov 15 2026: players couldn't tell
the two kinds apart, and the list said "Cast" over one and "Supporting — from
the story" over the other with nothing to say what either meant).

In code the main characters are the cast (`bundle.cast`) and the others
`bundle.supporting`. Who voices a main character is two flags on the card
(`is_player_available`, `author_only`, read by engine/rules.py `voicing`);
here they are one choice, `PlayedBy`, so no card can hold the pair that means
nothing (the storyteller's alone, and never voiced by it). A supporting
character's flags reach no prompt (`voicing` is only asked about the cast), so
the choice is shown for main characters only.

A fourth choice belongs to the own-model trial (engine/reactions.py): a model
of their own plays them (`Character.own_model`), as another player. They are
off the Playing list (`is_player_available` off, which is what the list
reads), and the storyteller works their answers in with the author's turn.
Offered in a story only: the model is the story's choice, not the card's.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QLabel, QVBoxLayout, QWidget

from sealedlore.gui.model_picker import ModelField
from sealedlore.gui.plain_text import plain_tip
from sealedlore.models.character import Character

MAIN = "Main character"
SUPPORTING = "Supporting character"
# The list's headings (the cast board and Setup's Characters tab).
MAIN_HEADING = "MAIN CHARACTERS"
SUPPORTING_HEADING = "SUPPORTING CHARACTERS"
# What each kind is, said on their card and over the list's headings.
MAIN_ABOUT = (
    "The storyteller has their whole card on every turn, and the scene always says where "
    "they are. You can play a main character."
)
SUPPORTING_ABOUT = (
    "Their card goes to the storyteller only on turns that name them or find them in the "
    "scene, to keep them as they were. The storyteller always writes them: make them a "
    "main character to play them."
)
MAKE_MAIN = "Make them a main character"
MAKE_SUPPORTING = "Make them a supporting character"
MAKE_MAIN_TIP = (
    "Their whole card on every turn, and you can play them. The storyteller's cached "
    "prompt changes once, so the next turn costs one cache miss."
)
MAKE_SUPPORTING_TIP = (
    "Their card only on turns that name them, and the storyteller writes them from then on."
)
# On the People page, over a card's name.
MAIN_TAG = "MAIN"
SUPPORTING_TAG = "SUPPORTING"

PLAYED_BY = "Played by"
# (value, label, what it means). Mapped to the card's two flags by
# `played_by` and `apply_played_by`.
CHOICES: tuple[tuple[str, str, str], ...] = (
    (
        "either",
        "You or the storyteller",
        "You can play them. On turns you play someone else, the storyteller writes them.",
    ),
    (
        "author",
        "Only you",
        "The storyteller never writes their words, thoughts or choices, even on turns you "
        "play someone else. For a group of characters you switch between.",
    ),
    (
        "storyteller",
        "Only the storyteller",
        "You can't play them: the storyteller writes their words, thoughts and choices in "
        "full. Choose this when you hand over a character you have been playing; the "
        "storyteller is told it now writes them.",
    ),
)
OWN = "own"
OWN_CHOICE = (
    OWN,
    "Their own model (a trial)",
    "A model of their own plays them, as another player: after each passage it says what "
    "they mean to do, without seeing your turn, and the storyteller works that in with your "
    "turn. You can't play them while it does. A local or fine-tuned model works, named as "
    "Endpoint::model. This is a trial: how well it plays isn't known yet.",
)
OWN_MODEL_HINT = "The model that plays them, e.g. Local::maela-lora"
OWN_MODEL_MISSING = (
    "Name the model that plays them. Until then the storyteller writes them in full."
)


def played_by(character: Character) -> str:
    """The card's flags as one choice (engine/rules.py `voicing`'s order:
    `author_only` wins, then `is_player_available`), a model of their own
    before both."""
    if (character.own_model or "").strip():
        return OWN
    if character.author_only:
        return "author"
    if not character.is_player_available:
        return "storyteller"
    return "either"


def apply_played_by(character: Character, value: str, model: str = "") -> None:
    """`model` is the own model's reference, for `OWN`; any other choice
    takes the own model away."""
    character.is_player_available = value not in ("storyteller", OWN)
    character.author_only = value == "author"
    character.own_model = (model.strip() or None) if value == OWN else None


class PlayedBy(QWidget):
    """Who plays a main character: one choice, and a line saying what it
    means. `in_a_story` off for Your characters, where it is said of the
    stories they join and a model of their own isn't offered."""

    changed = Signal()

    def __init__(self, *, in_a_story: bool = True) -> None:
        super().__init__()
        self._in_a_story = in_a_story
        self._browse: Callable[[], Callable[..., str | None] | None] | None = None
        self.combo = QComboBox()
        choices = (*CHOICES, OWN_CHOICE) if in_a_story else CHOICES
        for value, label, about in choices:
            self.combo.addItem(label, value)
            self.combo.setItemData(self.combo.count() - 1, plain_tip(about), Qt.ToolTipRole)
        self.combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.about = QLabel()
        self.about.setObjectName("hintLabel")
        self.about.setWordWrap(True)
        self.model = ModelField("", self._browse_models)
        self.model.setPlaceholderText(OWN_MODEL_HINT)
        self.model.textChanged.connect(self._model_typed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.combo, 0, Qt.AlignLeft)
        layout.addWidget(self.model)
        layout.addWidget(self.about)
        self.combo.currentIndexChanged.connect(self._chosen)
        self._say()

    def set_browse(self, browse: Callable[..., str | None]) -> None:
        """The window's model list, for the own model's Choose. Held weakly:
        the window holds this widget."""
        self._browse = (
            weakref.WeakMethod(browse) if hasattr(browse, "__self__") else (lambda: browse)
        )

    def _browse_models(self, current: str) -> str | None:
        browse = self._browse() if self._browse is not None else None
        return browse(current) if browse is not None else None

    def value(self) -> str:
        return str(self.combo.currentData())

    def set_value(self, value: str, model: str = "") -> None:
        """Shown without saying so (`changed` is for the author's choice)."""
        index = self.combo.findData(value)
        self.combo.blockSignals(True)
        self.model.blockSignals(True)
        self.combo.setCurrentIndex(max(index, 0))
        self.model.setText(model)
        self.model.blockSignals(False)
        self.combo.blockSignals(False)
        self._say()

    def show_character(self, character: Character) -> None:
        self.set_value(played_by(character), character.own_model or "")

    def apply_to(self, character: Character) -> None:
        apply_played_by(character, self.value(), self.model.text())

    def _chosen(self) -> None:
        self._say()
        self.changed.emit()

    def _model_typed(self, _text: str) -> None:
        self._say()
        if self.value() == OWN:
            self.changed.emit()

    def _say(self) -> None:
        own = self.value() == OWN
        self.model.setVisible(own)
        choices = (*CHOICES, OWN_CHOICE)
        about = dict((value, words) for value, _label, words in choices)[self.value()]
        if own and not self.model.text().strip():
            about = f"{OWN_MODEL_MISSING} {about}"
        if not self._in_a_story:
            about = f"In a story they join, {about[0].lower()}{about[1:]}"
        self.about.setText(about)
