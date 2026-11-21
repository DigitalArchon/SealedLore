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
from PySide6.QtWidgets import QComboBox, QLabel, QPushButton, QVBoxLayout, QWidget

from sealedlore.gui.model_picker import ModelField
from sealedlore.gui.plain_text import plain_tip
from sealedlore.models.character import Character
from sealedlore.models.config import RECOMMENDED_TEXT_MODELS
from sealedlore.models.route import ModelRoute

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
# Beside MAIN for a character a model of their own plays (the trial).
OWN_TAG = "OWN MODEL"
OWN_TAG_TIP = (
    "{model} plays them, as another player: it says what they mean to do after each "
    "passage. Played by on their card changes it."
)

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
    "A model of their own plays them, as another player: it says what they mean to do, and "
    "the storyteller works that in with your turn. Whether they answer after your turn (it "
    "plays better) or at the same time as you (faster) is the story's choice (Story → "
    "Setup… → About). You can't play "
    "them while it does. A local or fine-tuned model works, named as Endpoint::model. This "
    "is a trial: how well it plays isn't known yet.",
)
OWN_MODEL_HINT = "The model that plays them, e.g. Local::maela-lora"
OWN_PROMPT_LABEL = "What their model is sent…"
# When they answer (`StoryDefaults.round_order`), in Setup → About and Start.
ROUND_ORDER_LABEL = "They answer"
# "after" first: the default, and the better of the two (Nov 22 2026, the
# round_order kit in SealedLoreDocs design/own-models.md).
ROUND_ORDER_LABELS = {
    "after": "After your turn (plays better)",
    "together": "At the same time as you (faster)",
}
ROUND_ORDER_TIPS = {
    "after": (
        "You send first; each then answers having seen your turn (not each other's), as "
        "another player at the table would, and the storyteller writes. It plays better: "
        "they answer what you actually did. Each turn waits a few seconds for their answers "
        "before the storyteller starts."
    ),
    "together": (
        "Each answers the storyteller's passage while you write your turn, without seeing "
        "it; the storyteller writes everything together when you send. It is faster, since "
        "nobody waits, but their answers are a step behind your turn, and the storyteller "
        "sometimes works in something your turn has already answered or moved past."
    ),
}
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


def apply_played_by(
    character: Character, value: str, model: str = "", route: ModelRoute | None = None
) -> None:
    """`model` is the own model's reference, for `OWN`, and `route` the
    NanoGPT host chosen for it; any other choice takes both away."""
    character.is_player_available = value not in ("storyteller", OWN)
    character.author_only = value == "author"
    own = value == OWN and bool(model.strip())
    character.own_model = model.strip() if own else None
    character.own_route = route if own and route is not None and route.paid else None


def round_order_combo(value: str) -> QComboBox:
    """The choice of when they answer, set to `value`."""
    combo = QComboBox()
    for order, label in ROUND_ORDER_LABELS.items():
        combo.addItem(label, order)
        combo.setItemData(combo.count() - 1, plain_tip(ROUND_ORDER_TIPS[order]), Qt.ToolTipRole)
    combo.setCurrentIndex(max(combo.findData(value), 0))
    combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
    return combo


def _held(helper: Callable) -> Callable[[], Callable | None]:
    """A window's method, held weakly; anything else as it is."""
    if hasattr(helper, "__self__"):
        return weakref.WeakMethod(helper)
    return lambda: helper


class ModelHelpers:
    """The window's model list and route helpers, held weakly, for a field
    naming a character's own model (the card's Played by, the Start
    dialog's rows): Choose, and the NanoGPT host beside it as every model
    field with a route has (gui/model_picker.py `ModelField`). Until the
    window lends them, Choose does nothing and no route is offered."""

    def __init__(self) -> None:
        self._held: dict[str, Callable[[], Callable | None]] = {}

    def lend(
        self,
        browse: Callable[..., str | None] | None,
        *,
        browse_route: Callable | None = None,
        route_endpoint: Callable | None = None,
        hosts_of: Callable | None = None,
        qualify: Callable | None = None,
    ) -> None:
        self._held = {
            name: _held(helper)
            for name, helper in (
                ("browse", browse),
                ("browse_route", browse_route),
                ("route_endpoint", route_endpoint),
                ("hosts_of", hosts_of),
                ("qualify", qualify),
            )
            if helper is not None
        }

    def _get(self, name: str) -> Callable | None:
        held = self._held.get(name)
        return held() if held is not None else None

    def field(self) -> ModelField:
        """A model field using them (the field holds these methods weakly:
        keep this object as long as the field)."""
        field = ModelField(
            "",
            self.browse_models,
            route_endpoint=self.route_endpoint,
            browse_route=self.browse_route,
            hosts_of=self.hosts_of,
            # The models measured for playing a character, and what
            # SealedLore adjusts for a model in that use.
            recommended=RECOMMENDED_TEXT_MODELS.get("character", ()),
            qualify_recommended=self.qualify,
            role="character",
        )
        field.setPlaceholderText(OWN_MODEL_HINT)
        return field

    def browse_models(self, current: str) -> str | None:
        browse = self._get("browse")
        return browse(current) if browse is not None else None

    def browse_route(self, current: str, route: ModelRoute | None):
        """Choose: the model and its route where the window offers both,
        else the model alone, its route kept."""
        browse_route = self._get("browse_route")
        if browse_route is not None:
            return browse_route(current, route)
        picked = self.browse_models(current)
        return (picked, route) if picked else None

    def route_endpoint(self, model: str) -> str | None:
        getter = self._get("route_endpoint")
        return getter(model) if getter is not None else None

    def hosts_of(self, model: str) -> tuple[str, ...] | None:
        getter = self._get("hosts_of")
        return getter(model) if getter is not None else None

    def qualify(self, model: str) -> str:
        """A recommended id as the field holds it (the window's)."""
        qualify = self._get("qualify")
        return qualify(model) if qualify is not None else model


