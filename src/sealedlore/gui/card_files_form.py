"""Your characters' Files tab: every file on a character's card besides their
pictures (a background, pictures its greetings show, sounds, fonts, links),
to see, rename, retype, add, take off or save, for the card to go out again
as changed (the author, Nov 2026: "change, add or remove anything at all").

The window keeps the files themselves (storage/collection.py: each
character's in a folder of their own); this edits the card's list of them
(TavernFields.assets) in place and asks the window for what touches a file
(`file_requested`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.fields import FormScroll, set_field_text
from sealedlore.models.character import Character
from sealedlore.models.tavern import CardAsset, TavernFields
from sealedlore.storage.card_files import renamed_in
from sealedlore.storage.collection import files_said

if TYPE_CHECKING:
    from sealedlore.gui.cast_board import Thumbs

# The files as thumbnails.
FILE_ICON = 72
# The V3 spec's kinds, and RisuAI's for the files its greetings show (what
# most cards with many files use); any other can be typed in.
TYPES = (
    ("x-risu-asset", "A file the card's greetings or scripts use (RisuAI's kind)"),
    ("background", "Behind a chat; the one named main first"),
    ("emotion", "An expression picture, named by its feeling"),
    ("icon", "Another icon; the portrait is the main one"),
    ("user_icon", "A picture for whoever they talk to"),
    ("other", "Kept, and used by nothing"),
)
# A link the author types in: never fetched, but held to the HTTPS rule all
# the same (only the spec's own `ccdefault:` besides).
LINK_PREFIXES = ("https://", "ccdefault:")


def link_problem(address: str) -> str:
    """Why `address` can't be a link on the card, or ""."""
    if address.startswith(LINK_PREFIXES):
        return ""
    if address.startswith("http://"):
        return "Only https:// addresses: a plain http:// link isn't kept."
    return "A link begins with https://, or is ccdefault: for an app's own default."


def _tavern(character: Character) -> TavernFields:
    if character.tavern is None:
        character.tavern = TavernFields()
    return character.tavern


