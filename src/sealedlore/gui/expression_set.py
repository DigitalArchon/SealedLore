"""A set of expressions… on a character (engine/looks.py): the character
drawn with each of several expressions or poses, every one an edit of the
same picture of them, so the face, the clothes and everything else stay.

The author chooses the picture they are drawn from and the expressions, and
sees every prompt (each may be changed) and the price of the whole set
before anything is sent; each prompt is then sent exactly as it stands, in
the background, as an ordinary picture of the card. Each lands on the card
labelled: the source's look, and its own expression.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.image_variants import family_of, fitting_model, picture_support
from sealedlore.engine.images import ImageRequest
from sealedlore.engine.looks import COMMON_EXPRESSIONS, look_change, look_words
from sealedlore.engine.portraits import card_size
from sealedlore.gui.image_jobs import ImageCatalog
from sealedlore.gui.model_picker import ModelField
from sealedlore.gui.ref_images import thumbnail_of
from sealedlore.gui.sizing import GrowingText, size_to_screen
from sealedlore.models.character import Character
from sealedlore.models.config import RECOMMENDED_IMAGE_MODELS
from sealedlore.providers.images import ImageModelInfo

SET_LABEL = "A set of expressions…"
SET_TIP = (
    "The character drawn with each of several expressions or poses you tick, each an edit "
    "of the picture you choose, so they stay the same person in the same clothes. You see "
    "every prompt and the price first."
)
THUMB_SIDE = 120
# Ticked to begin with: a small set that covers most scenes, less what the
# picture's look already has (Doomsville's John has all four in his coat:
# ticked anyway, they would be drawn and paid for again).
FIRST_TICKED = ("neutral", "happy", "sad", "angry")


class ExpressionSetDialog(QDialog):
    def __init__(
        self,
        session,
        character: Character,
        *,
        source_id: str | None,
        catalog: ImageCatalog | None,
        browse: Callable[[str], str | None] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        assert character.reference_images, "drawn from one of their pictures"
        self.session = session
        self.character = character
        self._catalog = catalog
        self._prompts: dict[str, str] = {}  # an expression's prompt, as the author left it
        self._boxes: dict[str, GrowingText] = {}
        # The author has clicked a tick: another picture to draw from no
        # longer resets them.
        self._ticked_by_hand = False
        self.setWindowTitle(f"A set of expressions: {character.name}")
        self.setMinimumWidth(640)
        size_to_screen(self, 0.5, 0.85)

        intro = QLabel(
            f"{character.name} drawn with each expression you tick. Each is an edit of the "
            "picture below, sent with its prompt, so the face, the clothes and everything else "
            "stay; each is added to their pictures, labelled, when it is drawn."
        )
        intro.setWordWrap(True)
        self.source = QComboBox()
        for number, ref in enumerate(character.reference_images, 1):
            self.source.addItem(
                "Their portrait" if number == 1 else look_words(ref, number), ref.id
            )
        start = self.source.findData(source_id) if source_id else 0
        self.source.setCurrentIndex(max(start, 0))
        self.source.currentIndexChanged.connect(self._on_source)
        self.picture = QLabel()
        self.picture.setFixedSize(THUMB_SIDE, THUMB_SIDE)
        self.picture.setAlignment(Qt.AlignCenter)

        ticks = QGridLayout()
        ticks.setSpacing(4)
        self.ticks: dict[str, QCheckBox] = {}
        for index, expression in enumerate(COMMON_EXPRESSIONS):
            box = QCheckBox(expression)
            box.toggled.connect(self._rebuild)
            box.clicked.connect(self._by_hand)
            self.ticks[expression] = box
            ticks.addWidget(box, index // 3, index % 3)
        self.others = QLineEdit()
        self.others.setPlaceholderText("laughing, crying, asleep (separated by commas)")
        self.others.textChanged.connect(self._rebuild)

        self.model = ModelField(
            self._first_model(),
            browse,
            recommended=RECOMMENDED_IMAGE_MODELS["picture_edit"],
            pictures=self._picture_support,
        )
        self.model.textChanged.connect(self._sync)
        self.note = QLabel()
        self.note.setObjectName("hintLabel")
        self.note.setWordWrap(True)

        self.list = QWidget()
        self.list_layout = QVBoxLayout(self.list)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.list)

        form = QFormLayout()
        form.addRow("Drawn from", self.source)
        form.addRow("", self.picture)
        form.addRow("Expressions", ticks)
        form.addRow("Others", self.others)
        form.addRow("Drawn by", self.model)
        form.addRow(self.note)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.draw_button = self.buttons.addButton("Draw", QDialogButtonBox.AcceptRole)
        self.draw_button.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(QLabel("The prompts, each sent exactly as it is here:"))
        layout.addWidget(scroll, 1)
        layout.addWidget(self.buttons)
        if catalog is not None:
            catalog.changed.connect(self._sync)
            catalog.ensure()
        self._on_source()

    # --- what is drawn -----------------------------------------------------------------

    def source_ref(self):
        ref_id = self.source.currentData()
        return next(r for r in self.character.reference_images if r.id == ref_id)

    def expressions(self) -> list[str]:
        """The expressions ticked and written, each once, in order."""
        chosen = [word for word, box in self.ticks.items() if box.isChecked()]
        chosen += [" ".join(w.split()) for w in self.others.text().split(",") if w.strip()]
        seen: set[str] = set()
        return [w for w in chosen if not (w.lower() in seen or seen.add(w.lower()))]

    def _prompt_for(self, expression: str) -> str:
        return self.session.picture_edit_prompt(
            look_change(expression=expression, texts=self.session.texts), keep="person"
        )

    def _by_hand(self, *_args) -> None:
        self._ticked_by_hand = True

    def first_ticked(self) -> list[str]:
        """FIRST_TICKED less the expressions the picture's look already has
        among their pictures (the picture's own included)."""
        look = self.source_ref().look.strip().lower()
        have = {
            ref.expression.strip().lower()
            for ref in self.character.reference_images
            if ref.look.strip().lower() == look and ref.expression.strip()
        }
        return [expression for expression in FIRST_TICKED if expression not in have]

    def _on_source(self, *_args) -> None:
        if not self._ticked_by_hand:
            first = self.first_ticked()
            for expression, box in self.ticks.items():
                box.blockSignals(True)
                box.setChecked(expression in first)
                box.blockSignals(False)
        store = self.session.pictures
        data = store.read(self.source_ref().file) if store is not None else None
        thumb = thumbnail_of(data, THUMB_SIDE) if data else None
        if thumb is not None and not thumb.isNull():
            self.picture.setPixmap(thumb)
        self._rebuild()

    def _rebuild(self, *_args) -> None:
        """One prompt for each expression; one the author changed is kept."""
        for expression, box in self._boxes.items():
            self._prompts[expression] = box.toPlainText()
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._boxes = {}
        for expression in self.expressions():
            label = QLabel(expression)
            label.setObjectName("peopleSection")
            box = GrowingText(lines=2, most=8)
            box.setPlainText(self._prompts.get(expression) or self._prompt_for(expression))
            box.textChanged.connect(self._sync)
            self.list_layout.addWidget(label)
            self.list_layout.addWidget(box)
            self._boxes[expression] = box
        self.list_layout.addStretch(1)
        self._sync()

    # --- the model and the price --------------------------------------------------

    def _listing(self):
        return self._catalog.models if self._catalog is not None else []

    def _first_model(self) -> str:
        config = self.session.config
        for model in (config.portrait_model,):
            family = family_of(model, self._listing())
            if model and family is not None and family.takes_pictures:
                return family.id
        return RECOMMENDED_IMAGE_MODELS["picture_edit"][0].id

    def _picture_support(self, model: str) -> tuple[str, str]:
        return picture_support(model, self._listing())

    def sent_model(self) -> str:
        return fitting_model(self.model.text().strip(), 1, self._listing())

    def model_info(self) -> ImageModelInfo | None:
        model = self.sent_model()
        return next((m for m in self._listing() if m.id == model), None)

    def _sync(self, *_args) -> None:
        self.model.show_pictures()
        info = self.model_info()
        count = len(self._boxes)
        cannot = info is not None and not info.accepts_images
        each = info.price(card_size(self.character, info)) if info is not None else None
        if cannot:
            words = "It can't be given a picture, so it can't draw these: choose another."
        elif each is not None:
            words = (
                f"{count} picture{'s' if count != 1 else ''} at about ${each:.3f} each: about "
                f"${each * count:.2f} in all, and a picture sent with each may cost more"
            )
        else:
            words = f"{count} picture{'s' if count != 1 else ''}; the price isn't listed"
        self.note.setText(f"{self.sent_model() or 'No model'}: {words}.")
        filled = all(box.toPlainText().strip() for box in self._boxes.values())
        ready = bool(count) and filled and not cannot and bool(self.sent_model())
        self.draw_button.setEnabled(ready)
        self.draw_button.setText(f"Draw {count}" if count else "Draw")

    # --- the result -------------------------------------------------------------------

    def chosen_model(self) -> str:
        return self.model.text().strip()

    def requests(self) -> list[ImageRequest]:
        """Each expression's request, its prompt exactly as it stands."""
        source = self.source_ref()
        model, info = self.sent_model(), self.model_info()
        return [
            self.session.card_request(
                self.character,
                box.toPlainText(),
                model=model,
                info=info,
                first=False,
                caption="",
                source=source,
                look=source.look,
                expression=expression,
            )
            for expression, box in self._boxes.items()
        ]


def ask_expression_set(
    parent: QWidget,
    session,
    character: Character,
    *,
    source_id: str | None,
    catalog: ImageCatalog | None,
    browse,
) -> tuple[list[ImageRequest], str]:
    """The approved requests and the model chosen, or ([], "") for Cancel."""
    dialog = ExpressionSetDialog(
        session, character, source_id=source_id, catalog=catalog, browse=browse, parent=parent
    )
    accepted = dialog.exec() == QDialog.Accepted
    requests = dialog.requests() if accepted else []
    model = dialog.chosen_model() if accepted else ""
    dialog.deleteLater()
    return requests, model
