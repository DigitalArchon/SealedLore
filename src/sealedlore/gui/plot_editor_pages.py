"""The plot editor's pages: one form for each kind of thing a plot file holds.

Each page edits one item of a `PlotDocument` in place: `load(item, document)`
fills the widgets, any change writes every field back and emits `changed`.
The document is passed so the pickers can offer what the file already has (a
condition's fact and its values, a place, an event, a character), which is
what keeps the syntax-prone lines out of the author's hands: conditions,
`sets:`, `brings:`, `reveals:` and `at:` are chosen, never typed. A value the
file doesn't know (read from a hand-written file) is still offered, so the
importer's problem about it can be seen and fixed rather than lost.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.response_style import PRESETS
from sealedlore.gui.composer import (
    AGENCY_LABELS,
    AGENCY_TIPS,
    WORLD_ACTIVITY_LABELS,
    WORLD_ACTIVITY_TIPS,
)
from sealedlore.gui.plot_pictures import Folder, PlotPictureStrip, SaveFirst
from sealedlore.gui.style_panel import PERSPECTIVE_SUMMARIES
from sealedlore.models.plot_file import (
    AssignmentDoc,
    CharacterDoc,
    ConditionDoc,
    EntryDoc,
    EventDoc,
    FactDoc,
    PlotDocument,
    VariantDoc,
)

ROLE_LABELS = {
    "player": "Player — you can play them; the storyteller writes them when you don't",
    "storyteller": "Storyteller — always written by the storyteller",
    "author": "Author — only ever written by you",
    "supporting": "Supporting — a card for continuity, shown when they come up",
}
PACING_LABELS = {
    "quiet": "At a quiet moment — a lull, or settling down for the night",
    "any": "Any time the conditions hold",
}
HELD = "the character you are playing"
NOT_SET = "(not set — the app's default)"
NO_PLACE = "(no particular place)"

# A combo's data is a string: PySide6 doesn't give a tuple back from
# `currentData()`. The key is "<kind>" or "<kind>!" when negated.
_CONDITION_KINDS: list[tuple[str, str]] = [
    ("Fact is", "fact"),
    ("Fact is not", "fact!"),
    ("Is at place", "at"),
    ("Is not at place", "at!"),
    ("Has visited", "visited"),
    ("Has not visited", "visited!"),
    ("Event has happened", "happened"),
    ("Event has not happened", "happened!"),
    ("As written", "text"),
]


def condition_key(kind: str, negated: bool) -> str:
    return f"{kind}!" if negated else kind


def _kind_of(key: str | None) -> tuple[str, bool]:
    key = key or "fact"
    return key.rstrip("!"), key.endswith("!")


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


def _split(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _short_edit(placeholder: str = "", height: int = 60) -> QPlainTextEdit:
    edit = QPlainTextEdit()
    edit.setPlaceholderText(placeholder)
    edit.setMaximumHeight(height)
    return edit


def _fill(combo: QComboBox, choices: Iterable[tuple[str, str]], current: str) -> None:
    """Offer `choices` (data, label) and select `current` by data, adding it
    if the file doesn't know it so nothing read from a file is lost."""
    combo.blockSignals(True)
    combo.clear()
    known = False
    for data, label in choices:
        combo.addItem(label, data)
        known = known or data == current
    if current and not known:
        combo.addItem(f"{current} (not in this file)", current)
    combo.setCurrentIndex(max(combo.findData(current), 0))
    combo.blockSignals(False)


class Page(QWidget):
    """A form over one item. Subclasses fill `_form`, and implement `_fill`
    (item → widgets) and `_store` (widgets → item)."""

    changed = Signal()
    # (old name, new name): the window lets the document's references follow.
    renamed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._loading = False
        self.item = None
        self.document = PlotDocument()
        inner = QWidget()
        self._form = QFormLayout(inner)
        self._form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    def load(self, item, document: PlotDocument) -> None:
        self._loading = True
        self.item, self.document = item, document
        try:
            self._fill()
        finally:
            self._loading = False

    def _fill(self) -> None:
        raise NotImplementedError

    def _store(self) -> None:
        raise NotImplementedError

    def _on_change(self, *_args) -> None:
        if self._loading or self.item is None:
            return
        self._store()
        self.changed.emit()

    def _watch(self, *widgets: QWidget) -> None:
        for widget in widgets:
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._on_change)
            elif isinstance(widget, QPlainTextEdit):
                widget.textChanged.connect(self._on_change)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._on_change)
                if widget.isEditable():
                    widget.editTextChanged.connect(self._on_change)
            elif isinstance(widget, (QCheckBox, QRadioButton)):
                widget.toggled.connect(self._on_change)
            elif isinstance(widget, QSpinBox):
                widget.valueChanged.connect(self._on_change)
            elif isinstance(widget, QTimeEdit):
                widget.timeChanged.connect(self._on_change)
            elif isinstance(widget, (ConditionsEditor, SetsEditor, NamePicker, PlotPictureStrip)):
                widget.changed.connect(self._on_change)

    def _name_changed(self, old: str, new: str) -> None:
        if old != new and old.strip() and not self._loading:
            self.renamed.emit(old, new)


