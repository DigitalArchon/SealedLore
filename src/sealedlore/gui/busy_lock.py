"""A page that can be looked through, not changed, while a job runs.

The worker is the only writer of story state while a model writes (see
CLAUDE.md, Qt rules), so the Inspector's pages used to be disabled whole for
the length of every job, scene and plot reads included. The author (Oct 15
2026): "The user should be able to browse while waiting", look at another
character's portrait while the passage comes in. A `BusyLock` keeps the
page's lists, tables' scrolling, text selection and pictures alive, makes
its text read-only, and turns off every control that changes something. A
control the page turns back on meanwhile (picking another character enables
its buttons) is turned off again and remembered as on, and a list picked
anew locks the widgets the page has just built for it. Unlocked, each
control is as the page last left it.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QSlider,
    QTextEdit,
    QTreeWidget,
    QWidget,
)
from shiboken6 import isValid

_TEXT = (QLineEdit, QPlainTextEdit, QTextEdit)
_CONTROLS = (QAbstractButton, QComboBox, QAbstractSpinBox, QSlider)
# Lists to look through: kept usable. Other item views (a skills table) are
# edited in place, so they are controls.
_BROWSED = (QListWidget, QTreeWidget)


class BusyLock(QObject):
    def __init__(self, page: QWidget) -> None:
        super().__init__(page)
        self.page = page
        self.locked = False
        # A control's own enabled state as the page last set it.
        self._enabled: dict[QWidget, bool] = {}
        self._read_only: dict[QWidget, bool] = {}
        self._locking = False

    def set_locked(self, locked: bool) -> None:
        if locked == self.locked:
            if locked:
                self._lock_new()
            return
        self.locked = locked
        if locked:
            for view in self._browsed():
                view.currentItemChanged.connect(self._later)
            self._lock_new()
        else:
            for view in self._browsed():
                try:
                    view.currentItemChanged.disconnect(self._later)
                except (RuntimeError, TypeError):
                    pass
            self._unlock()

    def _browsed(self) -> list[QWidget]:
        return [w for w in self.page.findChildren(QWidget) if isinstance(w, _BROWSED)]

    def _later(self, *_args) -> None:
        # After the page has built what the new choice shows.
        QTimer.singleShot(0, self._lock_new)

    def _lock_new(self) -> None:
        if not self.locked:
            return
        self._locking = True
        try:
            for widget in self.page.findChildren(QWidget):
                if widget in self._enabled or widget in self._read_only:
                    continue
                if isinstance(widget, _TEXT):
                    self._read_only[widget] = widget.isReadOnly()
                    widget.setReadOnly(True)
                elif isinstance(widget, _CONTROLS) or (
                    isinstance(widget, QAbstractItemView) and not isinstance(widget, _BROWSED)
                ):
                    self._enabled[widget] = not widget.testAttribute(Qt.WA_ForceDisabled)
                    widget.setEnabled(False)
                    widget.installEventFilter(self)
        finally:
            self._locking = False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        if (
            self.locked
            and not self._locking
            and event.type() == QEvent.EnabledChange
            and watched in self._enabled
            and watched.isEnabled()
        ):
            # The page turned it on: remembered so, kept off for now.
            self._enabled[watched] = True
            self._locking = True
            try:
                watched.setEnabled(False)
            finally:
                self._locking = False
        return False

    def _unlock(self) -> None:
        self._locking = True
        try:
            # A widget the page dropped meanwhile (a skills row) is gone.
            for widget, enabled in self._enabled.items():
                if isValid(widget):
                    widget.removeEventFilter(self)
                    widget.setEnabled(enabled)
            for widget, read_only in self._read_only.items():
                if isValid(widget):
                    widget.setReadOnly(read_only)
        finally:
            self._locking = False
        self._enabled.clear()
        self._read_only.clear()
