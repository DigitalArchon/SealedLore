"""Drawing a sheet's gaps (gui/cast_board.py): each one an edit of the
picture it is drawn from (engine/look_sheet.py), so the face and clothes
stay, asking only for what differs.

As with A set of expressions…, the author sees every source picture and
every prompt (each may be changed, or left out) and the price of them all
before anything is sent; each prompt is then sent exactly as it stands, in
the background, as an ordinary picture of the card, and lands on it
labelled with its cell's look and expression.

The rest of a new look's row is approved with it: those wait (`Waiting`)
until the row's first picture is in, then are drawn from it with the prompt
approved here, so the whole row wears the same thing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

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
from sealedlore.engine.look_sheet import Gap, gap_change
from sealedlore.engine.looks import look_words
from sealedlore.engine.portraits import card_size
from sealedlore.gui.cast_board import ASPECT, cover
from sealedlore.gui.image_jobs import ImageCatalog
from sealedlore.gui.model_picker import ModelField
from sealedlore.gui.ref_images import pixmap_of
from sealedlore.gui.sizing import GrowingText, enter_only_on_buttons, size_to_screen
from sealedlore.models.character import Character
from sealedlore.models.config import RECOMMENDED_IMAGE_MODELS
from sealedlore.providers.images import ImageModelInfo

THUMB = 72


@dataclass(frozen=True)
class Waiting:
    """A gap approved to be drawn from its row's first picture once that is
    in: its prompt as approved, and the model chosen."""

    card_id: str
    look: str
    expression: str
    prompt: str
    model: str


def gap_words(gap: Gap) -> str:
    return " · ".join(part for part in (gap.look, gap.expression) if part) or "a picture"


def gap_words_first(gap: Gap) -> str:
    """A waiting gap's row's first cell, in words."""
    return " · ".join(part for part in (gap.look, gap.after) if part) or "first"


class FillGapsDialog(QDialog):
    def __init__(
        self,
        session,
        character: Character,
        gaps: Sequence[Gap],
        *,
        catalog: ImageCatalog | None,
        browse: Callable[[str], str | None] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.character = character
        self._catalog = catalog
        self._gaps = [gap for gap in gaps if gap.source is not None or gap.after]
        self.setWindowTitle(f"Fill the gaps: {character.name}")
        self.setMinimumWidth(720)
        size_to_screen(self, 0.55, 0.85)

        count = len(self._gaps)
        later = sum(1 for gap in self._gaps if gap.source is None)
        intro = QLabel(
            f"{character.name}, drawn {count} new way{'' if count == 1 else 's'}. Each is an "
            "edit of the picture beside it, sent with its prompt, so the face and clothes stay "
            "and only what differs changes; each lands on their card, labelled for its place "
            "on the sheet. Untick any you don't want."
            + (
                f" {later} wait{'s' if later == 1 else ''} for a new look's first picture and "
                f"{'is' if later == 1 else 'are'} drawn from it once it is in, so the row wears "
                "the same thing (while SealedLore stays open)."
                if later
                else ""
            )
        )
        intro.setWordWrap(True)

        self.ticks: list[QCheckBox] = []
        self.prompts: list[GrowingText] = []
        rows = QWidget()
        grid = QGridLayout(rows)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        store = session.pictures
        for row, gap in enumerate(self._gaps):
            picture = QLabel()
            picture.setFixedSize(QSize(THUMB, int(THUMB * ASPECT)))
            picture.setAlignment(Qt.AlignCenter)
            picture.setWordWrap(True)
            if gap.source is not None:
                data = store.read(gap.source.file) if store is not None else None
                pixmap = pixmap_of(data) if data else None
                if pixmap is not None and not pixmap.isNull():
                    picture.setPixmap(cover(pixmap, THUMB, int(THUMB * ASPECT)))
                source = f"their {look_words(gap.source) or 'picture'}"
            else:
                picture.setObjectName("hintLabel")
                picture.setText(f"the new {gap.look or 'look'}")
                source = f"their new {gap_words_first(gap)} picture, once it is in"
            picture.setToolTip(f"Drawn from {source}")
            tick = QCheckBox(gap_words(gap))
            tick.setChecked(True)
            tick.setToolTip(f"Drawn from {source}")
            tick.toggled.connect(self._sync)
            prompt = GrowingText(lines=2, most=6)
            prompt.setPlainText(
                session.card_edit_prompt(character, gap_change(gap, session.texts, character))
            )
            prompt.textChanged.connect(self._sync)
            column = QVBoxLayout()
            column.setSpacing(4)
            column.addWidget(tick)
            column.addWidget(prompt)
            grid.addWidget(picture, row, 0, Qt.AlignTop)
            grid.addLayout(column, row, 1)
            self.ticks.append(tick)
            self.prompts.append(prompt)
        grid.setColumnStretch(1, 1)
        # The rows as tall as their text; the room left goes under them.
        grid.setRowStretch(len(self._gaps), 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(rows)

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
        form.addRow("Drawn by", self.model)
        form.addRow(self.note)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.draw_button = self.buttons.addButton("Draw", QDialogButtonBox.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(QLabel("The prompts, each sent exactly as it is here:"))
        layout.addWidget(scroll, 1)
        layout.addLayout(form)
        layout.addWidget(self.buttons)
        if catalog is not None:
            catalog.changed.connect(self._sync)
            catalog.ensure()
        self._sync()

    # --- the model and the price, as A set of expressions… has them ---------------------------

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

    def chosen(self) -> list[int]:
        return [index for index, tick in enumerate(self.ticks) if tick.isChecked()]

    def _first_index(self, gap: Gap) -> int | None:
        """Where a waiting gap's row's first cell is in this list, if it is."""
        for index, other in enumerate(self._gaps):
            if (
                other.source is not None
                and other.look == gap.look
                and other.expression == gap.after
            ):
                return index
        return None

    def _hold_to_firsts(self) -> None:
        """A waiting gap goes only with its row's first: unticked, the rest
        of the row is too, until it is ticked again."""
        for index, gap in enumerate(self._gaps):
            if gap.source is not None:
                continue
            first = self._first_index(gap)
            if first is None:
                continue  # its first is being drawn already
            on = self.ticks[first].isChecked()
            tick = self.ticks[index]
            tick.blockSignals(True)
            if not on:
                tick.setChecked(False)
            tick.setEnabled(on)
            tick.blockSignals(False)

    def _sync(self, *_args) -> None:
        self._hold_to_firsts()
        self.model.show_pictures()
        info = self.model_info()
        count = len(self.chosen())
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
        filled = all(self.prompts[i].toPlainText().strip() for i in self.chosen())
        ready = bool(count) and filled and not cannot and bool(self.sent_model())
        self.draw_button.setEnabled(ready)
        self.draw_button.setText(f"Draw {count}" if count else "Draw")

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Enter in a field never draws: it presses only a push button that
        has the focus (gui/sizing.py `enter_only_on_buttons`). Enter in
        Drawn by drew every gap ticked."""
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)

    # --- the result -------------------------------------------------------------------

    def chosen_model(self) -> str:
        return self.model.text().strip()

    def requests(self) -> list[ImageRequest]:
        """Each chosen gap's request that can go now, its prompt exactly as
        it stands."""
        model, info = self.sent_model(), self.model_info()
        return [
            self.session.card_request(
                self.character,
                self.prompts[index].toPlainText(),
                model=model,
                info=info,
                first=False,
                caption="",
                source=self._gaps[index].source,
                look=self._gaps[index].look,
                expression=self._gaps[index].expression,
            )
            for index in self.chosen()
            if self._gaps[index].source is not None
        ]

    def waiting(self) -> list[Waiting]:
        """The chosen gaps that wait for their row's first picture."""
        return [
            Waiting(
                card_id=self.character.id,
                look=self._gaps[index].look,
                expression=self._gaps[index].expression,
                prompt=self.prompts[index].toPlainText(),
                model=self.sent_model(),
            )
            for index in self.chosen()
            if self._gaps[index].source is None
        ]


def ask_fill_gaps(
    parent: QWidget,
    session,
    character: Character,
    gaps: Sequence[Gap],
    *,
    catalog: ImageCatalog | None,
    browse,
) -> tuple[list[ImageRequest], list[Waiting]]:
    """The approved requests to send now and those waiting for a new look's
    first picture, or ([], []) for Cancel."""
    dialog = FillGapsDialog(session, character, gaps, catalog=catalog, browse=browse, parent=parent)
    accepted = dialog.exec() == QDialog.Accepted
    requests = dialog.requests() if accepted else []
    waiting = dialog.waiting() if accepted else []
    dialog.deleteLater()
    return requests, waiting
