"""The cast board (View → Cast board…, or Cast board… on the Cast page): every
character's pictures in the whole window. Everyone, each with their pictures
in a row; and a character's sheet, their looks down the side and their
expressions or poses across (engine/look_sheet.py), each gap blank but for
the words it would be drawn with, ready to be drawn.

The author (Nov 3 2026): managing many pictures per person should be
seamless and visually engaging, all the characters and all their pictures
for every emotion and dress or pose type in view, the missing ones easy to
fill in, types of their own easy to add. The page shows and edits a
character's sheet; drawing, saving and the story are the window's
(gui/window_board.py).

Nov 7 2026, the author's look over it: the rows and columns stay where they
are and move only when the author moves them (dragged by their headings, or
Move up / Move down); the tray of pictures set aside is always there, to put
one in while deciding; a gap no longer shows the picture it would be drawn
from, faded (it read as a picture there); headings wrap; and a heading's
words, the ones it is drawn with, show in its gaps.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QItemSelectionModel,
    QMimeData,
    QObject,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDrag,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionHeader,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.look_sheet import (
    DEFAULT_EXPRESSIONS,
    Gap,
    Sheet,
    add_label,
    gap_words,
    keep_order,
    label_key,
    move,
    relabel,
    remove_label,
    reorder,
    set_wording,
    sheet_for,
    split_wanted,
    wording,
)
from sealedlore.engine.looks import COMMON_EXPRESSIONS, COMMON_LOOKS, look_words
from sealedlore.engine.plot_md import PICTURE_SUFFIXES
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.gui import theme
from sealedlore.gui.ref_images import StoryPictures, pixmap_of
from sealedlore.gui.sheet_labels import HEADING_AND_WORDING, ask_sheet_label
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef

BOARD_LABEL = "Cast board"
SPOILERS_LABEL = "Show spoilers"
# Pictures only, for the template the story came from (window_board.py):
# "Keep for new stories…" read as keeping the characters themselves.
KEEP_LABEL = "Keep pictures for new stories…"
# The characters themselves, for any story (gui/save_characters.py).
SAVE_LABEL = "Save to Your characters…"
EVERYONE = "Everyone"
NO_LOOK = "No look named"
NO_EXPRESSION = "No expression named"
# A cell's width by the size chosen; its height is the portraits' 2:3.
CELL_WIDTHS = {"Small": 116, "Medium": 156, "Large": 216}
ASPECT = 1.5
ROW_HEADER = 150
CELL_ROLE = Qt.UserRole + 1
# A picture of a card, dragged within the board: its id.
PICTURE_MIME = "application/x-sealedlore-picture"
PEOPLE_ICON = QSize(40, 60)


def _characters(count: int) -> str:
    return f"{count} character{'' if count == 1 else 's'}"


def cover(pixmap: QPixmap, width: int, height: int) -> QPixmap:
    """The picture filling `width` by `height`, cut from the top (a face is
    near the top of a portrait), centred across."""
    scaled = pixmap.scaled(width, height, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x = max(0, (scaled.width() - width) // 2)
    y = max(0, min((scaled.height() - height) // 6, scaled.height() - height))
    return scaled.copy(x, y, width, height)


class Thumbs:
    """Pictures as the board shows them, each read once at each size, a few
    at a time: asked for one not read yet, the board draws its place empty
    and is told (`ready`) when there is more to show, so a cast of a
    hundred pictures never holds the window while its files are read."""

    PER_TICK = 3

    def __init__(self, pictures: StoryPictures, ready: Callable[[], None], owner: QObject) -> None:
        self._pictures = pictures
        self._ready = ready
        self._cache: dict[tuple[str, int, int], QPixmap] = {}
        self._queue: list[tuple[str, int, int]] = []
        # The owner's: a timer of its own outlived the page whose pictures it
        # read, and told the page's deleted widgets of them.
        self._timer = QTimer(owner)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._work)

    def forget(self) -> None:
        self._cache.clear()
        self._queue.clear()
        self._timer.stop()

    def forget_file(self, file: str) -> None:
        """One file read afresh next time: a card's file can come back under
        the name of one taken off (a picture's name is its own for good)."""
        for key in [key for key in self._cache if key[0] == file]:
            del self._cache[key]

    def get(self, file: str, width: int, height: int) -> QPixmap | None:
        key = (file, width, height)
        pixmap = self._cache.get(key)
        if pixmap is None and key not in self._queue:
            self._queue.append(key)
            self._timer.start()
        return pixmap

    def now(self, file: str, width: int, height: int) -> QPixmap:
        """Read at once (a small icon the list needs whole)."""
        key = (file, width, height)
        if key not in self._cache:
            self._cache[key] = self._read(*key)
        return self._cache[key]

    def _read(self, file: str, width: int, height: int) -> QPixmap:
        store = self._pictures()
        data = store.read(file) if store is not None and file else None
        pixmap = pixmap_of(data) if data else QPixmap()
        return QPixmap() if pixmap.isNull() else cover(pixmap, width, height)

    def _work(self) -> None:
        for _ in range(self.PER_TICK):
            if not self._queue:
                break
            key = self._queue.pop(0)
            self._cache[key] = self._read(*key)
        if not self._queue:
            self._timer.stop()
        self._ready()


@dataclass
class Cell:
    look: str
    expression: str
    refs: list[ImageRef] = field(default_factory=list)
    gap: Gap | None = None
    portrait_id: str | None = None
    shown_id: str | None = None
    drawing: int = 0
    front: int = 0  # which of `refs` is shown, when there are several
    # The words a gap would be drawn with: what its edit asks for.
    drawn_as: str = ""

    @property
    def ref(self) -> ImageRef | None:
        return self.refs[self.front % len(self.refs)] if self.refs else None

    def words(self) -> str:
        parts = [part for part in (self.look, self.expression) if part]
        return " · ".join(parts) or "no labels"


def scaled_font(font: QFont, factor: float) -> QFont:
    """`font` larger or smaller: the stylesheet sizes text in pixels (and
    Text size scales them), so a size in points may not be set."""
    scaled = QFont(font)
    if font.pixelSize() > 0:
        scaled.setPixelSize(max(7, round(font.pixelSize() * factor)))
    else:
        scaled.setPointSizeF(max(6.0, font.pointSizeF() * factor))
    return scaled


def _pill(painter: QPainter, rect: QRectF, text: str, background: QColor, colour: QColor) -> None:
    """A small rounded label at `rect`'s top left."""
    metrics = painter.fontMetrics()
    width = metrics.horizontalAdvance(text) + 12
    pill = QRectF(rect.left(), rect.top(), width, metrics.height() + 4)
    path = QPainterPath()
    path.addRoundedRect(pill, pill.height() / 2, pill.height() / 2)
    painter.fillPath(path, background)
    painter.setPen(colour)
    painter.drawText(pill, Qt.AlignCenter, text)


class SheetDelegate(QStyledItemDelegate):
    """Each cell drawn: its picture, or its gap with the picture it would be
    drawn from faded behind; what is in the story now, the portrait, a
    gap the story asked for, one being drawn."""

    def __init__(self, thumbs: Thumbs, parent=None) -> None:
        super().__init__(parent)
        self.thumbs = thumbs

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: D102
        cell: Cell | None = index.data(CELL_ROLE)
        if cell is None:
            return
        selected = bool(option.state & QStyle.State_Selected)
        rect = QRectF(option.rect.adjusted(5, 5, -5, -5))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        small = scaled_font(option.font, 0.85)
        painter.setFont(small)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        painter.fillPath(path, theme.colour("panel"))
        size = rect.size().toSize()
        ref = cell.ref
        if ref is not None:
            pixmap = self.thumbs.get(ref.file, size.width(), size.height())
            if pixmap is not None and not pixmap.isNull():
                painter.setClipPath(path)
                painter.drawPixmap(rect.topLeft().toPoint(), pixmap)
                painter.setClipping(False)
            shown = cell.shown_id is not None and cell.shown_id == ref.id
            colour = theme.colour("accent" if shown else "primary" if selected else "border")
            painter.setPen(QPen(colour, 3 if shown or selected else 1))
            painter.drawPath(path)
            inner = rect.adjusted(6, 6, -6, -6)
            dim = theme.colour("window", 210)
            if cell.portrait_id == ref.id:
                _pill(painter, inner, "Portrait", dim, theme.colour("text_bright"))
            if len(cell.refs) > 1:
                count = f"{cell.front % len(cell.refs) + 1} of {len(cell.refs)}"
                width = painter.fontMetrics().horizontalAdvance(count) + 12
                _pill(
                    painter,
                    QRectF(inner.right() - width, inner.top(), width, 0),
                    count,
                    dim,
                    theme.colour("text_bright"),
                )
            if shown:
                bottom = QRectF(
                    inner.left(), inner.bottom() - painter.fontMetrics().height() - 4, 0, 0
                )
                _pill(
                    painter,
                    bottom,
                    "In the story now",
                    theme.colour("accent"),
                    theme.colour("window"),
                )
        else:
            # Blank: the picture it would be drawn from, faded behind it, read
            # as a picture already there (the author, Nov 7 2026).
            gap = cell.gap
            wanted = gap.wanted if gap is not None else 0
            colour = theme.colour(
                "primary" if selected else "warning" if wanted else "control_border"
            )
            pen = QPen(colour, 2.5 if selected or wanted else 1.2, Qt.DashLine)
            painter.setPen(pen)
            painter.drawPath(path)
            words = rect.adjusted(8, 8, -8, -8)
            if cell.drawing or gap is None or (gap.source is None and not gap.after):
                text = "Drawing…" if cell.drawing else "Give them a\npicture first"
                painter.setPen(theme.colour("text_soft"))
                painter.drawText(words, Qt.AlignCenter | Qt.TextWordWrap, text)
            else:
                # A large "+" over what it would be.
                plus = scaled_font(option.font, 2.6)
                plus.setWeight(QFont.Light)
                painter.setFont(plus)
                painter.setPen(theme.colour("text_bright" if selected else "text_soft"))
                middle = words.center().y()
                painter.drawText(
                    QRectF(words.left(), middle - 44, words.width(), 44),
                    Qt.AlignHCenter | Qt.AlignBottom,
                    "+",
                )
                # The words it is drawn with: its heading is over the column.
                painter.setFont(small)
                label = cell.drawn_as or "as it is"
                if gap.source is None:
                    # The rest of a new look's row: drawn from its first.
                    label += f"\n(after the {gap.after or 'first'})"
                box = QRectF(words.left(), middle + 2, words.width(), words.bottom() - middle - 2)
                if wanted:
                    box.setBottom(box.bottom() - painter.fontMetrics().height() - 6)
                painter.drawText(box, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, label)
            if wanted:
                inner = rect.adjusted(6, 6, -6, -6)
                text = "Asked for" if wanted == 1 else f"Asked for ×{wanted}"
                bottom = QRectF(
                    inner.left(), inner.bottom() - painter.fontMetrics().height() - 4, 0, 0
                )
                _pill(painter, bottom, text, theme.colour("warning"), theme.colour("window"))
            if selected:
                tick = QRectF(rect.right() - 26, rect.top() + 6, 20, 20)
                circle = QPainterPath()
                circle.addEllipse(tick)
                painter.fillPath(circle, theme.colour("primary"))
                painter.setPen(QPen(theme.colour("primary_text"), 2))
                painter.drawLine(
                    tick.left() + 5, tick.center().y(), tick.center().x() - 1, tick.bottom() - 6
                )
                painter.drawLine(
                    tick.center().x() - 1, tick.bottom() - 6, tick.right() - 4, tick.top() + 6
                )
        painter.restore()


def picture_files(mime: QMimeData) -> list[Path]:
    """The pictures among files dragged from the desktop."""
    paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]
    return [path for path in paths if path.suffix.lower() in PICTURE_SUFFIXES and path.is_file()]


