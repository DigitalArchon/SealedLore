"""Room to read: dialogs that open as large as they need, text boxes as tall
as their text, and table columns the author can drag.

The author (Oct 18 2026): "Some windows like Settings open too small by
default, so you can't see everything clearly", a world written in a box that
grew wider with the window but never taller, and "the ability to clearly see
all text and adjust column widths or field sizes" wherever possible.

- `size_to_screen`: a dialog opens at a share of the screen it is on, never
  less than its layout needs and never past the screen. Fixed pixel sizes
  were chosen for a 1366px laptop; 1080p is the floor now.
- `GrowingText`: a plain text box that starts a few lines tall and grows with
  what is written in it, up to a limit, then scrolls; one that is a page's
  main field (`fill`) also takes the height the window gives it. Boxes were
  capped at 60-110px, two or three lines of a character's description. In a
  page that scrolls, the limit is most of the page's view (the author, Oct 31
  2026: "When editing any text, the size of text box is fixed, and too small
  for large texts").
- `adjustable_columns`: every column of a table or tree may be dragged wider
  or narrower. Qt allows no dragging of a column that sizes itself to its
  contents, so the columns fit their contents until the author drags one,
  and from then on are the author's.
- `typed_texts`, `entered`, `may_discard` and `enter_only_on_buttons`: what
  a dialog holds, so Cancel and Esc ask before it is lost, and an Enter that
  never saves a whole dialog by mistake (the window's review, Nov 2026).
"""

from __future__ import annotations

from collections.abc import Sequence
from math import ceil

from PySide6.QtCore import QObject, QSize, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QHeaderView,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QTreeView,
    QWidget,
)

# A dialog never opens past this share of the screen it is on.
MOST_OF_SCREEN = 0.92
# A text box in a page that scrolls grows to this share of the page's view.
MOST_OF_VIEW = 0.8


def size_to_screen(
    widget: QWidget, width: float, height: float, *, at_least: QSize | None = None
) -> None:
    """Resize `widget` to `width` × `height` of the available screen (shares,
    0-1), but no smaller than its layout needs or `at_least`, and no larger
    than `MOST_OF_SCREEN` of the screen. Shares and `at_least` grow with the
    text (View → Text size): at 150% Settings opened at its 100% size and its
    fields cut the model ids short."""
    from sealedlore.gui import text_size

    anchor = widget.parentWidget() or widget
    screen = anchor.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        return
    room = screen.availableGeometry().size()
    factor = max(1.0, text_size.scale())
    need = widget.minimumSizeHint().expandedTo(widget.minimumSize())
    if at_least is not None:
        need = need.expandedTo(at_least * factor)
    wide = max(int(room.width() * width * factor), need.width())
    tall = max(int(room.height() * height * factor), need.height())
    widget.resize(
        min(wide, int(room.width() * MOST_OF_SCREEN)),
        min(tall, int(room.height() * MOST_OF_SCREEN)),
    )


