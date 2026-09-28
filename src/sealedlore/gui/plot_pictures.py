"""Reference pictures in the plot file editor.

A plot file is text, so its pictures are files in a `pictures/` folder beside
it, named on the item's own lines (`- picture: pictures/john.png | front
view`). The folder travels with the file: a story is shared as the two
together. Adding a picture copies it there as any added picture is copied
(hidden data removed, scaled if large); the original is never touched.

The strip edits an item's list in place, as the story's own strip does
(`ref_images.RefImageStrip`), and says when it has changed.
"""

from __future__ import annotations

import inspect
import os
import re
import shutil
import weakref
from collections.abc import Callable, Iterable
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.ref_images import PICTURE_FILTER, THUMB, inside, picture_bytes, thumbnail
from sealedlore.ids import new_id
from sealedlore.models.plot_file import PictureDoc

PICTURES_DIR = "pictures"
# The folder the plot file is in; None until the file has been saved.
Folder = Callable[[], "Path | None"]
# Save the file (asking where), and say whether it was saved.
SaveFirst = Callable[[], bool]


def _file_name(owner: str, suffix: str) -> str:
    """`john-carver-3f2a9c.png`: readable in the folder, and never the name
    of a picture already there."""
    slug = re.sub(r"[^a-z0-9]+", "-", owner.lower()).strip("-")[:40] or "picture"
    return f"{slug}-{new_id()[:6]}{suffix}"


def add_picture(source: Path, folder: Path, owner: str, caption: str = "") -> PictureDoc:
    """Copy a picture into the plot file's pictures folder. Raises ValueError
    (not a picture the app reads) or OSError."""
    data, suffix = picture_bytes(source)
    target_dir = folder / PICTURES_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    name = _file_name(owner, suffix)
    tmp = target_dir / (name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target_dir / name)
    return PictureDoc(file=f"{PICTURES_DIR}/{name}", caption=caption)


def copy_pictures(pictures: Iterable[PictureDoc], old: Path, new: Path) -> list[str]:
    """After Save as… into another folder: take the pictures along, each at
    the same place under the new folder. A picture already there is left as
    it is. Returns the ones that couldn't be copied."""
    failed: list[str] = []
    for picture in pictures:
        source, target = inside(old, picture.file), inside(new, picture.file)
        if source is None or target is None or not source.is_file():
            failed.append(picture.file)
            continue
        if target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        except OSError:
            failed.append(picture.file)
    return failed


def _weak(method):
    """A callable that returns the method, or None once its owner has gone."""
    if method is None:
        return lambda: None
    return weakref.WeakMethod(method) if inspect.ismethod(method) else lambda: method


