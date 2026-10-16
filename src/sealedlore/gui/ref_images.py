"""Reference pictures on a character or lore entry, and bringing picture files in.

A picture added here is copied into the story's `images/refs/` (through its
picture store: the story's folder, or a memory-only chat's memory), so the
original can move or change and the story (and its archive) keeps it. A large
one is scaled so its longest side is 2048 pixels and saved as JPEG (PNG when
it has transparency): an image model gets the look from far less than a
camera's full frame, and the endpoint takes about 4 MB a picture.

Every copy has its hidden data removed (`storage.image_meta`): a picture
kept as it is loses it without being re-encoded; one that can't be stripped
that way is re-encoded, which carries none over.

Qt ignores a photo's EXIF orientation unless asked, so pictures are read
through `load_image` / `load_pixmap` (a file) or `image_of` / `pixmap_of`
(bytes from a story's store), which apply it: a phone's portrait photo shows
upright, and one that is scaled is stored upright.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.plot_md import ParsedPlotFile, PlotProblem
from sealedlore.gui.image_codec import encoded, read_image
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.ids import new_id
from sealedlore.models.image import ImageRef
from sealedlore.storage.image_meta import MetadataError, picture_kind, strip_metadata
from sealedlore.storage.images import IMAGES_DIR, REFS_DIR
from sealedlore.storage.picture_store import PictureStore

MAX_SIDE = 2048
# Files over this are re-encoded even when small enough in pixels.
MAX_BYTES = 3 * 1024 * 1024
KEPT_AS_IS = {".png", ".jpg", ".jpeg", ".webp"}
THUMB = 88
PICTURE_FILTER = "Pictures (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*)"

# Where the open story keeps its pictures; None with no story open.
StoryPictures = Callable[[], "PictureStore | None"]


def _encoded(image: QImage, fmt: str, quality: int = -1) -> bytes:
    return encoded(image, fmt, quality)  # under the process's codec lock


def load_image(path: Path) -> QImage:
    """A picture file as it should be seen: its EXIF orientation applied, and
    one pixel a pixel (Qt reads "face@2x.png" as twice as dense, and the crop
    dialog drew it at half size under a full-size frame)."""
    image = read_image(lambda: QImageReader(str(path)))
    image.setDevicePixelRatio(1.0)
    return image


def load_pixmap(path: Path) -> QPixmap:
    image = load_image(path)
    return QPixmap() if image.isNull() else QPixmap.fromImage(image)


def image_of(data: bytes | None) -> QImage:
    """A picture's bytes as they should be seen, as `load_image` reads a file."""
    if not data:
        return QImage()
    array = QByteArray(data)
    buffer = QBuffer(array)
    buffer.open(QIODevice.ReadOnly)
    image = read_image(lambda: QImageReader(buffer))
    buffer.close()
    return image


def pixmap_of(data: bytes | None) -> QPixmap:
    image = image_of(data)
    return QPixmap() if image.isNull() else QPixmap.fromImage(image)


def pixmap_within(data: bytes | None, box: QSize) -> QPixmap:
    """A picture's bytes as `pixmap_of` reads them, no larger than `box` (its
    shape kept, and never enlarged): for a view that shows it no larger.
    Whole, a 2048-pixel picture is 16 MB for as long as it is held.

    Read whole and then scaled, as the view itself would scale it, so what is
    shown is the same to the pixel. Having the decoder scale it
    (`QImageReader.setScaledSize`) read a picture in two thirds of the time
    and lost detail: a JPEG's text and fine lines came out visibly softer.
    """
    pixmap = pixmap_of(data)
    size = pixmap.size()
    if size.width() <= box.width() and size.height() <= box.height():
        return pixmap
    return pixmap.scaled(
        size.scaled(box, Qt.KeepAspectRatio), Qt.KeepAspectRatio, Qt.SmoothTransformation
    )


def picture_bytes(source: Path) -> tuple[bytes, str]:
    """A picture file's bytes and extension, its hidden data removed, and
    scaled and re-encoded if needed.

    Raises ValueError when the file isn't a picture Qt can read.
    """
    image = load_image(source)
    if image.isNull():
        raise ValueError(f"{source.name} isn't a picture this app can read")
    return _prepared(image, source.read_bytes(), source.suffix)


