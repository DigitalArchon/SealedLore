"""Tabs in two rows of buttons, for the inspector.

Eight pages did not fit a tab strip even at 1920px: the last tab was cut and
the rest scrolled behind arrows, so reaching a page could take several
clicks. Here every page's button is always in view, in two rows, over a
stacked page. The API is the part of `QTabWidget` the window uses, plus
hiding a page (the Plot page shows only for a story with a plot).

The stack's minimum width is the widest page's, whichever page is showing,
so switching pages never moves the dock.

With no story open it shows one empty page instead (`set_empty`,
`show_empty`): greyed-out forms there looked usable, but nothing in them
answered, not even their scrollbars. The two share a stack, so the dock keeps
its width when a story opens.
"""

from __future__ import annotations

import math

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

ROWS = 2


class TabRows(QWidget):
    currentChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._buttons: list[QPushButton] = []
        self._visible: list[bool] = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idClicked.connect(self.setCurrentIndex)

        bar = QWidget()
        bar.setObjectName("tabRows")
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(4)
        # Each row fills the width on its own, so a shorter second row has no
        # empty cell at its end.
        self._rows: list[QHBoxLayout] = []
        for _ in range(ROWS):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            bar_layout.addLayout(row)
            self._rows.append(row)
        self._stack = QStackedWidget()
        self._stack.setObjectName("tabRowsPage")

        pages = QWidget()
        pages_layout = QVBoxLayout(pages)
        pages_layout.setContentsMargins(0, 0, 0, 0)
        pages_layout.setSpacing(6)
        pages_layout.addWidget(bar)
        pages_layout.addWidget(self._stack, 1)
        # The pages, or the empty state in their place.
        self._outer = QStackedWidget()
        self._outer.addWidget(pages)
        self._empty: QWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._outer)

    # --- the QTabWidget subset --------------------------------------------

    def addTab(self, widget: QWidget, label: str) -> int:
        index = self._stack.addWidget(widget)
        button = QPushButton(label)
        button.setObjectName("tabRowButton")
        button.setCheckable(True)
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._group.addButton(button, index)
        self._buttons.append(button)
        self._visible.append(True)
        if index == 0:
            button.setChecked(True)
        self._arrange()
        return index

    def count(self) -> int:
        return self._stack.count()

    def tabText(self, index: int) -> str:
        return self._buttons[index].text()

    def widget(self, index: int) -> QWidget:
        return self._stack.widget(index)

    def indexOf(self, widget: QWidget) -> int:
        return self._stack.indexOf(widget)

    def currentIndex(self) -> int:
        return self._stack.currentIndex()

    def currentWidget(self) -> QWidget:
        return self._stack.currentWidget()

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < self.count() or not self._visible[index]:
            return
        self._buttons[index].setChecked(True)
        if index != self._stack.currentIndex():
            self._stack.setCurrentIndex(index)
            self.currentChanged.emit(index)

    def setCurrentWidget(self, widget: QWidget) -> None:
        self.setCurrentIndex(self.indexOf(widget))

    def isTabVisible(self, index: int) -> bool:
        return self._visible[index]

    def setTabVisible(self, index: int, visible: bool) -> None:
        if self._visible[index] == visible:
            return
        self._visible[index] = visible
        self._arrange()
        if not visible and self.currentIndex() == index:
            first = next((i for i, shown in enumerate(self._visible) if shown), None)
            if first is not None:
                self.setCurrentIndex(first)

    # --- the empty state ----------------------------------------------------

    def set_empty(self, widget: QWidget) -> None:
        """What shows instead of the pages while `show_empty(True)`."""
        if self._empty is not None:
            self._outer.removeWidget(self._empty)
        self._empty = widget
        self._outer.addWidget(widget)

    def show_empty(self, empty: bool) -> None:
        if self._empty is not None:
            self._outer.setCurrentWidget(self._empty if empty else self._outer.widget(0))

    def is_empty_shown(self) -> bool:
        return self._empty is not None and self._outer.currentWidget() is self._empty

    # --- layout -------------------------------------------------------------

    def _arrange(self) -> None:
        """Lay the visible buttons out in rows, as evenly as they go."""
        for button in self._buttons:
            for row in self._rows:
                row.removeWidget(button)
            button.hide()
        shown = [b for b, visible in zip(self._buttons, self._visible, strict=True) if visible]
        per_row = max(math.ceil(len(shown) / ROWS), 1)
        for position, button in enumerate(shown):
            self._rows[position // per_row].addWidget(button)
            button.show()
