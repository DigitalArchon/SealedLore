"""The main window's side of Find in story. A mixin for `MainWindow`; the bar
and the matching are `gui/find_bar.py`.

Ctrl+F is a window shortcut Qt handles itself, so it works the same under
Wayland and X11. A search starts at the most recent match, since the author
is usually looking back from where they are; Enter goes earlier.
"""

from __future__ import annotations

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QToolButton

from sealedlore.gui.find_bar import FindBar, Match, find_matches


class FindWindow:
    def _build_find(self) -> None:
        """Called from `__init__` before the centre is laid out."""
        self.find_bar = FindBar()
        self.find_bar.hide()
        self.find_bar.search_changed.connect(self._on_find_changed)
        self.find_bar.step_requested.connect(self._step_find)
        self.find_bar.closed.connect(self.close_find)
        self.find_button = QToolButton()
        self.find_button.setObjectName("viewToggleButton")
        self.find_button.setText("Find")
        self.find_button.setToolTip("Find text in this branch of the story (Ctrl+F)")
        self.find_button.clicked.connect(self.open_find)
        self._matches: list[Match] = []
        self._match_index: int | None = None

    def _find_actions(self, view_menu) -> None:
        """Called from `_build_actions`: View → Find in story…"""
        self.find_action = QAction("&Find in story…", self)
        self.find_action.setShortcut(QKeySequence(QKeySequence.Find))
        self.find_action.setStatusTip("Find text in this branch of the story")
        self.find_action.triggered.connect(self.open_find)
        view_menu.addAction(self.find_action)

    def open_find(self) -> None:
        if self.session is None:
            return
        if self.map_shown():
            self.show_map(False)
        self.find_bar.open()
        if self.find_bar.field.text().strip():
            self._on_find_changed(self.find_bar.field.text())

    def close_find(self) -> None:
        self.find_bar.hide()
        self.transcript.clear_match()
        self._matches, self._match_index = [], None
        self.composer.input.setFocus()

    def find_open(self) -> bool:
        return self.find_bar.isVisible()

    def _on_find_changed(self, text: str) -> None:
        self._matches = find_matches(self.transcript.searchable_texts, text)
        self._match_index = len(self._matches) - 1 if self._matches else None
        self._show_current_match()

    def _step_find(self, delta: int) -> None:
        if not self._matches:
            return
        index = self._match_index if self._match_index is not None else 0
        self._match_index = (index + delta) % len(self._matches)
        self._show_current_match()

    def _show_current_match(self) -> None:
        if self._match_index is None:
            self.transcript.clear_match()
        else:
            # Far back, the transcript builds a window around the match
            # (transcript.MAX_BUILT), not every message after it.
            self.transcript.show_match(*self._matches[self._match_index])
        self.find_bar.set_count(self._match_index, len(self._matches))

    def _refresh_find(self) -> None:
        """After the transcript is rebuilt: count again, but don't move the
        reader (a turn or an edit rebuilds it)."""
        if not self.find_open():
            return
        self._matches = find_matches(self.transcript.searchable_texts, self.find_bar.field.text())
        if not self._matches:
            self._match_index = None
        elif self._match_index is None or self._match_index >= len(self._matches):
            self._match_index = len(self._matches) - 1
        self.find_bar.set_count(self._match_index, len(self._matches))