# --- the story, the world, the opening ------------------------------------------


class StoryPage(Page):
    def __init__(self) -> None:
        super().__init__()
        self.title = QLineEdit()
        self.description = QLineEdit()
        self.description.setPlaceholderText("One line for the story list.")
        self.start_day = QSpinBox()
        self.start_day.setRange(1, 99999)
        self.start_time = QTimeEdit()
        self.start_time.setDisplayFormat("HH:mm")
        start = QHBoxLayout()
        start.addWidget(QLabel("Day"))
        start.addWidget(self.start_day)
        start.addWidget(QLabel("at"))
        start.addWidget(self.start_time)
        start.addStretch(1)
        self.agency = QComboBox()
        self.agency.addItem(NOT_SET, None)
        for mode, label in AGENCY_LABELS.items():
            self.agency.addItem(label, mode)
            self.agency.setItemData(self.agency.count() - 1, AGENCY_TIPS[mode], Qt.ToolTipRole)
        self.world_activity = QComboBox()
        self.world_activity.addItem(NOT_SET, None)
        for level, label in WORLD_ACTIVITY_LABELS.items():
            self.world_activity.addItem(label, level)
            self.world_activity.setItemData(
                self.world_activity.count() - 1, WORLD_ACTIVITY_TIPS[level], Qt.ToolTipRole
            )
        self.perspective = QComboBox()
        self.perspective.addItem(NOT_SET, None)
        for perspective, summary in PERSPECTIVE_SUMMARIES.items():
            label = "Whole story" if perspective == "whole_story" else "With your character"
            self.perspective.addItem(f"{label} — {summary}", perspective)
        self.length = QComboBox()
        self.length.addItem(NOT_SET, None)
        for style, preset in PRESETS.items():
            if style != "custom":
                self.length.addItem(preset.label, style)
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText(
            "Notes to yourself. Kept in the file; the app never reads them."
        )
        self.notes.setMaximumHeight(120)

        form = self._form
        form.addRow("Title", self.title)
        form.addRow("Description", self.description)
        form.addRow("Clock starts", start)
        form.addRow(hint("The story's clock at the first passage. Events' days count from day 1."))
        form.addRow("Outcomes", self.agency)
        form.addRow("World", self.world_activity)
        form.addRow("Perspective", self.perspective)
        form.addRow("Length", self.length)
        form.addRow(
            hint("Each is a setting the story starts with; the author can change it in the app.")
        )
        form.addRow("Notes", self.notes)
        self._watch(
            self.title,
            self.description,
            self.start_day,
            self.start_time,
            self.agency,
            self.world_activity,
            self.perspective,
            self.length,
            self.notes,
        )

    def _fill(self) -> None:
        document: PlotDocument = self.item
        self.title.setText(document.title)
        self.description.setText(document.description)
        self.start_day.setValue(document.start_day)
        time = QTime.fromString(document.start_time, "HH:mm")
        self.start_time.setTime(time if time.isValid() else QTime(0, 0))
        for combo, value in (
            (self.agency, document.agency),
            (self.world_activity, document.world_activity),
            (self.perspective, document.perspective),
            (self.length, document.length),
        ):
            combo.setCurrentIndex(max(combo.findData(value), 0) if value else 0)
        self.notes.setPlainText(document.notes)

    def _store(self) -> None:
        document: PlotDocument = self.item
        old = document.title
        document.title = self.title.text()
        document.description = self.description.text()
        document.start_day = self.start_day.value()
        document.start_time = self.start_time.time().toString("HH:mm")
        document.agency = self.agency.currentData()
        document.world_activity = self.world_activity.currentData()
        document.perspective = self.perspective.currentData()
        document.length = self.length.currentData()
        document.notes = self.notes.toPlainText()
        self._name_changed(old, document.title)


