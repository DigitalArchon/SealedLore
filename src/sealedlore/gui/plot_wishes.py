"""What the author wants of a plot the model writes (engine/plot_draft.py).

One form, in the new story guide's last step, in New story from a premise,
and in Story → Write a plot with the model…: no plot (where that is a
choice), one made up from the story alone, or one from what the author
describes. Described, the author's own words and a few choices: how many
events, how often, how big, how disruptive, and of what kinds; each "the
model decides" until chosen.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.plot_draft import PlotWishes
from sealedlore.gui.sizing import GrowingText

NONE, MADE_UP, DESCRIBED = "none", "made_up", "described"
CHOICE_LABELS = {
    NONE: "No plot: the story goes wherever you take it",
    MADE_UP: "A plot the model makes up from the story's world and characters",
    DESCRIBED: "A plot from what I describe",
}
UP_TO_THE_MODEL = "The model decides"
# (value, label) for each choice, in the order offered.
EVENTS = (("few", "A few (3 or 4)"), ("several", "Several (5 to 8)"), ("many", "Many (9 to 14)"))
SPACINGS = (
    ("close", "Close together (every day or two)"),
    ("steady", "Now and then (every few days)"),
    ("spread", "Far apart (a week or more)"),
)
SCALES = (
    ("personal", "Small and personal"),
    ("mixed", "Some of each"),
    ("world", "Big: the world can change"),
)
DISRUPTIONS = (
    ("gentle", "Gentle: they add to the story"),
    ("steady", "Steady: complications and turns"),
    ("upending", "Upending: they turn the story over"),
)
KIND_LABELS = (
    ("danger", "Danger and fights"),
    ("mystery", "Mysteries and secrets"),
    ("arrivals", "New people arriving"),
    ("relationships", "Bonds and rivalries"),
    ("intrigue", "Power and betrayal"),
    ("discovery", "Exploration and discovery"),
    ("disaster", "Disasters"),
    ("choices", "Hard choices"),
)
PREMISE_EXAMPLE = (
    "e.g. A rival crew is after the same wreck, and one of the regulars is secretly "
    "working for them. Something big should happen by the end of the first week."
)
ABOUT = (
    "A plot gives the story things that happen when the time and the moment are "
    "right: the storyteller learns of each event only as it begins, so you meet it as it "
    "happens. You can read it once it is written, or keep it a surprise."
)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


def _choice(options: tuple[tuple[str, str], ...]) -> QComboBox:
    combo = QComboBox()
    combo.addItem(UP_TO_THE_MODEL, "")
    for value, label in options:
        combo.addItem(label, value)
    return combo


class PlotWishesForm(QWidget):
    """The form. `offer_none`: "No plot" is one of the choices (and chosen
    to begin with), as in the guide and a premise draft."""

    changed = Signal()

    def __init__(self, *, offer_none: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.group = QButtonGroup(self)
        self.choices: dict[str, QRadioButton] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for key in (NONE, MADE_UP, DESCRIBED) if offer_none else (MADE_UP, DESCRIBED):
            button = QRadioButton(CHOICE_LABELS[key])
            self.group.addButton(button)
            self.choices[key] = button
            layout.addWidget(button)
        self.choices[NONE if offer_none else MADE_UP].setChecked(True)

        self.details = QWidget()
        form = QFormLayout(self.details)
        form.setContentsMargins(24, 4, 0, 0)
        self.premise = GrowingText(lines=3, most=10)
        self.premise.setPlaceholderText(PREMISE_EXAMPLE)
        self.premise.setTabChangesFocus(True)
        form.addRow("Your plot", self.premise)
        form.addRow(
            "",
            _hint(
                "In your own words: what should happen, to whom, the twists you want. "
                "Leave it blank to choose only what is below."
            ),
        )
        self.events = _choice(EVENTS)
        self.spacing = _choice(SPACINGS)
        self.scale = _choice(SCALES)
        self.disruption = _choice(DISRUPTIONS)
        form.addRow("How many events", self.events)
        form.addRow("How often", self.spacing)
        form.addRow("How big", self.scale)
        form.addRow("How disruptive", self.disruption)
        form.addRow("", _hint("How often is in the story's own time, its days and weeks."))
        kinds = QWidget()
        grid = QGridLayout(kinds)
        grid.setContentsMargins(0, 0, 0, 0)
        self.kinds: dict[str, QCheckBox] = {}
        for index, (key, label) in enumerate(KIND_LABELS):
            box = QCheckBox(label)
            self.kinds[key] = box
            grid.addWidget(box, index // 2, index % 2)
            box.toggled.connect(self.changed)
        form.addRow("Kinds of event", kinds)
        self.newcomers = QCheckBox("It may bring in new people and places")
        self.newcomers.setChecked(True)
        self.newcomers.setToolTip(
            "Kept hidden from the story, and from your pages, until an event brings them in"
        )
        form.addRow("", self.newcomers)
        layout.addWidget(self.details)

        self.group.buttonToggled.connect(lambda *_args: self._sync())
        self.premise.textChanged.connect(self.changed)
        for combo in (self.events, self.spacing, self.scale, self.disruption):
            combo.currentIndexChanged.connect(self.changed)
        self.newcomers.toggled.connect(self.changed)
        self._sync()

    def choice(self) -> str:
        return next((key for key, button in self.choices.items() if button.isChecked()), MADE_UP)

    def set_choice(self, key: str) -> None:
        if key in self.choices:
            self.choices[key].setChecked(True)

    def _sync(self) -> None:
        self.details.setVisible(self.choice() == DESCRIBED)
        self.changed.emit()

    def wishes(self) -> PlotWishes | None:
        """What was asked for; None for no plot."""
        choice = self.choice()
        if choice == NONE:
            return None
        if choice == MADE_UP:
            return PlotWishes()
        return PlotWishes(
            premise=self.premise.toPlainText().strip(),
            events=self.events.currentData() or "",
            spacing=self.spacing.currentData() or "",
            scale=self.scale.currentData() or "",
            disruption=self.disruption.currentData() or "",
            kinds=tuple(key for key, box in self.kinds.items() if box.isChecked()),
            newcomers=self.newcomers.isChecked(),
        )

    def set_read_only(self, read_only: bool) -> None:
        """While a draft is written: looked at, not changed."""
        for button in self.choices.values():
            button.setEnabled(not read_only)
        self.premise.setReadOnly(read_only)
        for widget in (
            self.events,
            self.spacing,
            self.scale,
            self.disruption,
            self.newcomers,
            *self.kinds.values(),
        ):
            widget.setEnabled(not read_only)

    def typed(self) -> str:
        """What the author wrote, to ask about before it is lost."""
        return self.premise.toPlainText()
