"""Restyle pictures…: cards' pictures redrawn in a style, keeping what each
shows (the author, Nov 3 2026: imported pictures, "quickly modify them to
suit the style").

Each is an edit of the picture (`restyle_prompt`), and as everywhere the
author sees every picture, its prompt (each may be changed or left out) and
the price before anything is sent. Ticked to begin with: the pictures not
drawn in the style chosen (`ImageRef.style`; a picture brought in has none).
A redrawn picture takes the old one's place on its card, with its labels and
framing (`card_replaces`); the old one stays at the end, unlabelled, to keep
or remove.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.image_variants import family_of, fitting_model, picture_support
from sealedlore.engine.images import ImageRequest
from sealedlore.engine.looks import look_words
from sealedlore.engine.picture_styles import style_key, style_words
from sealedlore.engine.portraits import Card, card_size
from sealedlore.gui.cast_board import ASPECT, cover
from sealedlore.gui.image_jobs import ImageCatalog
from sealedlore.gui.model_picker import ModelField
from sealedlore.gui.picture_style import PictureStyleField
from sealedlore.gui.portraits_dialog import card_name
from sealedlore.gui.ref_images import pixmap_of
from sealedlore.gui.sizing import GrowingText, enter_only_on_buttons, may_discard, size_to_screen
from sealedlore.models.config import RECOMMENDED_IMAGE_MODELS
from sealedlore.models.image import ImageRef
from sealedlore.providers.images import ImageModelInfo

THUMB = 72
RESTYLE_LABEL = "Restyle pictures…"


class _Row:
    """One picture: ticked or not, and its prompt."""

    def __init__(self, card: Card, ref: ImageRef, tick: QCheckBox, prompt: GrowingText) -> None:
        self.card, self.ref, self.tick, self.prompt = card, ref, tick, prompt
        self.edited = False


class RestyleDialog(QDialog):
    def __init__(
        self,
        session,
        cards: Sequence[Card],
        *,
        catalog: ImageCatalog | None,
        browse: Callable[[str], str | None] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self._catalog = catalog
        self.setWindowTitle("Restyle pictures")
        self.setMinimumWidth(720)
        size_to_screen(self, 0.55, 0.85)
        story = session.story

        intro = QLabel(
            "Pictures redrawn in a style, keeping what each shows: each is an edit of the "
            "picture beside it, sent with its prompt. A redrawn picture takes the old one's "
            "place on its card, with its labels; the old one stays at the end of the card's "
            "pictures, unlabelled (the cast board's tray), to keep or remove. Ticked to begin "
            "with: the pictures not drawn in the style chosen."
        )
        intro.setWordWrap(True)
        self.style = PictureStyleField(
            story.picture_style,
            story.image_style,
            texts=session.texts,
            picture=bool(story.style_picture),
        )
        self.style.changed.connect(self._restyle)

        self.rows: list[_Row] = []
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        store = session.pictures
        key = self.chosen_key()
        for card in cards:
            for ref in card.reference_images:
                picture = QLabel()
                picture.setFixedSize(QSize(THUMB, int(THUMB * ASPECT)))
                picture.setAlignment(Qt.AlignCenter)
                data = store.read(ref.file) if store is not None else None
                pixmap = pixmap_of(data) if data else None
                if pixmap is not None and not pixmap.isNull():
                    picture.setPixmap(cover(pixmap, THUMB, int(THUMB * ASPECT)))
                words = look_words(ref) or ref.caption
                tick = QCheckBox(f"{card_name(card)}: {words}" if words else card_name(card))
                tick.setChecked(ref.style != key)
                tick.toggled.connect(self._sync)
                prompt = GrowingText(lines=2, most=6)
                row = _Row(card, ref, tick, prompt)
                prompt.textChanged.connect(lambda r=row: self._on_edited(r))
                column = QVBoxLayout()
                column.setSpacing(4)
                column.addWidget(tick)
                column.addWidget(prompt)
                at = len(self.rows)
                grid.addWidget(picture, at, 0, Qt.AlignTop)
                grid.addLayout(column, at, 1)
                self.rows.append(row)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(len(self.rows), 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)

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
        form = QFormLayout()
        form.addRow("Redraw in", self.style)
        form.addRow("Drawn by", self.model)
        form.addRow(self.note)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.draw_button = self.buttons.addButton("Draw", QDialogButtonBox.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(QLabel("The prompts, each sent exactly as it is here:"))
        layout.addWidget(scroll, 1)
        layout.addWidget(self.buttons)
        self._restyle()
        if catalog is not None:
            catalog.changed.connect(self._sync)
            catalog.ensure()

    # --- the style and the prompts ----------------------------------------------------

    def chosen_key(self) -> str:
        return style_key(*self.style.value())

    def chosen_words(self) -> str:
        """The style's words: the default's realistic ones when none."""
        texts = self.session.texts
        return style_words(*self.style.value(), texts) or texts["pictures.style.default"].rstrip(
            "."
        )

    def style_reference(self):
        """The story's style picture, when redrawn in the story's own style
        with it (With its picture): sent as image 2."""
        if not self.style.picture_wanted():
            return None
        return self.session.style_reference(self.style.value())

    def _restyle(self) -> None:
        """Every prompt not changed by hand, in the style chosen now."""
        words = self.chosen_words()
        reference = 2 if self.style_reference() is not None else 0
        for row in self.rows:
            if not row.edited:
                row.prompt.blockSignals(True)
                row.prompt.setPlainText(
                    self.session.card_restyle_prompt(row.card, words, reference=reference)
                )
                row.prompt.blockSignals(False)
        self._sync()

    def _on_edited(self, row: _Row) -> None:
        row.edited = True
        self._sync()

    # --- the model and the price ------------------------------------------------------

    def _listing(self):
        return self._catalog.models if self._catalog is not None else []

    def _first_model(self) -> str:
        model = self.session.config.portrait_model
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

    def chosen(self) -> list[_Row]:
        return [row for row in self.rows if row.tick.isChecked()]

    def _sync(self, *_args) -> None:
        self.model.show_pictures()
        info = self.model_info()
        chosen = self.chosen()
        count = len(chosen)
        cannot = info is not None and not info.accepts_images
        # No listing yet (or no such model): no price, never a crash.
        prices = (
            [info.price(card_size(row.card, info, row.ref.framing or "head")) for row in chosen]
            if info is not None
            else []
        )
        total = sum(p for p in prices if p)
        if cannot:
            words = "It can't be given a picture, so it can't redraw these: choose another."
        elif total:
            words = (
                f"{count} picture{'s' if count != 1 else ''}, about ${total:.2f} in all; a "
                "picture sent with each may cost more"
            )
        else:
            words = f"{count} picture{'s' if count != 1 else ''}; the price isn't listed"
        self.note.setText(f"{self.sent_model() or 'No model'}: {words}.")
        filled = all(row.prompt.toPlainText().strip() for row in chosen)
        ready = bool(count) and filled and not cannot and bool(self.sent_model())
        self.draw_button.setEnabled(ready)
        self.draw_button.setText(f"Draw {count}" if count else "Draw")

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Enter in a field never draws: it presses only a push button that
        has the focus (gui/sizing.py `enter_only_on_buttons`). Enter in
        Drawn by redrew every ticked picture."""
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)

    def reject(self) -> None:
        """Cancel or Esc: prompts changed by hand are asked about before
        they are lost, as the picture dialog's is."""
        edited = [row.prompt.toPlainText() for row in self.rows if row.edited]
        if may_discard(self, [], edited):
            super().reject()

    # --- the result -------------------------------------------------------------------

    def requests(self) -> list[ImageRequest]:
        """One request a ticked picture, its prompt exactly as it stands,
        taking the picture's place on its card."""
        model, info = self.sent_model(), self.model_info()
        key = self.chosen_key()
        style_use = self.style_reference()
        return [
            self.session.card_request(
                row.card,
                row.prompt.toPlainText(),
                model=model,
                info=info,
                first=False,
                caption=row.ref.caption,
                source=row.ref,
                look=row.ref.look,
                expression=row.ref.expression,
                framing=row.ref.framing,
                style=key,
                replaces=row.ref.id,
                style_reference=style_use,
            )
            for row in self.chosen()
        ]


def ask_restyle(
    parent: QWidget,
    session,
    cards: Sequence[Card],
    *,
    catalog: ImageCatalog | None,
    browse,
) -> list[ImageRequest]:
    """The approved requests, or [] for Cancel."""
    dialog = RestyleDialog(session, cards, catalog=catalog, browse=browse, parent=parent)
    accepted = dialog.exec() == QDialog.Accepted
    requests = dialog.requests() if accepted else []
    dialog.deleteLater()
    return requests
