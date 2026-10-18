"""Draw again… and Edit… on a card's picture (a character's or a lore
entry's): the author's ask, Oct 15 2026. A picture drawn again, or a copy
with something changed (a scar, other clothes, another background, years
older), is added to the card's pictures; the original stays until the author
removes it. Edit… works the same on a picture of the story (the new one goes
after the same passage) and on the background (the new one goes behind the
story once drawn): the Media tab offers it on every picture.

Draw again sends the picture's own prompt (kept on the card when it was
drawn here; otherwise one is written from the card), by the same model or
another. Edit sends the picture itself with the change, to a model that can
be given a picture: the picture's own model when it can, else the default
edit model. Either way the dialog shows the whole request and its price, and
nothing is sent before Draw; the prompt is sent exactly as it stands.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.image_variants import fitting_model, version_words
from sealedlore.engine.images import ImageRequest
from sealedlore.engine.portraits import Card, card_size, size_like
from sealedlore.gui.image_jobs import ImageCatalog
from sealedlore.gui.model_picker import ModelField
from sealedlore.gui.picture_words import WORDS_ONLY
from sealedlore.gui.portraits_dialog import card_name
from sealedlore.gui.ref_images import image_of, thumbnail_of
from sealedlore.gui.sizing import GrowingText, size_to_screen
from sealedlore.gui.worker import GenerationWorker
from sealedlore.models.character import Character
from sealedlore.models.config import RECOMMENDED_IMAGE_MODELS, recommended_model
from sealedlore.models.image import GeneratedImage, ImageRef, RefUse
from sealedlore.providers.images import ImageModelInfo

THUMB_SIDE = 160
_SAME = "The model works from it, so the face and everything you don't change stay."
# The caption a picture drawn again is given on its card.
AGAIN_CAPTION = "drawn again"


@dataclass(frozen=True)
class VariantSubject:
    """What Draw again… or Edit… is of: a card's picture (`card`, `source`),
    a picture of the story (`image`), or the background (`background`)."""

    name: str
    # The picture in the story's pictures, and how it was drawn here (blank
    # for a picture from a file).
    file: str
    prompt: str = ""
    model: str = ""
    card: Card | None = None
    source: ImageRef | None = None
    image: GeneratedImage | None = None
    background: bool = False
    # The pictures it was drawn from, sent with its prompt (an edit's
    # picture, In the scene…'s): drawn again without them, "Edit this
    # picture" and "the man in image 1" came out as anyone at all.
    references: tuple[RefUse, ...] = ()

    @classmethod
    def of_card(
        cls, card: Card, source: ImageRef, drawn: GeneratedImage | None = None
    ) -> VariantSubject:
        """`drawn`: the picture drawn for the story it is a copy of
        (`ImageRef.drawn_as`), whose references go again with its prompt."""
        return cls(
            name=card_name(card),
            file=source.file,
            prompt=source.prompt,
            model=source.model,
            card=card,
            source=source,
            references=tuple(drawn.references) if drawn is not None else (),
        )

    @classmethod
    def of_picture(cls, image: GeneratedImage) -> VariantSubject:
        return cls(
            name="the picture", file=image.file, prompt=image.prompt, model=image.model, image=image
        )

    @classmethod
    def of_background(cls, file: str, drawn: GeneratedImage | None) -> VariantSubject:
        """The background; `drawn`, the picture drawn here it is a copy of."""
        return cls(
            name="the background",
            file=file,
            prompt=drawn.prompt if drawn is not None else "",
            model=drawn.model if drawn is not None else "",
            background=True,
        )

    @property
    def keep(self) -> str:
        """What an edit keeps (`portraits.EDIT_KEEPS`)."""
        if self.card is not None:
            return "person" if isinstance(self.card, Character) else "thing"
        return "thing" if self.background else "scene"


class PictureVariantDialog(QDialog):
    def __init__(
        self,
        session,
        subject: VariantSubject | Card,
        source: ImageRef | None = None,
        *,
        edit: bool,
        catalog: ImageCatalog | None,
        browse: Callable[[str], str | None] | None,
        parent: QWidget | None = None,
    ) -> None:
        """`subject`: what it is of; a card with its picture as `source`
        stands for `VariantSubject.of_card`. Only a card's picture is drawn
        again here (a picture of the story is drawn again in the picture
        dialog, with its references)."""
        super().__init__(parent)
        if not isinstance(subject, VariantSubject):
            assert source is not None
            drawn = (
                next((i for i in session.generated_images() if source.drawn_as == i.id), None)
                if source.drawn_as
                else None
            )
            subject = VariantSubject.of_card(subject, source, drawn)
        assert edit or subject.card is not None
        # What it was drawn from, of what is still there to send.
        store = session.pictures
        self._from = [] if edit else [u for u in subject.references if store.read(u.file)]
        self._gone = 0 if edit else len(subject.references) - len(self._from)
        self.session = session
        self.subject, self.edit = subject, edit
        self.card, self.source = subject.card, subject.source
        self._catalog = catalog
        self._closing = False
        self._hand_edited = False
        self._prompts: dict[str, str] = {}
        name = subject.name
        if subject.card is None:
            self.setWindowTitle(f"Change {name}")
        else:
            self.setWindowTitle(
                f"Change a picture of {name}" if edit else f"Same prompt again: {name}"
            )
        self.setMinimumWidth(620)
        size_to_screen(self, 0.45, 0.8)

        picture = QLabel()
        picture.setAlignment(Qt.AlignCenter)
        store = session.pictures
        data = store.read(subject.file) if store is not None else None
        # The picture's own shape: an edit is drawn in the size nearest it.
        shape = image_of(data).size() if data else None
        self._shape = (shape.width(), shape.height()) if shape is not None else (0, 0)
        thumb = thumbnail_of(data, THUMB_SIDE) if data else None
        if thumb is not None and not thumb.isNull():
            picture.setPixmap(thumb)
        if subject.background:
            goes = "The new picture becomes the background once it is drawn"
            stays = "this one stays in Media when it was drawn here"
        elif subject.image is not None:
            goes = "The new picture goes after the same passage, and in Media"
            stays = "this one stays until you delete it"
        else:
            goes = "The new picture is added to the card's pictures"
            stays = "this one stays until you remove it"
        intro = QLabel(
            "A copy of this picture with what you say changed: the picture itself is sent "
            f"with the prompt, so it stays the same. {goes}; {stays}."
            if edit
            else "Another take from this picture's own prompt, by the same model or another. "
            f"{goes}; {stays}."
        )
        intro.setWordWrap(True)
        # What goes with the prompt, said plainly (the author, Oct 18 2026:
        # "you can't clearly tell if a reference picture is being used").
        self.sends = QLabel()
        self.sends.setObjectName("sentWithPrompt")
        self.sends.setWordWrap(True)
        self.sends.setTextFormat(Qt.RichText)

        self.change = QLineEdit()
        if subject.keep == "person":
            examples = (
                "a fresh scar down his left cheek · twenty years older · a dark suit at dinner"
            )
        elif subject.keep == "scene":
            examples = "at night · in pouring rain · everyone soaked and laughing"
        else:
            examples = "at night, in a storm · burned out and abandoned · in high summer"
        self.change.setPlaceholderText(examples)
        self.change.textChanged.connect(self._compose)
        self.prompt = GrowingText(lines=6, most=30, fill=True)
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
        layout.addWidget(self.sends)
        layout.addLayout(form)
        layout.addWidget(self.busy)
        layout.addWidget(self.buttons)

        self._thread: QThread | None = None
        self._worker: GenerationWorker | None = None
        if edit:
            self._compose()
        elif subject.prompt:
            self._set_prompt(subject.prompt)
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
        own = self.subject.model or config.portrait_model
        if not self.edit:
            return own
        for model in (self.subject.model, config.portrait_model):
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
            self._set_prompt(
                self.session.picture_edit_prompt(self.change.text(), keep=self.subject.keep)
            )

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
        worker.failed.connect(self._on_write_failed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_written)
        self._worker, self._thread = worker, thread
        thread.start()

    def _on_write_failed(self, message: str) -> None:
        self._failure = message

    def _on_written(self) -> None:
        self._worker, self._thread = None, None
        if self._closing:
            super().reject()
            return
        self.busy.hide()
        self.prompt.setEnabled(True)
        failure = getattr(self, "_failure", None)
        self.prompt.setPlaceholderText(
            f"The prompt couldn't be written ({failure}): write one here, sent exactly as it is."
            if failure
            else "This text is sent exactly as it is here."
        )
        self._set_prompt(self._prompts.get(self.card.id, ""))

    # --- the model and the price -------------------------------------------------

    def _info(self, model: str) -> ImageModelInfo | None:
        if self._catalog is None:
            return None
        return next((m for m in self._catalog.models if m.id == model), None)

    def pictures_sent(self) -> int:
        """How many pictures go with the prompt: the picture itself for an
        edit, else those it was drawn from (as many as can be sent)."""
        return 1 if self.edit else len(self._from)

    def sent_model(self) -> str:
        """The chosen model's version the request goes to, for whether a
        picture goes with it (engine/image_variants.py)."""
        listing = self._catalog.models if self._catalog is not None else []
        return fitting_model(self.model.text().strip(), self.pictures_sent(), listing)

    def model_info(self) -> ImageModelInfo | None:
        return self._info(self.sent_model())

    def size(self) -> str:
        """A card's size, or, for a picture of the story or the background,
        the size the model lists nearest the picture's own shape."""
        info = self.model_info()
        if self.card is not None:
            return card_size(self.card, info)
        return size_like(info, *self._shape)

    def _sync(self, *_args) -> None:
        model = self.sent_model()
        info = self.model_info()
        size = self.size()
        each = info.price(size) if info is not None else None
        role = "picture_edit" if self.edit else "portrait_model"
        known = recommended_model(role, model)
        cannot = self.edit and info is not None and not info.accepts_images
        takes = info.max_inputs if info is not None else None
        drawn_from = len(self._from)
        if drawn_from and takes == 0:
            cannot = True
        price = f"about ${each:.3f}" if each is not None else "price not listed"
        if each is not None and (self.edit or drawn_from):
            # Listed for what is drawn; FLUX.2 klein charged $0.016, not
            # $0.010, with a picture sent (design/images.md).
            price += ", and a picture sent with it may cost more"
        parts = [f"{model or 'No model'}: {price}"]
        if model and model != self.model.text().strip():
            parts[0] += " (" + version_words(model, self.pictures_sent()) + ")"
        if cannot and drawn_from:
            parts.append(
                f"It was drawn from {_pictures(drawn_from)}, sent with its prompt, and this "
                "model can't be given a picture: choose another."
            )
        elif cannot:
            parts.append(
                "It can't be given a picture, so it can't edit one: choose another "
                "(Recommended lists those that edited well)."
            )
        else:
            sent = self._from[:takes] if takes is not None else self._from
            names = ", ".join(f"image {i}: {use.owner_name}" for i, use in enumerate(sent, 1))
            if drawn_from and takes is not None and takes < drawn_from:
                parts.append(f"Only the first {_pictures(takes)} it was drawn from can be sent")
            elif drawn_from == 1:
                parts.append("The picture it was drawn from goes with it")
            elif drawn_from:
                parts.append(f"The {drawn_from} pictures it was drawn from go with it")
            if sent:
                parts[-1] += f" ({names})"
            if self._gone or len(sent) < drawn_from:
                # Numbered as sent: the prompt's "image 2" may now be another.
                verb = "is" if self._gone == 1 else "are"
                gone = f"{_pictures(self._gone).capitalize()} it was drawn from {verb} gone. "
                gone = gone if self._gone else ""
                parts.append(
                    f"{gone}The prompt's image numbers may not match what is sent: check it"
                )
            if known is not None:
                parts.append(known.note)
        self.note.setText(". ".join(parts))
        self.sends.setText(self._sends_words(takes))
        ready = self._worker is None and bool(model) and bool(self.prompt.toPlainText().strip())
        if self.edit:
            ready = ready and bool(self.change.text().strip() or self._hand_edited)
        self.draw_button.setEnabled(ready and not cannot)

    def _sends_words(self, takes: int | None) -> str:
        """What goes with the prompt, for the line under the intro."""
        if self.edit:
            return f"<b>Sent with the prompt:</b> this picture, as image 1. {_SAME}"
        sent = self._from[:takes] if takes is not None else self._from
        if not sent:
            return f"<b>Sent with the prompt:</b> {html.escape(WORDS_ONLY[22:])}"
        names = ", ".join(
            f"image {i}: {html.escape(use.owner_name)}" for i, use in enumerate(sent, 1)
        )
        what = "the picture" if len(sent) == 1 else f"the {len(sent)} pictures"
        return (
            f"<b>Sent with the prompt:</b> {what} it was drawn from ({names}), as when it was "
            "first drawn. This picture itself isn't sent."
        )

    # --- the result ------------------------------------------------------------------

    def chosen_model(self) -> str:
        return self.model.text().strip()

    def request(self) -> ImageRequest:
        """The approved request: the prompt exactly as it stands."""
        # The version the dialog priced and named (what goes with it decides).
        prompt, model, info = self.prompt.toPlainText(), self.sent_model(), self.model_info()
        if self.subject.image is not None:
            return self.session.picture_edit_request(
                self.subject.image, prompt, model=model, info=info, size=self.size()
            )
        if self.subject.background:
            return self.session.background_edit_request(
                prompt, model=model, info=info, size=self.size()
            )
        caption = " ".join(self.change.text().split()) if self.edit else AGAIN_CAPTION
        takes = info.max_inputs if info is not None else len(self._from)
        return self.session.card_request(
            self.card,
            prompt,
            model=model,
            info=info,
            first=False,
            caption=caption or "edited",
            source=self.source if self.edit else None,
            references=self._from[:takes],
        )

    def reject(self) -> None:
        if self._worker is not None:
            self._closing = True
            self._worker.stop()
            return
        super().reject()


def _pictures(count: int) -> str:
    return "one picture" if count == 1 else f"{count} pictures"