class GrowingText(QPlainTextEdit):
    """A text box as tall as its text: at least `lines` lines, growing with
    what is written up to `most`, or in a page that scrolls up to most of its
    view, then scrolling. With `fill` it also takes whatever height its layout
    has to give (a dialog's main field)."""

    def __init__(
        self, text: str = "", *, lines: int = 3, most: int = 16, fill: bool = False
    ) -> None:
        super().__init__(text)
        # Tab goes on to the next field, as in every other box of a form; a
        # tab character in a card or a message is never what was meant.
        self.setTabChangesFocus(True)
        self._lines = max(1, lines)
        self._most = max(self._lines, most)
        self._measuring = False
        # What the box is taller than its view of the text: the frame and the
        # stylesheet's padding, which no margin reports. Known once the box
        # has been laid out (`resizeEvent`).
        self._chrome = 2 * self.frameWidth()
        vertical = QSizePolicy.Expanding if fill else QSizePolicy.Preferred
        self.setSizePolicy(QSizePolicy.Expanding, vertical)
        self.document().documentLayout().documentSizeChanged.connect(self._grown)
        # Grown past the bottom of a page that scrolls, the line being typed
        # is off the page: once the page has laid the box out, bring it back.
        self._follow = QTimer(self)
        self._follow.setSingleShot(True)
        self._follow.setInterval(0)
        self._follow.timeout.connect(self._show_cursor)
        self.cursorPositionChanged.connect(self._follow.start)

    def _scroll_area(self) -> QAbstractScrollArea | None:
        """The page around this box that scrolls, if it is in one."""
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, QAbstractScrollArea):
                return widget
            widget = None if widget.isWindow() else widget.parentWidget()
        return None

    def _most_lines(self) -> int:
        area = self._scroll_area()
        if area is None:
            return self._most
        # Past the view, Save would be off the page while the text is scrolled
        # to: `most` alone made a box taller than a small window's view.
        fits = area.viewport().height() * MOST_OF_VIEW / self.fontMetrics().lineSpacing()
        return max(self._lines, int(fits))

    def _text_height(self, most: float) -> float:
        """How tall the text is, up to `most`. A block at a time, each laid
        out first: a plain text document's own line count has a block it
        hasn't painted as one line, so a text of several paragraphs measured
        a line a paragraph, and grew by steps as it painted. In the document's
        own heights: a font's lines aren't always a whole number of pixels."""
        layout = self.document().documentLayout()
        tall = 0.0
        block = self.document().begin()
        self._measuring = True
        try:
            while block.isValid() and tall < most:
                layout.ensureBlockLayout(block)
                tall += layout.blockBoundingRect(block).height()
                block = block.next()
        finally:
            self._measuring = False
        return tall

    def _lines_tall(self, lines: int) -> float:
        return lines * self.fontMetrics().lineSpacing() + self.document().documentMargin()

    def _height(self, tall: float) -> int:
        # The document's height holds one of its margins. One pixel more:
        # QPlainTextEdit counts a line as out of view a pixel early, and a
        # text that fitted got a scroll bar for its last line.
        return ceil(tall + self.document().documentMargin() + self._chrome + 1)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        most = self._lines_tall(self._most_lines())
        tall = min(max(self._text_height(most), self._lines_tall(self._lines)), most)
        return QSize(super().sizeHint().width(), self._height(tall))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        # A resizable QScrollArea sizes its page from the minimum hints, so in
        # a page that scrolls the minimum is the text's height: the page has
        # the room, and once it was full every box was squeezed to `lines`.
        width = super().minimumSizeHint().width()
        if self._scroll_area() is not None:
            return QSize(width, self.sizeHint().height())
        return QSize(width, self._height(self._lines_tall(self._lines)))

    def _grown(self, *_args) -> None:
        if not self._measuring:
            self.updateGeometry()

    def _show_cursor(self) -> None:
        area = self._scroll_area()
        if not self.hasFocus() or not isinstance(area, QScrollArea) or area.widget() is None:
            return
        cursor = self.cursorRect()
        at = self.viewport().mapTo(area.widget(), cursor.center())
        area.ensureVisible(at.x(), at.y(), 0, cursor.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        chrome = self.height() - self.viewport().height()
        # A new width wraps the text into more or fewer lines.
        if chrome != self._chrome or event.oldSize().width() != event.size().width():
            self._chrome = chrome
            self.updateGeometry()


class _ColumnFitter(QObject):
    """Fits a view's columns to their contents whenever its rows change,
    until the author drags one (see the module)."""

    def __init__(self, view: QTreeView | QTableView, stretch: int | None, widest: int) -> None:
        super().__init__(view)
        self._view = view
        self._stretch = stretch
        self._widest = widest
        self._header = _header(view)
        self._fitting = False
        self.dragged = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.fit)
        model = view.model()
        for signal in (model.rowsInserted, model.modelReset, model.layoutChanged):
            signal.connect(self._later)
        model.dataChanged.connect(self._later)
        self._header.sectionResized.connect(self._resized)

    def _later(self, *_args) -> None:
        if not self.dragged:
            self._timer.start(0)

    def _resized(self, *_args) -> None:
        # The author dragging a column's edge: a section resized with the
        # button down over the header. A fit is `_fitting`; the stretch
        # column follows the window with no button down on the header.
        if self._fitting:
            return
        pressed = QGuiApplication.mouseButtons() & Qt.LeftButton
        if pressed and self._header.underMouse():
            self.dragged = True

    def fit(self) -> None:
        if self.dragged:
            return
        self._fitting = True
        try:
            for column in range(self._header.count()):
                if column == self._stretch or self._header.isSectionHidden(column):
                    continue
                self._view.resizeColumnToContents(column)
                if self._header.sectionSize(column) > self._widest:
                    self._header.resizeSection(column, self._widest)
        finally:
            self._fitting = False


