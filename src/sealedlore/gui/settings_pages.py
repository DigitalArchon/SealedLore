"""Settings' pages: a list down the left, grouped, and the page on the right
under its name and what it is for.

Eight tabs in a row ("Endpoints, Models, Generation, Context, Lore, Images,
Video, Private") said nothing to a new player about which held what (the
author, Oct 18 2026: make them "more organized/user friendly"). Grouped by
what they are about, each page headed by a sentence, it reads as a list of
questions: where are my models, which does what, how does it write, how
much does it remember, pictures, privacy.

The page names are kept, so "Settings → Images" in the manual still finds
its page. `SettingsPages` answers the part of `QTabWidget`'s interface the
dialog, the window and the tests use (`addTab`, `count`, `tabText`,
`widget`, `indexOf`, `currentIndex`, `setCurrentIndex`, `currentWidget`,
`setCurrentWidget`, `currentChanged`).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# A page's list item carries its page number; a group heading carries none.
PAGE_ROLE = Qt.UserRole + 1


class SettingsPages(QWidget):
    currentChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.list = QListWidget()
        self.list.setObjectName("settingsNav")
        self.list.setFixedWidth(190)
        self.list.currentItemChanged.connect(self._on_item)
        self.heading = QLabel()
        self.heading.setObjectName("settingsHeading")
        self.about = QLabel()
        self.about.setObjectName("settingsAbout")
        self.about.setWordWrap(True)
        self.stack = QStackedWidget()
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(4)
        right.addWidget(self.heading)
        right.addWidget(self.about)
        right.addSpacing(6)
        right.addWidget(self.stack, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(self.list)
        layout.addLayout(right, 1)
        self._labels: list[str] = []
        self._abouts: list[str] = []
        self._items: list[QListWidgetItem] = []

    # --- building -------------------------------------------------------------

    def add_group(self, title: str) -> None:
        """A heading in the list over the pages that follow; not a page."""
        item = QListWidgetItem(title.upper())
        item.setFlags(Qt.NoItemFlags)
        item.setData(PAGE_ROLE, None)
        self.list.addItem(item)

    def addTab(self, widget: QWidget, label: str, about: str = "") -> int:  # noqa: N802
        index = self.stack.addWidget(widget)
        self._labels.append(label)
        self._abouts.append(about)
        item = QListWidgetItem(label)
        item.setData(PAGE_ROLE, index)
        item.setToolTip(about)
        self.list.addItem(item)
        self._items.append(item)
        if index == 0:
            self.setCurrentIndex(0)
        return index

    # --- QTabWidget's interface --------------------------------------------------

    def count(self) -> int:
        return self.stack.count()

    def tabText(self, index: int) -> str:  # noqa: N802 - Qt naming
        return self._labels[index]

    def tabToolTip(self, index: int) -> str:  # noqa: N802 - Qt naming
        return self._abouts[index]

    def widget(self, index: int) -> QWidget:
        return self.stack.widget(index)

    def indexOf(self, widget: QWidget) -> int:  # noqa: N802 - Qt naming
        return self.stack.indexOf(widget)

    def currentIndex(self) -> int:  # noqa: N802 - Qt naming
        return self.stack.currentIndex()

    def currentWidget(self) -> QWidget:  # noqa: N802 - Qt naming
        return self.stack.currentWidget()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt naming
        if not 0 <= index < self.count():
            return
        self.list.setCurrentItem(self._items[index])
        self._show(index)

    def setCurrentWidget(self, widget: QWidget) -> None:  # noqa: N802 - Qt naming
        self.setCurrentIndex(self.indexOf(widget))

    def _on_item(self, item: QListWidgetItem | None, _previous) -> None:
        index = item.data(PAGE_ROLE) if item is not None else None
        if index is not None:
            self._show(int(index))

    def _show(self, index: int) -> None:
        changed = index != self.stack.currentIndex()
        self.stack.setCurrentIndex(index)
        self.heading.setText(self._labels[index])
        self.about.setText(self._abouts[index])
        self.about.setVisible(bool(self._abouts[index]))
        if changed:
            self.currentChanged.emit(index)


class Fold(QObject):
    """Rows of a page's form put away under a heading that opens them: the
    options most people never change. The rows stay in the page's own form,
    so their labels line up with the rest. A child of its button, so it
    lives as long as the page does: a plain object was collected when the
    page's builder returned, and the button opened nothing."""

    def __init__(self, form: QFormLayout, title: str, *, tip: str = "") -> None:
        button = QToolButton()
        super().__init__(button)
        self.button = button
        self.form = form
        self.title = title
        self.rows: list[QWidget | QLayout] = []
        self.button.setObjectName("foldButton")
        self.button.setCheckable(True)
        self.button.setToolTip(tip)
        self.button.toggled.connect(self._toggle)
        form.addRow(self.button)
        self._toggle(False)

    def add_row(self, label_or_field, field: QWidget | QLayout | None = None) -> None:
        if field is None:
            self.form.addRow(label_or_field)
            key = label_or_field
        else:
            self.form.addRow(label_or_field, field)
            key = field
        self.rows.append(key)
        self.form.setRowVisible(key, self.button.isChecked())

    def is_open(self) -> bool:
        return self.button.isChecked()

    def set_open(self, on: bool) -> None:
        self.button.setChecked(on)

    def _toggle(self, on: bool) -> None:
        self.button.setText(f"{'▾' if on else '▸'}  {self.title}")
        for key in self.rows:
            self.form.setRowVisible(key, on)
