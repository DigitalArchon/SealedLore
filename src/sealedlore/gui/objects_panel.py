"""Objects… : make objects by saying what they are, place them on the play
view, remove what is there (the author, Oct 18 2026: "The user just says
what object or objects they want and they are created. Then we need an
interface where the user can just place or remove any object they want",
mainly to set an area up at the start of a story). A tool window beside the
map, never modal, so the map can be clicked while it is open; the session
does the making and placing (engine/session_objects.py)."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from sealedlore.engine.session_objects import ObjectChoice
from sealedlore.gui.area_render import sprite_image
from sealedlore.gui.sizing import size_to_screen

ICON = 56
# A cell in the lists: the picture and two lines of its name under it.
CELL = QSize(ICON + 64, ICON + 70)
MINE_HEADING = "Your objects"
SET_HEADING = "The tile set's things"
PLACEHOLDER = "an armchair, a small side table, a worn rug"
INTRO = (
    "Say what you want, separated by commas, and press Make: each is drawn in the tile "
    "set's style. Then choose one and click on the map to place it (it stays chosen, so you "
    "can place several). Remove things takes anything off the map, what was there already "
    "too. Drag what is placed to move it; the map's right-click menu has Undo."
)


def _bare(name: str) -> str:
    for article in ("a ", "an ", "the "):
        if name.lower().startswith(article):
            name = name[len(article) :]
            break
    return name[:1].upper() + name[1:]


def _icon(data: bytes | None) -> QIcon:
    """The object's picture on a square, or a plain box for none yet."""
    canvas = QPixmap(ICON, ICON)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    image: QImage | None = sprite_image(data) if data else None
    if image is not None and not image.isNull():
        scaled = image.scaled(ICON - 4, ICON - 4, Qt.KeepAspectRatio, Qt.FastTransformation)
        painter.drawImage((ICON - scaled.width()) // 2, (ICON - scaled.height()) // 2, scaled)
    else:
        painter.setPen(QColor(150, 150, 160))
        painter.setBrush(QColor(70, 72, 82))
        painter.drawRect(10, 10, ICON - 20, ICON - 20)
    painter.end()
    return QIcon(canvas)


class ObjectsPanel(QDialog):
    # Make these (the names as read from what was typed).
    create_requested = Signal(list)
    # Place this object with the next clicks on the map ("": stop).
    place_chosen = Signal(str)
    # Remove things with the next clicks on the map, or no longer.
    remove_toggled = Signal(bool)
    # One of the author's own objects taken off the list.
    forget_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("objectsPanel")
        self.setWindowTitle("Objects")
        self.setWindowFlag(Qt.Tool, True)
        self.setModal(False)
        # What Make would cost and do for these names: the window's words
        # (the session's offer, priced), or "" with nothing typed.
        self.offer_for: Callable[[list[str]], tuple[str, bool]] = lambda _names: ("", True)
        self.parse: Callable[[str], list[str]] = lambda text: [text] if text.strip() else []
        self.picture_of: Callable[[ObjectChoice], bytes | None] = lambda _c: None
        self._choices: list[ObjectChoice] = []
        self._shown: tuple = ()
        self._icons: dict[tuple[str, bool], QIcon] = {}

        intro = QLabel(INTRO)
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)
        self.what = QLineEdit()
        self.what.setPlaceholderText(PLACEHOLDER)
        self.what.textChanged.connect(self._sync_offer)
        self.what.returnPressed.connect(self._make)
        self.make_button = QPushButton("Make")
        self.make_button.setDefault(True)
        self.make_button.clicked.connect(self._make)
        make_row = QHBoxLayout()
        make_row.addWidget(self.what, 1)
        make_row.addWidget(self.make_button)
        self.offer = QLabel()
        self.offer.setObjectName("hintLabel")
        self.offer.setWordWrap(True)

        self.find = QLineEdit()
        self.find.setPlaceholderText("Find an object…")
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(self._show)
        # The author's objects, and every thing the tile set has a picture of.
        self.mine_heading = QLabel(MINE_HEADING)
        self.mine_heading.setObjectName("peopleSituation")
        self.mine = self._icon_list()
        self.set_heading = QLabel(SET_HEADING)
        self.set_heading.setObjectName("peopleSituation")
        self.set_list = self._icon_list()
        self.lists = (self.mine, self.set_list)

        self.remove_button = QPushButton("Remove things")
        self.remove_button.setCheckable(True)
        self.remove_button.setToolTip("Click things on the map to take them off it")
        self.remove_button.toggled.connect(self._remove_toggled)
        self.forget_button = QPushButton("Forget")
        self.forget_button.setToolTip(
            "Take this object off your list (anything already placed stays)"
        )
        self.forget_button.clicked.connect(self._forget)
        self.forget_button.setEnabled(False)
        close = QPushButton("Done")
        close.clicked.connect(self.close)
        tools = QHBoxLayout()
        tools.addWidget(self.remove_button)
        tools.addWidget(self.forget_button)
        tools.addStretch(1)
        tools.addWidget(close)
        self.status = QLabel()
        self.status.setObjectName("hintLabel")
        self.status.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(make_row)
        layout.addWidget(self.offer)
        layout.addWidget(self.find)
        layout.addWidget(self.mine_heading)
        layout.addWidget(self.mine)
        layout.addWidget(self.set_heading)
        layout.addWidget(self.set_list, 1)
        layout.addWidget(self.status)
        layout.addLayout(tools)
        size_to_screen(self, 0.26, 0.6, at_least=QSize(380, 480))
        self._sync_offer()

    def _icon_list(self) -> QListWidget:
        found = QListWidget()
        found.setObjectName("objectsList")
        found.setViewMode(QListWidget.IconMode)
        found.setResizeMode(QListWidget.Adjust)
        found.setMovement(QListWidget.Static)
        found.setIconSize(QSize(ICON, ICON))
        found.setGridSize(CELL)
        found.setWordWrap(True)
        found.setSpacing(4)
        found.itemSelectionChanged.connect(lambda f=found: self._chosen(f))
        return found

    def place_beside(self, window) -> None:
        """Opened over the story's side of the window, clear of the map."""
        top_left = window.mapToGlobal(window.rect().topLeft())
        self.move(top_left.x() + 40, top_left.y() + 110)

    # --- making ---------------------------------------------------------------

    def names(self) -> list[str]:
        return self.parse(self.what.text())

    def _sync_offer(self) -> None:
        names = self.names()
        if not names:
            self.offer.setText("")
            self.make_button.setEnabled(False)
            return
        words, _can_draw = self.offer_for(names)
        self.offer.setText(words)
        self.make_button.setEnabled(True)

    def _make(self) -> None:
        names = self.names()
        if not names:
            return
        self.create_requested.emit(names)
        self.what.clear()

    # --- the list --------------------------------------------------------------

    def set_choices(self, choices: Sequence[ObjectChoice]) -> None:
        """Everything that can be placed (the session's `object_choices`):
        redrawn only when it changed, keeping the choice in hand."""
        shown = tuple((c.name, c.mine, c.drawn, c.drawing) for c in choices)
        if shown == self._shown:
            return
        self._shown = shown
        self._choices = list(choices)
        self._show()

    def _icon_for(self, choice: ObjectChoice) -> QIcon:
        # Not drawn yet: the stand-in it is drawn as meanwhile, if any.
        key = (choice.slug, choice.drawn)
        if key not in self._icons:
            self._icons[key] = _icon(self.picture_of(choice))
        return self._icons[key]

    def _show(self) -> None:
        chosen = self.chosen()
        wanted = self.find.text().strip().lower()
        for found, mine in ((self.mine, True), (self.set_list, False)):
            found.blockSignals(True)
            found.clear()
            for choice in self._choices:
                if choice.mine != mine or (wanted and wanted not in choice.name.lower()):
                    continue
                label = _bare(choice.name)
                tip = label
                if choice.drawing:
                    label += "\n(being drawn)"
                    tip += ": being drawn; until then it stands in as the nearest thing"
                elif not choice.drawn:
                    label += "\n(stand-in)"
                    tip += ": no picture of its own yet; it shows as the tile set's nearest thing"
                item = QListWidgetItem(self._icon_for(choice), label)
                item.setData(Qt.UserRole, choice.name)
                item.setToolTip(tip)
                found.addItem(item)
                if chosen is not None and choice.name == chosen:
                    item.setSelected(True)
            found.blockSignals(False)
        has_mine = self.mine.count() > 0
        self.mine.setVisible(has_mine)
        self.mine_heading.setVisible(has_mine)
        # As tall as its rows, two at most (then it scrolls).
        width = self.mine.viewport().width() or self.width()
        per_row = max(1, width // CELL.width())
        rows = min(2, max(1, -(-self.mine.count() // per_row)))
        self.mine.setFixedHeight(rows * CELL.height() + 12)

    def chosen(self) -> str | None:
        for found in self.lists:
            items = found.selectedItems()
            if items:
                return items[0].data(Qt.UserRole)
        return None

    def _chosen(self, source: QListWidget | None = None) -> None:
        if source is not None and source.selectedItems():
            # One choice across both lists.
            for found in self.lists:
                if found is not source:
                    found.blockSignals(True)
                    found.clearSelection()
                    found.blockSignals(False)
        name = self.chosen()
        mine = next((c.mine for c in self._choices if c.name == name), False)
        self.forget_button.setEnabled(name is not None and mine)
        if name is not None and self.remove_button.isChecked():
            self.remove_button.setChecked(False)
        self.place_chosen.emit(name or "")
        self.status.setText(f"Click on the map where {_bare(name).lower()} goes." if name else "")

    def _clear(self) -> None:
        for found in self.lists:
            found.clearSelection()

    def _remove_toggled(self, on: bool) -> None:
        if on:
            self._clear()
        self.remove_toggled.emit(on)
        self.status.setText("Click a thing on the map to take it off." if on else "")

    def _forget(self) -> None:
        name = self.chosen()
        if name:
            self._clear()
            self.forget_requested.emit(name)

    def tool_stopped(self) -> None:
        """The map's Esc: nothing in hand."""
        self._clear()
        self.remove_button.setChecked(False)
        self.status.setText("")

    def say(self, text: str) -> None:
        self.status.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        self.tool_stopped()
        self.place_chosen.emit("")
        self.remove_toggled.emit(False)
        super().closeEvent(event)