def _header(view: QAbstractItemView) -> QHeaderView:
    return view.horizontalHeader() if isinstance(view, QTableView) else view.header()


def adjustable_columns(
    view: QTreeView | QTableView, *, stretch: int | None = None, widest: int = 420
) -> _ColumnFitter:
    """Every column draggable; each fits its contents (no wider than
    `widest`) until the author drags one; `stretch` takes the room left."""
    header = _header(view)
    header.setStretchLastSection(False)
    for column in range(header.count()):
        mode = QHeaderView.Stretch if column == stretch else QHeaderView.Interactive
        header.setSectionResizeMode(column, mode)
    header.setMinimumSectionSize(40)
    fitter = _ColumnFitter(view, stretch, widest)
    fitter.fit()
    return fitter


def typed_texts(widget: QWidget) -> list[str]:
    """What a dialog's text boxes hold now: to tell, on Cancel, whether
    anything typed would be lost. One `findChildren` a type: given a union of
    them it raised in PySide6, so the dialogs using this never asked."""
    from PySide6.QtWidgets import QLineEdit

    lines = [child.text() for child in widget.findChildren(QLineEdit) if not child.isReadOnly()]
    boxes = [
        child.toPlainText()
        for child in widget.findChildren(QPlainTextEdit)
        if not child.isReadOnly()
    ]
    return lines + boxes


def entered(widget: QWidget, *, leaving_out: Sequence[QWidget] = ()) -> dict[QWidget, object]:
    """Everything set in a dialog's fields now, by field: what each text box
    holds, which entry each list shows and whether each box is ticked (a
    check box or a radio button: a tool button toggled is a fold opened, not
    a setting). By field, not in a list: Qt lists a page's fields in another
    order once another page has been shown. `leaving_out`: fields (and what
    is in them) whose changes the dialog tells its own way."""
    from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QRadioButton

    def counted(child: QWidget) -> bool:
        return not any(field is child or field.isAncestorOf(child) for field in leaving_out)

    found: dict[QWidget, object] = {}
    for child in widget.findChildren(QLineEdit):
        if not child.isReadOnly() and counted(child):
            found[child] = child.text()
    for child in widget.findChildren(QPlainTextEdit):
        if not child.isReadOnly() and counted(child):
            found[child] = child.toPlainText()
    for child in widget.findChildren(QComboBox):
        # An editable list's text is its line's, above.
        if not child.isEditable() and counted(child):
            found[child] = child.currentIndex()
    for kind in (QCheckBox, QRadioButton):
        for child in widget.findChildren(kind):
            if counted(child):
                found[child] = child.isChecked()
    return found


def may_discard(dialog: QWidget, before: list | None, now: list | None = None) -> bool:
    """Cancel or Esc: True when nothing typed would be lost, or the author
    says to discard it. `before`: what the boxes held once the dialog was
    filled in (`typed_texts`); `now`: what to compare it with, when the
    dialog tells its changes its own way (`typed_texts` by default)."""
    from PySide6.QtWidgets import QMessageBox

    if before is None or (typed_texts(dialog) if now is None else now) == before:
        return True
    answer = QMessageBox.question(
        dialog,
        "Discard what you wrote?",
        "Close without keeping what you typed here?",
        QMessageBox.Discard | QMessageBox.Cancel,
        QMessageBox.Cancel,
    )
    return answer == QMessageBox.Discard


def enter_only_on_buttons(dialog: QWidget, event) -> bool:
    """A dialog's Enter, for one that must never be saved by Enter in a
    field: True when it was taken here. It presses only a push button that
    has the keyboard's focus, as Space does, and does nothing anywhere else.
    Handed on to QDialog, Enter pressed the default button, which the button
    box makes Save again when the dialog shows: Enter on a focused Cancel, a
    check box, a radio button or Browse… saved the dialog and closed it."""
    from PySide6.QtWidgets import QPushButton

    if event.key() not in (Qt.Key_Return, Qt.Key_Enter):
        return False
    event.accept()
    button = dialog.focusWidget()
    if isinstance(button, QPushButton) and button.isEnabled() and button.isVisible():
        button.click()
    return True
