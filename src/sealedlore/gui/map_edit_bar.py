"""Edit Mode's bar over the play view (engine/map_edit.py; the author, Oct 18
2026): the tool in hand, what Paint lays, Objects…, Undo, and Done."""

from __future__ import annotations

from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.map_edit import EDIT_TILES
from sealedlore.gui.wrap_row import WrapRow

EDIT_LABEL = "✎ Edit map"
DONE_LABEL = "Done"
# The tools, in the bar's order: (name, label, what it does).
TOOLS: tuple[tuple[str, str, str], ...] = (
    (
        "move",
        "Move",
        "Drag a person or a thing to where they belong; click one for its menu (rename, "
        "remove, broken…). Drag the ground to look around.",
    ),
    (
        "paint",
        "Paint",
        "Click or drag to lay the tile chosen beside it: walls, doors, windows, floor, ground, "
        "water… Painting a tile back to what the plan has takes your change off.",
    ),
    (
        "room",
        "Room",
        "Drag from corner to corner: walls round it with a door onto open floor. Outdoors, a "
        "walled yard.",
    ),
    (
        "fence",
        "Fence",
        "Drag from corner to corner: a fence round it with a gap for a gate.",
    ),
    (
        "building",
        "Building",
        "Outdoors: drag from corner to corner and name it. It goes on the map where you drew "
        "it, gets walls and a door, and its inside is drawn as any building's is.",
    ),
    (
        "remove",
        "Remove building",
        "Click a building to take it off: one the map knows comes off the map (whoever is in "
        "it goes to the place round it), and its ground is open again.",
    ),
    (
        "inside",
        "Go inside",
        "Click a building to edit its inside (its rooms, walls and doors) without walking "
        "there. Its inside must be drawn first.",
    ),
)
TOOL_WORDS = {name: words for name, _label, words in TOOLS}
# What each tile looks like in the palette and while it is painted (before
# the area is drawn again in the tile set).
TILE_COLOURS: dict[str, str] = {
    "_": "#b08a5a",
    "#": "#4a4a52",
    "+": "#8a5a2a",
    "W": "#9fd0e8",
    ".": "#7aa05a",
    ",": "#5f8a3f",
    "=": "#a89070",
    "s": "#dcc690",
    "T": "#2f6b34",
    "^": "#8a8a8a",
    "~": "#3f78b8",
    "%": "#8a6a3a",
    ":": "#7a6a5a",
    " ": "#101014",
}


def swatch(ch: str, size: int = 14) -> QIcon:
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(QColor(TILE_COLOURS.get(ch, "#808080")))
    return QIcon(pixmap)


class EditBar(QWidget):
    tool_changed = Signal(str)
    objects_requested = Signal()
    undo_requested = Signal()
    back_requested = Signal()
    done_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("mapEditBar")
        self.heading = QLabel("Editing the map")
        self.heading.setObjectName("peopleSituation")
        self.buttons: dict[str, QToolButton] = {}
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for name, label, words in TOOLS:
            button = QToolButton()
            button.setObjectName("viewToggleButton")
            button.setText(label)
            button.setToolTip(words)
            button.setCheckable(True)
            button.clicked.connect(lambda _c=False, n=name: self.set_tool(n))
            self.group.addButton(button)
            self.buttons[name] = button
        self.tile = QComboBox()
        self.tile.setToolTip("What Paint lays")
        for ch, label in EDIT_TILES.items():
            self.tile.addItem(swatch(ch), label, ch)
        self.tile.setCurrentIndex(self.tile.findData("#"))
        self.tile.activated.connect(lambda _i: self.set_tool("paint"))
        self.objects_button = QPushButton("Objects…")
        self.objects_button.setToolTip(
            "Make things by saying what they are, and put them where you want them"
        )
        self.objects_button.clicked.connect(self.objects_requested.emit)
        self.undo_button = QPushButton("Undo")
        self.undo_button.clicked.connect(self.undo_requested.emit)
        self.back_button = QPushButton()
        self.back_button.clicked.connect(self.back_requested.emit)
        self.back_button.hide()
        self.done_button = QPushButton(DONE_LABEL)
        self.done_button.setObjectName("sendButton")
        self.done_button.setToolTip("Back to playing: the map is as you left it")
        self.done_button.clicked.connect(self.done_requested.emit)
        row = WrapRow()
        row.add_group([self.heading])
        row.add_group([self.buttons[name] for name, _l, _w in TOOLS[:2]] + [self.tile])
        row.add_group([self.buttons[name] for name, _l, _w in TOOLS[2:]])
        row.add_group([self.objects_button, self.undo_button, self.back_button])
        row.add_group([self.done_button])
        self.words = QLabel()
        self.words.setObjectName("hintLabel")
        self.words.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        holder = QHBoxLayout()
        holder.setContentsMargins(0, 0, 0, 0)
        holder.addLayout(row)
        layout.addLayout(holder)
        layout.addWidget(self.words)
        self._tool = "move"
        self.buttons["move"].setChecked(True)
        self.words.setText(TOOL_WORDS["move"])
        self.set_undo(None)

    def tool(self) -> str:
        return self._tool

    def paint_tile(self) -> str:
        return str(self.tile.currentData())

    def set_tool(self, name: str) -> None:
        self._tool = name
        self.buttons[name].setChecked(True)
        self.words.setText(TOOL_WORDS[name])
        self.tool_changed.emit(name)

    def set_undo(self, words: str | None) -> None:
        self.undo_button.setEnabled(bool(words))
        self.undo_button.setToolTip(f"Undo: {words}" if words else "Nothing to undo")

    def set_back(self, name: str | None) -> None:
        """Editing another area than the one you stand in: the way back."""
        self.back_button.setVisible(bool(name))
        self.back_button.setText(f"Back to {name}" if name else "")

    def set_outdoor(self, outdoor: bool) -> None:
        """The building tools are for the ground outside; a room is any area's."""
        for name in ("building", "remove", "inside"):
            self.buttons[name].setEnabled(outdoor)
        if not outdoor and self._tool in ("building", "remove", "inside"):
            self.set_tool("move")
