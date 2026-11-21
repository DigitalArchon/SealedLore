"""The story's always-on entries against the lore budget (the Lore page's
Always-on entries…): which stay always on, which become optional (sent when
their keywords come up, as any other entry), condensed versions written,
and whether they are sent when the always-on entries are too much.

Nothing changes until OK, but condensed versions written here: they are kept
beside their entries either way (they change nothing until they are needed).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.lore_condense import StandingFit, worth_condensing
from sealedlore.gui.sizing import (
    adjustable_columns,
    enter_only_on_buttons,
    may_discard,
    size_to_screen,
)
from sealedlore.models.lore import LoreEntry

TITLE = "Always-on entries"
INTRO = (
    "Always-on entries are sent with every turn, whatever is said. Untick the ones that "
    "needn't be: they become optional, sent when their keywords come up as any other "
    "entry is. A condensed version is a shorter one written beside an entry; with the box "
    "below ticked it is sent in the entry's place when the always-on entries are more than "
    "the lore budget holds, the fewest needed, the least important first (lowest order "
    "under SillyTavern's rules, lowest priority under SealedLore's, then the lowest in the "
    "list). The entries themselves are never changed."
)
USE_CONDENSED = (
    "Use the condensed versions when the always-on entries are more than the lore budget"
)
COLUMNS = ("Always on", "Entry", "Tokens", "Condensed", "Note")


def summary(fit: StandingFit, *, on: int, total: int) -> str:
    """The always-on entries against the lore budget, in words."""
    head = (
        f"{on} of {total} entries always on: {fit.whole:,} tokens whole, "
        f"against a lore budget of {fit.room:,}."
    )
    if fit.whole <= fit.room:
        return head + " They fit."
    if fit.condensed:
        tail = f" {len(fit.condensed)} sent condensed: {fit.sent:,} tokens."
        if fit.over:
            tail += " Still more than the budget: make some optional, or condense more."
        return head + tail
    return head + (
        " More than the budget: make some optional, or write condensed versions."
        if fit.whole > fit.room
        else ""
    )


class AlwaysOnDialog(QDialog):
    """`entries`: the always-on entries a model may see, in the lorebook's
    order; `fit(standing, use)` the always-on entries as they would be sent;
    `tokens(text)` a count; `condense(entries)` writes condensed versions of
    those (the window's, while a progress dialog waits)."""

    def __init__(
        self,
        entries: Sequence[LoreEntry],
        *,
        use_condensed: bool,
        fit: Callable[[list[LoreEntry], bool], StandingFit],
        tokens: Callable[[str], int],
        condense: Callable[[list[LoreEntry]], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self._entries = list(entries)
        self._fit = fit
        self._tokens = tokens
        self._condense = condense
        self._loading = False
        column = QVBoxLayout(self)
        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        intro.setObjectName("hintLabel")
        intro.setTextFormat(Qt.PlainText)
        column.addWidget(intro)
        self.said = QLabel()
        self.said.setWordWrap(True)
        self.said.setTextFormat(Qt.PlainText)
        column.addWidget(self.said)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.itemChanged.connect(self._changed)
        column.addWidget(self.table, 1)
        self.use_condensed = QCheckBox(USE_CONDENSED)
        self.use_condensed.setChecked(use_condensed)
        self.use_condensed.toggled.connect(lambda _on: self._refresh())
        column.addWidget(self.use_condensed)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.condense_button = QPushButton("Write condensed versions…")
        self.condense_button.setToolTip(
            "The authoring model writes a shorter version of each always-on entry ticked "
            "above that is long enough and has none: one call an entry"
        )
        self.condense_button.clicked.connect(self._write)
        buttons.addButton(self.condense_button, QDialogButtonBox.ActionRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)
        self._fill()
        adjustable_columns(self.table, stretch=1)
        size_to_screen(self, 0.55, 0.7, at_least=QSize(720, 480))
        self._opened = self.chosen()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt's name
        # Enter presses only a focused button: in a table of ticks it was OK.
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)

    def reject(self) -> None:
        # Ticks changed: asked before they are thrown away.
        if may_discard(self, [self._opened], [self.chosen()]):
            super().reject()

    def _fill(self) -> None:
        self._loading = True
        on = (
            {entry.id: self._is_on(row) for row, entry in enumerate(self._entries)}
            if self.table.rowCount()
            else {}
        )
        self.table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            tick.setCheckState(Qt.Checked if on.get(entry.id, True) else Qt.Unchecked)
            self.table.setItem(row, 0, tick)
            self.table.setItem(row, 1, QTableWidgetItem(entry.title or "(untitled)"))
            whole = self._tokens(entry.content)
            self.table.setItem(row, 2, QTableWidgetItem(f"{whole:,}"))
            shorter = entry.condensed_now()
            if shorter:
                condensed = f"{self._tokens(shorter):,}"
            elif entry.condensed:
                condensed = "out of date"
            else:
                condensed = "—"
            self.table.setItem(row, 3, QTableWidgetItem(condensed))
            self.table.setItem(row, 4, QTableWidgetItem(""))
        self._loading = False
        self._refresh()

    def _is_on(self, row: int) -> bool:
        item = self.table.item(row, 0)
        return item is not None and item.checkState() == Qt.Checked

    def _changed(self, item: QTableWidgetItem) -> None:
        if not self._loading and item.column() == 0:
            self._refresh()

    def _refresh(self) -> None:
        on = [entry for row, entry in enumerate(self._entries) if self._is_on(row)]
        fit = self._fit(on, self.use_condensed.isChecked())
        sent_condensed = set(fit.condensed)
        self._loading = True
        for row, entry in enumerate(self._entries):
            if not self._is_on(row):
                note = (
                    "Optional: sent when its keywords come up"
                    if entry.keywords
                    else "Optional, but it has no keywords: it would never be sent"
                )
            elif entry.id in sent_condensed:
                note = "Sent condensed"
            elif not entry.condensed_now() and worth_condensing(entry.content):
                note = "Can be condensed"
            else:
                note = ""
            self.table.item(row, 4).setText(note)
        self._loading = False
        self.said.setText(summary(fit, on=len(on), total=len(self._entries)))
        self.condense_button.setEnabled(bool(self._to_condense()))

    def _to_condense(self) -> list[LoreEntry]:
        return [
            entry
            for row, entry in enumerate(self._entries)
            if self._is_on(row) and worth_condensing(entry.content) and not entry.condensed_now()
        ]

    def _write(self) -> None:
        wanted = self._to_condense()
        if wanted:
            self._condense(wanted)
            self._fill()

    def chosen(self) -> tuple[dict[str, bool], bool]:
        """Each entry's Always on, by id, and whether condensed versions are used."""
        return (
            {entry.id: self._is_on(row) for row, entry in enumerate(self._entries)},
            self.use_condensed.isChecked(),
        )
