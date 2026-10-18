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
  capped at 60-110px, two or three lines of a character's description.
- `adjustable_columns`: every column of a table or tree may be dragged wider
  or narrower. Qt allows no dragging of a column that sizes itself to its
  contents, so the columns fit their contents until the author drags one,
  and from then on are the author's.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QSize, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QPlainTextEdit,
    QSizePolicy,
    QTableView,
    QTreeView,
    QWidget,
)

# A dialog never opens past this share of the screen it is on.
MOST_OF_SCREEN = 0.92


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
    what is written up to `most`, then scrolling. With `fill` it also takes
    whatever height its layout has to give (a dialog's main field)."""

    def __init__(
        self, text: str = "", *, lines: int = 3, most: int = 16, fill: bool = False
    ) -> None:
        super().__init__(text)
        self._lines = max(1, lines)
        self._most = max(self._lines, most)
        vertical = QSizePolicy.Expanding if fill else QSizePolicy.Preferred
        self.setSizePolicy(QSizePolicy.Expanding, vertical)
        self.document().documentLayout().documentSizeChanged.connect(self._grown)

    def _height(self, lines: int) -> int:
        margins = self.contentsMargins()
        document = self.document().documentMargin()
        return int(
            lines * self.fontMetrics().lineSpacing()
            + 2 * document
            + margins.top()
            + margins.bottom()
            + 2
        )

    def _wrapped_lines(self) -> int:
        # A plain text document's layout is measured in lines, wrapped ones
        # counted (QPlainTextDocumentLayout).
        return int(self.document().documentLayout().documentSize().height())

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        lines = min(max(self._wrapped_lines(), self._lines), self._most)
        return QSize(super().sizeHint().width(), self._height(lines))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(super().minimumSizeHint().width(), self._height(self._lines))

    def _grown(self, *_args) -> None:
        self.updateGeometry()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        # A new width wraps the text into more or fewer lines.
        if event.oldSize().width() != event.size().width():
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
