"""The plot's clock, facts and events (models/plot.py).

After each passage a small model reads how much story time passed and which
facts changed (engine/chronicle_read.py); this panel shows the result with an
Undo, and lets the author correct either. A correction is a snapshot on the
current passage, so it holds from there on and the next read starts from it.

Spoilers are off by default: the author sees the clock and what has happened
that their character knows of. "Show plot" adds the facts, every event, the
day each is due, what is armed, and the controls to mark an event by hand.
"""

from __future__ import annotations

from PySide6.QtCore import QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.chronicle import clock_minutes, day_of, describe_changes, format_clock
from sealedlore.models.plot import MINUTES_PER_DAY, Chronicle, EventDef, EventStatus, Plot

_STATE_LABELS = {
    "pending": "to come",
    "led_in": "foreshadowed",
    "directed": "passed on",
    "happened": "happened",
    "missed": "skipped",
}


class ClockDialog(QDialog):
    """Set the story's clock: a day and a time."""

    def __init__(self, minutes: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set the story's clock")
        self.day = QSpinBox()
        self.day.setRange(1, 100_000)
        self.day.setValue(day_of(minutes))
        within = minutes % MINUTES_PER_DAY
        self.time = QTimeEdit(QTime(within // 60, within % 60))
        self.time.setDisplayFormat("HH:mm")
        form = QFormLayout()
        form.addRow("Day", self.day)
        form.addRow("Time", self.time)
        note = QLabel("Holds from the current passage on; the next read counts from here.")
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def minutes(self) -> int:
        time = self.time.time()
        return clock_minutes(self.day.value(), time.hour(), time.minute())


class PlotPanel(QWidget):
    clock_edit_requested = Signal()
    fact_changed = Signal(str, str)
    undo_requested = Signal()
    spoilers_toggled = Signal(bool)
    bring_in_requested = Signal(str)
    # (event id, state): "happened", "missed" or "pending".
    event_marked = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._loading = False
        self._shape: list[tuple[str, tuple[str, ...]]] = []
        self._editors: dict[str, QComboBox | QLineEdit] = {}
        self._event_ids: list[str] = []
        self._started = False

        self.empty = QLabel(
            "This story has no plot. Story → Import plot… gives it one from a plot file; "
            "File → Plot file editor… writes one. The format guide and two sample plots "
            "are under Help → Plot format guide and samples."
        )
        self.empty.setObjectName("hintLabel")
        self.empty.setWordWrap(True)

        self.clock = QLabel()
        self.clock.setObjectName("plotClock")
        self.clock_button = QPushButton("Set…")
        self.clock_button.setToolTip("Correct the story's clock")
        self.clock_button.clicked.connect(self.clock_edit_requested)
        clock_row = QHBoxLayout()
        clock_row.setContentsMargins(0, 0, 0, 0)
        clock_row.addWidget(QLabel("Story time"))
        clock_row.addWidget(self.clock, 1)
        clock_row.addWidget(self.clock_button)

        self.changes = QLabel()
        self.changes.setObjectName("hintLabel")
        self.changes.setWordWrap(True)
        self.undo_button = QPushButton("Undo")
        self.undo_button.setToolTip("Put the clock and facts back as they were before this passage")
        self.undo_button.clicked.connect(self.undo_requested)
        changes_row = QHBoxLayout()
        changes_row.setContentsMargins(0, 0, 0, 0)
        changes_row.addWidget(self.changes, 1)
        changes_row.addWidget(self.undo_button)
        self.changes_strip = QWidget()
        self.changes_strip.setLayout(changes_row)
        self.changes_strip.hide()

        self.facts = QTableWidget(0, 2)
        self.facts.setHorizontalHeaderLabels(["Fact", "Now"])
        self.facts.verticalHeader().setVisible(False)
        self.facts.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.facts.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.facts.setSelectionMode(QTableWidget.NoSelection)

        self.spoilers = QCheckBox("Show plot (spoilers)")
        self.spoilers.setToolTip(
            "Show the facts, every event still to come, the day each is due, and what "
            "the director could act on now."
        )
        self.spoilers.toggled.connect(self._spoilers_toggled)

        self.events = QTableWidget(0, 3)
        self.events.setHorizontalHeaderLabels(["Event", "State", "When"])
        self.events.verticalHeader().setVisible(False)
        self.events.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.events.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.events.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.events.setSelectionBehavior(QTableWidget.SelectRows)
        self.events.setSelectionMode(QTableWidget.SingleSelection)
        self.events.setEditTriggers(QTableWidget.NoEditTriggers)
        self.events.itemSelectionChanged.connect(self._update_mark_buttons)
        self.mark_happened = QPushButton("Mark happened")
        self.mark_happened.setToolTip("It has happened: what it sets applies from this passage")
        self.mark_skipped = QPushButton("Skip")
        self.mark_skipped.setToolTip("It won't happen in this playthrough")
        self.mark_pending = QPushButton("Still to come")
        self.mark_pending.setToolTip("Put it back among the events still to come")
        for button, state in (
            (self.mark_happened, "happened"),
            (self.mark_skipped, "missed"),
            (self.mark_pending, "pending"),
        ):
            button.clicked.connect(lambda _=False, state=state: self._mark(state))
        marks = QHBoxLayout()
        marks.setContentsMargins(0, 0, 0, 0)
        marks.addWidget(self.mark_happened)
        marks.addWidget(self.mark_skipped)
        marks.addWidget(self.mark_pending)
        marks.addStretch(1)
        self.marks = QWidget()
        self.marks.setLayout(marks)
        self.where = QLabel()
        self.where.setWordWrap(True)
        self.held_back_label = QLabel("Held back until an event brings them in")
        self.held_back = QTableWidget(0, 2)
        self.held_back.setHorizontalHeaderLabels(["Who or where", "In the story"])
        self.held_back.verticalHeader().setVisible(False)
        self.held_back.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.held_back.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.held_back.setSelectionBehavior(QTableWidget.SelectRows)
        self.held_back.setSelectionMode(QTableWidget.SingleSelection)
        self.held_back.setEditTriggers(QTableWidget.NoEditTriggers)
        self.bring_in_button = QPushButton("Bring in")
        self.bring_in_button.setToolTip(
            "Into the story from this passage on: the storyteller may use them"
        )
        self.bring_in_button.clicked.connect(self._bring_in)
        self._held_back_ids: list[str] = []
        self.facts_label = QLabel("Facts")
        self.events_label = QLabel("What has happened")

        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.addLayout(clock_row)
        body.addWidget(self.where)
        body.addWidget(self.changes_strip)
        body.addWidget(self.spoilers)
        body.addWidget(self.events_label)
        body.addWidget(self.events, 1)
        body.addWidget(self.marks)
        body.addWidget(self.facts_label)
        body.addWidget(self.facts, 1)
        body.addWidget(self.held_back_label)
        body.addWidget(self.held_back)
        body.addWidget(self.bring_in_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.empty)
        layout.addWidget(self.body, 1)
        self.set_plot(None, None, started=False)

    def set_plot(
        self,
        plot: Plot | None,
        chronicle: Chronicle | None,
        *,
        started: bool,
        spoilers: bool = False,
        days: dict[str, int] | None = None,
        armed_ids: frozenset[str] = frozenset(),
        held_back: list[tuple[str, str, bool]] | None = None,
    ) -> None:
        """`held_back`: (id, name, brought in yet) for each hidden character and place."""
        self._loading = True
        try:
            has_plot = plot is not None and chronicle is not None
            self.empty.setVisible(not has_plot)
            self.body.setVisible(has_plot)
            if not has_plot:
                return
            assert plot is not None and chronicle is not None
            self.clock.setText(format_clock(chronicle.minutes))
            self.spoilers.setChecked(spoilers)
            for widget in (self.facts_label, self.facts, self.marks):
                widget.setVisible(spoilers)
            self.events_label.setText("Events" if spoilers else "What has happened")
            self._started = started
            self._fill_events(plot, chronicle, spoilers, days or {}, armed_ids)
            been = [p for p in chronicle.visited if p != chronicle.place]
            self.where.setText(
                f"Where: {chronicle.place or 'none of the named places'}"
                + (f" · been to: {', '.join(been)}" if been else "")
            )
            rows = held_back or []
            for widget in (self.held_back_label, self.held_back, self.bring_in_button):
                widget.setVisible(spoilers and bool(rows))
            self._held_back_ids = [item_id for item_id, _, _ in rows]
            self.held_back.setRowCount(len(rows))
            for row, (_, name, shown) in enumerate(rows):
                self.held_back.setItem(row, 0, QTableWidgetItem(name))
                self.held_back.setItem(row, 1, QTableWidgetItem("yes" if shown else "not yet"))
            self.bring_in_button.setEnabled(started)
            shape = [(fact.name, tuple(fact.values)) for fact in plot.facts]
            if shape != self._shape:
                self._build(plot)
                self._shape = shape
            for fact in plot.facts:
                editor = self._editors[fact.name]
                value = chronicle.facts.get(fact.name, fact.initial)
                if isinstance(editor, QComboBox):
                    editor.setCurrentText(value)
                else:
                    editor.setText(value)
                # Facts are set on a passage; before the first there is none.
                editor.setEnabled(started)
        finally:
            self._loading = False

    def _fill_events(
        self,
        plot: Plot,
        chronicle: Chronicle,
        spoilers: bool,
        days: dict[str, int],
        armed_ids: frozenset[str],
    ) -> None:
        """Every event with spoilers on; otherwise only what the character knows of."""
        rows: list[tuple[EventDef, EventStatus]] = []
        for event in plot.events:
            status = chronicle.events.get(event.id, EventStatus())
            if spoilers or (status.state == "happened" and status.revealed):
                rows.append((event, status))
        selected = self._selected_event()
        self._event_ids = [event.id for event, _ in rows]
        self.events.setRowCount(len(rows))
        for row, (event, status) in enumerate(rows):
            state = _STATE_LABELS[status.state]
            if status.state == "happened" and not status.revealed:
                state = "happened, unseen"
            elif status.state == "happened" and status.account:
                state = "happened in the time that passed"
            if status.overdue:
                state += ", overdue"
            if event.id in armed_ids and status.state not in ("happened", "missed"):
                state += ", could happen now"
            if status.state == "happened" and status.at_minutes is not None:
                when = format_clock(status.at_minutes)
            elif event.id in days and event.window is not None:
                when = f"day {days[event.id]} (of {event.window[0]}–{event.window[1]})"
            elif event.window is not None:
                when = f"days {event.window[0]}–{event.window[1]}"
            else:
                when = "when it fits"
            title = event.title + (f" — {status.variant}" if spoilers and status.variant else "")
            for column, text in enumerate((title, state, when)):
                item = QTableWidgetItem(text)
                if column == 0:
                    # What the character knows of it once it is known: how a
                    # skipped-over event was told, else what it left behind.
                    # The trigger and the tell are the plot's, for spoilers.
                    known = status.state == "happened" and status.revealed
                    told = status.account or event.aftermath
                    item.setToolTip(
                        told
                        if known and told
                        else (event.trigger or event.tell)
                        if spoilers
                        else ""
                    )
                self.events.setItem(row, column, item)
        if selected in self._event_ids:
            self.events.selectRow(self._event_ids.index(selected))
        self._update_mark_buttons()

    def _selected_event(self) -> str | None:
        rows = {index.row() for index in self.events.selectedIndexes()}
        ids = self._event_ids
        return ids[min(rows)] if rows and min(rows) < len(ids) else None

    def _update_mark_buttons(self) -> None:
        chosen = self._selected_event() is not None and self._started
        for button in (self.mark_happened, self.mark_skipped, self.mark_pending):
            button.setEnabled(chosen)

    def _mark(self, state: str) -> None:
        event_id = self._selected_event()
        if event_id is not None:
            self.event_marked.emit(event_id, state)

    def _bring_in(self) -> None:
        rows = {index.row() for index in self.held_back.selectedIndexes()}
        if rows and min(rows) < len(self._held_back_ids):
            self.bring_in_requested.emit(self._held_back_ids[min(rows)])

    def _spoilers_toggled(self, on: bool) -> None:
        if not self._loading:
            self.spoilers_toggled.emit(on)

    def _build(self, plot: Plot) -> None:
        """One row per fact. Rebuilt only when the facts themselves change.

        Refilled in place otherwise: a fact edited from its combo refreshes
        the panel from inside that combo's signal, and replacing the combo
        there would delete the widget that is still emitting.
        """
        self._editors = {}
        self.facts.setRowCount(len(plot.facts))
        for row, fact in enumerate(plot.facts):
            name = QTableWidgetItem(fact.name)
            name.setToolTip(fact.meaning)
            self.facts.setItem(row, 0, name)
            editor = self._editor(fact.name, fact.values)
            editor.setToolTip(fact.meaning)
            self.facts.setCellWidget(row, 1, editor)
            self._editors[fact.name] = editor

    def _editor(self, name: str, values: list[str]) -> QComboBox | QLineEdit:
        if values:
            combo = QComboBox()
            combo.addItems(values)
            combo.currentTextChanged.connect(lambda text: self._edited(name, text))
            return combo
        line = QLineEdit()
        line.editingFinished.connect(lambda: self._edited(name, " ".join(line.text().split())))
        return line

    def _edited(self, name: str, value: str) -> None:
        if not self._loading and value:
            self.fact_changed.emit(name, value.lower())

    def show_changes(self, chronicle: Chronicle | None, *, passage: int, can_undo: bool) -> None:
        """ "Changed by the story at passage 12: +8h, now Day 36 · 06:30; …" and Undo."""
        if chronicle is None or not can_undo:
            self.changes_strip.hide()
            return
        words = describe_changes(chronicle)
        self.changes.setText(
            f"Changed by the story at passage {passage}: {words}"
            if words
            else f"Read at passage {passage}: no time passed, no change."
        )
        self.undo_button.setVisible(bool(words))
        self.changes_strip.show()
