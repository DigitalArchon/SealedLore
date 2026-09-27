"""Reference pictures on a character or lore entry, and bringing picture files in.

A picture added here is copied into the story's `images/refs/` folder, so the
original can move or change and the story (and its archive) keeps it. A large
one is scaled so its longest side is 2048 pixels and saved as JPEG (PNG when
it has transparency): an image model gets the look from far less than a
camera's full frame, and the endpoint takes about 4 MB a picture.

Every copy has its hidden data removed (`storage.image_meta`): a picture
kept as it is loses it without being re-encoded; one that can't be stripped
that way is re-encoded, which carries none over.

Qt ignores a photo's EXIF orientation unless asked, so pictures are read
through `load_image` / `load_pixmap`, which apply it: a phone's portrait
photo shows upright, and one that is scaled is stored upright.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QImageReader, QPixmap
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

from sealedlore.ids import new_id
from sealedlore.models.image import ImageRef
from sealedlore.storage.image_meta import MetadataError, picture_kind, strip_metadata
from sealedlore.storage.images import REFS_DIR, story_path, write_story_file

MAX_SIDE = 2048
# Files over this are re-encoded even when small enough in pixels.
MAX_BYTES = 3 * 1024 * 1024
KEPT_AS_IS = {".png", ".jpg", ".jpeg", ".webp"}
THUMB = 88
PICTURE_FILTER = "Pictures (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*)"

# Where the open story keeps its files: (story id, data root).
StoryFolder = Callable[[], "tuple[str, Path | None] | None"]


def _encoded(image: QImage, fmt: str, quality: int = -1) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, fmt, quality)
    buffer.close()
    return bytes(data.data())


def load_image(path: Path) -> QImage:
    """A picture file as it should be seen: its EXIF orientation applied."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    return reader.read()


def load_pixmap(path: Path) -> QPixmap:
    image = load_image(path)
    return QPixmap() if image.isNull() else QPixmap.fromImage(image)


def picture_bytes(source: Path) -> tuple[bytes, str]:
    """A picture file's bytes and extension, its hidden data removed, and
    scaled and re-encoded if needed.

    Raises ValueError when the file isn't a picture Qt can read.
    """
    image = load_image(source)
    if image.isNull():
        raise ValueError(f"{source.name} isn't a picture this app can read")
    data = source.read_bytes()
    if (
        source.suffix.lower() in KEPT_AS_IS
        and len(data) <= MAX_BYTES
        and max(image.width(), image.height()) <= MAX_SIDE
    ):
        try:
            data = strip_metadata(data)
            return data, picture_kind(data)
        except MetadataError:
            pass  # re-encoded below instead
    if max(image.width(), image.height()) > MAX_SIDE:
        image = image.scaled(MAX_SIDE, MAX_SIDE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    # The orientation is in the pixels now. Qt copies a file's text (PNG
    # text, XMP, comments) into what it writes, so this is stripped too.
    if image.hasAlphaChannel():
        return strip_metadata(_encoded(image, "PNG")), ".png"
    encoded = _encoded(image.convertToFormat(QImage.Format_RGB32), "JPEG", 90)
    return strip_metadata(encoded), ".jpg"


def import_reference(source: Path, story_id: str, root: Path | None, caption: str = "") -> ImageRef:
    """Copy a picture into the story as a reference. Raises ValueError or OSError."""
    data, suffix = picture_bytes(source)
    ref_id = new_id()
    relative = f"{REFS_DIR}/{ref_id}{suffix}"
    write_story_file(story_id, relative, data, root)
    return ImageRef(id=ref_id, file=relative, caption=caption)


def thumbnail(path: Path, side: int = THUMB) -> QPixmap:
    pixmap = load_pixmap(path)
    if pixmap.isNull():
        return QPixmap()
    return pixmap.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)