class WorldPage(Page):
    def __init__(self) -> None:
        super().__init__()
        self.world = QPlainTextEdit()
        self.world.setPlaceholderText(
            "The world bible: the standing rules of the setting, sent with every turn."
        )
        self._form.addRow(
            hint(
                "Taken as written, sub-headings included. Standing rules belong here; "
                "detail that matters only sometimes belongs in Lore."
            )
        )
        self._form.addRow(self.world)
        self._watch(self.world)

    def _fill(self) -> None:
        self.world.setPlainText(self.item.world)

    def _store(self) -> None:
        self.item.world = self.world.toPlainText()


class OpeningPage(Page):
    def __init__(self) -> None:
        super().__init__()
        self.generate = QRadioButton("The model writes the opening from these notes")
        self.as_written = QRadioButton("Use exactly as written — this is the opening passage")
        self.location = QLineEdit()
        self.time = QLineEdit()
        self.situation = QLineEdit()
        self.notes = QPlainTextEdit()
        form = self._form
        form.addRow(self.generate)
        form.addRow(self.as_written)
        form.addRow("Location", self.location)
        form.addRow("Time", self.time)
        form.addRow("Situation", self.situation)
        form.addRow(hint("Where the starting scene is, for the scene card."))
        form.addRow("Opening", self.notes)
        self._watch(
            self.generate, self.as_written, self.location, self.time, self.situation, self.notes
        )

    def _fill(self) -> None:
        opening = self.item.opening
        (self.as_written if opening.mode == "as_written" else self.generate).setChecked(True)
        self.location.setText(opening.location)
        self.time.setText(opening.time)
        self.situation.setText(opening.situation)
        self.notes.setPlainText(opening.notes)

    def _store(self) -> None:
        opening = self.item.opening
        opening.mode = "as_written" if self.as_written.isChecked() else "generate"
        opening.location = self.location.text()
        opening.time = self.time.text()
        opening.situation = self.situation.text()
        opening.notes = self.notes.toPlainText()


# --- characters, places, lore, facts ----------------------------------------------


class CharacterPage(Page):
    def __init__(self, folder: Folder | None = None, save_first: SaveFirst | None = None) -> None:
        super().__init__()
        self.pictures = PlotPictureStrip(folder, save_first)
        self.name = QLineEdit()
        self.role = QComboBox()
        for role, label in ROLE_LABELS.items():
            self.role.addItem(label, role)
        self.choose = QCheckBox("One of the characters the author chooses between at the start")
        self.present = QCheckBox("In the opening scene")
        self.hidden = QCheckBox("Hidden until an event brings them in")
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText("comma-separated")
        self.voice = QLineEdit()
        self.canon = QLineEdit()
        self.canon.setPlaceholderText("The work they come from, if the model already knows them")
        self.summary = QLineEdit()
        self.summary.setPlaceholderText("One line; otherwise the first sentence of the description")
        self.description = QPlainTextEdit()
        form = self._form
        form.addRow("Name", self.name)
        form.addRow("Role", self.role)
        form.addRow(self.choose)
        form.addRow(self.present)
        form.addRow(self.hidden)
        form.addRow(
            hint(
                "A hidden character reaches no model and no tab until an event's "
                "“brings” names them. Keep the description to who they are; when and "
                "how they turn up belongs to the event."
            )
        )
        form.addRow("Aliases", self.aliases)
        form.addRow("Voice", self.voice)
        form.addRow("Canon", self.canon)
        form.addRow("Summary", self.summary)
        form.addRow("Description", self.description)
        # Under its label, the width of the form: beside it, the strip's
        # buttons set the page's minimum width.
        form.addRow(QLabel("Pictures"))
        form.addRow(self.pictures)
        self.role.currentIndexChanged.connect(self._sync_choose)
        self._watch(
            self.name,
            self.role,
            self.choose,
            self.present,
            self.hidden,
            self.aliases,
            self.voice,
            self.canon,
            self.summary,
            self.description,
            self.pictures,
        )

    def _sync_choose(self) -> None:
        playable = self.role.currentData() in ("player", "author")
        self.choose.setEnabled(playable)
        if not playable and self.choose.isChecked():
            self.choose.setChecked(False)

    def _fill(self) -> None:
        character: CharacterDoc = self.item
        self.name.setText(character.name)
        self.role.setCurrentIndex(max(self.role.findData(character.role), 0))
        self.choose.setChecked(character.choose)
        self.present.setChecked(character.present)
        self.hidden.setChecked(character.hidden)
        self.aliases.setText(", ".join(character.aliases))
        self.voice.setText(character.voice)
        self.canon.setText(character.canon)
        self.summary.setText(character.summary)
        self.description.setPlainText(character.description)
        self.pictures.set_pictures(character.pictures, character.name)
        self._sync_choose()

    def _store(self) -> None:
        character: CharacterDoc = self.item
        old = character.name
        character.name = self.name.text()
        character.role = self.role.currentData()
        character.choose = self.choose.isChecked()
        character.present = self.present.isChecked()
        character.hidden = self.hidden.isChecked()
        character.aliases = _split(self.aliases.text())
        character.voice = self.voice.text()
        character.canon = self.canon.text()
        character.summary = self.summary.text()
        character.description = self.description.toPlainText()
        self._name_changed(old, character.name)


