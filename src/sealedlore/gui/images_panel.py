"""Every picture made for this story, newest first.

Including those whose passage was deleted or sits on another branch: the
transcript shows a picture only after its passage, and a picture costs money,
so it is never lost with a branch.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.ref_images import thumbnail
from sealedlore.gui.transcript import IMAGE_ACTIONS, picture_caption
from sealedlore.models.image import GeneratedImage
from sealedlore.storage.images import story_path

GALLERY_THUMB = 150


class ImagesPanel(QWidget):
    # (action, image id), as the transcript's picture menu.
    action_requested = Signal(str, str)
    generate_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.list = QListWidget()
        self.list.setObjectName("imageGallery")
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListWidget.Static)
        self.list.setIconSize(QSize(GALLERY_THUMB, GALLERY_THUMB))
        self.list.setSpacing(6)
        self.list.setWordWrap(True)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.itemActivated.connect(
            lambda item: self.action_requested.emit("open", item.data(Qt.UserRole))
        )
        self.hint = QLabel(
            "No pictures yet. Image… beside Send (or Story → Generate image…) pictures what "
            "is happening; right-click a picture here for more."
        )
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)
        self.generate_button = QPushButton("Generate image…")
        self.generate_button.clicked.connect(self.generate_requested)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.hint)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.generate_button)

    def set_images(
        self,
        images: list[GeneratedImage],
        folder: tuple[str, Path | None] | None,
        on_path: set[str],
    ) -> None:
        self.list.clear()
        for image in reversed(images):
            elsewhere = image.anchor_node_id is not None and image.anchor_node_id not in on_path
            text = picture_caption(image).removeprefix("Picture  ·  ")
            item = QListWidgetItem(text + ("\n(not on this branch)" if elsewhere else ""))
            item.setData(Qt.UserRole, image.id)
            item.setToolTip(image.prompt)
            if folder is not None:
                try:
                    item.setIcon(
                        QIcon(
                            thumbnail(story_path(folder[0], image.file, folder[1]), GALLERY_THUMB)
                        )
                    )
                except ValueError:
                    pass
            self.list.addItem(item)
        self.hint.setVisible(not images)

    def _menu(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        if item is None:
            return
        image_id = item.data(Qt.UserRole)
        menu = QMenu(self)
        for key, text in IMAGE_ACTIONS:
            if key == "-":
                menu.addSeparator()
                continue
            action = menu.addAction(text)
            action.triggered.connect(
                lambda _checked=False, k=key: self.action_requested.emit(k, image_id)
            )
        menu.exec(self.list.viewport().mapToGlobal(position))