class RefImageStrip(QWidget):
    """Thumbnails of one owner's reference pictures, with add, caption and remove."""

    changed = Signal()

    def __init__(self, folder: StoryFolder, owner_word: str = "them") -> None:
        super().__init__()
        self._folder = folder
        self._refs: list[ImageRef] | None = None

        self.list = QListWidget()
        self.list.setObjectName("refImageList")
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setFlow(QListWidget.LeftToRight)
        self.list.setWrapping(False)
        self.list.setIconSize(QSize(THUMB, THUMB))
        self.list.setMovement(QListWidget.Static)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setFixedHeight(THUMB + 44)
        # One elided line of caption under each picture; the tooltip has it all.
        self.list.setGridSize(QSize(THUMB + 24, THUMB + 28))
        self.list.setWordWrap(False)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.itemDoubleClicked.connect(self._edit_caption)
        self.list.currentItemChanged.connect(self._sync_buttons)

        self.add_button = QPushButton("Add…")
        self.add_button.setToolTip(
            f"Pictures of {owner_word}, sent with an image prompt so every picture "
            "draws them the same way"
        )
        self.add_button.clicked.connect(self._add)
        self.caption_button = QPushButton("Caption…")
        self.caption_button.setToolTip("What the picture shows: 'front view', 'in flight gear'")
        self.caption_button.clicked.connect(lambda: self._edit_caption(self.list.currentItem()))
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        self.hint = QLabel("No pictures. Add one to keep their look in generated images.")
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
        self.set_refs(None)

    def set_refs(self, refs: list[ImageRef] | None) -> None:
        """Show (and edit in place) an owner's list; None for no owner."""
        self._refs = refs
        self.list.clear()
        folder = self._folder()
        for ref in refs or []:
            item = QListWidgetItem(ref.caption or "(no caption)")
            item.setData(Qt.UserRole, ref.id)
            item.setToolTip(ref.caption or ref.file)
            if folder is not None:
                try:
                    item.setIcon(QIcon(thumbnail(story_path(folder[0], ref.file, folder[1]))))
                except ValueError:
                    pass
            self.list.addItem(item)
        self.list.setVisible(bool(refs))
        self.hint.setVisible(refs is not None and not refs)
        self.add_button.setEnabled(refs is not None and folder is not None)
        self._sync_buttons()

    def _sync_buttons(self, *_args) -> None:
        selected = self.list.currentItem() is not None
        self.caption_button.setEnabled(selected)
        self.remove_button.setEnabled(selected)

    def _ref(self, item: QListWidgetItem | None) -> ImageRef | None:
        if item is None or self._refs is None:
            return None
        ref_id = item.data(Qt.UserRole)
        return next((ref for ref in self._refs if ref.id == ref_id), None)

    def _add(self) -> None:
        folder = self._folder()
        if self._refs is None or folder is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add reference pictures", str(Path.home()), PICTURE_FILTER
        )
        added, problems = False, []
        for path in paths:
            try:
                self._refs.append(import_reference(Path(path), folder[0], folder[1]))
                added = True
            except (OSError, ValueError) as exc:
                problems.append(str(exc))
        if problems:
            QMessageBox.warning(self, "Couldn't add", "\n".join(problems))
        if added:
            self.set_refs(self._refs)
            self.changed.emit()

    def _edit_caption(self, item: QListWidgetItem | None) -> None:
        ref = self._ref(item)
        if ref is None:
            return
        text, ok = QInputDialog.getText(
            self, "Caption", "What this picture shows:", text=ref.caption
        )
        if ok:
            ref.caption = text.strip()
            self.set_refs(self._refs)
            self.changed.emit()

    def _remove(self) -> None:
        ref = self._ref(self.list.currentItem())
        if ref is None or self._refs is None:
            return
        # The file stays: a generated image's record may name it, and an
        # Undo of the card would want it back. It is small.
        self._refs.remove(ref)
        self.set_refs(self._refs)
        self.changed.emit()
