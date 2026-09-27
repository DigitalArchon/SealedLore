"""Filling text fields so they read from the start.

`QLineEdit.setText` (and the constructor) leaves the cursor at the end, so a
value longer than its field showed only its tail: Scene → Location read
"e tree line, within sight of the Hellsville watchtower", Style's combos
"– grounded and sensory, not sparse". Fields filled from story data go
through here.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFrame,
    QLineEdit,
    QProxyStyle,
    QScrollArea,
    QStackedWidget,
    QWidget,
)


def set_field_text(field: QLineEdit | QComboBox, text: str) -> None:
    """Set a line edit's text, or an editable combo's, showing its start."""
    if isinstance(field, QComboBox):
        field.setCurrentText(text)
        edit = field.lineEdit()
    else:
        field.setText(text)
        edit = field
    if edit is not None:
        edit.setCursorPosition(0)


def line_edit(text: str = "") -> QLineEdit:
    """A line edit holding `text`, showing its start."""
    edit = QLineEdit(text)
    edit.setCursorPosition(0)
    return edit


class FormScroll(QScrollArea):
    """A form that scrolls, and asks for the width its fields need.

    A plain `QScrollArea` hides its content's minimum width, so the dock
    around it could shrink below what the form needs: at the default window
    size the Cast form ran 46px past the inspector's edge, cut off behind a
    horizontal scrollbar. Reporting it keeps the inspector wide enough, and
    only vertical scrolling is left.
    """

    def __init__(self, content: QWidget | None = None) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        if content is not None:
            self.setWidget(content)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        hint = super().minimumSizeHint()
        content = self.widget()
        if content is None:
            return hint
        needed = (
            content.minimumSizeHint().width()
            + self.verticalScrollBar().sizeHint().width()
            + 2 * self.frameWidth()
        )
        return QSize(max(hint.width(), needed), hint.height())


class PageStack(QStackedWidget):
    """A stack whose height only the page showing sets.

    A `QStackedWidget` asks for its tallest page's minimum: the story map's
    header and hint held the transcript 100px taller than it needs, which
    under a large text size was the difference between the window fitting a
    768px screen or not. The width stays the widest page's, so switching
    pages never moves the docks.
    """

    def __init__(self) -> None:
        super().__init__()
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        hint = super().minimumSizeHint()
        page = self.currentWidget()
        if page is None:
            return hint
        return QSize(hint.width(), page.minimumSizeHint().height())


class WheelGuard(QObject):
    """The mouse wheel scrolls a page, not the control under the pointer.

    Over a combo or spin box in a scrolling page, the wheel used to change
    the value instead: wheeling down the Style page stepped Person, Tense and
    Pacing through their choices, and those are saved and sent to the
    storyteller. A control takes the wheel only once it has focus (clicked
    or tabbed into); otherwise the wheel goes to the page's scrollbar.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        if (
            event.type() == QEvent.Wheel
            and isinstance(watched, QComboBox | QAbstractSpinBox)
            and not watched.hasFocus()
        ):
            area = _scroll_area_of(watched)
            if area is not None:
                QApplication.sendEvent(area.verticalScrollBar(), event)
                return True
        return False


def _scroll_area_of(widget: QWidget) -> QAbstractScrollArea | None:
    """The nearest enclosing area that can scroll: a skill's tier combo sits
    in a table (itself a scroll area, with nothing to scroll) inside the
    Cast form, and it is the form that should move."""
    nearest = None
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            if parent.verticalScrollBar().maximum() > 0:
                return parent
            nearest = nearest or parent
        parent = parent.parentWidget()
    return nearest


class _GuardingStyle(QProxyStyle):
    """Puts the wheel guard on each combo and spin box as it is polished.

    The guard used to watch the whole application, so every event in the
    window went through Python: building 500 messages took 4.2s with it and
    2.9s without (tools/bench/transcript_bench.py). On the controls alone it
    costs nothing anywhere else. `polish` runs once per widget as it is
    created or restyled, which is when a combo or spin box can first take the
    wheel.
    """

    def __init__(self, base: str, guard: WheelGuard) -> None:
        super().__init__(base)
        self._guard = guard

    def polish(self, target):  # noqa: ANN001 - QStyle.polish takes a widget, palette or app
        if isinstance(target, QComboBox | QAbstractSpinBox):
            target.installEventFilter(self._guard)
        return super().polish(target)


_GUARD: WheelGuard | None = None
_STYLE: _GuardingStyle | None = None


def _base_style(app: QApplication) -> str:
    """The style under the stylesheet: the platform's, except on Windows.
    style.qss was drawn on Fusion; Windows' own styles (windowsvista,
    windows11) size their controls wider and draw some natively over the
    sheet, and the inspector opened 108px wider on GitHub's runner."""
    if sys.platform == "win32":
        return "fusion"
    return app.style().name()


def install_wheel_guard(app: QApplication) -> None:
    """Once per application, before any widget exists and before the
    stylesheet: `build_app` calls it first thing, and so does the tests'
    conftest. Swapping the application's style under live widgets crashed a
    test run every time (the old style is deleted while widgets still point
    at it). The main window calls it too, which does nothing by then."""
    global _GUARD, _STYLE
    if _GUARD is not None:
        return
    _GUARD = WheelGuard()
    # Under a stylesheet the app's style has no name to wrap: lift the sheet
    # for a moment to find the style underneath.
    sheet = app.styleSheet()
    if sheet:
        app.setStyleSheet("")
    _STYLE = _GuardingStyle(_base_style(app), _GUARD)
    app.setStyle(_STYLE)
    if sheet:
        app.setStyleSheet(sheet)