class FilesForm(QWidget):
    changed = Signal()
    # What to do with which of the card's files (its place in
    # `tavern.assets`; -1 for "add" and "link"): "whole" (seen full size),
    # "save", "picture" (a copy among their pictures), "add" (files from
    # disk), "link", "remove", "renamed" (its file renamed to match).
    file_requested = Signal(str, int)
    # Files dropped from the desktop (their paths), to add.
    files_dropped = Signal(list)
    # A rename changed the greetings too (For chats shows them).
    greetings_changed = Signal()

    def __init__(self, thumbs: Thumbs | None = None) -> None:
        super().__init__()
        self._character: Character | None = None
        self._loading = False
        self._thumbs = thumbs
        self._name_before = ""
        self.said = QLabel()
        self.said.setWordWrap(True)
        self.files = QListWidget()
        self.files.setObjectName("cardFiles")
        self.files.setViewMode(QListView.IconMode)
        self.files.setMovement(QListView.Static)
        self.files.setResizeMode(QListView.Adjust)
        self.files.setWrapping(True)
        self.files.setIconSize(QSize(FILE_ICON, FILE_ICON))
        self.files.setGridSize(QSize(FILE_ICON + 28, FILE_ICON + 36))
        self.files.setSelectionMode(QListWidget.SingleSelection)
        self.files.itemSelectionChanged.connect(self._chosen)
        # Double-click (or Enter) shows a picture full size, as everywhere a
        # picture is listed; a click chooses a file to rename or save.
        self.files.itemActivated.connect(self._activated)
        self.files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self._menu)
        remove_key = QShortcut(QKeySequence(QKeySequence.Delete), self.files)
        remove_key.setContext(Qt.WidgetShortcut)
        remove_key.activated.connect(lambda: self._action("remove"))
        # Files dragged in from the desktop are added, as Add files… does.
        self.files.setAcceptDrops(True)
        self.files.viewport().setAcceptDrops(True)
        self.files.viewport().installEventFilter(self)

        self.add_button = QPushButton("Add files…")
        self.add_button.setToolTip("Files from this computer onto their card")
        self.add_button.clicked.connect(lambda: self.file_requested.emit("add", -1))
        self.link_button = QPushButton("Add a link…")
        self.link_button.setToolTip("A file the card only points to; never fetched")
        self.link_button.clicked.connect(lambda: self.file_requested.emit("link", -1))
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(lambda: self._action("remove"))
        self.save_button = QPushButton("Save as…")
        self.save_button.setToolTip("Save this file of the card's where you choose")
        self.save_button.clicked.connect(lambda: self._action("save"))
        self.picture_button = QPushButton("Add to their pictures")
        self.picture_button.setToolTip(
            "A copy among their pictures (Card and Pictures tabs), to use as any of theirs; "
            "the card keeps its own"
        )
        self.picture_button.clicked.connect(lambda: self._action("picture"))
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        for button in (
            self.add_button,
            self.link_button,
            self.remove_button,
            self.save_button,
            self.picture_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)

        self.name = QLineEdit()
        self.name.setPlaceholderText("What the card calls it: greetings name it so")
        self.name.textChanged.connect(self._renamed)
        self.name.editingFinished.connect(self._rename_finished)
        self.type = QComboBox()
        self.type.setEditable(True)
        for kind, tip in TYPES:
            self.type.addItem(kind)
            self.type.setItemData(self.type.count() - 1, tip, Qt.ToolTipRole)
        self.type.setToolTip("What kind of file the card says it is; any other may be typed")
        self.type.currentTextChanged.connect(self._retyped)
        self.type_said = QLabel()
        self.type_said.setObjectName("hintLabel")
        self.kept = QLabel()
        self.kept.setObjectName("hintLabel")
        self.link = QLineEdit()
        self.link.editingFinished.connect(self._relinked)
        self.link_said = QLabel()
        self.link_said.setObjectName("hintLabel")
        self.link_said.setWordWrap(True)
        self.editor = QWidget()
        editor = QFormLayout(self.editor)
        editor.setContentsMargins(0, 6, 0, 0)
        editor.addRow("Name", self.name)
        editor.addRow("Kind", self.type)
        editor.addRow("", self.type_said)
        editor.addRow("Kept", self.kept)
        editor.addRow("Link", self.link)
        editor.addRow("", self.link_said)
        self._editor = editor

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 0)
        layout.setSpacing(6)
        layout.addWidget(self.said)
        layout.addWidget(self.files)
        layout.addLayout(buttons)
        layout.addWidget(self.editor)
        layout.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(FormScroll(content))
        self.load(None)

    @property
    def _assets(self) -> list[CardAsset]:
        return _tavern(self._character).assets if self._character is not None else []

    def load(self, character: Character | None, *, chosen: int | None = None) -> None:
        self._character = character
        self.setEnabled(character is not None)
        self.show_files(chosen)

    def show_files(self, chosen: int | None = None) -> None:
        """The card's files, each by its name, a picture as a thumbnail."""
        assets = self._assets
        self.files.blockSignals(True)
        self.files.clear()
        self.said.setText(
            files_said(assets) + " Double-click a picture to see it full size."
            if assets
            else "No files besides their pictures. A card's background, the pictures its "
            "greetings show and its sounds are kept here, and go out with it as a CHARX. "
            "Add files…, or drop them here."
        )
        for index, asset in enumerate(assets):
            item = QListWidgetItem(asset.name or asset.type)
            item.setData(Qt.UserRole, index)
            self._decorate(item, asset)
            self.files.addItem(item)
        if chosen is not None and 0 <= chosen < self.files.count():
            self.files.setCurrentRow(chosen)
            self.files.item(chosen).setSelected(True)
        self.files.blockSignals(False)
        # Shown empty too: files are dropped on it.
        self._fit_list()
        self.refresh_file_icons()
        self._chosen()

    def _decorate(self, item: QListWidgetItem, asset: CardAsset) -> None:
        where = asset.uri if not asset.held else f"{asset.ext.upper()} file"
        tip = f"{asset.name or '(no name)'} · {asset.type} · {where}"
        if asset.held and asset.picture:
            tip += "\nClick to see it full size."
        item.setToolTip(tip)
        style = self.style()
        if not asset.held:
            item.setIcon(style.standardIcon(QStyle.SP_DriveNetIcon))
        elif not asset.picture:
            item.setIcon(style.standardIcon(QStyle.SP_FileIcon))

    def refresh_file_icons(self) -> None:
        """Thumbnails as they are read (a card may have hundreds)."""
        if self._thumbs is None or self._character is None:
            return
        assets = self._assets
        for row in range(self.files.count()):
            item = self.files.item(row)
            index = item.data(Qt.UserRole)
            asset = assets[index] if 0 <= index < len(assets) else None
            if asset is None or not asset.held or not asset.picture:
                continue
            pixmap = self._thumbs.get(asset.file, FILE_ICON, FILE_ICON)
            if pixmap is not None:
                item.setIcon(QIcon(pixmap))

    def chosen_index(self) -> int | None:
        item = self.files.currentItem()
        return item.data(Qt.UserRole) if item is not None and item.isSelected() else None

    def _chosen_asset(self) -> CardAsset | None:
        index = self.chosen_index()
        assets = self._assets
        return assets[index] if index is not None and 0 <= index < len(assets) else None

    def _chosen(self) -> None:
        asset = self._chosen_asset()
        self.remove_button.setEnabled(asset is not None)
        self.save_button.setEnabled(asset is not None and asset.held)
        self.picture_button.setEnabled(asset is not None and asset.held and asset.picture)
        self.editor.setVisible(asset is not None)
        if asset is None:
            return
        self._loading = True
        set_field_text(self.name, asset.name)
        self.type.setCurrentText(asset.type)
        set_field_text(self.link, asset.uri)
        self._loading = False
        self._said_kind(asset.type)
        self._name_before = asset.name
        self.kept.setText(
            f"A {asset.ext.upper() or 'file'} kept with the card" if asset.held else ""
        )
        self._editor.setRowVisible(self.kept, asset.held)
        self._editor.setRowVisible(self.link, not asset.held)
        self._editor.setRowVisible(self.link_said, False)

    def _activated(self, item: QListWidgetItem) -> None:
        index = item.data(Qt.UserRole)
        assets = self._assets
        if 0 <= index < len(assets) and assets[index].held and assets[index].picture:
            self.file_requested.emit("whole", index)

    def _menu(self, point) -> None:
        item = self.files.itemAt(point)
        if item is None:
            return
        self.files.setCurrentItem(item)
        item.setSelected(True)
        asset = self._chosen_asset()
        if asset is None:
            return
        menu = QMenu(self)
        whole = menu.addAction("See it full size")
        whole.setEnabled(asset.held and asset.picture)
        whole.triggered.connect(lambda: self._activated(item))
        for button in (self.save_button, self.picture_button, self.remove_button):
            action = menu.addAction(button.text())
            action.setEnabled(button.isEnabled())
            action.triggered.connect(button.click)
        menu.exec(self.files.viewport().mapToGlobal(point))

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        kind = event.type()
        if watched is self.files.viewport() and kind in (
            QEvent.DragEnter,
            QEvent.DragMove,
            QEvent.Drop,
        ):
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if not paths or self._character is None:
                return False
            event.acceptProposedAction()
            if kind == QEvent.Drop:
                self.files_dropped.emit(paths)
            return True
        return super().eventFilter(watched, event)

    def _fit_list(self) -> None:
        """As tall as its files need, one row to five, at the width it has."""
        count = self.files.count()
        across = max(1, self.files.viewport().width() // (FILE_ICON + 28))
        rows = min(max(1, -(-count // across)), 5)
        self.files.setFixedHeight(rows * (FILE_ICON + 36) + 12)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit_list()

    def _said_kind(self, kind: str) -> None:
        said = dict(TYPES).get(kind.strip(), "A kind of the card's own: kept as it is")
        self.type_said.setText(said)

    def _action(self, action: str) -> None:
        index = self.chosen_index()
        if index is not None:
            self.file_requested.emit(action, index)

    def _touched(self, asset: CardAsset) -> None:
        item = self.files.currentItem()
        if item is not None:
            item.setText(asset.name or asset.type)
            self._decorate(item, asset)
            self.refresh_file_icons()
        self.changed.emit()

    def _renamed(self) -> None:
        asset = self._chosen_asset()
        if self._loading or asset is None:
            return
        asset.name = self.name.text().strip()
        self._touched(asset)

    def _rename_finished(self) -> None:
        index = self.chosen_index()
        if index is None:
            return
        asset = self._assets[index]
        old, self._name_before = self._name_before, asset.name
        if old and asset.name and old != asset.name:
            self._rename_in_greetings(old, asset.name)
        if asset.held:
            self.file_requested.emit("renamed", index)

    def _rename_in_greetings(self, old: str, new: str) -> None:
        """Greetings show a file by its name: offered the new one too."""
        tavern = _tavern(self._character)
        groups = (
            [tavern.first_mes],
            tavern.alternate_greetings,
            tavern.group_only_greetings,
        )
        count = sum(renamed_in(text, old, new)[1] for group in groups for text in group)
        if not count:
            return
        answer = QMessageBox.question(
            self,
            "Rename it in the greetings too?",
            f"{count} place{'s' if count != 1 else ''} in their greetings show “{old}”. Show "
            f"“{new}” there instead? Otherwise they show nothing.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        tavern.first_mes = renamed_in(tavern.first_mes, old, new)[0]
        for group in (tavern.alternate_greetings, tavern.group_only_greetings):
            group[:] = [renamed_in(text, old, new)[0] for text in group]
        self.changed.emit()
        self.greetings_changed.emit()

    def _retyped(self, text: str) -> None:
        self._said_kind(text)
        asset = self._chosen_asset()
        if self._loading or asset is None:
            return
        asset.type = text.strip() or "other"
        self._touched(asset)

    def _relinked(self) -> None:
        asset = self._chosen_asset()
        if self._loading or asset is None or asset.held:
            return
        address = self.link.text().strip()
        if address == asset.uri:
            return
        problem = link_problem(address)
        self.link_said.setText(problem)
        self._editor.setRowVisible(self.link_said, bool(problem))
        if problem:
            return
        asset.uri = address
        self._touched(asset)
