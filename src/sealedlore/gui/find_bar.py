"""Find in the story (Ctrl+F, View → Find in story…, or Find above the transcript).

The author asked for a quick way to find an old part of the story to check or
change. The search runs over the messages of the branch being read, in the
view showing, as their text: older messages the transcript hasn't built yet
(it builds a page at a time) are found too, and built when one is shown.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NamedTuple

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QToolButton

# Typing searches after a short pause: showing a match may build earlier pages.
SEARCH_DELAY_MS = 250


class Match(NamedTuple):
    node_id: str
    start: int
    length: int


def find_matches(texts: Sequence[tuple[str, str]], query: str) -> list[Match]:
    """Every place `query` occurs, ignoring case, in story order.

    `texts` is (message id, text) as the transcript shows them. The offsets
    are into that text as it is (a regex with IGNORECASE, not lower(), which
    can change a string's length and so every offset after it).
    """
    query = query.strip()
    if not query:
        return []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    return [
        Match(node_id, found.start(), found.end() - found.start())
        for node_id, text in texts
        for found in pattern.finditer(text)
    ]


class _Field(QLineEdit):
    """Enter goes to the earlier match, Shift+Enter to the later one."""

    step = Signal(int)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.step.emit(1 if event.modifiers() & Qt.ShiftModifier else -1)
            return
        super().keyPressEvent(event)


class FindBar(QFrame):
    """The bar itself; the window runs the search (gui/window_find.py)."""

    search_changed = Signal(str)
    # -1 goes earlier in the story, +1 later.
    step_requested = Signal(int)
    closed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("findBar")
        self.field = _Field()
        self.field.setObjectName("findField")
        self.field.setPlaceholderText("Find in this branch")
        self.field.setClearButtonEnabled(True)
        self.field.setToolTip(
            "Enter: the earlier match  ·  Shift+Enter: the later one  ·  Esc: close"
        )
        self.count = QLabel()
        self.count.setObjectName("hintLabel")
        self.earlier = QToolButton()
        self.earlier.setObjectName("viewToggleButton")
        self.earlier.setText("↑ Earlier")
        self.earlier.setToolTip("The match before this one in the story (Enter)")
        self.later = QToolButton()
        self.later.setObjectName("viewToggleButton")
        self.later.setText("↓ Later")
        self.later.setToolTip("The match after this one in the story (Shift+Enter)")
        close = QToolButton()
        close.setObjectName("messageAction")
        close.setText("✕")
        close.setToolTip("Close (Esc)")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.field, 1)
        layout.addWidget(self.count)
        layout.addWidget(self.earlier)
        layout.addWidget(self.later)
        layout.addWidget(close)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SEARCH_DELAY_MS)
        self._timer.timeout.connect(lambda: self.search_changed.emit(self.field.text()))
        self.field.textChanged.connect(lambda _text: self._timer.start())
        self.field.step.connect(self._step)
        self.earlier.clicked.connect(lambda: self._step(-1))
        self.later.clicked.connect(lambda: self._step(1))
        close.clicked.connect(self.closed)
        self.set_count(None, 0)

    def _step(self, delta: int) -> None:
        # A step before the pause is up searches first: Enter straight after
        # typing goes to a match of what was typed.
        if self._timer.isActive():
            self._timer.stop()
            self.search_changed.emit(self.field.text())
            return
        self.step_requested.emit(delta)

    def open(self) -> None:
        self.show()
        self.field.setFocus()
        self.field.selectAll()

    def set_count(self, index: int | None, total: int) -> None:
        """Show "3 of 12", counted in story order, or "No matches"."""
        text = self.field.text().strip()
        if not text:
            self.count.setText("")
        elif total == 0:
            self.count.setText("No matches")
        else:
            self.count.setText(f"{(index or 0) + 1} of {total}")
        self.earlier.setEnabled(total > 1)
        self.later.setEnabled(total > 1)
