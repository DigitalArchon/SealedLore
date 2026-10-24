"""Use as a character's picture… / Use as a place's picture… (the author, Oct
24 2026): a picture the story made (or the background, a picture added from
disk, a card's picture, a video's last frame) kept as a card's own. The
author chooses the card, crops or highlights the part that matters (the one
person for a portrait, the place without the rest of the scene), captions
it, and says what it becomes: the card's main picture (a character's
portrait, shown first; a place's current picture, shown behind the story
while the story is there), one among the card's pictures, or a reference
that a new picture is at once drawn from (a portrait of the person
highlighted, the place with nobody in it), in the picture dialog with its
default direction, which the author may change.

The crop is the one every picture from disk goes through
(gui/crop_dialog.py's view and geometry); what comes out is prepared as an
added picture is (`picture_bytes_of`: hidden data removed, scaled if large).
The engine's rule for where the picture goes is `engine/picture_use.py`;
the window writes the file and opens the picture dialog (gui/window_images.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRect, QSize
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.picture_shapes import FREE, ORIGINAL, PICTURE, PORTRAIT, SQUARE
from sealedlore.engine.picture_use import (
    FROM_SCENE_CAPTION,
    KEEP_DRAW,
    KEEP_MAIN,
    KEEP_OTHER,
    PORTRAIT_CAPTION,
    Card,
    main_words,
)
from sealedlore.engine.places import PLACE_CAPTION, place_for, place_title
from sealedlore.gui.crop_dialog import MIN_SIDE, CropView, shapes
from sealedlore.gui.ref_images import _encoded, image_of, picture_bytes_of
from sealedlore.gui.sizing import size_to_screen
from sealedlore.models.character import Character
from sealedlore.storage.image_meta import picture_kind

# The kinds the dialog is opened for (which cards it lists first).
CHARACTER, PLACE = "character", "place"
# The shapes offered: the two the cards are drawn in, and the free ones.
SHAPE_KEYS = (FREE, ORIGINAL, PORTRAIT, PICTURE, SQUARE)
# The chooser's entry for a lore entry made for the scene's location.
NEW_PLACE = "new-place"


@dataclass(frozen=True)
class PictureUse:
    """What the author chose: the card (None for a new lore entry named
    `new_title`, for the scene's location), the part (prepared bytes and
    their extension), its caption, and how it is kept (`picture_use.KEEPS`)."""

    card: Card | None
    new_title: str
    data: bytes
    suffix: str
    caption: str
    how: str


class UsePictureDialog(QDialog):
    def __init__(
        self,
        session,
        data: bytes,
        kind: str | None = None,
        parent: QWidget | None = None,
        *,
        preselect: str | None = None,
    ) -> None:
        """`kind`: CHARACTER or PLACE, which cards come first and are chosen
        to begin with; None lists both kinds alike. `preselect`: a card's
        id to start on."""
        super().__init__(parent)
        self.session = session
        self._data = data
        self._image = image_of(data)
        self.result_use: PictureUse | None = None
        self.setWindowTitle(
            "Use as a character's picture"
            if kind == CHARACTER
            else "Use as a place's picture"
            if kind == PLACE
            else "Use as a card's picture"
        )
        characters, lore = session.picture_cards()
        location = (session.story.scene.location or "").strip() if not session.story.chat else ""
        current = place_for(location, lore) if location else None

        intro = QLabel(
            "Keep this picture, or a part of it, as a card's own: drag a frame round the one "
            "person for a portrait, or round the place without the people in it. It can be "
            "the card's picture from now on, one more picture of it, or the reference a new "
            "picture is drawn from."
        )
        intro.setWordWrap(True)
        intro.setObjectName("hintLabel")

        self.card_choice = QComboBox()
        first = characters if kind == CHARACTER else lore
        second = lore if kind == CHARACTER else characters
        for cards in (first, second):
            for card in cards:
                label = (
                    f"Character: {card.name}"
                    if isinstance(card, Character)
                    else f"Place or lore: {card.title}"
                )
                self.card_choice.addItem(label, card.id)
            if cards is lore and location and current is None:
                # The scene is somewhere no entry names: one made for it, as
                # Picture of this place… does (engine/places.py).
                self.card_choice.addItem(
                    f"New lore entry for the scene's location: {place_title(location)}", NEW_PLACE
                )
        self._cards = {card.id: card for card in (*characters, *lore)}
        self._location = location
        wanted = preselect
        if wanted is None and kind == PLACE:
            wanted = current.id if current is not None else NEW_PLACE if location else None
        if wanted is None and kind == CHARACTER:
            held = session.story.held_character_id
            wanted = held if held in self._cards else None
        index = self.card_choice.findData(wanted) if wanted is not None else -1
        self.card_choice.setCurrentIndex(max(index, 0))
        self.card_choice.currentIndexChanged.connect(self._on_card)

        # Made before anything that syncs to them (a radio ticked, the frame set).
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.keep_button = self.buttons.addButton("Keep", QDialogButtonBox.AcceptRole)
        self.keep_button.setDefault(True)
        self.buttons.accepted.connect(self._approve)
        self.buttons.rejected.connect(self.reject)

        self.view = CropView(self._image)
        self.shape = QComboBox()
        for shape in shapes():
            if shape.key in SHAPE_KEYS:
                self.shape.addItem(shape.label, shape.key)
        self.shape.currentIndexChanged.connect(self._on_shape)
        self.whole_button = QPushButton("Use the whole picture")
        self.whole_button.setToolTip("Frame the whole picture: nothing is cut off")
        self.whole_button.clicked.connect(self._take_whole)
        self.size_label = QLabel()
        self.size_label.setObjectName("hintLabel")
        self.size_label.setWordWrap(True)
        self.view.changed.connect(self._show_size)

        self.caption = QLineEdit()
        self.caption.setPlaceholderText(
            "What the picture shows (optional): 'in the rain', 'at night'"
        )

        self.keep_main = QRadioButton()
        self.keep_other = QRadioButton()
        self.keep_draw = QRadioButton()
        self._group = QButtonGroup(self)
        for button in (self.keep_main, self.keep_other, self.keep_draw):
            self._group.addButton(button)
            button.toggled.connect(self._sync)
        self.keep_main.setChecked(True)

        card_row = QHBoxLayout()
        card_row.addWidget(QLabel("For"))
        card_row.addWidget(self.card_choice, 1)
        shape_row = QHBoxLayout()
        shape_row.addWidget(QLabel("Shape"))
        shape_row.addWidget(self.shape, 1)
        shape_row.addWidget(self.whole_button)
        caption_row = QHBoxLayout()
        caption_row.addWidget(QLabel("Caption"))
        caption_row.addWidget(self.caption, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(card_row)
        layout.addWidget(self.view, 1)
        layout.addLayout(shape_row)
        layout.addWidget(self.size_label)
        layout.addLayout(caption_row)
        for button in (self.keep_main, self.keep_other, self.keep_draw):
            layout.addWidget(button)
        layout.addWidget(self.buttons)
        self._on_card()
        size_to_screen(self, 0.55, 0.85, at_least=QSize(960, 760))

    # --- the card ----------------------------------------------------------------

    def card(self) -> Card | None:
        """The card chosen; None for a new lore entry (`new_title`)."""
        return self._cards.get(self.card_choice.currentData())

    def new_title(self) -> str:
        return place_title(self._location) if self.card_choice.currentData() == NEW_PLACE else ""

    def _on_card(self) -> None:
        """The words and the first shape follow the card: a portrait's for a
        character, a place picture's for a lore entry."""
        card = self.card()
        person = isinstance(card, Character)
        name = card.name if person else card.title if card is not None else self.new_title()
        main = main_words(card) if card is not None else f"the picture of {name}"
        has = bool(card.reference_images) if card is not None else False
        self.keep_main.setText(
            f"Keep this part as {main}"
            + (
                " (shown first on their card)"
                if person
                else " (shown behind the story while the story is there)"
            )
        )
        not_as = "the portrait" if person else "the current one"
        self.keep_other.setText(f"Keep it among {name}'s pictures, not as {not_as}")
        self.keep_other.setEnabled(has)
        self.keep_other.setToolTip(
            "" if has else f"{name} has no picture yet, so this one would be the only one"
        )
        if not has and self.keep_other.isChecked():
            self.keep_main.setChecked(True)
        self.keep_draw.setText(
            f"Keep it as a reference, and draw {'a portrait of' if person else 'a picture of'} "
            f"{name} from it…"
        )
        self.keep_draw.setToolTip(
            "The picture dialog opens with this part as image 1 and a direction for "
            + (
                "a portrait of the person shown; the portrait drawn becomes theirs."
                if person
                else "the place shown with nobody in it; the picture drawn becomes the place's."
            )
        )
        index = self.shape.findData(PORTRAIT if person else PICTURE)
        if index >= 0 and self.shape.currentIndex() != index:
            self.shape.setCurrentIndex(index)
        else:
            self._on_shape()
        self._sync()

    # --- the frame -----------------------------------------------------------------

    def _ratio(self, key: str) -> float | None:
        if key == ORIGINAL:
            return self._image.width() / max(self._image.height(), 1)
        shape = next((s for s in shapes() if s.key == key), None)
        return shape.ratio if shape is not None else None

    def _on_shape(self) -> None:
        self.view.set_ratio(self._ratio(str(self.shape.currentData())))

    def _take_whole(self) -> None:
        index = self.shape.findData(ORIGINAL)
        if index >= 0:
            self.shape.setCurrentIndex(index)
        self.view.set_rect((0.0, 0.0, float(self._image.width()), float(self._image.height())))

    def whole(self) -> bool:
        return self.view.crop() == QRect(0, 0, self._image.width(), self._image.height())

    def _show_size(self) -> None:
        crop = self.view.crop()
        if self.whole():
            text = f"The whole picture, {self._image.width()} × {self._image.height()} pixels."
        else:
            text = (
                f"Keeps {crop.width()} × {crop.height()} pixels of {self._image.width()} × "
                f"{self._image.height()}."
            )
        self.size_label.setText(text)
        self._sync()

    def _sync(self, *_args) -> None:
        crop = self.view.crop()
        least = min(MIN_SIDE, self._image.width(), self._image.height())
        self.keep_button.setEnabled(min(crop.width(), crop.height()) >= least)
        self.keep_button.setText("Keep and draw…" if self.keep_draw.isChecked() else "Keep")

    # --- the result ------------------------------------------------------------------

    def how(self) -> str:
        if self.keep_draw.isChecked():
            return KEEP_DRAW
        return KEEP_OTHER if self.keep_other.isChecked() else KEEP_MAIN

    def _part(self) -> tuple[bytes, str]:
        """The framed part, prepared as an added picture is; the whole
        picture as it is when nothing is cut off."""
        if self.whole():
            return picture_bytes_of(self._data, picture_kind(self._data) or ".png")
        part = self._image.copy(self.view.crop())
        if part.hasAlphaChannel():
            return picture_bytes_of(_encoded(part, "PNG"), ".png")
        return picture_bytes_of(_encoded(part, "JPEG", 92), ".jpg")

    def _approve(self) -> None:
        card = self.card()
        how = self.how()
        caption = " ".join(self.caption.text().split())
        if not caption:
            person = isinstance(card, Character)
            if how == KEEP_DRAW:
                caption = FROM_SCENE_CAPTION
            elif how == KEEP_MAIN:
                caption = PORTRAIT_CAPTION if person else PLACE_CAPTION
        data, suffix = self._part()
        self.result_use = PictureUse(
            card=card,
            new_title=self.new_title(),
            data=data,
            suffix=suffix,
            caption=caption,
            how=how,
        )
        self.accept()
