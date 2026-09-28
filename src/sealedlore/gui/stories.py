"""The story list (left dock).

It shares the dock with the branch and chapter navigator (navigator.py), in a
splitter below it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QBrush, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.storage.atomic import read_json
from sealedlore.storage.paths import is_safe_story_id, stories_dir


class StoryListPanel(QWidget):
    story_activated = Signal(str)
    new_story_requested = Signal()
    # Right-click menu (and the Delete key); each carries the story id.
    duplicate_settings_requested = Signal(str)
    duplicate_story_requested = Signal(str)
    delete_requested = Signal(str)
    open_folder_requested = Signal(str)

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self.root = root

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.list = QListWidget()
        self.list.setObjectName("storyList")
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        # A long title is cut with "…" (the tooltip has it whole), rather than
        # a horizontal scrollbar under a dock this narrow.
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.itemActivated.connect(self._activate)
        self.list.itemDoubleClicked.connect(self._activate)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_menu)
        delete_key = QShortcut(QKeySequence.Delete, self.list)
        delete_key.setContext(Qt.WidgetShortcut)
        delete_key.activated.connect(self._delete_current)
        layout.addWidget(self.list, 1)

        new_button = QPushButton("New story")
        new_button.clicked.connect(self.new_story_requested)
        layout.addWidget(new_button)

    def refresh(self, *, selected_id: str | None = None) -> None:
        """Rebuild the list from `story.json` alone (and a count of the
        messages), most recently played first.

        This runs after every job; loading and validating every story's whole
        bundle here grew with the library and was paid on every turn.
        """
        self.list.clear()
        directory = stories_dir(self.root)
        if not directory.exists():
            return
        rows: list[tuple[str, str, str, str | None]] = []  # (updated, title, id, error)
        for entry in directory.iterdir():
            if not entry.is_dir() or not is_safe_story_id(entry.name):
                continue
            try:
                story = read_json(entry / "story.json")
                if not isinstance(story, dict):
                    raise ValueError("no story.json")
                nodes = read_json(entry / "nodes.json", [])
                count = len(nodes) if isinstance(nodes, list) else 0
                title = str(story.get("title") or entry.name)
                updated = str(story.get("updated_at") or "")
                rows.append(
                    (
                        updated,
                        f"{title}\n{count} message{'s' if count != 1 else ''}",
                        entry.name,
                        None,
                    )
                )
            except (OSError, ValueError) as exc:
                # Shown rather than skipped: a story that vanished without a
                # word can't be looked into or deleted. Opening it says why.
                reason = (str(exc).splitlines() or ["unreadable"])[0][:80]
                rows.append(("", f"{entry.name}\n⚠ can't be read: {reason}", entry.name, str(exc)))
        for _updated, label, story_id, error in sorted(rows, key=lambda row: row[0], reverse=True):
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, story_id)
            item.setToolTip(label.split("\n", 1)[0])
            if error is not None:
                item.setForeground(QBrush(Qt.gray))
                item.setToolTip(error)
            self.list.addItem(item)
            if story_id == selected_id:
                self.list.setCurrentItem(item)

    def _show_menu(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        story_id = item.data(Qt.UserRole) if item is not None else None
        if not story_id:
            return
        self.list.setCurrentItem(item)
        self.menu_for(story_id).exec(self.list.viewport().mapToGlobal(position))

    def menu_for(self, story_id: str) -> QMenu:
        """A story's right-click menu."""
        menu = QMenu(self)
        menu.addAction("Open", lambda: self.story_activated.emit(story_id))
        # On an unreadable story too: that's where its files most need a look.
        open_folder = menu.addAction(
            "Open folder", lambda: self.open_folder_requested.emit(story_id)
        )
        open_folder.setToolTip("The story's files, in your file manager")
        menu.addSeparator()
        duplicate_settings = menu.addAction(
            "Duplicate settings only", lambda: self.duplicate_settings_requested.emit(story_id)
        )
        duplicate_settings.setToolTip(
            "A new, unplayed story with the same world, cast, lore, style and opening"
        )
        duplicate_story = menu.addAction(
            "Duplicate entire story", lambda: self.duplicate_story_requested.emit(story_id)
        )
        duplicate_story.setToolTip("Everything: every branch, summary and question, and its costs")
        menu.setToolTipsVisible(True)
        menu.addSeparator()
        menu.addAction("Delete…", lambda: self.delete_requested.emit(story_id))
        return menu

    def _delete_current(self) -> None:
        item = self.list.currentItem()
        story_id = item.data(Qt.UserRole) if item is not None else None
        if story_id:
            self.delete_requested.emit(story_id)

    def _activate(self, item: QListWidgetItem) -> None:
        story_id = item.data(Qt.UserRole)
        if story_id:
            self.story_activated.emit(story_id)
