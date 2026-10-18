"""Look ▾ → Edit the tile set… (map-first play; engine/session_area.py):
every asset the map is drawn with, one at a time. The author sees each
picture, the words it was drawn from and where it came from (the set's,
drawn for this story, changed for this story), and may draw one again from
words of their own (asked first, with the price), put a picture of their own
in its place, or go back to the set's own. A change is for this story only,
over whichever set is in use.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.picture_shapes import FREE, SQUARE
from sealedlore.engine.session_area import TileAsset
from sealedlore.engine.tile_sets import TEXTURES
from sealedlore.gui.crop_dialog import cropped_picture
from sealedlore.gui.image_codec import image_from_data
from sealedlore.gui.ref_images import PICTURE_FILTER
from sealedlore.gui.sizing import GrowingText, size_to_screen

ICON = 48
PREVIEW = 256
# Thumbnails read a few at a time, so a set of hundreds opens at once.
ICONS_A_TICK = 24
# How often the dialog looks whether a redraw has come in.
POLL_MS = 1000
ROLE = Qt.UserRole


class TileSetEditor(QDialog):
    def __init__(self, session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Edit the tile set")
        size_to_screen(self, 0.55, 0.8, at_least=QSize(900, 640))
        art = session.map_art_copy()
        name = session.tile_set_name(art)
        titles = dict(session.tile_set_choices())
        heading = QLabel(
            f"The map is drawn in <b>{titles.get(name, name or 'no set')}</b>. A change here is "
            "for this story only."
        )
        heading.setWordWrap(True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find an asset…")
        self.search.textChanged.connect(self._filter)
        self.list = QListWidget()
        self.list.setIconSize(QSize(ICON, ICON))
        self.list.setUniformItemSizes(False)
        self.list.currentItemChanged.connect(self._chosen)
        self.preview = QLabel()
        self.preview.setFixedSize(PREVIEW, PREVIEW)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setObjectName("tilePreview")
        self.label = QLabel()
        self.label.setObjectName("emptyTitle")
        self.source = QLabel()
        self.source.setObjectName("hintLabel")
        self.words = GrowingText(lines=3, most=10)
        self.words.setPlaceholderText("What it shows, in a few words")
        self.redraw_button = QPushButton("Draw it again…")
        self.redraw_button.setToolTip(
            "Draw this one again from the words above: asked first, with the model and price"
        )
        self.redraw_button.clicked.connect(self._redraw)
        self.own_button = QPushButton("Use a picture of your own…")
        self.own_button.clicked.connect(self._own)
        self.reset_button = QPushButton("Back to the set's own")
        self.reset_button.clicked.connect(self._reset)
        self.status = QLabel()
        self.status.setObjectName("hintLabel")
        self.status.setWordWrap(True)
        side = QVBoxLayout()
        side.addWidget(self.preview, 0, Qt.AlignHCenter)
        side.addWidget(self.label)
        side.addWidget(self.source)
        side.addWidget(QLabel("What it shows:"))
        side.addWidget(self.words)
        side.addWidget(self.redraw_button)
        side.addWidget(self.own_button)
        side.addWidget(self.reset_button)
        side.addWidget(self.status)
        side.addStretch(1)
        left = QVBoxLayout()
        left.addWidget(self.search)
        left.addWidget(self.list, 1)
        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addLayout(side)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addLayout(body, 1)
        layout.addWidget(buttons)
        self._assets: dict[str, TileAsset] = {}
        self._unread: list[QListWidgetItem] = []
        # The set's reader, taken once a fill: through the session it copied
        # the map's art for every row.
        self._read = None
        self._drawing = session.tile_asset_drawing
        self._icons = QTimer(self)
        self._icons.setInterval(0)
        self._icons.timeout.connect(self._read_icons)
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._check_drawing)
        self._poll.start()
        self._fill()

    # --- the list ------------------------------------------------------------------

    def _fill(self, keep: str | None = None) -> None:
        self.list.clear()
        self._unread = []
        self._assets = {a.role: a for a in self.session.tile_set_assets()}
        _info, self._read = self.session.tile_set()
        group = None
        current = None
        for asset in self._assets.values():
            if asset.group != group:
                group = asset.group
                header = QListWidgetItem(group)
                header.setFlags(Qt.NoItemFlags)
                font = header.font()
                font.setBold(True)
                header.setFont(font)
                self.list.addItem(header)
            item = QListWidgetItem(self._line(asset))
            item.setData(ROLE, asset.role)
            self.list.addItem(item)
            self._unread.append(item)
            if asset.role == keep:
                current = item
        self._icons.start()
        if current is not None:
            self.list.setCurrentItem(current)
        else:
            items = (self.list.item(i) for i in range(self.list.count()))
            first = next((item for item in items if item.data(ROLE)), None)
            if first is not None:
                self.list.setCurrentItem(first)
        self._filter(self.search.text())

    @staticmethod
    def _line(asset: TileAsset) -> str:
        mark = "" if asset.source == "the set's" else "  ✎" if "changed" in asset.source else "  ●"
        return f"{asset.label}{mark}"

    def _read_icons(self) -> None:
        for _ in range(ICONS_A_TICK):
            if not self._unread:
                self._icons.stop()
                return
            item = self._unread.pop(0)
            role = item.data(ROLE)
            image = self._image(role)
            if image is not None:
                item.setIcon(QIcon(QPixmap.fromImage(_fit(image, ICON))))

    def _image(self, role: str) -> QImage | None:
        if role.startswith("walker/") or self._read is None:
            data = self.session.tile_asset_bytes(role)
        else:
            data = self._read(role)
        if not data:
            return None
        image = image_from_data(data)
        return None if image.isNull() else image

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        for i in range(self.list.count()):
            item = self.list.item(i)
            role = item.data(ROLE)
            if not role:
                item.setHidden(bool(words))
                continue
            asset = self._assets[role]
            hay = f"{asset.label} {asset.words} {asset.group}".lower()
            item.setHidden(not all(w in hay for w in words))

    # --- one asset -------------------------------------------------------------------

    def _current(self) -> TileAsset | None:
        item = self.list.currentItem()
        role = item.data(ROLE) if item is not None else None
        return self._assets.get(role) if role else None

    def _chosen(self, *_args) -> None:
        asset = self._current()
        enabled = asset is not None
        for widget in (self.words, self.redraw_button, self.own_button, self.reset_button):
            widget.setEnabled(enabled)
        if asset is None:
            self.preview.clear()
            self.label.setText("")
            self.source.setText("")
            return
        image = self._image(asset.role)
        if image is not None:
            self.preview.setPixmap(QPixmap.fromImage(_fit(image, PREVIEW)))
        else:
            self.preview.setText("Not drawn: the map uses a plain stand-in")
        self.label.setText(asset.label)
        self.source.setText(f"{asset.group}: {asset.source}")
        self.words.setPlainText(asset.words)
        self.reset_button.setEnabled(asset.source != "the set's")
        self._sync_status()

    def _sync_status(self) -> None:
        drawing = self.session.tile_asset_drawing
        asset = self._current()
        if drawing is not None:
            label = self._assets[drawing].label if drawing in self._assets else drawing
            self.status.setText(f"Drawing {label}…")
        else:
            self.status.setText("")
        self.redraw_button.setEnabled(asset is not None and drawing is None)

    def _check_drawing(self) -> None:
        drawing = self.session.tile_asset_drawing
        if drawing != self._drawing:
            finished = self._drawing
            self._drawing = drawing
            if finished is not None and drawing is None:
                current = self._current()
                self._fill(keep=current.role if current else finished)
        self._sync_status()

    def _redraw(self) -> None:
        asset = self._current()
        if asset is None:
            return
        words = self.words.toPlainText()
        model, price, problem = self.session.tile_asset_offer(asset.role, words)
        if problem is not None:
            QMessageBox.information(self, "Draw it again", problem)
            return
        cost = f"about ${price:.3f}" if price is not None else "at the model's listed price"
        answer = QMessageBox.question(
            self,
            "Draw it again?",
            f"{asset.label} is drawn again by {model}, {cost}, from the words you gave. It counts "
            "against today's map pictures, and the map takes it once it is in.",
        )
        if answer != QMessageBox.Yes:
            return
        if self.session.redraw_tile_asset(asset.role, words):
            self._drawing = asset.role
            self._sync_status()

    def _own(self) -> None:
        asset = self._current()
        if asset is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f"A picture for {asset.label}", str(Path.home()), PICTURE_FILTER
        )
        if not path:
            return
        # The ground and walls tile, so they are square; a sprite or a
        # walker is whatever shape it is (every asset was cropped square).
        shape = SQUARE if asset.role in TEXTURES else FREE
        try:
            picture = cropped_picture(self, Path(path), shape)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Couldn't use the picture", str(exc))
            return
        if picture is None:
            return
        self.session.use_own_tile_asset(asset.role, picture[0])
        self._fill(keep=asset.role)

    def _reset(self) -> None:
        asset = self._current()
        if asset is None:
            return
        self.session.reset_tile_asset(asset.role)
        self._fill(keep=asset.role)


def _fit(image: QImage, side: int) -> QImage:
    return image.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