class PlotPictureStrip(QWidget):
    """Thumbnails of one item's pictures, with add, caption and remove."""

    changed = Signal()

    def __init__(self, folder: Folder | None = None, save_first: SaveFirst | None = None) -> None:
        super().__init__()
        # Weakly: both are usually methods of the window this strip sits in,
        # and a widget holding its own window makes a cycle whose collection
        # has crashed PySide before (gui/attest_check.py).
        self._folder_ref = _weak(folder)
        self._save_ref = _weak(save_first)
        self._pictures: list[PictureDoc] | None = None
        self._owner = ""

        self.list = QListWidget()
        self.list.setObjectName("refImageList")
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setFlow(QListWidget.LeftToRight)
        self.list.setWrapping(False)
        self.list.setIconSize(QSize(THUMB, THUMB))
        self.list.setMovement(QListWidget.Static)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setFixedHeight(THUMB + 44)
        self.list.setGridSize(QSize(THUMB + 24, THUMB + 28))
        self.list.setWordWrap(False)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.itemDoubleClicked.connect(self._edit_caption)
        self.list.currentItemChanged.connect(self._sync_buttons)

        self.add_button = QPushButton("Add…")
        self.add_button.setToolTip(
            "Pictures that come with the story: copied into a “pictures” folder beside the "
            "plot file, and put on this card when the file is imported"
        )
        self.add_button.clicked.connect(self._add)
        self.caption_button = QPushButton("Caption…")
        self.caption_button.setToolTip("What the picture shows: 'front view', 'the north gate'")
        self.caption_button.clicked.connect(lambda: self._edit_caption(self.list.currentItem()))
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Take it off this item. Its file stays in the folder.")
        self.remove_button.clicked.connect(self._remove)
        self.hint = QLabel("No pictures. Share the “pictures” folder along with the file.")
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.caption_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.list)
        layout.addWidget(self.hint)
        layout.addLayout(buttons)
        self.set_pictures(None, "")

    def set_pictures(self, pictures: list[PictureDoc] | None, owner: str) -> None:
        """Show (and edit in place) an item's list; None for no item."""
        self._pictures, self._owner = pictures, owner
        self.list.clear()
        folder = self._folder()
        for index, picture in enumerate(pictures or []):
            path = inside(folder, picture.file) if folder is not None else None
            missing = path is None or not path.is_file()
            label = picture.caption or Path(picture.file).name
            item = QListWidgetItem(f"{label} (missing)" if missing else label)
            item.setData(Qt.UserRole, index)
            item.setToolTip(
                f"{picture.file}: no such file beside the plot file" if missing else picture.file
            )
            if not missing:
                item.setIcon(QIcon(thumbnail(path)))
            self.list.addItem(item)
        self.list.setVisible(bool(pictures))
        self.hint.setVisible(pictures is not None and not pictures)
        self.add_button.setEnabled(pictures is not None)
        self._sync_buttons()

    def _sync_buttons(self, *_args) -> None:
        selected = self.list.currentItem() is not None
        self.caption_button.setEnabled(selected)
        self.remove_button.setEnabled(selected)

    def _picture(self, item: QListWidgetItem | None) -> PictureDoc | None:
        if item is None or self._pictures is None:
            return None
        index = item.data(Qt.UserRole)
        return self._pictures[index] if 0 <= index < len(self._pictures) else None

    def _folder(self) -> Path | None:
        get = self._folder_ref()
        return get() if get is not None else None

    def _saved_folder(self) -> Path | None:
        """The plot file's folder, saving the file first if it has none."""
        folder = self._folder()
        save = self._save_ref()
        if folder is not None or save is None:
            return folder
        answer = QMessageBox.question(
            self,
            "Save the plot file first",
            "Pictures are kept in a folder beside the plot file, so the file needs a place "
            "of its own first. Save it now?",
            QMessageBox.Save | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer != QMessageBox.Save or not save():
            return None
        return self._folder()

    def _add(self) -> None:
        if self._pictures is None:
            return
        folder = self._saved_folder()
        if folder is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add pictures", str(Path.home()), PICTURE_FILTER
        )
        added, problems = False, []
        for path in paths:
            try:
                self._pictures.append(add_picture(Path(path), folder, self._owner))
                added = True
            except (OSError, ValueError) as exc:
                problems.append(str(exc))
        if problems:
            QMessageBox.warning(self, "Couldn't add", "\n".join(problems))
        if added:
            self.set_pictures(self._pictures, self._owner)
            self.changed.emit()

    def _edit_caption(self, item: QListWidgetItem | None) -> None:
        picture = self._picture(item)
        if picture is None:
            return
        text, ok = QInputDialog.getText(
            self, "Caption", "What this picture shows:", text=picture.caption
        )
        if ok:
            picture.caption = text.strip()
            self.set_pictures(self._pictures, self._owner)
            self.changed.emit()

    def _remove(self) -> None:
        picture = self._picture(self.list.currentItem())
        if picture is None or self._pictures is None:
            return
        # The file stays: another item, or a copy of the plot file, may name it.
        self._pictures.remove(picture)
        self.set_pictures(self._pictures, self._owner)
        self.changed.emit()