class EntryPage(Page):
    """A place, or with `is_place` off a lore entry."""

    def __init__(
        self,
        *,
        is_place: bool,
        folder: Folder | None = None,
        save_first: SaveFirst | None = None,
    ) -> None:
        super().__init__()
        self.pictures = PlotPictureStrip(folder, save_first)
        self.name = QLineEdit()
        self.keywords = QLineEdit()
        self.keywords.setPlaceholderText("comma-separated; the name itself is always one")
        self.always = QCheckBox("Sent every turn")
        self.hidden = QCheckBox("Hidden until an event reveals it")
        self.description = QPlainTextEdit()
        form = self._form
        form.addRow("Name", self.name)
        form.addRow("Keywords", self.keywords)
        form.addRow(self.always)
        form.addRow(self.hidden)
        form.addRow(
            hint(
                "Somewhere the story can be: events can test “at” and “visited” against it, "
                "and the program records which place the player's character is at."
                if is_place
                else "Background the storyteller needs now and then: how a technology works, "
                "a history, a people. Never somewhere to be."
            )
        )
        form.addRow("Description", self.description)
        form.addRow(QLabel("Pictures"))
        form.addRow(self.pictures)
        self._watch(
            self.name, self.keywords, self.always, self.hidden, self.description, self.pictures
        )

    def _fill(self) -> None:
        entry: EntryDoc = self.item
        self.name.setText(entry.name)
        self.keywords.setText(", ".join(entry.keywords))
        self.always.setChecked(entry.always)
        self.hidden.setChecked(entry.hidden)
        self.description.setPlainText(entry.description)
        self.pictures.set_pictures(entry.pictures, entry.name)

    def _store(self) -> None:
        entry: EntryDoc = self.item
        old = entry.name
        entry.name = self.name.text()
        entry.keywords = _split(self.keywords.text())
        entry.always = self.always.isChecked()
        entry.hidden = self.hidden.isChecked()
        entry.description = self.description.toPlainText()
        self._name_changed(old, entry.name)


class FactPage(Page):
    def __init__(self) -> None:
        super().__init__()
        self.name = QLineEdit()
        self.name.setPlaceholderText("lower-case letters, digits and _ (has_companions)")
        self.initial = QLineEdit()
        self.values = QLineEdit()
        self.values.setPlaceholderText("comma-separated; empty for a yes/no fact, or any value")
        self.watched = QCheckBox("Watched: read from the prose after each passage")
        self.meaning = _short_edit("What it means and when it changes, for the read that keeps it")
        form = self._form
        form.addRow("Name", self.name)
        form.addRow("Starts as", self.initial)
        form.addRow("Values", self.values)
        form.addRow(self.watched)
        form.addRow(
            hint(
                "Only watched facts are read from the prose; every other fact changes only "
                "when an event sets it. Watch as few as you can. Whether an event has happened "
                "and where the character is are tracked already: don't make facts for those."
            )
        )
        form.addRow("Meaning", self.meaning)
        self._watch(self.name, self.initial, self.values, self.watched, self.meaning)

    def _fill(self) -> None:
        fact: FactDoc = self.item
        self.name.setText(fact.name)
        self.initial.setText(fact.initial)
        self.values.setText(", ".join(fact.values))
        self.watched.setChecked(fact.watched)
        self.meaning.setPlainText(fact.meaning)

    def _store(self) -> None:
        fact: FactDoc = self.item
        old = fact.name
        fact.name = self.name.text().strip()
        fact.initial = self.initial.text().strip()
        fact.values = _split(self.values.text())
        fact.watched = self.watched.isChecked()
        fact.meaning = self.meaning.toPlainText()
        self._name_changed(old, fact.name)


