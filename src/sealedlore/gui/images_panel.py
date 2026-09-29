"""Every picture and video made for this story, newest first.

Including those whose passage was deleted or sits on another branch: the
transcript shows a picture only after its passage, and a picture costs money,
so it is never lost with a branch.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.ref_images import thumbnail_of
from sealedlore.gui.transcript import IMAGE_ACTIONS, picture_caption
from sealedlore.gui.video_view import VIDEO_ACTIONS, video_caption
from sealedlore.models.image import GeneratedImage
from sealedlore.models.video import GeneratedVideo
from sealedlore.storage.picture_store import PictureStore

GALLERY_THUMB = 150


class ImagesPanel(QWidget):
    # (action, image id), as the transcript's picture menu.
    action_requested = Signal(str, str)
    # (action, video id), as the transcript's video menu.
    video_action_requested = Signal(str, str)
    generate_requested = Signal()
    generate_video_requested = Signal()
    save_all_requested = Signal()

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
        self.list.itemActivated.connect(self._activate)
        self.hint = QLabel(
            "No pictures or videos yet. Image… beside Send (or Story → Generate image…) "
            "pictures what is happening, and Story → Generate video… makes a few seconds of "
            "it; right-click one here for more."
        )
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)
        self.generate_button = QPushButton("Image…")
        self.generate_button.setToolTip("Generate a picture of what is happening")
        self.generate_button.clicked.connect(self.generate_requested)
        self.generate_video_button = QPushButton("Video…")
        self.generate_video_button.setToolTip(
            "Generate a few seconds of video. Far dearer than a picture: the price is shown "
            "before anything is sent"
        )
        self.generate_video_button.clicked.connect(self.generate_video_requested)
        self.save_all_button = QPushButton("Save all…")
        self.save_all_button.setToolTip("Copy every picture here into a folder you choose")
        self.save_all_button.clicked.connect(self.save_all_requested)
        self.save_all_button.setEnabled(False)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.generate_button, 1)
        buttons.addWidget(self.generate_video_button, 1)
        buttons.addWidget(self.save_all_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.hint)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)

    def set_images(
        self,
        images: list[GeneratedImage],
        store: PictureStore | None,
        on_path: set[str],
        videos: list[GeneratedVideo] | None = None,
    ) -> None:
        self.list.clear()
        made = sorted(
            [*(("image", image) for image in images), *(("video", v) for v in videos or ())],
            key=lambda pair: pair[1].created_at,
            reverse=True,
        )
        for kind, record in made:
            if kind == "video":
                self._add_video(record, store, on_path)
                continue
            image = record
            elsewhere = image.anchor_node_id is not None and image.anchor_node_id not in on_path
            text = picture_caption(image).removeprefix("Picture  ·  ")
            item = QListWidgetItem(text + ("\n(not on this branch)" if elsewhere else ""))
            item.setData(Qt.UserRole, image.id)
            item.setData(Qt.UserRole + 1, "image")
            item.setToolTip(image.prompt)
            if store is not None:
                item.setIcon(QIcon(thumbnail_of(store.read(image.file), GALLERY_THUMB)))
            self.list.addItem(item)
        self.hint.setVisible(not made)
        self.save_all_button.setEnabled(bool(made))

    def _add_video(
        self, video: GeneratedVideo, store: PictureStore | None, on_path: set[str]
    ) -> None:
        elsewhere = video.anchor_node_id is not None and video.anchor_node_id not in on_path
        state = {"pending": "\n(being made)", "failed": "\n(failed)"}.get(video.status, "")
        text = "▶ " + video_caption(video).removeprefix("Video  ·  ") + state
        item = QListWidgetItem(text + ("\n(not on this branch)" if elsewhere else ""))
        item.setData(Qt.UserRole, video.id)
        item.setData(Qt.UserRole + 1, "video")
        item.setToolTip(video.prompt)
        if store is not None and video.poster:
            item.setIcon(QIcon(thumbnail_of(store.read(video.poster), GALLERY_THUMB)))
        self.list.addItem(item)

    def _activate(self, item: QListWidgetItem) -> None:
        if item.data(Qt.UserRole + 1) == "video":
            self.video_action_requested.emit("show", item.data(Qt.UserRole))
        else:
            self.action_requested.emit("open", item.data(Qt.UserRole))

    def _menu(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        if item is None:
            return
        image_id = item.data(Qt.UserRole)
        menu = QMenu(self)
        if item.data(Qt.UserRole + 1) == "video":
            for key, text in VIDEO_ACTIONS:
                if key == "-":
                    menu.addSeparator()
                    continue
                if key == "play":
                    key, text = "show", "Show in the story"
                action = menu.addAction(text)
                action.triggered.connect(
                    lambda _checked=False, k=key: self.video_action_requested.emit(k, image_id)
                )
            menu.exec(self.list.viewport().mapToGlobal(position))
            return
        for key, text in IMAGE_ACTIONS:
            if key == "-":
                menu.addSeparator()
                continue
            action = menu.addAction(text)
            action.triggered.connect(
                lambda _checked=False, k=key: self.action_requested.emit(k, image_id)
            )
        menu.exec(self.list.viewport().mapToGlobal(position))
