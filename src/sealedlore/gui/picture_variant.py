"""Draw again… and Edit… on a card's picture (a character's or a lore
entry's): the author's ask, Oct 15 2026. A picture drawn again, or a copy
with something changed (a scar, other clothes, another background, years
older), is added to the card's pictures; the original stays until the author
removes it.

Draw again sends the picture's own prompt (kept on the card when it was
drawn here; otherwise one is written from the card), by the same model or
another. Edit sends the picture itself with the change, to a model that can
be given a picture: the card's own model when it can, else the default edit
model. Either way the dialog shows the whole request and its price, and
nothing is sent before Draw; the prompt is sent exactly as it stands.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.images import ImageRequest
from sealedlore.engine.portraits import Card, card_size
from sealedlore.gui.image_jobs import ImageCatalog
from sealedlore.gui.model_picker import ModelField
from sealedlore.gui.portraits_dialog import card_name
from sealedlore.gui.ref_images import thumbnail_of
from sealedlore.gui.worker import GenerationWorker
from sealedlore.models.character import Character
from sealedlore.models.config import RECOMMENDED_IMAGE_MODELS, recommended_model
from sealedlore.models.image import ImageRef
from sealedlore.providers.images import ImageModelInfo

THUMB_SIDE = 160
# The caption a picture drawn again is given on its card.
AGAIN_CAPTION = "drawn again"


class PictureVariantDialog(QDialog):
    def __init__(
        self,
        session,
        card: Card,
        source: ImageRef,
        *,
        edit: bool,
        catalog: ImageCatalog | None,
        browse: Callable[[str], str | None] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.card, self.source, self.edit = card, source, edit
        self._catalog = catalog
        self._closing = False
        self._hand_edited = False
        self._prompts: dict[str, str] = {}
        name = card_name(card)
        self.setWindowTitle(f"Edit a picture of {name}" if edit else f"Draw {name} again")
        self.setMinimumWidth(620)

        picture = QLabel()
        picture.setAlignment(Qt.AlignCenter)
        store = session.pictures
        thumb = thumbnail_of(store.read(source.file), THUMB_SIDE) if store is not None else None
        if thumb is not None and not thumb.isNull():
            picture.setPixmap(thumb)
        intro = QLabel(
            "A copy of this picture with what you say changed. The picture itself is sent "
            "with the prompt, so the model must be one that can be given a picture. The new "
            "picture is added to the card's pictures; this one stays until you remove it."
            if edit
            else "This picture drawn again from its prompt, by the same model or another. "
            "The new picture is added to the card's pictures; this one stays until you "
            "remove it."
        )
        intro.setWordWrap(True)

        self.change = QLineEdit()
        self.change.setPlaceholderText(
            "a fresh scar down his left cheek · twenty years older · a dark suit at dinner"
            if isinstance(card, Character)
            else "at night, in a storm · burned out and abandoned · in high summer"
        )
        self.change.textChanged.connect(self._compose)
        self.prompt = QPlainTextEdit()
        self.prompt.setMinimumHeight(130)
        self.prompt.setPlaceholderText("This text is sent exactly as it is here.")
        self.prompt.textChanged.connect(self._on_prompt_edited)
        self.model = ModelField(
            self._first_model(),
            browse,
            recommended=RECOMMENDED_IMAGE_MODELS["picture_edit" if edit else "portrait_model"],
        )
        self.model.textChanged.connect(self._sync)
        self.note = QLabel()
        self.note.setObjectName("hintLabel")
        self.note.setWordWrap(True)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setTextVisible(False)
        self.busy.setMaximumHeight(6)
        self.busy.hide()

        form = QFormLayout()
        if edit:
            form.addRow("What to change", self.change)
        form.addRow("Prompt", self.prompt)
        form.addRow("Drawn by", self.model)
        form.addRow(self.note)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.draw_button = self.buttons.addButton("Draw", QDialogButtonBox.AcceptRole)
        self.draw_button.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(picture)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.busy)
        layout.addWidget(self.buttons)

        self._thread: QThread | None = None
        self._worker: GenerationWorker | None = None
        if edit:
            self._compose()
        elif source.prompt:
            self._set_prompt(source.prompt)
        else:
            self._write()  # a picture from a file: a prompt from the card
        if catalog is not None:
            catalog.changed.connect(self._sync)
            catalog.ensure()
        self._sync()

    def _first_model(self) -> str:
        """Drawing again: the picture's own model, else the portrait model.
        Editing: the picture's own model if it can be given a picture, else
        the portrait model if it can, else the recommended edit model."""
        config = self.session.config
        own = self.source.model or config.portrait_model
        if not self.edit:
            return own
        for model in (self.source.model, config.portrait_model):
            info = self._info(model)
            if model and info is not None and info.accepts_images:
                return model
        return RECOMMENDED_IMAGE_MODELS["picture_edit"][0].id

    # --- the prompt ------------------------------------------------------------------

    def _set_prompt(self, text: str) -> None:
        self.prompt.blockSignals(True)
        self.prompt.setPlainText(text)
        self.prompt.blockSignals(False)
        self._sync()

    def _compose(self) -> None:
        """The edit's prompt from the change, until the author writes in
        the prompt itself."""
        if self.edit and not self._hand_edited:
            self._set_prompt(self.session.card_edit_prompt(self.card, self.change.text()))

    def _on_prompt_edited(self) -> None:
        self._hand_edited = True
        self._sync()

    def _write(self) -> None:
        characters = [self.card] if isinstance(self.card, Character) else []
        lore = [] if characters else [self.card]

        def job():
            self._prompts, _problem = self.session.write_card_prompts(characters, lore)
            return
            yield  # a generator, so GenerationWorker can run it

        self.prompt.setEnabled(False)
        self.prompt.setPlaceholderText("Writing a prompt from the card…")
        self.busy.show()
        worker = GenerationWorker(job, provider=self.session.provider)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_written)
        self._worker, self._thread = worker, thread
        thread.start()

    def _on_written(self) -> None:
        self._worker, self._thread = None, None
        if self._closing:
            super().reject()
            return
        self.busy.hide()
        self.prompt.setEnabled(True)
        self.prompt.setPlaceholderText("This text is sent exactly as it is here.")
        self._set_prompt(self._prompts.get(self.card.id, ""))

    # --- the model and the price -------------------------------------------------

    def _info(self, model: str) -> ImageModelInfo | None:
        if self._catalog is None:
            return None
        return next((m for m in self._catalog.models if m.id == model), None)

    def model_info(self) -> ImageModelInfo | None:
        return self._info(self.model.text().strip())

    def _sync(self, *_args) -> None:
        model = self.model.text().strip()
        info = self.model_info()
        size = card_size(self.card, info)
        each = info.price(size) if info is not None else None
        role = "picture_edit" if self.edit else "portrait_model"
        known = recommended_model(role, model)
        cannot = self.edit and info is not None and not info.accepts_images
        price = f"about ${each:.3f}" if each else "price not listed"
        parts = [f"{model or 'No model'}: {price}"]
        if cannot:
            parts.append(
                "It can't be given a picture, so it can't edit one: choose another "
                "(Recommended lists those that edited well)."
            )
        elif known is not None:
            parts.append(known.note)
        self.note.setText(". ".join(parts))
        ready = self._worker is None and bool(model) and bool(self.prompt.toPlainText().strip())
        if self.edit:
            ready = ready and bool(self.change.text().strip() or self._hand_edited)
        self.draw_button.setEnabled(ready and not cannot)

    # --- the result ------------------------------------------------------------------

    def chosen_model(self) -> str:
        return self.model.text().strip()

    def request(self) -> ImageRequest:
        """The approved request: the prompt exactly as it stands."""
        caption = " ".join(self.change.text().split()) if self.edit else AGAIN_CAPTION
        return self.session.card_request(
            self.card,
            self.prompt.toPlainText(),
            model=self.chosen_model(),
            info=self.model_info(),
            first=False,
            caption=caption or "edited",
            source=self.source if self.edit else None,
        )

    def reject(self) -> None:
        if self._worker is not None:
            self._closing = True
            self._worker.stop()
            return
        super().reject()