# --- events: conditions, sets, and what they bring -----------------------------------


class ConditionRow(QWidget):
    changed = Signal()
    removed = Signal(object)

    def __init__(self, condition: ConditionDoc, document: PlotDocument, own_title: str) -> None:
        super().__init__()
        self.condition = condition
        self.document = document
        self.own_title = own_title
        self.kind = QComboBox()
        for label, key in _CONDITION_KINDS:
            self.kind.addItem(label, key)
        self.who = QComboBox()
        self.who.setToolTip("Whose place: the character the author is playing, or a named one")
        self.first = QComboBox()
        self.second = QComboBox()
        self.second.setEditable(True)
        self.text = QLineEdit()
        remove = QPushButton("×")
        remove.setObjectName("smallButton")
        remove.setToolTip("Remove this condition")
        remove.clicked.connect(lambda: self.removed.emit(self))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.kind)
        layout.addWidget(self.who)
        layout.addWidget(self.first, 1)
        layout.addWidget(self.second, 1)
        layout.addWidget(self.text, 2)
        layout.addWidget(remove)
        self._loading = True
        self._fill()
        self._loading = False
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.who.currentIndexChanged.connect(self._store)
        self.first.currentIndexChanged.connect(self._first_changed)
        self.second.currentIndexChanged.connect(self._store)
        self.second.editTextChanged.connect(self._store)
        self.text.textChanged.connect(self._store)

    def _fill(self) -> None:
        condition = self.condition
        key = condition_key(condition.kind, condition.negated)
        self.kind.setCurrentIndex(max(self.kind.findData(key), 0))
        self._layout_for_kind()
        if condition.kind == "fact":
            _fill(self.first, ((f.name, f.name) for f in self.document.facts), condition.fact)
            self._fill_values(condition.value)
        elif condition.kind in ("at", "visited"):
            players = [
                c.name
                for c in self.document.characters
                if c.role in ("player", "author") and c.name.strip()
            ]
            _fill(self.who, [("", HELD), *((name, name) for name in players)], condition.who)
            _fill(self.first, ((p.name, p.name) for p in self.document.places), condition.place)
        elif condition.kind == "happened":
            _fill(
                self.first,
                (
                    (e.title, e.title)
                    for e in self.document.events
                    if e.title.strip() and e.title != self.own_title
                ),
                condition.event,
            )
        else:
            self.text.setText(condition.text)

    def _fill_values(self, current: str) -> None:
        fact = next((f for f in self.document.facts if f.name == self.first.currentData()), None)
        values = list(fact.values) if fact else []
        if fact and not values and fact.initial in ("yes", "no"):
            values = ["yes", "no"]
        self.second.blockSignals(True)
        self.second.clear()
        for value in values:
            self.second.addItem(value, value)
        self.second.setEditText(current)
        self.second.blockSignals(False)

    def _layout_for_kind(self) -> None:
        kind, _ = _kind_of(self.kind.currentData())
        self.who.setVisible(kind in ("at", "visited"))
        self.first.setVisible(kind != "text")
        self.second.setVisible(kind == "fact")
        self.text.setVisible(kind == "text")

    def _kind_changed(self) -> None:
        kind, negated = _kind_of(self.kind.currentData())
        self.condition.kind, self.condition.negated = kind, negated
        self._loading = True
        self._fill()
        self._loading = False
        self._store()

    def _first_changed(self) -> None:
        if self.condition.kind == "fact" and not self._loading:
            self._loading = True
            self._fill_values("")
            self._loading = False
        self._store()

    def _store(self) -> None:
        if self._loading:
            return
        condition = self.condition
        condition.kind, condition.negated = _kind_of(self.kind.currentData())
        if condition.kind == "fact":
            condition.fact = self.first.currentData() or ""
            condition.value = self.second.currentText().strip()
        elif condition.kind in ("at", "visited"):
            condition.who = self.who.currentData() or ""
            condition.place = self.first.currentData() or ""
        elif condition.kind == "happened":
            condition.event = self.first.currentData() or ""
        else:
            condition.text = self.text.text()
        self.changed.emit()