def picture_bytes_of(data: bytes, suffix: str) -> tuple[bytes, str]:
    """As `picture_bytes`, for a picture already held as bytes (one the
    story made, used as a reference)."""
    image = image_of(data)
    if image.isNull():
        raise ValueError("that isn't a picture this app can read")
    return _prepared(image, data, suffix)


def _prepared(image: QImage, data: bytes, suffix: str) -> tuple[bytes, str]:
    if (
        suffix.lower() in KEPT_AS_IS
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


def clean_incoming(files: dict[str, bytes]) -> dict[str, bytes]:
    """A scenario's or a backup's files with nothing hidden left in their
    pictures. Storage strips what it can in place (JPEG, PNG, WebP) and,
    having no Qt, keeps anything else as it came; such a picture (a GIF, a
    BMP, a JPEG it can't follow) is re-encoded here, as one added from disk
    is. Records, videos (storage strips those) and anything that isn't a
    picture are left as they are."""
    from sealedlore.storage.images import RECORD_FILES
    from sealedlore.storage.video_meta import is_mp4

    cleaned: dict[str, bytes] = {}
    for relative, data in files.items():
        cleaned[relative] = data
        if relative in RECORD_FILES or is_mp4(data):
            continue
        try:
            cleaned[relative] = strip_metadata(data)
            continue
        except MetadataError:
            pass
        image = image_of(data)
        if image.isNull():
            continue
        if image.hasAlphaChannel():
            cleaned[relative] = strip_metadata(_encoded(image, "PNG"))
        else:
            cleaned[relative] = strip_metadata(_encoded(image, "JPEG", 92))
    return cleaned


def import_reference(source: Path, store: PictureStore, caption: str = "") -> ImageRef:
    """Copy a picture into the story as a reference. Raises ValueError or OSError."""
    data, suffix = picture_bytes(source)
    return store_reference(data, suffix, store, caption)


def store_reference(data: bytes, suffix: str, store: PictureStore, caption: str = "") -> ImageRef:
    """A picture already prepared (`picture_bytes`, or cropped) kept as a
    reference in the story."""
    ref_id = new_id()
    relative = f"{REFS_DIR}/{ref_id}{suffix}"
    store.write(relative, data)
    return ImageRef(id=ref_id, file=relative, caption=caption)


def inside(folder: Path, relative: str) -> Path | None:
    """`relative` under `folder`, or None if it leads out of it (by `..`, an
    absolute path, or a link): a plot file may be someone else's."""
    try:
        base = folder.resolve()
        target = (base / relative).resolve()
    except (OSError, RuntimeError):
        return None
    return target if target.is_relative_to(base) and target != base else None


def load_plot_pictures(parsed: ParsedPlotFile, folder: Path) -> list[PlotProblem]:
    """Bring a plot file's pictures into the scenario it was read into.

    Each `- picture:` line names a file beside the plot file. It is read as
    any added picture is (hidden data removed, scaled if large), becomes a
    reference picture on its character's card or its place's or lore entry's,
    and its bytes ride in the scenario to be written with the new story. The
    front matter's `background:` is read the same way and becomes the
    picture behind the transcript. One that is missing or unreadable is left
    out and returned as a note: it never stops the import.
    """
    scenario = parsed.scenario
    owners = {c.id: c for c in [*scenario.cast, *scenario.supporting]}
    owners.update({entry.id: entry for entry in scenario.lore})
    problems: list[PlotProblem] = []
    for picture in parsed.pictures:
        owner = owners.get(picture.owner_id)
        source = inside(folder, picture.source)
        background = picture.owner_kind == "background"
        if owner is None and not background:
            continue
        try:
            if source is None:
                raise ValueError("it leads outside the plot file's folder")
            if not source.is_file():
                raise ValueError("there is no such file beside the plot file")
            data, suffix = picture_bytes(source)
        except (OSError, ValueError) as exc:
            problems.append(
                PlotProblem(
                    picture.line,
                    f"{picture.owner_name}'s picture “{picture.source}” was left out: {exc}.",
                )
            )
            continue
        if background:
            # Where the window keeps a background chosen by hand.
            scenario.background_image = f"{IMAGES_DIR}/background{suffix}"
            scenario.reference_files[scenario.background_image] = base64.b64encode(data).decode(
                "ascii"
            )
            continue
        ref_id = new_id()
        ref = ImageRef(id=ref_id, file=f"{REFS_DIR}/{ref_id}{suffix}", caption=picture.caption)
        owner.reference_images.append(ref)
        scenario.reference_files[ref.file] = base64.b64encode(data).decode("ascii")
    return problems


def _scaled(pixmap: QPixmap, side: int) -> QPixmap:
    if pixmap.isNull():
        return QPixmap()
    return pixmap.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def thumbnail(path: Path, side: int = THUMB) -> QPixmap:
    """A file's thumbnail (the plot editor's pictures, beside the plot file)."""
    return _scaled(load_pixmap(path), side)


def thumbnail_of(data: bytes | None, side: int = THUMB) -> QPixmap:
    """A stored picture's thumbnail, made here and kept nowhere."""
    return _scaled(pixmap_of(data), side)


class RefImageStrip(QWidget):
    """Thumbnails of one owner's reference pictures, with add, caption and remove."""

    changed = Signal()
    # (picture id) Draw again… and Edit… on the chosen picture (after_portrait).
    again_requested = Signal(str)
    edit_requested = Signal(str)

    def __init__(
        self,
        pictures: StoryPictures,
        owner_word: str = "them",
        *,
        after_portrait: bool = False,
        crop: str = "free",
    ) -> None:
        """`after_portrait`: the first picture is the owner's portrait, shown
        large elsewhere (gui/portrait_view.py); the strip shows the others,
        and Make portrait puts the chosen one first. `crop` is the shape a
        picture added from disk is first offered at (engine/picture_shapes.py)."""
        super().__init__()
        self._pictures = pictures
        self._refs: list[ImageRef] | None = None
        self._after_portrait = after_portrait
        self._crop = crop

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
        person = owner_word == "them"
        self.portrait_button = QPushButton("Make portrait" if person else "Show first")
        self.portrait_button.setToolTip(
            "Show this one large, as their portrait" if person else "Show this one large, first"
        )
        self.portrait_button.clicked.connect(self._make_portrait)
        self.again_button = QPushButton("Draw again…")
        self.again_button.setToolTip("This picture drawn again; the new one is added")
        self.again_button.clicked.connect(lambda: self._ask(self.again_requested))
        self.edit_button = QPushButton("Edit…")
        self.edit_button.setToolTip("A changed copy of this picture; it is added")
        self.edit_button.clicked.connect(lambda: self._ask(self.edit_requested))
        if not after_portrait:
            # Hidden, never shown here: shown before it has a parent, a
            # widget is a window of its own (CLAUDE.md, the transcript's
            # rules); with one, it shows with the strip.
            for button in (self.portrait_button, self.again_button, self.edit_button):
                button.hide()
        self.hint = QLabel(
            "No other pictures. Add more to keep their look in generated images."
            if after_portrait
            else "No pictures. Add one to keep their look in generated images."
        )
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)

        # Wraps rather than widen the form: the Inspector is only as narrow
        # as its widest row (gui/fields.py, FormScroll).
        buttons = WrapRow(spacing=6, group_spacing=6)
        for button in (
            self.add_button,
            self.caption_button,
            self.remove_button,
            self.portrait_button,
            self.again_button,
            self.edit_button,
        ):
            buttons.add_group([button])

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
        store = self._pictures()
        shown = (refs or [])[1:] if self._after_portrait else (refs or [])
        for ref in shown:
            item = QListWidgetItem(ref.caption or "(no caption)")
            item.setData(Qt.UserRole, ref.id)
            item.setToolTip(ref.caption or ref.file)
            if store is not None:
                item.setIcon(QIcon(thumbnail_of(store.read(ref.file))))
            self.list.addItem(item)
        self.list.setVisible(bool(shown))
        self.hint.setVisible(refs is not None and not shown)
        self.add_button.setEnabled(refs is not None and store is not None)
        self._sync_buttons()

    def _sync_buttons(self, *_args) -> None:
        selected = self.list.currentItem() is not None
        self.caption_button.setEnabled(selected)
        self.remove_button.setEnabled(selected)
        self.portrait_button.setEnabled(selected)
        self.again_button.setEnabled(selected)
        self.edit_button.setEnabled(selected)

    def _ref(self, item: QListWidgetItem | None) -> ImageRef | None:
        if item is None or self._refs is None:
            return None
        ref_id = item.data(Qt.UserRole)
        return next((ref for ref in self._refs if ref.id == ref_id), None)

    def _add(self) -> None:
        store = self._pictures()
        if self._refs is None or store is None:
            return
        from sealedlore.gui.crop_dialog import cropped_picture

        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add reference pictures", str(Path.home()), PICTURE_FILTER
        )
        added, problems = False, []
        for number, path in enumerate(paths, 1):
            counter = f"{number} of {len(paths)}" if len(paths) > 1 else ""
            try:
                picture = cropped_picture(self, Path(path), self._crop, counter=counter)
                if picture is None:
                    continue  # this one left out
                self._refs.append(store_reference(*picture, store))
                added = True
            except (OSError, ValueError) as exc:
                problems.append(str(exc))
        if problems:
            QMessageBox.warning(self, "Couldn't add", "\n".join(problems))
        if added:
            self.set_refs(self._refs)
            self.changed.emit()

    def _edit_caption(self, item: QListWidgetItem | None) -> None:
        if not self.caption_button.isEnabled():
            return  # a double-click while the page is locked (gui/busy_lock.py)
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

    def _ask(self, signal) -> None:
        ref = self._ref(self.list.currentItem())
        if ref is not None:
            signal.emit(ref.id)

    def remove_first(self) -> None:
        """Take the first picture (the portrait, shown large) off; its file
        stays, as Remove leaves a picture's."""
        if self._refs:
            self.remove_ref(self._refs[0].id)

    def remove_ref(self, ref_id: str) -> None:
        """Take this picture off, wherever it now stands (a picture that
        landed meanwhile may have taken its place); its file stays."""
        if self._refs is None:
            return
        for index, ref in enumerate(self._refs):
            if ref.id == ref_id:
                self._refs.pop(index)
                self.set_refs(self._refs)
                self.changed.emit()
                return

    def _make_portrait(self) -> None:
        ref = self._ref(self.list.currentItem())
        if ref is None or self._refs is None:
            return
        self._refs.remove(ref)
        self._refs.insert(0, ref)
        self.set_refs(self._refs)
        self.changed.emit()

    def add_portrait(self) -> bool:
        """Choose a file to be the owner's portrait: it goes first. Whether
        one was added."""
        store = self._pictures()
        if self._refs is None or store is None:
            return False
        from sealedlore.gui.crop_dialog import cropped_picture

        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a portrait", str(Path.home()), PICTURE_FILTER
        )
        if not path:
            return False
        try:
            picture = cropped_picture(self, Path(path), self._crop)
            if picture is None:
                return False
            self._refs.insert(0, store_reference(*picture, store, "portrait"))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Couldn't add", str(exc))
            return False
        self.set_refs(self._refs)
        self.changed.emit()
        return True

    def _remove(self) -> None:
        ref = self._ref(self.list.currentItem())
        if ref is None or self._refs is None:
            return
        # The file stays: a generated image's record may name it, and an
        # Undo of the card would want it back. It is small.
        self._refs.remove(ref)
        self.set_refs(self._refs)
        self.changed.emit()


class CardIcons:
    """Cards' first pictures, small, for a list of names (the Cast and Lore
    pages): read once per picture and kept until another story's pictures
    are shown. A card with none gets a blank square, so the names line up."""

    def __init__(self, pictures: StoryPictures, side: int) -> None:
        self._pictures = pictures
        self._side = side
        self._icons: dict[str, QIcon] = {}
        self._store: object = None

    def icon(self, refs: list[ImageRef]) -> QIcon:
        store = self._pictures()
        # Another story's pictures: by the story, not the object, which a
        # story on disk makes anew each time it is asked (so nothing was ever
        # read once: every refresh read and decoded every card's picture).
        key = (store.story_id, store.root) if hasattr(store, "story_id") else id(store)
        if key != self._store:
            self._icons.clear()
            self._store = key
        first = refs[0] if refs else None
        key = first.file if first is not None else ""
        icon = self._icons.get(key)
        if icon is None:
            pixmap = (
                thumbnail_of(store.read(first.file), self._side)
                if first is not None and store is not None
                else QPixmap()
            )
            if pixmap.isNull():
                pixmap = QPixmap(self._side, self._side)
                pixmap.fill(Qt.transparent)
            icon = QIcon(pixmap)
            self._icons[key] = icon
        return icon
