"""Every picture and video in the story, in one place (the author, Oct 15
2026): the cast's pictures, the lore's, the story's own, and the map's, each
a section.

The story's are its background first (the author: it belongs with the
story's pictures, to be seen, saved, drawn anew or edited), then every
picture and video made for it, newest first, including those whose passage
was deleted or sits on another branch: the transcript shows a picture only
after its passage, and a picture costs money, so it is never lost with a
branch. A picture drawn for a card shows under its card (`ImageRef.drawn_as`),
not twice; taken off the card, it is the story's again. The background drawn
here shows as the background (`Story.background_drawn_as`), not twice
either. Cards the plot hasn't brought in are left out unless spoilers are
shown. Every picture can be drawn again or edited from its menu, as on the
Cast and Lore pages. The map's are its paintings and postcards.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.picture_words import AGAIN_LABEL, EDIT_LABEL
from sealedlore.gui.ref_images import thumbnail_of
from sealedlore.gui.transcript import IMAGE_ACTIONS, picture_caption
from sealedlore.gui.video_view import VIDEO_ACTIONS, video_caption
from sealedlore.models.character import Character
from sealedlore.models.image import GeneratedImage, ImageRef
from sealedlore.models.lore import LoreEntry
from sealedlore.models.video import GeneratedVideo
from sealedlore.models.world import MapPicture
from sealedlore.storage.picture_store import PictureStore

GALLERY_THUMB = 150
CARD_THUMB = 110


class _Gallery(QListWidget):
    """Pictures in a grid as tall as its rows: the page scrolls, not it."""

    def __init__(self, thumb: int) -> None:
        super().__init__()
        self.setObjectName("imageGallery")
        self.setViewMode(QListWidget.IconMode)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setIconSize(QSize(thumb, thumb))
        # Room for two lines of caption under the picture.
        self.setGridSize(QSize(thumb + 40, thumb + 64))
        self.setSpacing(4)
        self.setWordWrap(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.CustomContextMenu)

    def fit(self) -> None:
        grid = self.gridSize()
        columns = max(1, (self.viewport().width() or self.width()) // max(1, grid.width()))
        rows = -(-self.count() // columns)
        self.setFixedHeight(rows * grid.height() + 2 * self.frameWidth() + 4)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self.fit()


class ImagesPanel(QWidget):
    # (action, image id), as the transcript's picture menu.
    action_requested = Signal(str, str)
    # (action, video id), as the transcript's video menu.
    video_action_requested = Signal(str, str)
    generate_requested = Signal()
    generate_video_requested = Signal()
    save_all_requested = Signal()
    # (action, card id, picture id) for a card's picture: "open", "show"
    # (on its page), "save", "again", "edit".
    card_action_requested = Signal(str, str, str)
    # An action on the background (BACKGROUND_ACTIONS).
    background_action_requested = Signal(str)
    # (action, map picture id) for an isometric view or a postcard (MAP_ACTIONS).
    map_action_requested = Signal(str, str)
    # (action, picture id) for a picture added from disk to no card
    # (`Story.reference_images`, ADDED_ACTIONS).
    added_action_requested = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        # The story's own pictures and videos (`list`, as it has always been).
        self.list = _Gallery(GALLERY_THUMB)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.itemActivated.connect(self._activate)
        self.cast_list = _Gallery(CARD_THUMB)
        self.lore_list = _Gallery(CARD_THUMB)
        for gallery in (self.cast_list, self.lore_list):
            gallery.itemActivated.connect(lambda item: self._card_action("open", item))
            gallery.customContextMenuRequested.connect(
                lambda position, g=gallery: self._card_menu(g, position)
            )
        self.map_list = _Gallery(CARD_THUMB)
        self.map_list.itemActivated.connect(lambda item: self._map_action("open", item))
        self.map_list.customContextMenuRequested.connect(self._map_menu)
        self.cast_heading = _heading()
        self.lore_heading = _heading()
        self.story_heading = _heading()
        self.map_heading = _heading()
        self.hint = QLabel(
            "No pictures or videos yet. Picture… beside Send (or Story → Draw a picture…) "
            "pictures what is happening, and Story → Generate video… makes a few seconds of "
            "it; right-click one here for more."
        )
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)
        self.generate_button = QPushButton("Picture…")
        self.generate_button.setToolTip("Generate a picture of what is happening")
        self.generate_button.clicked.connect(self.generate_requested)
        self.generate_video_button = QPushButton("Video…")
        self.generate_video_button.setToolTip(
            "Generate a few seconds of video. Far dearer than a picture: the price is shown "
            "before anything is sent"
        )
        self.generate_video_button.clicked.connect(self.generate_video_requested)
        self.save_all_button = QPushButton("Save all…")
        self.save_all_button.setToolTip(
            "Copy every picture and video here, the cards' too, into a folder you choose"
        )
        self.save_all_button.clicked.connect(self.save_all_requested)
        self.save_all_button.setEnabled(False)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.generate_button, 1)
        buttons.addWidget(self.generate_video_button, 1)
        buttons.addWidget(self.save_all_button)

        page = QWidget()
        sections = QVBoxLayout(page)
        sections.setContentsMargins(0, 0, 0, 0)
        sections.setSpacing(6)
        for heading, gallery in (
            (self.cast_heading, self.cast_list),
            (self.lore_heading, self.lore_list),
            (self.story_heading, self.list),
            (self.map_heading, self.map_list),
        ):
            sections.addWidget(heading)
            sections.addWidget(gallery)
        sections.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.hint)
        layout.addWidget(scroll, 1)
        layout.addLayout(buttons)

    def set_images(
        self,
        images: list[GeneratedImage],
        store: PictureStore | None,
        on_path: set[str],
        videos: list[GeneratedVideo] | None = None,
        *,
        cast: list[Character] | None = None,
        lore: list[LoreEntry] | None = None,
        background: str | None = None,
        background_drawn_as: str = "",
        map_pictures: list[MapPicture] | None = None,
        shown_views: set[str] | None = None,
        drawn_for_cards: set[str] | None = None,
        added: list[ImageRef] | None = None,
    ) -> None:
        """`cast` and `lore`: the cards whose pictures to show (those the
        author may see), each picture under its card's name;
        `drawn_for_cards`: every picture drawn as a card's, those not shown
        too (a hidden card's drawing showed under Story, its prompt with it).
        `background`: the background's file, first among the story's,
        standing for the picture it was drawn as; `added`: the pictures
        added from disk to no card. `map_pictures`: the map's views and
        postcards (those of places the author may see); `shown_views`: the
        paintings laid over the map now, said so (an area may have
        several, and any can be shown from here)."""
        cast, lore = cast or [], lore or []
        on_cards = {ref.drawn_as for card in (*cast, *lore) for ref in card.reference_images}
        on_cards |= drawn_for_cards or set()
        if background:
            on_cards.add(background_drawn_as)
        self.list.clear()
        data = store.read(background) if store is not None and background else None
        if data is not None:
            item = QListWidgetItem("Background\n(behind the story's text)")
            item.setData(Qt.UserRole, "background")
            item.setData(Qt.UserRole + 1, "background")
            item.setToolTip(
                "The picture behind the story's text. Right-click to see it whole, save it, "
                "draw a new one or edit it"
            )
            item.setIcon(QIcon(thumbnail_of(data, GALLERY_THUMB)))
            self.list.addItem(item)
        for ref in added or []:
            item = QListWidgetItem("Added picture" + (f"\n{ref.caption}" if ref.caption else ""))
            item.setData(Qt.UserRole, ref.id)
            item.setData(Qt.UserRole + 1, "added")
            item.setToolTip("A picture added from disk, to send with picture prompts")
            if store is not None:
                item.setIcon(QIcon(thumbnail_of(store.read(ref.file), GALLERY_THUMB)))
            self.list.addItem(item)
        made = sorted(
            [
                *(("image", image) for image in images if image.id not in on_cards),
                *(("video", v) for v in videos or ()),
            ],
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
        counts: dict[str, int] = {}
        card_galleries = ((self.cast_list, cast, "Cast"), (self.lore_list, lore, "Lore"))
        for gallery, cards, word in card_galleries:
            gallery.clear()
            for card in cards:
                name = card.name if isinstance(card, Character) else card.title
                for ref in card.reference_images:
                    shown = ref.caption if ref.caption and ref.caption != "portrait" else ""
                    caption = f"{name}\n{shown}" if shown else name
                    item = QListWidgetItem(caption)
                    item.setData(Qt.UserRole, card.id)
                    item.setData(Qt.UserRole + 1, ref.id)
                    item.setToolTip(ref.prompt or ref.caption or name)
                    if store is not None:
                        item.setIcon(QIcon(thumbnail_of(store.read(ref.file), CARD_THUMB)))
                    gallery.addItem(item)
            counts[word] = gallery.count()
        counts["Story"] = self.list.count()
        self.map_list.clear()
        for picture in sorted(map_pictures or [], key=lambda p: p.made_at, reverse=True):
            kind = {"isometric": "Painting", "postcard": "Postcard", "walker": "Avatar"}.get(
                picture.subject, "Picture"
            )
            on_map = picture.id in (shown_views or set())
            walker = picture.subject == "walker"
            marked = (
                "\n✓ walks on the map" if on_map and walker else "\n✓ on the map" if on_map else ""
            )
            item = QListWidgetItem(f"{kind}\n{picture.name}" + marked)
            item.setData(Qt.UserRole, picture.id)
            item.setData(Qt.UserRole + 1, picture.subject)
            made = "drawn" if walker else "painted"
            item.setToolTip(
                f"{kind} of {picture.name}"
                + (f", {made} by {picture.model}" if picture.model else ", your own picture")
                + (
                    ", the one walking for them now"
                    if on_map and walker
                    else "; right-click to have it walk for them"
                    if walker
                    else ", the one laid over the map now"
                    if on_map
                    else "; right-click to lay it over the map"
                    if picture.subject == "isometric"
                    else ""
                )
            )
            if store is not None:
                item.setIcon(QIcon(thumbnail_of(store.read(picture.file), CARD_THUMB)))
            self.map_list.addItem(item)
        counts["Map"] = self.map_list.count()
        for heading, gallery, word in (
            (self.cast_heading, self.cast_list, "Cast"),
            (self.lore_heading, self.lore_list, "Lore"),
            (self.story_heading, self.list, "Story"),
            (self.map_heading, self.map_list, "Map"),
        ):
            heading.setText(f"{word.upper()} · {counts[word]}")
            shown = counts[word] > 0
            heading.setVisible(shown)
            gallery.setVisible(shown)
            gallery.fit()
        anything = any(counts.values())
        self.hint.setVisible(not anything)
        self.save_all_button.setEnabled(anything)

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

    def _card_action(self, action: str, item: QListWidgetItem | None) -> None:
        if item is not None:
            self.card_action_requested.emit(
                action, item.data(Qt.UserRole), item.data(Qt.UserRole + 1)
            )

    def _card_menu(self, gallery: QListWidget, position: QPoint) -> None:
        item = gallery.itemAt(position)
        if item is None:
            return
        # Read now: a picture landing while the menu is open rebuilds the
        # gallery, and the item is gone by the time a choice is made.
        card_id, ref_id = item.data(Qt.UserRole), item.data(Qt.UserRole + 1)
        menu = QMenu(self)
        for key, text in CARD_ACTIONS:
            if key == "-":
                menu.addSeparator()
                continue
            menu.addAction(text).triggered.connect(
                lambda _checked=False, k=key: self.card_action_requested.emit(k, card_id, ref_id)
            )
        menu.exec(gallery.viewport().mapToGlobal(position))

    def _map_action(self, action: str, item: QListWidgetItem | None) -> None:
        if item is not None:
            self.map_action_requested.emit(action, item.data(Qt.UserRole))

    def _map_menu(self, position: QPoint) -> None:
        item = self.map_list.itemAt(position)
        if item is None:
            return
        picture_id = item.data(Qt.UserRole)  # read now, as `_card_menu`
        isometric = item.data(Qt.UserRole + 1) == "isometric"
        walker = item.data(Qt.UserRole + 1) == "walker"
        menu = QMenu(self)
        for key, text in MAP_ACTIONS:
            if key == "use" and walker:
                text = "Use as the avatar"
            elif key == "use" and not isometric:
                continue  # a postcard is the place's one picture
            menu.addAction(text).triggered.connect(
                lambda _checked=False, k=key: self.map_action_requested.emit(k, picture_id)
            )
        menu.exec(self.map_list.viewport().mapToGlobal(position))

    def _activate(self, item: QListWidgetItem) -> None:
        kind = item.data(Qt.UserRole + 1)
        if kind == "video":
            self.video_action_requested.emit("show", item.data(Qt.UserRole))
        elif kind == "background":
            self.background_action_requested.emit("open")
        elif kind == "added":
            self.added_action_requested.emit("open", item.data(Qt.UserRole))
        else:
            self.action_requested.emit("open", item.data(Qt.UserRole))

    def _menu(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        if item is None:
            return
        image_id = item.data(Qt.UserRole)
        menu = QMenu(self)
        if item.data(Qt.UserRole + 1) == "added":
            for key, text in ADDED_ACTIONS:
                menu.addAction(text).triggered.connect(
                    lambda _checked=False, k=key: self.added_action_requested.emit(k, image_id)
                )
            menu.exec(self.list.viewport().mapToGlobal(position))
            return
        if item.data(Qt.UserRole + 1) == "background":
            for key, text in BACKGROUND_ACTIONS:
                if key == "-":
                    menu.addSeparator()
                    continue
                menu.addAction(text).triggered.connect(
                    lambda _checked=False, k=key: self.background_action_requested.emit(k)
                )
            menu.exec(self.list.viewport().mapToGlobal(position))
            return
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


CARD_ACTIONS = (
    ("open", "See it whole"),
    ("show", "Show its card"),
    ("save", "Save picture as…"),
    ("-", ""),
    ("again", AGAIN_LABEL),
    ("edit", EDIT_LABEL),
)
BACKGROUND_ACTIONS = (
    ("open", "See it whole"),
    ("save", "Save picture as…"),
    ("-", ""),
    ("draw", "Draw a new background…"),
    ("edit", EDIT_LABEL),
    ("file", "Choose a file instead…"),
    ("-", ""),
    ("clear", "Clear the background"),
)
MAP_ACTIONS = (
    ("open", "See it whole"),
    ("show", "Show on the map"),
    # A painting's alone: chosen as the one laid over its area.
    ("use", "Show this painting on the map"),
    ("save", "Save picture as…"),
)
ADDED_ACTIONS = (
    ("open", "See it whole"),
    ("save", "Save picture as…"),
)


def _heading() -> QLabel:
    label = QLabel()
    label.setObjectName("composerCaption")
    return label
