"""The cast board (View → Cast board…, or Cast board… on the Cast page): every
character's pictures in the whole window. Everyone, each with their pictures
in a row; and a character's sheet, their looks down the side and their
expressions or poses across (engine/look_sheet.py), each gap shown as the
picture it would be drawn from, faded, ready to be drawn.

The author (Nov 3 2026): managing many pictures per person should be
seamless and visually engaging, all the characters and all their pictures
for every emotion and dress or pose type in view, the missing ones easy to
fill in, types of their own easy to add. The page shows and edits a
character's sheet; drawing, saving and the story are the window's
(gui/window_board.py).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QByteArray, QMimeData, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
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
    label_key,
    relabel,
    sheet_for,
    split_wanted,
)
from sealedlore.engine.looks import COMMON_EXPRESSIONS, COMMON_LOOKS, look_words
from sealedlore.engine.plot_md import PICTURE_SUFFIXES
from sealedlore.gui import theme
from sealedlore.gui.ref_images import StoryPictures, pixmap_of
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef

BOARD_LABEL = "Cast board"
SPOILERS_LABEL = "Show spoilers"
KEEP_LABEL = "Keep for new stories…"
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

    def __init__(self, pictures: StoryPictures, ready: Callable[[], None]) -> None:
        self._pictures = pictures
        self._ready = ready
        self._cache: dict[tuple[str, int, int], QPixmap] = {}
        self._queue: list[tuple[str, int, int]] = []
        self._timer = QTimer()
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._work)

    def forget(self) -> None:
        self._cache.clear()
        self._queue.clear()
        self._timer.stop()

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
            gap = cell.gap
            if gap is not None and gap.source is not None and not cell.drawing:
                pixmap = self.thumbs.get(gap.source.file, size.width(), size.height())
                if pixmap is not None and not pixmap.isNull():
                    painter.setOpacity(0.2)
                    painter.setClipPath(path)
                    painter.drawPixmap(rect.topLeft().toPoint(), pixmap)
                    painter.setClipping(False)
                    painter.setOpacity(1.0)
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
                painter.setFont(option.font)
                label = cell.expression or cell.look or "draw"
                if gap.source is None:
                    # The rest of a new look's row: drawn from its first.
                    label += f"\nafter the {gap.after or 'first'}"
                painter.drawText(
                    QRectF(words.left(), middle + 2, words.width(), words.height() / 2 - 2),
                    Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                    label,
                )
                painter.setFont(small)
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
        for header in (self.horizontalHeader(), self.verticalHeader()):
            header.setSectionResizeMode(header.ResizeMode.Fixed)
            header.setContextMenuPolicy(Qt.CustomContextMenu)
            header.setHighlightSections(True)
        self.verticalHeader().setFixedWidth(ROW_HEADER)
        self.verticalHeader().setDefaultAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def set_cell_size(self, width: int) -> None:
        self.horizontalHeader().setDefaultSectionSize(width)
        self.verticalHeader().setDefaultSectionSize(int(width * ASPECT))
        for column in range(self.columnCount()):
            self.setColumnWidth(column, width)
        for row in range(self.rowCount()):
            self.setRowHeight(row, int(width * ASPECT))

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
    """Pictures not labelled yet: dragged onto a place, they take its labels;
    a picture of the sheet dropped here loses its own."""

    # (picture id): a picture of the sheet dropped here.
    picture_dropped = Signal(str)

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
        if event.mimeData().hasFormat(PICTURE_MIME) and event.source() is not self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        mime = event.mimeData()
        if not mime.hasFormat(PICTURE_MIME) or event.source() is self:
            event.ignore()
            return
        self.picture_dropped.emit(bytes(mime.data(PICTURE_MIME)).decode())
        event.setDropAction(Qt.MoveAction)
        event.accept()


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

    def __init__(self, pictures: StoryPictures, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("castBoard")
        self._thumbs = Thumbs(pictures, self._thumbs_ready)
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

        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(self.title, 1)
        header.addWidget(self.spoilers)
        header.addWidget(QLabel("Size"))
        header.addWidget(self.size_choice)
        header.addWidget(self.add_look_button)
        header.addWidget(self.add_expression_button)
        header.addWidget(self.files_button)
        header.addWidget(self.fill_button)
        header.addWidget(self.keep_button)
        header.addWidget(self.back_button)

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
        self.sheet.files_dropped.connect(self._files_on_sheet)
        self.sheet.activated.connect(self._activated)
        self.sheet.remove_pressed.connect(lambda ref_id: self._ask("remove", ref_id))
        self.sheet.labels_pressed.connect(lambda ref_id: self._ask("labels", ref_id))
        self.hint = QLabel(
            "Drag across gaps, or Ctrl-click them, to choose which to draw; a header chooses its "
            "whole row or column. Drag a picture to another place to relabel it, or drop "
            "pictures from your computer on a place. Right-click a picture for more."
        )
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)

        self.tray_title = QLabel(
            "Not labelled yet: drag one to its place, or double-click it to say what it shows"
        )
        self.tray_title.setObjectName("peopleSection")
        self.tray = Tray()
        self.tray.picture_dropped.connect(self._unlabel)
        self.tray.itemDoubleClicked.connect(
            lambda item: (
                self._current
                and self.picture_action.emit("labels", self._current, item.data(Qt.UserRole))
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
        self.spoilers.setVisible(bool(self._hidden))
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
        self.keep_button.setVisible(bool(template))
        already = f" {kept} are kept for it now." if kept else ""
        self.keep_button.setToolTip(
            f"Keep everyone's pictures, labels and sheets for new stories from {template}: "
            f"starting it again, you can add them or leave them out, and {template} itself "
            f"stays as it is.{already}"
        )

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
        everyone = QListWidgetItem(f"{EVERYONE}\n{len(self._cards)} characters")
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
        self.tray.setFixedHeight(int(width * 0.6 * ASPECT) + 40)
        self._show()

    def _show(self) -> None:
        card = self._card(self._current)
        editing = card is not None
        for widget in (self.add_look_button, self.add_expression_button, self.files_button):
            widget.setVisible(editing)
            widget.setEnabled(editing and self._can_change)
        if card is None:
            self.title.setText(EVERYONE)
            total = sum(len(c.reference_images) for c in self._cards)
            self.stats.setText(
                f"{len(self._cards)} characters, {total} pictures. Click one to see their "
                "pictures as a sheet of looks and expressions."
            )
            self._sheet = None
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
        table.clearSelection()
        table.setRowCount(len(sheet.looks))
        table.setColumnCount(len(sheet.expressions))
        table.setHorizontalHeaderLabels([e or NO_EXPRESSION for e in sheet.expressions])
        table.setVerticalHeaderLabels([look or NO_LOOK for look in sheet.looks])
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
        self.stats.setText(self._stats(sheet))
        self.tray.clear()
        width = int(self._width() * 0.6)
        for ref in sheet.unlabelled:
            item = QListWidgetItem(ref.caption or "no labels")
            item.setData(Qt.UserRole, ref.id)
            item.setIcon(QIcon(self._thumbs.now(ref.file, width, int(width * ASPECT))))
            self.tray.addItem(item)
        self.tray.setVisible(bool(sheet.unlabelled))
        self.tray_title.setVisible(bool(sheet.unlabelled))

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
        words = f"{cell.words()}: none yet. Drawn from their {look_words(gap.source) or 'picture'}"
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
            name.clicked.connect(
                lambda _checked=False, card_id=card.id: self.show_character(card_id)
            )
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
            strip.itemClicked.connect(lambda _item, card_id=card.id: self.show_character(card_id))
            self._overview_lists.append(strip)
            self._overview_rows.addWidget(name)
            self._overview_rows.addWidget(strip)
        self._overview_rows.addStretch(1)

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
        else:
            self.fill_button.setText("No gaps to fill")
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
        ref.look, ref.expression = look, expression
        self._changed(card)

    def _unlabel(self, ref_id: str) -> None:
        """A picture of theirs dropped on the tray: its labels taken off."""
        card = self._card(self._current)
        ref = next((r for r in card.reference_images if r.id == ref_id), None) if card else None
        if ref is None or not self._can_change or not (ref.look or ref.expression):
            return
        ref.look = ref.expression = ""
        self._changed(card)

    def _files_on_sheet(self, paths: list, row: int, column: int) -> None:
        if self._current is None or self._sheet is None or not self._can_change:
            return
        look, expression = self._sheet.looks[row], self._sheet.expressions[column]
        self.files_dropped.emit(self._current, look, expression, list(paths))

    # --- menus ----------------------------------------------------------------------------

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
        rename = menu.addAction("Rename…")
        rename.setEnabled(self._can_change)
        rename.triggered.connect(lambda: self._rename(card, expression=expression))
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
        rename = menu.addAction("Rename…")
        rename.setEnabled(self._can_change)
        rename.triggered.connect(lambda: self._rename(card, look=look))
        remove = menu.addAction("Remove this row")
        remove.setEnabled(self._can_change and not self._line_has_pictures(row=row))
        remove.triggered.connect(lambda: self._remove(card, look=look))
        menu.exec(self.sheet.verticalHeader().mapToGlobal(position))

    # --- rows and columns -----------------------------------------------------------------

    def _changed(self, card: Character) -> None:
        self.sheet_changed.emit(card.id)
        self._fill_people()
        self._show()

    def _ask_label(self, title: str, prompt: str, offered: Sequence[str], text: str = "") -> str:
        if text or not offered:
            value, ok = QInputDialog.getText(self, title, prompt, text=text)
        else:
            value, ok = QInputDialog.getItem(self, title, prompt, list(offered), 0, True)
        return " ".join(value.split()) if ok else ""

    def _add_look(self) -> None:
        card = self._card(self._current)
        if card is None or self._sheet is None or not self._can_change:
            return
        have = {label_key(look) for look in self._sheet.looks}
        offered = [look for look in COMMON_LOOKS if label_key(look) not in have]
        look = self._ask_label(
            "Add a look",
            f"A look for {card.name}: clothes, armour, an injury, anything that lasts.",
            offered,
        )
        if look and add_label(card.sheet_looks, look):
            self._changed(card)

    def _add_expression(self, *, everyone: bool) -> None:
        card = self._card(self._current)
        if not self._can_change or (card is None and not everyone):
            return
        have = set() if self._sheet is None else {label_key(e) for e in self._sheet.expressions}
        offered = [e for e in COMMON_EXPRESSIONS if label_key(e) not in have]
        who = "everyone in the story" if everyone else card.name if card else "them"
        expression = self._ask_label(
            "Add an expression",
            f"An expression or pose for {who}: laughing, crying, in a fighting stance.",
            offered,
        )
        if not expression:
            return
        changed = []
        for each in self._cards if everyone else [card]:
            listed = each.sheet_expressions or list(DEFAULT_EXPRESSIONS)
            if add_label(listed, expression):
                each.sheet_expressions = listed
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
        old = look if look is not None else expression
        what = "look" if look is not None else "expression"
        new = self._ask_label(
            f"Rename the {what}",
            f"A new name for {old or 'this ' + what}: every picture of {card.name}'s "
            f"labelled so is relabelled.",
            (),
            text=old or "",
        )
        if not new or new == old:
            return
        if look is not None:
            relabel(card, look=(look, new))
        else:
            relabel(card, expression=(expression or "", new))
        self._changed(card)

    def _remove(
        self, card: Character, *, look: str | None = None, expression: str | None = None
    ) -> None:
        if look is not None:
            card.sheet_looks = [x for x in card.sheet_looks if label_key(x) != label_key(look)]
        if expression is not None:
            listed = card.sheet_expressions or list(DEFAULT_EXPRESSIONS)
            card.sheet_expressions = [x for x in listed if label_key(x) != label_key(expression)]
        self._changed(card)
