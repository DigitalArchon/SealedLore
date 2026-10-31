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
"""

from __future__ import annotations

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
    than `MOST_OF_SCREEN` of the screen."""
    anchor = widget.parentWidget() or widget
    screen = anchor.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        return
    room = screen.availableGeometry().size()
    need = widget.minimumSizeHint().expandedTo(widget.minimumSize())
    if at_least is not None:
        need = need.expandedTo(at_least)
    wide = max(int(room.width() * width), need.width())
    tall = max(int(room.height() * height), need.height())
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