class PlayedBy(QWidget):
    """Who plays a main character: one choice, and a line saying what it
    means. `in_a_story` off for Your characters, where it is said of the
    stories they join and a model of their own isn't offered."""

    changed = Signal()
    # What their model is sent… (gui/character_prompt_dialog.py).
    prompt_requested = Signal()

    def __init__(self, *, in_a_story: bool = True) -> None:
        super().__init__()
        self._in_a_story = in_a_story
        self._showing = False
        self.helpers = ModelHelpers()
        self.combo = QComboBox()
        choices = (*CHOICES, OWN_CHOICE) if in_a_story else CHOICES
        for value, label, about in choices:
            self.combo.addItem(label, value)
            self.combo.setItemData(self.combo.count() - 1, plain_tip(about), Qt.ToolTipRole)
        self.combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.about = QLabel()
        self.about.setObjectName("hintLabel")
        self.about.setWordWrap(True)
        # NanoGPT's host for it too, with the window's helpers (`set_browse`).
        self.model = self.helpers.field()
        self.model.textChanged.connect(self._model_typed)
        self.model.routeChanged.connect(self._route_chosen)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.combo, 0, Qt.AlignLeft)
        layout.addWidget(self.model)
        self.prompt_button = QPushButton(OWN_PROMPT_LABEL)
        self.prompt_button.setToolTip(
            "Their model's next request, your notes for it, and what they remember"
        )
        self.prompt_button.clicked.connect(self.prompt_requested)
        layout.addWidget(self.prompt_button, 0, Qt.AlignLeft)
        layout.addWidget(self.about)
        self.combo.currentIndexChanged.connect(self._chosen)
        self._say()

    def set_browse(self, browse: Callable[..., str | None], **routes: Callable) -> None:
        """The window's model list, for the own model's Choose, and its
        route helpers (`ModelHelpers.lend`). Held weakly: the window holds
        this widget."""
        self.helpers.lend(browse, **routes)
        self._showing = True
        self.model.set_route(self.model.route)  # its button, now it can be shown
        self._showing = False

    def _route_chosen(self) -> None:
        if not self._showing and self.value() == OWN:
            self.changed.emit()

    def value(self) -> str:
        return str(self.combo.currentData())

    def set_value(self, value: str, model: str = "", route: ModelRoute | None = None) -> None:
        """Shown without saying so (`changed` is for the author's choice)."""
        index = self.combo.findData(value)
        self._showing = True
        self.combo.blockSignals(True)
        self.model.blockSignals(True)
        try:
            self.combo.setCurrentIndex(max(index, 0))
            self.model.setText(model)
        finally:
            self.model.blockSignals(False)
            self.combo.blockSignals(False)
        # After the text: the route button shows for the model in the field.
        self.model.set_route(route)
        self._showing = False
        self._say()

    def show_character(self, character: Character) -> None:
        self.set_value(played_by(character), character.own_model or "", character.own_route)

    def apply_to(self, character: Character) -> None:
        apply_played_by(character, self.value(), self.model.text(), self.model.effective_route())

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
        self.prompt_button.setVisible(own and bool(self.model.text().strip()))
        choices = (*CHOICES, OWN_CHOICE)
        about = dict((value, words) for value, _label, words in choices)[self.value()]
        if own and not self.model.text().strip():
            about = f"{OWN_MODEL_MISSING} {about}"
        if not self._in_a_story:
            about = f"In a story they join, {about[0].lower()}{about[1:]}"
        self.about.setText(about)