class ConditionsEditor(QWidget):
    """A `requires:` line, built from rows rather than typed."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.conditions: list[ConditionDoc] = []
        self.document = PlotDocument()
        self.own_title = ""
        self.rows = QVBoxLayout()
        self.rows.setContentsMargins(0, 0, 0, 0)
        add = QPushButton("Add condition")
        add.clicked.connect(self._add)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.rows)
        layout.addWidget(add, 0, Qt.AlignLeft)

    def load(self, conditions: list[ConditionDoc], document: PlotDocument, own_title: str) -> None:
        self.conditions, self.document, self.own_title = conditions, document, own_title
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for condition in conditions:
            self._add_row(condition)

    def _add_row(self, condition: ConditionDoc) -> None:
        row = ConditionRow(condition, self.document, self.own_title)
        row.changed.connect(self.changed)
        row.removed.connect(self._remove)
        self.rows.addWidget(row)

    def _add(self) -> None:
        condition = ConditionDoc()
        if self.document.facts:
            condition.fact = self.document.facts[0].name
        self.conditions.append(condition)
        self._add_row(condition)
        self.changed.emit()

    def _remove(self, row: ConditionRow) -> None:
        if row.condition in self.conditions:
            self.conditions.remove(row.condition)
        self.rows.removeWidget(row)
        row.deleteLater()
        self.changed.emit()


class SetRow(QWidget):
    changed = Signal()
    removed = Signal(object)

    def __init__(self, assignment: AssignmentDoc, document: PlotDocument) -> None:
        super().__init__()
        self.assignment = assignment
        self.document = document
        self.fact = QComboBox()
        self.value = QComboBox()
        self.value.setEditable(True)
        remove = QPushButton("×")
        remove.setObjectName("smallButton")
        remove.clicked.connect(lambda: self.removed.emit(self))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.fact, 1)
        layout.addWidget(QLabel("="))
        layout.addWidget(self.value, 1)
        layout.addWidget(remove)
        self._loading = True
        _fill(self.fact, ((f.name, f.name) for f in document.facts), assignment.fact)
        self._fill_values(assignment.value)
        self._loading = False
        self.fact.currentIndexChanged.connect(self._fact_changed)
        self.value.currentIndexChanged.connect(self._store)
        self.value.editTextChanged.connect(self._store)

    def _fill_values(self, current: str) -> None:
        fact = next((f for f in self.document.facts if f.name == self.fact.currentData()), None)
        values = list(fact.values) if fact else []
        if fact and not values and fact.initial in ("yes", "no"):
            values = ["yes", "no"]
        self.value.blockSignals(True)
        self.value.clear()
        for value in values:
            self.value.addItem(value, value)
        self.value.setEditText(current)
        self.value.blockSignals(False)

    def _fact_changed(self) -> None:
        self._loading = True
        self._fill_values("")
        self._loading = False
        self._store()

    def _store(self) -> None:
        if self._loading:
            return
        self.assignment.fact = self.fact.currentData() or ""
        self.assignment.value = self.value.currentText().strip()
        self.changed.emit()


class SetsEditor(QWidget):
    """A `sets:` line: which facts the event sets, and to what."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.assignments: list[AssignmentDoc] = []
        self.document = PlotDocument()
        self.rows = QVBoxLayout()
        self.rows.setContentsMargins(0, 0, 0, 0)
        add = QPushButton("Set a fact")
        add.clicked.connect(self._add)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.rows)
        layout.addWidget(add, 0, Qt.AlignLeft)

    def load(self, assignments: list[AssignmentDoc], document: PlotDocument) -> None:
        self.assignments, self.document = assignments, document
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for assignment in assignments:
            self._add_row(assignment)

    def _add_row(self, assignment: AssignmentDoc) -> None:
        row = SetRow(assignment, self.document)
        row.changed.connect(self.changed)
        row.removed.connect(self._remove)
        self.rows.addWidget(row)

    def _add(self) -> None:
        assignment = AssignmentDoc(fact=self.document.facts[0].name if self.document.facts else "")
        self.assignments.append(assignment)
        self._add_row(assignment)
        self.changed.emit()

    def _remove(self, row: SetRow) -> None:
        if row.assignment in self.assignments:
            self.assignments.remove(row.assignment)
        self.rows.removeWidget(row)
        row.deleteLater()
        self.changed.emit()