def _picture_drag(source: QWidget, ref_id: str, pixmap: QPixmap | None) -> None:
    """A picture of the card picked up: its id in a type of the app's own."""
    mime = QMimeData()
    mime.setData(PICTURE_MIME, QByteArray(ref_id.encode()))
    drag = QDrag(source)
    drag.setMimeData(mime)
    if pixmap is not None and not pixmap.isNull():
        drag.setPixmap(pixmap.scaledToWidth(72, Qt.SmoothTransformation))
    drag.exec(Qt.MoveAction)


class WrappingHeader(QHeaderView):
    """A sheet's headings, wrapped onto as many lines as they take (a look's
    name was cut short at the row's width), and dragged to move a row or a
    column (`moved`, once the drag is done)."""

    # (the labels in their new order, by logical index)
    moved = Signal(list)
    PADDING_X, PADDING_Y = 8, 6

    def __init__(self, orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        self.setSectionsMovable(True)
        self.setSectionsClickable(True)
        self.setHighlightSections(True)
        self.sectionMoved.connect(self._section_moved)

    def _text(self, logical: int) -> str:
        model = self.model()
        if model is None:
            return ""
        value = model.headerData(logical, self.orientation(), Qt.DisplayRole)
        return str(value) if value is not None else ""

    def paintSection(self, painter: QPainter, rect, logical: int) -> None:  # noqa: N802
        if not rect.isValid():
            return
        option = QStyleOptionHeader()
        self.initStyleOptionForIndex(option, logical)
        option.rect = rect
        option.text = ""
        painter.save()
        # The section as the stylesheet draws it, then its words, wrapped.
        self.style().drawControl(QStyle.CE_HeaderSection, option, painter, self)
        # Brighter over a chosen cell's heading, or under the pointer (the
        # stylesheet's hover colour).
        bright = bool(option.state & (QStyle.State_On | QStyle.State_MouseOver))
        painter.setPen(theme.colour("text_heading" if bright else "text_dim"))
        painter.setFont(self.font())
        inner = rect.adjusted(self.PADDING_X, self.PADDING_Y, -self.PADDING_X, -self.PADDING_Y)
        align = (
            Qt.AlignRight | Qt.AlignVCenter
            if self.orientation() == Qt.Vertical
            else Qt.AlignHCenter | Qt.AlignVCenter
        )
        painter.drawText(inner, align | Qt.TextWordWrap, self._text(logical))
        painter.restore()

    def wrapped_height(self) -> int:
        """The height its headings take wrapped at their widths (a
        horizontal header's)."""
        metrics = self.fontMetrics()
        tallest = metrics.height()
        for logical in range(self.count()):
            width = max(10, self.sectionSize(logical) - 2 * self.PADDING_X)
            bounds = metrics.boundingRect(
                0, 0, width, 10_000, Qt.AlignHCenter | Qt.TextWordWrap, self._text(logical)
            )
            tallest = max(tallest, bounds.height())
        return tallest + 2 * self.PADDING_Y + 2

    def _section_moved(self, _logical: int, _old: int, _new: int) -> None:
        order = [self.logicalIndex(visual) for visual in range(self.count())]
        # After the drag: the sheet is rebuilt in its new order, which this
        # header must not be in the middle of.
        QTimer.singleShot(0, self, lambda: self.moved.emit(order))


class SheetTable(QTableWidget):
    """A character's sheet: looks down the side, expressions across; both
    stay in view as the cells scroll. Drag across gaps, or Ctrl-click them,
    to choose them; drag a picture to another place to relabel it, or drop
    pictures from the desktop on a place to add them there. Enter sees a
    picture whole or draws the chosen gaps, Delete removes a picture, F2 is
    its labels, the menu key the menu."""

    # (picture id, row, column): a picture of theirs dropped on a place.
    picture_dropped = Signal(str, int, int)
    # (files, row, column): pictures from the desktop dropped on a place.
    files_dropped = Signal(list, int, int)
    # (row, column): Enter.
    activated = Signal(int, int)
    # (picture id): Delete, F2.
    remove_pressed = Signal(str)
    labels_pressed = Signal(str)

    def __init__(self, thumbs: Thumbs, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("castSheet")
        self._thumbs = thumbs
        self.setItemDelegate(SheetDelegate(thumbs, self))
        # A picture drags itself (its cell's flags say so); a gap, pressed
        # and dragged, chooses the gaps it crosses.
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDropIndicatorShown(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setShowGrid(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setHorizontalHeader(WrappingHeader(Qt.Horizontal, self))
        self.setVerticalHeader(WrappingHeader(Qt.Vertical, self))
        for header in (self.horizontalHeader(), self.verticalHeader()):
            header.setSectionResizeMode(header.ResizeMode.Fixed)
            header.setContextMenuPolicy(Qt.CustomContextMenu)
        self.verticalHeader().setFixedWidth(ROW_HEADER)
        self.verticalHeader().setDefaultAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def set_cell_size(self, width: int) -> None:
        self.horizontalHeader().setDefaultSectionSize(width)
        self.verticalHeader().setDefaultSectionSize(int(width * ASPECT))
        for column in range(self.columnCount()):
            self.setColumnWidth(column, width)
        for row in range(self.rowCount()):
            self.setRowHeight(row, int(width * ASPECT))
        self.fit_headings()

    def fit_headings(self) -> None:
        """The headings across as tall as their wrapped words."""
        header = self.horizontalHeader()
        header.setFixedHeight(header.wrapped_height())

    def cell_at(self, row: int, column: int) -> Cell | None:
        item = self.item(row, column)
        return item.data(CELL_ROLE) if item is not None else None

    # --- dragging ---------------------------------------------------------------------

    def startDrag(self, _actions) -> None:  # noqa: N802 - Qt naming
        index = self.currentIndex()
        cell = self.cell_at(index.row(), index.column()) if index.isValid() else None
        if cell is None or cell.ref is None:
            return
        rect = self.visualRect(index)
        pixmap = self._thumbs.get(cell.ref.file, rect.width() - 10, rect.height() - 10)
        _picture_drag(self, cell.ref.id, pixmap)

    def _accepts(self, event) -> bool:
        mime = event.mimeData()
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return False
        return mime.hasFormat(PICTURE_MIME) or bool(picture_files(mime))

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        mime = event.mimeData()
        if mime.hasFormat(PICTURE_MIME) or picture_files(mime):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._accepts(event):
            index = self.indexAt(event.position().toPoint())
            self.setCurrentIndex(index)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Handled here whole: the table's own drop would move its items."""
        index = self.indexAt(event.position().toPoint())
        mime = event.mimeData()
        if not index.isValid():
            event.ignore()
            return
        if mime.hasFormat(PICTURE_MIME):
            ref_id = bytes(mime.data(PICTURE_MIME)).decode()
            self.picture_dropped.emit(ref_id, index.row(), index.column())
        elif files := picture_files(mime):
            self.files_dropped.emit([str(path) for path in files], index.row(), index.column())
        else:
            event.ignore()
            return
        event.setDropAction(Qt.CopyAction if not mime.hasFormat(PICTURE_MIME) else Qt.MoveAction)
        event.accept()

    # --- keys -------------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        index = self.currentIndex()
        cell = self.cell_at(index.row(), index.column()) if index.isValid() else None
        key = event.key()
        if cell is not None and key in (Qt.Key_Return, Qt.Key_Enter):
            self.activated.emit(index.row(), index.column())
            return
        if cell is not None and cell.ref is not None and key == Qt.Key_Delete:
            self.remove_pressed.emit(cell.ref.id)
            return
        if cell is not None and cell.ref is not None and key == Qt.Key_F2:
            self.labels_pressed.emit(cell.ref.id)
            return
        if key == Qt.Key_Menu or (key == Qt.Key_F10 and event.modifiers() & Qt.ShiftModifier):
            if index.isValid():
                self.customContextMenuRequested.emit(self.visualRect(index).center())
            return
        super().keyPressEvent(event)


class Tray(QListWidget):
    """Pictures set aside, with no labels: dragged onto a place, they take its
    labels; a picture of the sheet dropped here loses its own, and is kept
    here while the author decides. Always there under a sheet, empty too (it
    used to show only while something was in it, so with every picture in
    place there was nowhere to put one aside)."""

    # (picture id): a picture of the sheet dropped here.
    picture_dropped = Signal(str)
    # (files): pictures from the desktop dropped here, to add unlabelled.
    files_dropped = Signal(list)
    EMPTY = (
        "Nothing set aside. Drag a picture here to take it off the sheet while you decide "
        "where it goes, or drop pictures from your computer"
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("refImageList")
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListWidget.Static)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)

    def startDrag(self, _actions) -> None:  # noqa: N802 - Qt naming
        item = self.currentItem()
        if item is not None:
            _picture_drag(self, item.data(Qt.UserRole), item.icon().pixmap(72, 108))

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        mime = event.mimeData()
        if (mime.hasFormat(PICTURE_MIME) and event.source() is not self) or picture_files(mime):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        mime = event.mimeData()
        if mime.hasFormat(PICTURE_MIME) and event.source() is not self:
            self.picture_dropped.emit(bytes(mime.data(PICTURE_MIME)).decode())
            event.setDropAction(Qt.MoveAction)
        elif files := picture_files(mime):
            self.files_dropped.emit([str(path) for path in files])
            event.setDropAction(Qt.CopyAction)
        else:
            event.ignore()
            return
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        if self.count():
            return
        painter = QPainter(self.viewport())
        painter.setPen(theme.colour("text_soft"))
        painter.drawText(
            self.viewport().rect().adjusted(16, 8, -16, -8),
            Qt.AlignCenter | Qt.TextWordWrap,
            self.EMPTY,
        )
        painter.end()


TILE_ROLE = Qt.UserRole + 2


class TileDelegate(QStyledItemDelegate):
    """A picture on the overview: the picture filling its tile, its labels
    over a shade at the bottom, the portrait marked."""

    def __init__(self, tile: QSize, thumbs: Thumbs, parent=None) -> None:
        super().__init__(parent)
        self.tile = tile
        self.thumbs = thumbs

    def sizeHint(self, _option, _index) -> QSize:  # noqa: N802 - Qt naming
        return self.tile + QSize(10, 10)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: D102
        ref, portrait = index.data(TILE_ROLE) or (None, False)
        rect = QRectF(option.rect.adjusted(5, 5, -5, -5))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        painter.fillPath(path, theme.colour("panel"))
        painter.setClipPath(path)
        if ref is not None:
            pixmap = self.thumbs.get(ref.file, self.tile.width(), self.tile.height())
            if pixmap is not None and not pixmap.isNull():
                painter.drawPixmap(rect.topLeft().toPoint(), pixmap)
        lines = [part for part in (ref.look, ref.expression) if part] if ref else ["No pictures"]
        if ref is not None and not lines and ref.caption:
            lines = [ref.caption]
        if lines:
            font = scaled_font(option.font, 0.85)
            painter.setFont(font)
            band = (painter.fontMetrics().height() + 2) * len(lines) + 10
            shade = QRectF(rect.left(), rect.bottom() - band, rect.width(), band)
            painter.fillRect(shade, theme.colour("window", 190))
            painter.setPen(theme.colour("text_bright"))
            painter.drawText(
                shade.adjusted(6, 4, -6, -4),
                Qt.AlignHCenter | Qt.AlignVCenter,
                "\n".join(
                    painter.fontMetrics().elidedText(line, Qt.ElideRight, int(rect.width()) - 12)
                    for line in lines
                ),
            )
        painter.setClipping(False)
        if portrait:
            _pill(
                painter,
                rect.adjusted(6, 6, -6, -6),
                "Portrait",
                theme.colour("window", 210),
                theme.colour("text_bright"),
            )
        if option.state & QStyle.State_MouseOver:
            painter.setPen(QPen(theme.colour("primary"), 2))
            painter.drawPath(path)
        painter.restore()


class Strip(QListWidget):
    """One character's pictures on the overview, wrapping onto as many
    lines as they take: its height follows its width."""

    def __init__(self, tile: QSize, thumbs: Thumbs, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("boardStrip")
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setGridSize(tile + QSize(10, 10))
        self.setItemDelegate(TileDelegate(tile, thumbs, self))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        grid = self.gridSize()
        per_line = max(1, self.viewport().width() // max(1, grid.width()))
        lines = max(1, -(-self.count() // per_line))
        height = lines * grid.height() + 2 * self.frameWidth() + 6
        if self.height() != height:
            self.setFixedHeight(height)


class CastBoard(QWidget):
    """The page (see the module): the window fills it (`set_cards`) and acts
    on what it asks for; edits to a sheet's rows and columns it makes
    itself, on the card, and says so (`sheet_changed`)."""

    back_requested = Signal()
    # (what, card id, picture id): "whole", "show", "portrait", "labels",
    # "remove", "edit", "again", "set".
    picture_action = Signal(str, str, str)
    # (card id, the gaps to draw)
    fill_requested = Signal(str, object)
    # (card id, the story's words for a look it found missing)
    asked_requested = Signal(str, str)
    # (card id): a row, a column or a label changed here; the window saves.
    sheet_changed = Signal(str)
    # (card id, look, expression): pictures from files, labelled so.
    files_requested = Signal(str, str, str)
    # (card id, look, expression, files): pictures dropped from the desktop.
    files_dropped = Signal(str, str, str, list)
    # Show spoilers: the plot's hidden characters too (Config.show_plot_spoilers).
    spoilers_toggled = Signal(bool)
    # Keep everyone's pictures for new stories from the story's template.
    keep_requested = Signal()
    # (card id, "" for everyone shown): saved to Your characters.
    save_requested = Signal(str)
    # (card id, "" for everyone shown): pictures redrawn in a style.
    restyle_requested = Signal(str)
    # (card id): one chosen on the overview, to see their sheet.
    character_chosen = Signal(str)

    def __init__(self, pictures: StoryPictures, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("castBoard")
        self._thumbs = Thumbs(pictures, self._thumbs_ready, self)
        self._cards: list[Character] = []
        self._shown: dict[str, str] = {}
        self._wanted: dict[str, Mapping[str, int]] = {}
        self._drawing: dict[tuple[str, str, str], int] = {}
        self._begun = False
        self._can_change = True
        self._current: str | None = None
        self._sheet: Sheet | None = None
        self._fronts: dict[tuple[str, str, str], int] = {}
        self._hidden: set[str] = set()
        # Inside a page that has its own list of people, title and buttons
        # (Your characters, the story's cast board page): those are hidden.
        self._embedded = False
        self._template = ""
        # The prompt texts in force, for the usual words a heading is drawn with.
        self._texts: PromptTexts = DEFAULT_TEXTS
        # Whose sheet the table holds now.
        self._filled_for: str | None = None

        self.people = QListWidget()
        self.people.setObjectName("boardPeople")
        self.people.setIconSize(PEOPLE_ICON)
        self.people.setFixedWidth(240)
        self.people.setSpacing(2)
        self.people.currentItemChanged.connect(self._on_person)

        self.title = QLabel()
        self.title.setObjectName("boardTitle")
        self.stats = QLabel()
        self.stats.setObjectName("hintLabel")
        self.size_choice = QComboBox()
        self.size_choice.addItems(list(CELL_WIDTHS))
        self.size_choice.setCurrentText("Medium")
        self.size_choice.setToolTip("How large the pictures are")
        self.size_choice.currentTextChanged.connect(self._resize)
        self.add_look_button = QPushButton("Add a look…")
        self.add_look_button.setToolTip(
            "A row for a look they don't have yet (combat armour, formal clothes, injured): its "
            "first picture is drawn from one they have, and the rest of the row from that"
        )
        self.add_look_button.clicked.connect(self._add_look)
        self.add_expression_button = QToolButton()
        self.add_expression_button.setText("Add an expression…")
        self.add_expression_button.setToolTip(
            "A column for an expression or pose (laughing, crying, in a fighting stance)"
        )
        self.add_expression_button.setPopupMode(QToolButton.InstantPopup)
        expression_menu = QMenu(self.add_expression_button)
        self.for_one_action = expression_menu.addAction("For this character…")
        self.for_one_action.triggered.connect(lambda: self._add_expression(everyone=False))
        expression_menu.addAction("For everyone…").triggered.connect(
            lambda: self._add_expression(everyone=True)
        )
        self.add_expression_button.setMenu(expression_menu)
        self.files_button = QPushButton("Add pictures…")
        self.files_button.setToolTip("Pictures from your computer, onto this character's card")
        self.files_button.clicked.connect(
            lambda: self._current and self.files_requested.emit(self._current, "", "")
        )
        self.restyle_button = QPushButton("Restyle…")
        self.restyle_button.setToolTip(
            "Their pictures redrawn in a style (the story's, or another), keeping what each shows"
        )
        self.restyle_button.clicked.connect(
            lambda: self.restyle_requested.emit(self._current or "")
        )
        self.fill_button = QPushButton()
        self.fill_button.setObjectName("sendButton")
        self.fill_button.clicked.connect(self._fill)
        self.back_button = QPushButton("Back to the story")
        self.back_button.setToolTip("Esc")
        self.back_button.clicked.connect(self.back_requested)
        self.spoilers = QCheckBox(SPOILERS_LABEL)
        self.spoilers.setToolTip(
            "Everyone, the characters the plot hasn't brought in yet too, as the Plot page's Show "
            "plot shows them. No model hears of them before the plot brings them in; filling their "
            "gaps sends an image model only their pictures and the change asked for."
        )
        self.spoilers.toggled.connect(self.spoilers_toggled)
        self.keep_button = QPushButton(KEEP_LABEL)
        self.keep_button.clicked.connect(self.keep_requested)
        self.keep_button.hide()
        # Shown by the story's window only: the board in Your characters'
        # page is of a character kept there already.
        self.save_button = QPushButton(SAVE_LABEL)
        self.save_button.setToolTip(
            "The character shown (or everyone) kept in Your characters with their pictures, "
            "to join any story"
        )
        self.save_button.clicked.connect(lambda: self.save_requested.emit(self._current or ""))
        self.save_button.hide()

        # A row that wraps: on a narrower window the buttons go onto a second
        # line rather than squeeze their words (the lab's 1280px desktops).
        header = WrapRow(group_spacing=12)
        header.add_group([self.title])
        header.add_group([self.spoilers])
        header.add_group([QLabel("Size"), self.size_choice])
        header.add_group(
            [
                self.add_look_button,
                self.add_expression_button,
                self.files_button,
                self.restyle_button,
            ]
        )
        header.add_group([self.fill_button])
        header.add_group([self.save_button, self.keep_button, self.back_button], right=True)

        self.asked = QWidget()
        self._asked_row = QHBoxLayout(self.asked)
        self._asked_row.setContentsMargins(0, 0, 0, 0)
        self._asked_row.setSpacing(6)

        self.sheet = SheetTable(self._thumbs)
        self.sheet.itemSelectionChanged.connect(self._sync_fill)
        self.sheet.cellDoubleClicked.connect(self._double_clicked)
        self.sheet.customContextMenuRequested.connect(self._cell_menu)
        self.sheet.horizontalHeader().customContextMenuRequested.connect(self._column_menu)
        self.sheet.verticalHeader().customContextMenuRequested.connect(self._row_menu)
        self.sheet.picture_dropped.connect(self._dropped_on_sheet)
        self.sheet.horizontalHeader().moved.connect(
            lambda order: self._headings_moved(order, False)
        )
        self.sheet.verticalHeader().moved.connect(lambda order: self._headings_moved(order, True))
        self.sheet.files_dropped.connect(self._files_on_sheet)
        self.sheet.activated.connect(self._activated)
        self.sheet.remove_pressed.connect(lambda ref_id: self._ask("remove", ref_id))
        self.sheet.labels_pressed.connect(lambda ref_id: self._ask("labels", ref_id))
        self.hint = QLabel(
            "Drag across gaps, or Ctrl-click them, to choose which to draw; a heading chooses its "
            "whole row or column, and dragged moves it. Drag a picture to another place to "
            "relabel it, or drop pictures from your computer on a place. Right-click a picture "
            "or a heading for more."
        )
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)

        self.tray_title = QLabel(
            "Set aside, no labels: drag one to its place on the sheet, or one from the sheet "
            "here; double-click to see it, right-click for more"
        )
        self.tray_title.setObjectName("peopleSection")
        self.tray_title.setWordWrap(True)
        self.tray = Tray()
        self.tray.picture_dropped.connect(self._unlabel)
        self.tray.files_dropped.connect(lambda paths: self._files_on_tray(paths))
        # As on the sheet: double-click (or Enter) shows it whole.
        self.tray.itemActivated.connect(
            lambda item: (
                self._current
                and self.picture_action.emit("whole", self._current, item.data(Qt.UserRole))
            )
        )
        self.tray.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tray.customContextMenuRequested.connect(self._tray_menu)
        tray_remove = QShortcut(QKeySequence(QKeySequence.Delete), self.tray)
        tray_remove.setContext(Qt.WidgetShortcut)
        tray_remove.activated.connect(
            lambda: (
                self._current
                and self.tray.currentItem() is not None
                and self._can_change
                and self.picture_action.emit(
                    "remove", self._current, self.tray.currentItem().data(Qt.UserRole)
                )
            )
        )

        sheet_page = QWidget()
        sheet_layout = QVBoxLayout(sheet_page)
        sheet_layout.setContentsMargins(0, 0, 0, 0)
        sheet_layout.setSpacing(6)
        sheet_layout.addWidget(self.sheet, 1)
        sheet_layout.addWidget(self.tray_title)
        sheet_layout.addWidget(self.tray)
        sheet_layout.addWidget(self.hint)

        self.overview = QWidget()
        self._overview_rows = QVBoxLayout(self.overview)
        self._overview_rows.setContentsMargins(0, 0, 8, 0)
        self._overview_rows.setSpacing(14)
        overview_scroll = QScrollArea()
        overview_scroll.setWidgetResizable(True)
        overview_scroll.setWidget(self.overview)
        # Always on: each row's height follows its width (it wraps), and an
        # as-needed bar coming and going resized the page inside its own
        # resize (the People page's crash on Hyprland, Oct 29 2026).
        overview_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self._overview_lists: list[QListWidget] = []

        self.pages = QStackedWidget()
        self.pages.addWidget(overview_scroll)
        self.pages.addWidget(sheet_page)

        right = QVBoxLayout()
        right.setSpacing(8)
        right.addLayout(header)
        # A line of its own: beside the buttons it was cut short.
        self.stats.setWordWrap(True)
        right.addWidget(self.stats)
        right.addWidget(self.asked)
        right.addWidget(self.pages, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)
        layout.addWidget(self.people)
        layout.addLayout(right, 1)
        self._resize(self.size_choice.currentText())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        # Shown only in the whole window, the story's panels put away: its
        # header must not widen the story's place (the stack it shares keeps
        # its widest page's width), which squeezed the side panel.
        return QSize(0, 0)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(0, 0)

    # --- what the window gives it -----------------------------------------------------------

    def set_cards(
        self,
        cards: Sequence[Character],
        *,
        shown: Mapping[str, str],
        wanted: Mapping[str, Mapping[str, int]],
        drawing: Mapping[tuple[str, str, str], int],
        begun: bool,
        can_change: bool,
        hidden: Collection[str] = (),
        spoilers: bool = False,
    ) -> None:
        """The characters the author may see, who is shown with which
        picture in the story now, the looks the story asked for, the cells
        being drawn ((card id, look key, expression key) → how many), whether
        the story has begun (Show as needs a message) and whether cards may
        be changed now (not while the story's job runs). `hidden`: the
        plot's characters not brought in yet (among `cards` only with
        `spoilers` on); Show spoilers is offered while there are any."""
        self._cards = list(cards)
        self._hidden = set(hidden)
        self.spoilers.blockSignals(True)
        self.spoilers.setChecked(spoilers)
        self.spoilers.blockSignals(False)
        self.spoilers.setVisible(bool(self._hidden) and not self._embedded)
        self._shown = dict(shown)
        self._wanted = {key: dict(value) for key, value in wanted.items()}
        self._drawing = dict(drawing)
        self._begun = begun
        self._can_change = can_change
        if self._current is not None and self._card(self._current) is None:
            self._current = None
        self._fill_people()
        self._show()

    def set_template(self, template: str, kept: int = 0) -> None:
        """The template the story was made from ("" for none): its pictures
        can be kept for the next story from it; `kept` were kept already."""
        self._template = template
        self.keep_button.setVisible(bool(template) and not self._embedded)
        already = f" {kept} are kept for it now." if kept else ""
        self.keep_button.setToolTip(
            f"Pictures only: everyone's pictures, labels and sheets, kept for the next story "
            f"started from {template}, which then offers to put them on its own characters "
            f"of the same names. {template} itself stays as it is.{already} To keep the "
            f"characters themselves for any story, use {SAVE_LABEL}"
        )

    def set_texts(self, texts: PromptTexts) -> None:
        """The prompt texts in force (the usual words a heading is drawn with
        are prompt texts, which may be edited)."""
        self._texts = texts

    def set_savable(self, savable: bool) -> None:
        """Whether Save to Your characters… is offered (the story's board)."""
        self.save_button.setVisible(savable and not self._embedded)

    def embed(self) -> None:
        """Inside a page with its own list of people, title and buttons: the
        board's own are put away for good."""
        self._embedded = True
        for widget in (
            self.people,
            self.title,
            self.back_button,
            self.save_button,
            self.keep_button,
            self.spoilers,
        ):
            widget.hide()

    @property
    def template(self) -> tuple[str, str]:
        """(the template its pictures can be kept for, "" for none; what Keep
        pictures for new stories… says of it)."""
        return self._template, self.keep_button.toolTip()

    @property
    def offers_spoilers(self) -> bool:
        """Whether the plot hides anyone (Show spoilers is offered)."""
        return bool(self._hidden)

    def aim(self, card_id: str | None) -> None:
        """Whose sheet the next `set_cards` shows (None: everyone), without
        showing anything yet."""
        self._current = card_id

    def show_character(self, card_id: str | None) -> None:
        """A character's sheet, or everyone (None)."""
        self._current = card_id if card_id is None or self._card(card_id) else None
        self._select_person()
        self._show()

    def forget_pictures(self) -> None:
        """Another story's, or pictures changed under it."""
        self._thumbs.forget()

    @property
    def current(self) -> str | None:
        return self._current

    # --- the list of people ---------------------------------------------------------------

    def _card(self, card_id: str | None) -> Character | None:
        return next((c for c in self._cards if c.id == card_id), None)

    def _fill_people(self) -> None:
        self.people.blockSignals(True)
        self.people.clear()
        everyone = QListWidgetItem(
            f"{EVERYONE}\n{len(self._cards)} character{'' if len(self._cards) == 1 else 's'}"
        )
        everyone.setData(Qt.UserRole, None)
        self.people.addItem(everyone)
        for card in self._cards:
            sheet = sheet_for(card, self._wanted.get(card.id))
            gaps = len(sheet.drawable())
            words = f"{sheet.picture_count} picture{'' if sheet.picture_count == 1 else 's'}"
            if gaps:
                words += f" · {gaps} gap{'' if gaps == 1 else 's'}"
            if card.id in self._hidden:
                words += "\nnot in the story yet"
            item = QListWidgetItem(f"{card.name}\n{words}")
            item.setData(Qt.UserRole, card.id)
            first = card.reference_images[0] if card.reference_images else None
            if first is not None:
                item.setIcon(
                    QIcon(self._thumbs.now(first.file, PEOPLE_ICON.width(), PEOPLE_ICON.height()))
                )
            self.people.addItem(item)
        self.people.blockSignals(False)
        self._select_person()

    def _select_person(self) -> None:
        self.people.blockSignals(True)
        for row in range(self.people.count()):
            if self.people.item(row).data(Qt.UserRole) == self._current:
                self.people.setCurrentRow(row)
                break
        self.people.blockSignals(False)

    def _on_person(self, item: QListWidgetItem | None, _previous=None) -> None:
        if item is None:
            return
        self._current = item.data(Qt.UserRole)
        self._show()

    # --- showing --------------------------------------------------------------------------

    def _width(self) -> int:
        return CELL_WIDTHS.get(self.size_choice.currentText(), CELL_WIDTHS["Medium"])

    def _resize(self, _size: str = "") -> None:
        width = self._width()
        self.sheet.set_cell_size(width)
        self.tray.setIconSize(QSize(int(width * 0.6), int(width * 0.6 * ASPECT)))
        self._show()

    def _fit_tray(self) -> None:
        """As tall as its pictures, or a strip of words with none: empty, it
        took a row's height from the sheet."""
        if self.tray.count():
            height = int(self._width() * 0.6 * ASPECT) + 40
        else:
            height = 3 * self.tray.fontMetrics().height() + 2 * self.tray.frameWidth() + 8
        self.tray.setFixedHeight(height)

    def _show(self) -> None:
        card = self._card(self._current)
        editing = card is not None
        for widget in (self.add_look_button, self.add_expression_button, self.files_button):
            widget.setVisible(editing)
            widget.setEnabled(editing and self._can_change)
        # One character's pictures, or everyone's from the overview.
        self.restyle_button.setEnabled(self._can_change)
        self.restyle_button.setText("Restyle…" if editing else "Restyle everyone…")
        if card is None:
            self.title.setText(EVERYONE)
            total = sum(len(c.reference_images) for c in self._cards)
            self.stats.setText(
                f"{_characters(len(self._cards))}, {total} pictures. Click one to see their "
                "pictures as a sheet of looks and expressions."
            )
            self._sheet = None
            self._filled_for = None
            self._show_asked(None)
            self._fill_overview()
            self.pages.setCurrentIndex(0)
            self._sync_fill()
            return
        self.title.setText(card.name)
        self._sheet = sheet_for(card, self._wanted.get(card.id))
        self._fill_sheet(card, self._sheet)
        self._show_asked(card)
        self.pages.setCurrentIndex(1)
        self._sync_fill()

    def _stats(self, sheet: Sheet) -> str:
        parts = [f"{sheet.picture_count} picture{'' if sheet.picture_count == 1 else 's'}"]
        parts.append(f"{len(sheet.looks)} look{'' if len(sheet.looks) == 1 else 's'}")
        gaps = len(sheet.drawable())
        if gaps:
            parts.append(f"{gaps} gap{'' if gaps == 1 else 's'} to draw")
        asked = sum(gap.wanted for gap in sheet.gaps.values()) + sum(n for _w, n in sheet.asked)
        if asked:
            parts.append(
                f"the story asked for missing looks {asked} time{'' if asked == 1 else 's'}"
            )
        return " · ".join(parts)

    def _fill_sheet(self, card: Character, sheet: Sheet) -> None:
        table = self.sheet
        # The same character again (a picture landed, a label changed): the
        # gaps chosen stay chosen and the sheet stays where it was scrolled.
        # Both were lost on every refresh, so a picture landing from an
        # earlier drawing took away the choice being made.
        same = self._filled_for == card.id
        chosen = (
            {(label_key(c.look), label_key(c.expression)) for c in self._selected_cells()}
            if same
            else set()
        )
        scrolled = (
            (table.horizontalScrollBar().value(), table.verticalScrollBar().value())
            if same
            else (0, 0)
        )
        self._filled_for = card.id
        table.clearSelection()
        for header in (table.horizontalHeader(), table.verticalHeader()):
            if header.sectionsMoved():
                # A heading dragged: the sheet is in its new order now, and
                # the header's own reordering is undone by emptying it.
                table.setRowCount(0)
                table.setColumnCount(0)
        table.setRowCount(len(sheet.looks))
        table.setColumnCount(len(sheet.expressions))
        table.setHorizontalHeaderLabels([e or NO_EXPRESSION for e in sheet.expressions])
        table.setVerticalHeaderLabels([look or NO_LOOK for look in sheet.looks])
        for column, expression in enumerate(sheet.expressions):
            table.horizontalHeaderItem(column).setToolTip(self._heading_tip(card, expression))
        for row, look in enumerate(sheet.looks):
            table.verticalHeaderItem(row).setToolTip(self._heading_tip(card, look, look=True))
        shown = self._shown.get(card.id) or sheet.portrait_id
        for row, look in enumerate(sheet.looks):
            for column, expression in enumerate(sheet.expressions):
                key = (card.id, label_key(look), label_key(expression))
                cell = Cell(
                    look=look,
                    expression=expression,
                    refs=list(sheet.pictures(look, expression)),
                    gap=sheet.gap(look, expression),
                    portrait_id=sheet.portrait_id,
                    shown_id=shown if self._begun else None,
                    drawing=self._drawing.get(key, 0),
                    front=self._fronts.get(key, 0),
                    drawn_as=self._drawn_as(card, sheet.gap(look, expression)),
                )
                item = QTableWidgetItem()
                item.setData(CELL_ROLE, cell)
                item.setToolTip(self._tip(cell))
                # A picture can be picked up; a gap, pressed and dragged,
                # chooses the gaps it crosses instead.
                flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDropEnabled
                if cell.refs and self._can_change:
                    flags |= Qt.ItemIsDragEnabled
                item.setFlags(flags)
                table.setItem(row, column, item)
        table.set_cell_size(self._width())
        if chosen:
            selection = table.selectionModel()
            table.blockSignals(True)
            for row, look in enumerate(sheet.looks):
                for column, expression in enumerate(sheet.expressions):
                    key = (label_key(look), label_key(expression))
                    if key in chosen and key in sheet.gaps:
                        selection.select(
                            table.model().index(row, column), QItemSelectionModel.Select
                        )
            table.blockSignals(False)
        table.horizontalScrollBar().setValue(scrolled[0])
        table.verticalScrollBar().setValue(scrolled[1])
        self.stats.setText(self._stats(sheet))
        self.tray.clear()
        width = int(self._width() * 0.6)
        for ref in sheet.unlabelled:
            item = QListWidgetItem(ref.caption or "no labels")
            item.setData(Qt.UserRole, ref.id)
            item.setIcon(QIcon(self._thumbs.now(ref.file, width, int(width * ASPECT))))
            self.tray.addItem(item)
        self._fit_tray()

    def _drawn_as(self, card: Character, gap: Gap | None) -> str:
        if gap is None or (gap.source is None and not gap.after):
            return ""
        return gap_words(gap, self._texts, card)

    def _heading_tip(self, card: Character, label: str, *, look: bool = False) -> str:
        if not label:
            return (
                "Pictures labelled with an expression and no look"
                if look
                else "Pictures labelled with a look and no expression"
            )
        words = wording(card, label, look=look, texts=self._texts)
        return (
            f"{label}: drawn as “{words}”. Drag to move this "
            f"{'row' if look else 'column'}; right-click to change its heading or wording."
        )

    def _tip(self, cell: Cell) -> str:
        if cell.refs:
            words = f"{cell.words()}: {len(cell.refs)} picture{'' if len(cell.refs) == 1 else 's'}"
            if cell.ref is not None and cell.ref.id == cell.shown_id:
                words += ", the one the story shows now"
            return words + ". Double-click to see it whole; right-click for more."
        gap = cell.gap
        if cell.drawing:
            return f"{cell.words()}: being drawn."
        if gap is not None and gap.source is None and gap.after:
            return (
                f"{cell.words()}: none yet. Drawn from this look's {gap.after} picture once it is "
                "in, so the row wears the same thing; choosing it chooses that one too."
            )
        if gap is None or gap.source is None:
            return f"{cell.words()}: they have no picture to draw it from yet."
        words = (
            f"{cell.words()}: none yet. Drawn as “{cell.drawn_as}”, from their "
            f"{look_words(gap.source) or 'picture'}"
        )
        if gap.wanted:
            words += f"; the story asked for it {gap.wanted} time{'' if gap.wanted == 1 else 's'}"
        return words + ". Double-click to draw it."

    def _fill_overview(self) -> None:
        while self._overview_rows.count():
            item = self._overview_rows.takeAt(0)
            if item.widget() is not None:
                # Hidden now: deleted later, it would be drawn till then.
                item.widget().hide()
                item.widget().deleteLater()
        self._overview_lists = []
        width = int(self._width() * 0.75)
        tile = QSize(width, int(width * ASPECT))
        for card in self._cards:
            sheet = sheet_for(card, self._wanted.get(card.id))
            name = QPushButton(f"{card.name}    {self._stats(sheet)}")
            name.setObjectName("boardRowTitle")
            name.setFlat(True)
            name.setCursor(Qt.PointingHandCursor)
            name.setToolTip(f"{card.name}'s pictures as a sheet of looks and expressions")
            name.clicked.connect(lambda _checked=False, card_id=card.id: self._choose(card_id))
            strip = Strip(tile, self._thumbs)
            for index, ref in enumerate(card.reference_images):
                item = QListWidgetItem()
                item.setData(TILE_ROLE, (ref, index == 0))
                item.setToolTip(f"{card.name}: {look_words(ref)}. Click for their sheet.")
                strip.addItem(item)
            if not card.reference_images:
                item = QListWidgetItem()
                item.setData(TILE_ROLE, (None, False))
                item.setToolTip(f"{card.name} has no pictures yet. Click for their sheet.")
                strip.addItem(item)
            strip.itemClicked.connect(lambda _item, card_id=card.id: self._choose(card_id))
            self._overview_lists.append(strip)
            self._overview_rows.addWidget(name)
            self._overview_rows.addWidget(strip)
        self._overview_rows.addStretch(1)

    def _choose(self, card_id: str) -> None:
        """One chosen on the overview: their sheet (and the page that holds
        the board shows them in its list)."""
        self.show_character(card_id)
        self.character_chosen.emit(card_id)

    def _thumbs_ready(self) -> None:
        """Pictures read since: drawn where they go."""
        self.sheet.viewport().update()
        for strip in self._overview_lists:
            strip.viewport().update()

    def _show_asked(self, card: Character | None) -> None:
        while self._asked_row.count():
            item = self._asked_row.takeAt(0)
            if item.widget() is not None:
                # Hidden now: deleted later, it would be drawn till then.
                item.widget().hide()
                item.widget().deleteLater()
        asked = self._sheet.asked if (card is not None and self._sheet is not None) else []
        self.asked.setVisible(bool(asked))
        if not asked:
            return
        label = QLabel("The story asked for:")
        label.setObjectName("hintLabel")
        self._asked_row.addWidget(label)
        for words, count in asked:
            chip = QToolButton()
            chip.setText(words if count == 1 else f"{words}  ×{count}")
            chip.setToolTip("A look the story found none of their pictures fitted")
            chip.setPopupMode(QToolButton.InstantPopup)
            menu = QMenu(chip)
            menu.addAction("Draw it as the story put it…").triggered.connect(
                lambda _checked=False, words=words: self.asked_requested.emit(card.id, words)
            )
            add = menu.addAction("Add it to the sheet")
            add.setEnabled(self._can_change)
            add.triggered.connect(lambda _checked=False, words=words: self._add_asked(words))
            chip.setMenu(menu)
            self._asked_row.addWidget(chip)
        self._asked_row.addStretch(1)

    # --- choosing and drawing gaps --------------------------------------------------------

    def _selected_cells(self) -> list[Cell]:
        cells = []
        for item in self.sheet.selectedItems():
            cell = item.data(CELL_ROLE)
            if cell is not None:
                cells.append(cell)
        return cells

    def _being_drawn(self, gap: Gap) -> int:
        return self._drawing.get((self._current, label_key(gap.look), label_key(gap.expression)), 0)

    def _with_firsts(self, gaps: Sequence[Gap]) -> list[Gap]:
        """`gaps` that can be drawn, each waiting one with its row's first
        cell (added if it isn't there and isn't being drawn), in sheet order."""
        if self._sheet is None:
            return []
        chosen = {(label_key(g.look), label_key(g.expression)) for g in gaps}
        for gap in gaps:
            first = self._sheet.first_of(gap)
            if first is not None and not self._being_drawn(first):
                chosen.add((label_key(first.look), label_key(first.expression)))
        keep = []
        for gap in self._sheet.fillable():
            key = (label_key(gap.look), label_key(gap.expression))
            if key not in chosen or self._being_drawn(gap):
                continue
            first = self._sheet.first_of(gap)
            if first is not None and not (
                (label_key(first.look), label_key(first.expression)) in chosen
                or self._being_drawn(first)
            ):
                continue
            keep.append(gap)
        return keep

    def _selected_gaps(self) -> list[Gap]:
        return self._with_firsts(
            [cell.gap for cell in self._selected_cells() if cell.gap is not None]
        )

    def _drawable(self) -> list[Gap]:
        if self._sheet is None or self._current is None:
            return []
        return self._with_firsts(self._sheet.fillable())

    def _sync_fill(self) -> None:
        if self._sheet is None:
            self.fill_button.setVisible(False)
            return
        self.fill_button.setVisible(True)
        chosen = self._selected_gaps()
        every = self._drawable()
        if chosen:
            self.fill_button.setText(f"Draw {len(chosen)} chosen…")
        elif every:
            self.fill_button.setText(f"Fill every gap ({len(every)})…")
        elif self._sheet.gaps and not self._sheet.cells:
            self.fill_button.setText("No picture to draw from yet")
        else:
            self.fill_button.setText("No gaps to fill")
        self.fill_button.setToolTip(
            "Every picture here is drawn from one of theirs: give them a portrait first "
            "(Draw a portrait… on their card), or drop a picture on a place"
            if self._sheet.gaps and not self._sheet.cells and not (chosen or every)
            else ""
        )
        self.fill_button.setEnabled(bool(chosen or every))

    def _fill(self) -> None:
        gaps = self._selected_gaps() or self._drawable()
        if gaps and self._current is not None:
            self.fill_requested.emit(self._current, gaps)

    def _double_clicked(self, row: int, column: int) -> None:
        cell = self.sheet.cell_at(row, column)
        if cell is None or self._current is None:
            return
        if cell.ref is not None:
            self.picture_action.emit("whole", self._current, cell.ref.id)
        elif cell.gap is not None and not cell.drawing:
            gaps = self._with_firsts([cell.gap])
            if gaps:
                self.fill_requested.emit(self._current, gaps)

    def _activated(self, row: int, column: int) -> None:
        """Enter: a picture seen whole; on a gap, the chosen gaps drawn (this
        one, when it isn't among them)."""
        cell = self.sheet.cell_at(row, column)
        if cell is None or self._current is None:
            return
        if cell.ref is not None:
            self.picture_action.emit("whole", self._current, cell.ref.id)
            return
        chosen = self._selected_gaps()
        if cell.gap is not None and any(g is cell.gap or g == cell.gap for g in chosen):
            self.fill_requested.emit(self._current, chosen)
        else:
            self._double_clicked(row, column)

    def _ask(self, what: str, ref_id: str) -> None:
        if self._current is not None:
            self.picture_action.emit(what, self._current, ref_id)

    # --- dropped pictures ---------------------------------------------------------------

    def _dropped_on_sheet(self, ref_id: str, row: int, column: int) -> None:
        """A picture of theirs dropped on a place: labelled for it."""
        card = self._card(self._current)
        if card is None or self._sheet is None or not self._can_change:
            return
        ref = next((r for r in card.reference_images if r.id == ref_id), None)
        if ref is None:
            return
        look, expression = self._sheet.looks[row], self._sheet.expressions[column]
        if (label_key(ref.look), label_key(ref.expression)) == (
            label_key(look),
            label_key(expression),
        ):
            return
        # The rows and columns as the author sees them, kept first: moved
        # away, a picture took its row with it, or moved it.
        keep_order(card)
        ref.look, ref.expression = look, expression
        self._changed(card)

    def _unlabel(self, ref_id: str) -> None:
        """A picture of theirs dropped on the tray: its labels taken off."""
        card = self._card(self._current)
        ref = next((r for r in card.reference_images if r.id == ref_id), None) if card else None
        if ref is None or not self._can_change or not (ref.look or ref.expression):
            return
        keep_order(card)
        ref.look = ref.expression = ""
        self._changed(card)

    def _files_on_tray(self, paths: list) -> None:
        if self._current is not None and self._can_change:
            self.files_dropped.emit(self._current, "", "", list(paths))

    def _files_on_sheet(self, paths: list, row: int, column: int) -> None:
        if self._current is None or self._sheet is None or not self._can_change:
            return
        look, expression = self._sheet.looks[row], self._sheet.expressions[column]
        self.files_dropped.emit(self._current, look, expression, list(paths))

    # --- menus ----------------------------------------------------------------------------

    def _tray_menu(self, position) -> None:
        """A picture not labelled yet: what the sheet's pictures offer."""
        item = self.tray.itemAt(position)
        card_id = self._current
        if item is None or card_id is None:
            return
        ref_id = item.data(Qt.UserRole)
        emit = self.picture_action.emit
        menu = QMenu(self)
        menu.addAction("See it whole").triggered.connect(lambda: emit("whole", card_id, ref_id))
        for words, what in (
            ("Make it their portrait", "portrait"),
            ("Labels…", "labels"),
            ("Remove…", "remove"),
        ):
            action = menu.addAction(words)
            action.setEnabled(self._can_change)
            action.triggered.connect(lambda _on=False, what=what: emit(what, card_id, ref_id))
        menu.exec(self.tray.viewport().mapToGlobal(position))

    def _cell_menu(self, position) -> None:
        index = self.sheet.indexAt(position)
        cell = self.sheet.cell_at(index.row(), index.column()) if index.isValid() else None
        card_id = self._current
        if cell is None or card_id is None:
            return
        menu = QMenu(self)
        ref = cell.ref
        if ref is not None:
            emit = self.picture_action.emit
            menu.addAction("See it whole").triggered.connect(lambda: emit("whole", card_id, ref.id))
            if self._begun:
                show = menu.addAction("Show them like this in the story")
                show.setCheckable(True)
                show.setChecked(ref.id == cell.shown_id)
                show.triggered.connect(lambda: emit("show", card_id, ref.id))
            if ref.id != cell.portrait_id:
                portrait = menu.addAction("Make it their portrait")
                portrait.setEnabled(self._can_change)
                portrait.triggered.connect(lambda: emit("portrait", card_id, ref.id))
            labels = menu.addAction("Labels…")
            labels.setEnabled(self._can_change)
            labels.triggered.connect(lambda: emit("labels", card_id, ref.id))
            menu.addSeparator()
            menu.addAction("Change this picture…").triggered.connect(
                lambda: emit("edit", card_id, ref.id)
            )
            menu.addAction("Same prompt again…").triggered.connect(
                lambda: emit("again", card_id, ref.id)
            )
            menu.addAction("A set of expressions from it…").triggered.connect(
                lambda: emit("set", card_id, ref.id)
            )
            if len(cell.refs) > 1:
                menu.addSeparator()
                key = (card_id, label_key(cell.look), label_key(cell.expression))
                ahead = (cell.front + 1) % len(cell.refs)
                menu.addAction(
                    f"The next picture here ({ahead + 1} of {len(cell.refs)})"
                ).triggered.connect(lambda: self._turn(key, ahead))
            menu.addSeparator()
            remove = menu.addAction("Remove from their card…")
            remove.setEnabled(self._can_change)
            remove.triggered.connect(lambda: emit("remove", card_id, ref.id))
        else:
            gap = cell.gap
            chosen = self._selected_gaps()
            if len(chosen) > 1:
                menu.addAction(f"Draw the {len(chosen)} chosen…").triggered.connect(
                    lambda: self.fill_requested.emit(card_id, chosen)
                )
            draw = menu.addAction("Draw this one…")
            draw.setEnabled(gap is not None and gap.source is not None and not cell.drawing)
            draw.triggered.connect(lambda: self.fill_requested.emit(card_id, [gap]))
            from_file = menu.addAction("A picture from a file, here…")
            from_file.setEnabled(self._can_change)
            from_file.triggered.connect(
                lambda: self.files_requested.emit(card_id, cell.look, cell.expression)
            )
        menu.exec(self.sheet.viewport().mapToGlobal(position))

    def _turn(self, key: tuple[str, str, str], front: int) -> None:
        self._fronts[key] = front
        self._show()

    def _line_gaps(self, *, row: int | None = None, column: int | None = None) -> list[Gap]:
        cells = []
        table = self.sheet
        rows = [row] if row is not None else range(table.rowCount())
        columns = [column] if column is not None else range(table.columnCount())
        for r in rows:
            for c in columns:
                cell = table.cell_at(r, c)
                if cell is not None and cell.gap is not None:
                    cells.append(cell.gap)
        return self._with_firsts(cells)

    def _line_has_pictures(self, *, row: int | None = None, column: int | None = None) -> bool:
        table = self.sheet
        rows = [row] if row is not None else range(table.rowCount())
        columns = [column] if column is not None else range(table.columnCount())
        return any(
            (cell := table.cell_at(r, c)) is not None and cell.refs for r in rows for c in columns
        )

    def _column_menu(self, position) -> None:
        column = self.sheet.horizontalHeader().logicalIndexAt(position)
        card = self._card(self._current)
        if column < 0 or card is None or self._sheet is None:
            return
        expression = self._sheet.expressions[column]
        gaps = self._line_gaps(column=column)
        menu = QMenu(self)
        fill = menu.addAction(f"Fill this column ({len(gaps)})…")
        fill.setEnabled(bool(gaps))
        fill.triggered.connect(lambda: self.fill_requested.emit(card.id, gaps))
        rename = menu.addAction(HEADING_AND_WORDING)
        rename.setEnabled(self._can_change and bool(expression))
        rename.triggered.connect(lambda: self._rename(card, expression=expression))
        named = [e for e in self._sheet.expressions if e]
        for words, step in (("Move left", -1), ("Move right", 1)):
            action = menu.addAction(words)
            at = named.index(expression) if expression in named else -1
            action.setEnabled(self._can_change and at >= 0 and 0 <= at + step < len(named))
            action.triggered.connect(
                lambda _on=False, step=step: self._move(card, expression, step, look=False)
            )
        remove = menu.addAction("Remove this column")
        remove.setEnabled(
            self._can_change
            and len(self._sheet.expressions) > 1
            and not self._line_has_pictures(column=column)
        )
        remove.triggered.connect(lambda: self._remove(card, expression=expression))
        menu.exec(self.sheet.horizontalHeader().mapToGlobal(position))

    def _row_menu(self, position) -> None:
        row = self.sheet.verticalHeader().logicalIndexAt(position)
        card = self._card(self._current)
        if row < 0 or card is None or self._sheet is None:
            return
        look = self._sheet.looks[row]
        gaps = self._line_gaps(row=row)
        menu = QMenu(self)
        fill = menu.addAction(f"Fill this row ({len(gaps)})…")
        fill.setEnabled(bool(gaps))
        fill.triggered.connect(lambda: self.fill_requested.emit(card.id, gaps))
        rename = menu.addAction(HEADING_AND_WORDING)
        rename.setEnabled(self._can_change and bool(look))
        rename.triggered.connect(lambda: self._rename(card, look=look))
        named = [x for x in self._sheet.looks if x]
        for words, step in (("Move up", -1), ("Move down", 1)):
            action = menu.addAction(words)
            at = named.index(look) if look in named else -1
            action.setEnabled(self._can_change and at >= 0 and 0 <= at + step < len(named))
            action.triggered.connect(
                lambda _on=False, step=step: self._move(card, look, step, look=True)
            )
        remove = menu.addAction("Remove this row")
        remove.setEnabled(self._can_change and not self._line_has_pictures(row=row))
        remove.triggered.connect(lambda: self._remove(card, look=look))
        menu.exec(self.sheet.verticalHeader().mapToGlobal(position))

    # --- rows and columns -----------------------------------------------------------------

    def _changed(self, card: Character) -> None:
        self.sheet_changed.emit(card.id)
        self._fill_people()
        self._show()

    def _ask_heading(self, card: Character | None, **kwargs) -> tuple[str, str] | None:
        """A heading and the words it is drawn with (gui/sheet_labels.py)."""
        return ask_sheet_label(self, texts=self._texts, **kwargs)

    def _add_look(self) -> None:
        card = self._card(self._current)
        if card is None or self._sheet is None or not self._can_change:
            return
        have = [look for look in self._sheet.looks if look]
        offered = [look for look in COMMON_LOOKS if label_key(look) not in map(label_key, have)]
        asked = self._ask_heading(
            card,
            title="Add a look",
            about=f"A look for {card.name}: clothes, armour, an injury, anything that lasts. "
            "It goes at the bottom of the sheet; drag its heading to move it.",
            look=True,
            offered=offered,
            taken=have,
        )
        if asked is None:
            return
        heading, words = asked
        keep_order(card)
        if add_label(card.sheet_looks, heading):
            set_wording(card, heading, words, look=True)
            self._changed(card)

    def _add_expression(self, *, everyone: bool) -> None:
        card = self._card(self._current)
        if not self._can_change or (card is None and not everyone):
            return
        have = [] if self._sheet is None else [e for e in self._sheet.expressions if e]
        offered = [e for e in COMMON_EXPRESSIONS if label_key(e) not in map(label_key, have)]
        who = "everyone in the story" if everyone else card.name if card else "them"
        asked = self._ask_heading(
            card,
            title="Add an expression",
            about=f"An expression or pose for {who}: laughing, crying, in a fighting stance. "
            "It goes at the right of the sheet; drag its heading to move it."
            + (" Each keeps their own words for it if they have some." if everyone else ""),
            look=False,
            offered=offered,
            taken=() if everyone else have,
        )
        if asked is None:
            return
        heading, words = asked
        changed = []
        for each in self._cards if everyone else [card]:
            keep_order(each)
            listed = each.sheet_expressions or list(DEFAULT_EXPRESSIONS)
            added = add_label(listed, heading)
            if added:
                each.sheet_expressions = listed
            worded = bool(words) and not (each.expression_wording.get(label_key(heading)))
            if worded:
                set_wording(each, heading, words)
            if added or worded:
                changed.append(each)
        for each in changed:
            self.sheet_changed.emit(each.id)
        if changed:
            self._fill_people()
            self._show()

    def _add_asked(self, words: str) -> None:
        card = self._card(self._current)
        if card is None or not self._can_change:
            return
        keep_order(card)
        look, expression = split_wanted(words)
        if look:
            add_label(card.sheet_looks, look)
        if expression:
            listed = card.sheet_expressions or list(DEFAULT_EXPRESSIONS)
            add_label(listed, expression)
            card.sheet_expressions = listed
        self._changed(card)

    def _rename(
        self, card: Character, *, look: str | None = None, expression: str | None = None
    ) -> None:
        """Heading and wording…: a row's or column's heading (every picture
        of theirs labelled so is relabelled; onto another's, the two become
        one) and the words it is drawn with."""
        is_look = look is not None
        old = (look if is_look else expression) or ""
        what = "look" if is_look else "expression"
        if not old or not self._can_change:
            return
        asked = self._ask_heading(
            card,
            title=f"The {what}: heading and wording",
            about=f"The heading names the {'row' if is_look else 'column'} and labels "
            f"{card.name}'s pictures in it: changed, every one is relabelled.",
            look=is_look,
            heading=old,
            words=wording(card, old, look=is_look, texts=self._texts),
        )
        if asked is None:
            return
        heading, words = asked
        changed = False
        if heading and heading != old:
            if is_look:
                relabel(card, look=(old, heading))
            else:
                relabel(card, expression=(old, heading))
            changed = True
        changed = set_wording(card, heading or old, words, look=is_look) or changed
        if changed:
            self._changed(card)

    def _move(self, card: Character, label: str, step: int, *, look: bool) -> None:
        if self._can_change and move(card, label, step, look=look):
            self._changed(card)

    def _headings_moved(self, order: list, look: bool) -> None:
        """A heading dragged to another place: the sheet put in that order
        (the unnamed row and column stay last)."""
        card = self._card(self._current)
        sheet = self._sheet
        if card is None or sheet is None:
            return
        labels = sheet.looks if look else sheet.expressions
        if not self._can_change or any(index >= len(labels) for index in order):
            self._show()  # back as it was
            return
        named = [labels[index] for index in order if labels[index]]
        if look:
            reorder(card, looks=named)
        else:
            reorder(card, expressions=named)
        self._changed(card)

    def _remove(
        self, card: Character, *, look: str | None = None, expression: str | None = None
    ) -> None:
        remove_label(card, look=look, expression=expression)
        self._changed(card)