class NamePicker(QListWidget):
    """Tick the names an event brings in or reveals. Hidden ones come first,
    since they are what this is for; a name the file doesn't have stays
    ticked and says so."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setMaximumHeight(110)
        self.names: list[str] = []
        self._loading = False
        self.itemChanged.connect(self._store)

    def load(self, names: list[str], candidates: list[tuple[str, bool]]) -> None:
        """`candidates` are (name, hidden)."""
        self._loading = True
        self.names = names
        self.clear()
        ordered = sorted(candidates, key=lambda pair: (not pair[1], pair[0].lower()))
        offered = {name for name, _ in ordered}
        for name, hidden in ordered:
            self._add(name, f"{name}  (hidden)" if hidden else name, name in names)
        for name in names:
            if name not in offered:
                self._add(name, f"{name}  (not in this file)", True)
        self._loading = False

    def _add(self, name: str, label: str, checked: bool) -> None:
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, name)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.addItem(item)

    def _store(self) -> None:
        if self._loading:
            return
        self.names[:] = [
            self.item(i).data(Qt.UserRole)
            for i in range(self.count())
            if self.item(i).checkState() == Qt.Checked
        ]
        self.changed.emit()


class WayPage(Page):
    """What an event and a variant share: conditions, what it brings, what it
    sets, what the storyteller is told."""

    def __init__(self, *, is_event: bool) -> None:
        super().__init__()
        self.is_event = is_event
        self._event: EventDoc | None = None
        self.title = QLineEdit()
        self.timeline = QCheckBox("On the timeline: happens within a window of days")
        self.days_only = QCheckBox("Only between certain days")
        self.first_day = QSpinBox()
        self.first_day.setRange(1, 99999)
        self.last_day = QSpinBox()
        self.last_day.setRange(1, 99999)
        days = QHBoxLayout()
        days.addWidget(QLabel("Day"))
        days.addWidget(self.first_day)
        days.addWidget(QLabel("to"))
        days.addWidget(self.last_day)
        days.addStretch(1)
        self.days_row = QWidget()
        self.days_row.setLayout(days)
        self.pacing = QComboBox()
        for pacing, label in PACING_LABELS.items():
            self.pacing.addItem(label, pacing)
        self.lead_in = QCheckBox("Lead-in: the storyteller may be told ahead, so it can foreshadow")
        self.must_happen = QComboBox()
        self.must_happen.addItem("Default — yes, unless the event has conditions of its own", None)
        self.must_happen.addItem("Yes — resolves one way or another when its window closes", True)
        self.must_happen.addItem("No — skipped if it hasn't happened by then", False)
        self.at = QComboBox()
        self.requires = ConditionsEditor()
        self.trigger = _short_edit(
            "In plain words, what has to be happening. The director judges it."
        )
        self.brings = NamePicker()
        self.reveals = NamePicker()
        self.sets = SetsEditor()
        self.offscreen = QCheckBox("Offscreen: happens without the player's character; facts only")
        self.offscreen_choice = QComboBox()
        self.offscreen_choice.addItem("As the event says", None)
        self.offscreen_choice.addItem("Yes — no prose, only the facts", True)
        self.offscreen_choice.addItem("No — on screen", False)
        self.aftermath = _short_edit(
            "What is left afterwards, and what people say. Passed on when it becomes relevant."
        )
        self.tell = QPlainTextEdit()
        self.tell.setPlaceholderText("What the storyteller is told should happen.")

        form = self._form
        form.addRow("Title", self.title)
        if is_event:
            form.addRow(self.timeline)
            form.addRow(self.days_only)
            form.addRow("Days", self.days_row)
            form.addRow(
                hint(
                    "A day in the window is drawn at random. Off the timeline, the first day "
                    "is the earliest it can happen; past the last it is skipped if its "
                    "conditions don't hold."
                )
            )
            form.addRow("Pacing", self.pacing)
            form.addRow(self.lead_in)
            form.addRow("Must happen", self.must_happen)
        form.addRow("Happens at", self.at)
        form.addRow("Requires", self.requires)
        form.addRow(
            hint("Checked by the program, never a model. Anything fuzzy goes in the trigger.")
        )
        form.addRow("Trigger", self.trigger)
        form.addRow("Brings in", self.brings)
        form.addRow("Reveals", self.reveals)
        form.addRow("Sets", self.sets)
        form.addRow(self.offscreen if is_event else self.offscreen_choice)
        if is_event:
            form.addRow("Aftermath", self.aftermath)
        form.addRow("Tell", self.tell)
        self.timeline.toggled.connect(self._sync_days)
        self.days_only.toggled.connect(self._sync_days)
        self._watch(
            self.title,
            self.timeline,
            self.days_only,
            self.first_day,
            self.last_day,
            self.pacing,
            self.lead_in,
            self.must_happen,
            self.at,
            self.requires,
            self.trigger,
            self.brings,
            self.reveals,
            self.sets,
            self.offscreen,
            self.offscreen_choice,
            self.aftermath,
            self.tell,
        )

    def _sync_days(self) -> None:
        timeline = self.timeline.isChecked()
        self.days_only.setVisible(not timeline)
        self.days_row.setEnabled(timeline or self.days_only.isChecked())
        self.must_happen.setEnabled(timeline)
        self.lead_in.setEnabled(True)

    def load_way(self, way: VariantDoc, event: EventDoc | None, document: PlotDocument) -> None:
        """`event` is the variant's event, for the offscreen default; None for an event."""
        self._event = event
        self.load(way, document)

    def _fill(self) -> None:
        way: VariantDoc = self.item
        document = self.document
        self.title.setText(way.title)
        if self.is_event:
            event: EventDoc = way  # type: ignore[assignment]
            self.timeline.setChecked(event.timeline)
            self.days_only.setChecked(event.when is not None)
            first, last = event.when or (1, 1)
            self.first_day.setValue(first)
            self.last_day.setValue(last)
            self.pacing.setCurrentIndex(max(self.pacing.findData(event.pacing), 0))
            self.lead_in.setChecked(event.lead_in)
            self.must_happen.setCurrentIndex(max(self.must_happen.findData(event.must_happen), 0))
            self.aftermath.setPlainText(event.aftermath)
            self.offscreen.setChecked(bool(event.offscreen))
            self._sync_days()
        else:
            self.offscreen_choice.setCurrentIndex(
                max(self.offscreen_choice.findData(way.offscreen), 0)
            )
        _fill(
            self.at,
            [("", NO_PLACE), *((p.name, p.name) for p in document.places if p.name.strip())],
            way.at,
        )
        own = way.title if self.is_event else (self._event.title if self._event else "")
        self.requires.load(way.requires, document, own)
        self.trigger.setPlainText(way.trigger)
        self.brings.load(
            way.brings, [(c.name, c.hidden) for c in document.characters if c.name.strip()]
        )
        self.reveals.load(
            way.reveals,
            [(e.name, e.hidden) for e in (*document.places, *document.lore) if e.name.strip()],
        )
        self.sets.load(way.sets, document)
        self.tell.setPlainText(way.tell)

    def _store(self) -> None:
        way: VariantDoc = self.item
        old = way.title
        way.title = self.title.text()
        if self.is_event:
            event: EventDoc = way  # type: ignore[assignment]
            event.timeline = self.timeline.isChecked()
            if event.timeline or self.days_only.isChecked():
                first, last = self.first_day.value(), self.last_day.value()
                event.when = (first, max(first, last))
            else:
                event.when = None
            event.pacing = self.pacing.currentData()
            event.lead_in = self.lead_in.isChecked()
            event.must_happen = self.must_happen.currentData()
            event.aftermath = self.aftermath.toPlainText()
            event.offscreen = True if self.offscreen.isChecked() else None
        else:
            way.offscreen = self.offscreen_choice.currentData()
        way.at = self.at.currentData() or ""
        way.trigger = self.trigger.toPlainText()
        way.tell = self.tell.toPlainText()
        # Conditions, sets, brings and reveals write into their lists directly.
        if self.is_event:
            self._name_changed(old, way.title)
