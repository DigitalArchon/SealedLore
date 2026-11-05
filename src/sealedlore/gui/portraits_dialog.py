"""Pictures for the cards (engine/portraits.py, engine/session_portraits.py):
a portrait of each character and a picture of each lore entry, offered when
a story starts, on the Cast and Lore pages, and under Story.

The prompt writer writes a prompt for each card while the dialog is open;
the author sees every one, may change any of them and untick whatever they
don't want drawn, and chooses the model with its price. Nothing is drawn
until Draw: each ticked prompt is then sent exactly as it stands
(`requests`), as an ordinary picture, in the background.

The style (the story's, engine/picture_styles.py) and how much of each
character a portrait shows (engine/portraits.py FRAMINGS) are chosen here
too (the author, Nov 3 2026); a change writes the prompts again, all but
those the author has changed by hand. A style changed here becomes the
story's once Draw is pressed (`chosen_style`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QSize, Qt, QThread, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.image_variants import family_id, picture_support
from sealedlore.engine.images import ImageRequest
from sealedlore.engine.picture_styles import style_key
from sealedlore.engine.portraits import (
    DEFAULT_FRAMING,
    FRAMING_LABELS,
    FRAMINGS,
    Card,
    card_size,
)
from sealedlore.gui.image_jobs import ImageCatalog
from sealedlore.gui.model_picker import ModelField
from sealedlore.gui.picture_style import PictureStyleField
from sealedlore.gui.picture_words import WORDS_ONLY
from sealedlore.gui.ref_images import thumbnail_of
from sealedlore.gui.sizing import GrowingText, size_to_screen
from sealedlore.gui.worker import GenerationWorker
from sealedlore.models.character import Character
from sealedlore.models.config import RECOMMENDED_IMAGE_MODELS, recommended_model
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.images import ImageModelInfo

THUMB_SIDE = 64
WRITING = "Writing…"
# Typing one's own style words waits this long before the prompts are
# written again.
RESTYLE_DELAY_MS = 900
NOTHING_TO_SEE = "The writer found nothing here a picture could show; write a prompt to draw one."
UNTICKED = "Tick it to draw it, and its prompt is written."


def card_name(card: Card) -> str:
    return card.name if isinstance(card, Character) else card.title


class _Row(QFrame):
    """One card: drawn or not, what it looks like now, the prompt."""

    def __init__(self, card: Card, thumbnail) -> None:
        super().__init__()
        self.setObjectName("portraitRow")
        self.card = card
        self.character = card  # the portraits' name for it
        self.tick = QCheckBox(card_name(card))
        self.tick.setChecked(True)
        self.prompt = GrowingText(WRITING, lines=4, most=12)
        self.prompt.setEnabled(False)
        picture = QLabel()
        picture.setFixedSize(THUMB_SIDE, THUMB_SIDE)
        picture.setAlignment(Qt.AlignCenter)
        if thumbnail is not None and not thumbnail.isNull():
            picture.setPixmap(thumbnail)
            picture.setToolTip("Its first picture now: a new one goes first, this one stays")
        else:
            picture.setText("—")
            picture.setToolTip("No picture yet")
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(picture, 0, Qt.AlignTop)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(4)
        right.addWidget(self.tick)
        right.addWidget(self.prompt)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.addLayout(left)
        layout.addLayout(right, 1)
        self.tick.toggled.connect(lambda on: self.prompt.setEnabled(on and self.written))
        self.prompt.textChanged.connect(self._on_edited)
        self.written = False
        # The author changed the prompt: a new style or framing leaves it be.
        self.edited = False
        self._filling = False

    def fill(self, prompt: str) -> None:
        self.written = True
        if not prompt:
            # A lore entry with nothing to see: left out unless the author
            # writes a prompt for it.
            self.tick.setChecked(False)
            self.prompt.setPlaceholderText(NOTHING_TO_SEE)
        self._filling = True
        self.prompt.setPlainText(prompt)
        self._filling = False
        self.prompt.setEnabled(self.tick.isChecked())

    def rewrite(self) -> None:
        """Its prompt is being written again."""
        self.written = False
        self._filling = True
        self.prompt.setPlainText(WRITING)
        self._filling = False
        self.prompt.setEnabled(False)

    def wait(self) -> None:
        """No prompt until it is ticked: the writer is asked only for cards
        that are to be drawn."""
        self.written = False
        self._filling = True
        self.prompt.setPlainText("")
        self._filling = False
        self.prompt.setPlaceholderText(UNTICKED)
        self.prompt.setEnabled(False)

    def _on_edited(self) -> None:
        if self._filling:
            return
        if self.written:
            self.edited = True
        if self.written and self.prompt.toPlainText().strip() and not self.tick.isEnabled():
            self.tick.setEnabled(True)

    def wanted(self) -> bool:
        return self.written and self.tick.isChecked() and bool(self.prompt.toPlainText().strip())


class PortraitsDialog(QDialog):
    """Pictures for the given characters (portraits) and lore entries."""

    def __init__(
        self,
        session,
        characters: Sequence[Character],
        *,
        lore: Sequence[LoreEntry] = (),
        catalog: ImageCatalog | None,
        browse: Callable[[str], str | None] | None,
        parent: QWidget | None = None,
        describe: Callable[[], str | None] | None = None,
    ) -> None:
        """`describe`: the words for a picture's style (From a picture…)."""
        super().__init__(parent)
        self.setWindowTitle("Pictures for the cards" if lore else "Portraits")
        self.setMinimumWidth(620)
        self.session = session
        self._catalog = catalog
        self._closing = False
        self._prompts: dict[str, str] = {}
        self._problem: str | None = None
        self._failure: str | None = None
        self._characters, self._lore = list(characters), list(lore)

        what = "A portrait of each character" if characters else "A picture"
        if characters and lore:
            what += " and a picture of each lore entry"
        elif lore:
            what = "A picture of each lore entry"
        writer = session.image_prompt_model or "The story's model"
        intro = QLabel(
            f"{what}, drawn from the cards. {writer} writes a prompt for each: read them, "
            "change any you like and untick anything you'd rather not draw. Nothing is "
            "drawn until you press Draw. Each picture goes first among its card's pictures, "
            "shown large on the Cast or Lore page, and "
            "is kept in Media with its prompt and cost."
        )
        intro.setWordWrap(True)
        # What goes with each prompt: the words, or the style's picture too.
        self.sends = QLabel(WORDS_ONLY)
        self.sends.setObjectName("sentWithPrompt")
        self.sends.setWordWrap(True)
        self.model = ModelField(
            family_id(session.config.portrait_model, catalog.models if catalog is not None else []),
            browse,
            recommended=RECOMMENDED_IMAGE_MODELS["portrait_model"],
            pictures=self._picture_support,
        )
        self.model_note = QLabel()
        self.model_note.setObjectName("hintLabel")
        self.model_note.setWordWrap(True)
        story = session.story
        self.style = PictureStyleField(
            story.picture_style,
            story.image_style,
            texts=session.texts,
            from_picture=describe if describe is not None else None,
            picture=bool(story.style_picture),
        )
        self.style_note = QLabel(
            "Every picture of this story is drawn in it: a style chosen here becomes the "
            "story's (Story → Setup… → About)."
        )
        self.style_note.setObjectName("hintLabel")
        self.style_note.setWordWrap(True)
        self.framing = QComboBox()
        for framing in FRAMINGS:
            self.framing.addItem(FRAMING_LABELS[framing], framing)
        self.framing.setToolTip("How much of each character the portraits show")
        form = QFormLayout()
        form.addRow("Style", self.style)
        form.addRow(self.style_note)
        if self._characters:
            form.addRow("Framing", self.framing)
        form.addRow("Drawn by", self.model)
        form.addRow(self.model_note)

        pictures = session.pictures
        self.rows: list[_Row] = []
        # Rows whose prompts are being written, and rows ticked meanwhile
        # that wait for theirs.
        self._writing: list[_Row] = []
        self._ticked_meanwhile: list[_Row] = []
        holder = QWidget()
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 6, 0)
        for heading, cards in (("Characters", self._characters), ("Lore", self._lore)):
            if not cards:
                continue
            if self._characters and self._lore:
                title = QLabel(heading.upper())
                title.setObjectName("composerCaption")
                rows.addWidget(title)
            for card in cards:
                first = card.reference_images[0] if card.reference_images else None
                thumb = (
                    thumbnail_of(pictures.read(first.file), THUMB_SIDE)
                    if first is not None and pictures is not None
                    else None
                )
                row = _Row(card, thumb)
                if first is not None and len(characters) + len(lore) > 1:
                    # Has a picture already: drawn again only if ticked (all
                    # were, and each new one went in front of the old).
                    row.tick.setChecked(False)
                    row.wait()
                row.tick.toggled.connect(lambda on, row=row: self._ticked(row, on))
                row.prompt.textChanged.connect(self._sync)
                self.rows.append(row)
                rows.addWidget(row)
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(holder)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumHeight(min(560, 120 * max(1, len(self.rows))))

        self.status = QLabel()
        self.status.setObjectName("hintLabel")
        self.status.setWordWrap(True)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setTextVisible(False)
        self.busy.setMaximumHeight(6)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.draw_button = self.buttons.addButton("Draw", QDialogButtonBox.AcceptRole)
        self.draw_button.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.sends)
        layout.addLayout(form)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.busy)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        # Before anything can call `_sync`: a stale listing makes `ensure`
        # say it changed at once, and `_sync` reads the worker.
        self._thread: QThread | None = None
        self._worker: GenerationWorker | None = None
        # A new style or framing asked for while the prompts were written.
        self._again = False
        self._restyle_timer = QTimer(self)
        self._restyle_timer.setSingleShot(True)
        self._restyle_timer.setInterval(RESTYLE_DELAY_MS)
        self._restyle_timer.timeout.connect(self._restyle)
        self.style.combo.currentIndexChanged.connect(lambda _i: self._restyle())
        self.style.own.textEdited.connect(lambda _t: self._restyle_timer.start())
        self.style.with_picture.toggled.connect(lambda _on: self._restyle())
        self.style.taken.connect(self._restyle)
        self.framing.currentIndexChanged.connect(lambda _i: self._restyle())
        self.model.textChanged.connect(self._sync)
        if catalog is not None:
            catalog.changed.connect(self._sync)
            catalog.ensure()
        # Only the cards to be drawn: every prompt was written, and paid for,
        # the unticked ones' too.
        ticked = [row for row in self.rows if row.tick.isChecked()]
        if ticked:
            self._write(ticked)
        self._sync()
        size_to_screen(self, 0.5, 0.88, at_least=QSize(720, 760))

    # --- writing the prompts ---------------------------------------------------------

    def _write(self, rows: list[_Row]) -> None:
        """The prompts for `rows` written, in the style and framing chosen
        now (read here, on the GUI thread)."""
        self._writing = rows
        for row in rows:
            row.rewrite()
        characters = [row.card for row in rows if isinstance(row.card, Character)]
        lore = [row.card for row in rows if isinstance(row.card, LoreEntry)]
        style, framing = self.chosen_style(), self.chosen_framing()
        with_picture = self.style.picture_wanted()
        self._prompts, self._problem, self._failure = {}, None, None

        def job():
            self._prompts, self._problem = self.session.write_card_prompts(
                characters, lore, framing=framing, style=style, style_picture=with_picture
            )
            return
            yield  # a generator, so GenerationWorker can run it

        worker = GenerationWorker(job, provider=self.session.provider)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        # Cleanup on the thread, never the worker's own finished (CONTRIBUTING.md).
        thread.finished.connect(self._on_written)
        self._worker, self._thread = worker, thread
        thread.start()

    def _on_failed(self, message: str) -> None:
        self._failure = message

    def _on_written(self) -> None:
        self._worker, self._thread = None, None
        if self._closing:
            super().reject()
            return
        for row in self._writing:
            row.fill(self._prompts.get(row.card.id, ""))
        self._writing = []
        if self._failure is not None:
            self._problem = f"The prompts couldn't be written: {self._failure}"
        waiting, self._ticked_meanwhile = self._ticked_meanwhile, []
        if self._again:
            self._again = False
            self._restyle()  # those ticked meanwhile among them
            return
        waiting = [row for row in waiting if row.tick.isChecked() and not row.written]
        if waiting:
            self._write(waiting)
        self._sync()

    def _ticked(self, row: _Row, on: bool) -> None:
        """A card ticked with no prompt yet: its prompt written now, or once
        the writing under way is done."""
        if on and not row.written and not row.edited:
            if self.writing():
                if row not in self._ticked_meanwhile:
                    self._ticked_meanwhile.append(row)
                row.rewrite()
            else:
                self._write([row])
        self._sync()

    def _restyle(self) -> None:
        """A new style or framing: the prompts written again, all but those
        the author changed, once any writing under way is done."""
        self._restyle_timer.stop()
        if self.writing():
            self._again = True
            return
        for row in self.rows:
            if not row.edited and not row.tick.isChecked():
                row.wait()  # written in the new style once it is ticked
        rows = [row for row in self.rows if not row.edited and row.tick.isChecked()]
        if rows:
            self._write(rows)
        self._sync()

    def writing(self) -> bool:
        return self._worker is not None

    # --- the price and the button ---------------------------------------------------

    def model_info(self) -> ImageModelInfo | None:
        if self._catalog is None:
            return None
        model = self.model.text().strip()
        return next((m for m in self._catalog.models if m.id == model), None)

    def _picture_support(self, model: str) -> tuple[str, str]:
        return picture_support(model, self._catalog.models if self._catalog is not None else [])

    def _sync(self, *_args) -> None:
        self.model.show_pictures()
        model = self.model.text().strip()
        info = self.model_info()
        wanted = [row for row in self.rows if row.wanted()]
        framing = self.chosen_framing()
        prices = [info.price(card_size(row.card, info, framing)) for row in wanted] if info else []
        first = self.rows[0].card if self.rows else None
        each = info.price(card_size(first, info, framing)) if info and first else None
        known = recommended_model("portrait_model", model)
        price = f"about ${each:.3f} a picture" if each is not None else "price not listed"
        note = f"{model or 'No model'}: {price}." + (f" {known.note}" if known else "")
        if self.style.picture_wanted():
            self.sends.setText(
                "Sent with the prompt: the picture the story's style was taken from, as image "
                "1, so each follows its style."
            )
            if info is not None and not info.accepts_images:
                note += (
                    " It can't be given a picture: the prompts name an image 1 it won't get. "
                    "Choose a model that takes pictures, or untick With its picture."
                )
        else:
            self.sends.setText(WORDS_ONLY)
        self.model_note.setText(note)
        self.busy.setVisible(self.writing())
        count = len(wanted)
        if self.writing():
            self.status.setText("Writing the prompts…")
        else:
            total = sum(p for p in prices if p)
            cost = f", about ${total:.2f} in all" if total else ""
            self.status.setText(
                (self._problem + " " if self._problem else "")
                + f"{count} picture{'' if count == 1 else 's'} to draw{cost}."
            )
        self.draw_button.setText(f"Draw {count}" if count else "Draw")
        self.draw_button.setEnabled(not self.writing() and count > 0 and bool(model))

    # --- the result -----------------------------------------------------------------

    def chosen_model(self) -> str:
        return self.model.text().strip()

    def chosen_style(self) -> tuple[str, str]:
        """(a ready-made style's id, the author's own words), as the story
        keeps them."""
        return self.style.value()

    def chosen_framing(self) -> str:
        return self.framing.currentData() or DEFAULT_FRAMING

    def requests(self) -> list[ImageRequest]:
        """One request a ticked card, each prompt exactly as it stands; a
        portrait's size and label by the framing chosen. With its picture,
        the style's picture goes as image 1: read once the window has kept
        the style and its picture for the story (`draw_card_pictures`)."""
        model, info = self.chosen_model(), self.model_info()
        framing = self.chosen_framing()
        style = style_key(*self.chosen_style())
        style_use = self.session.style_reference() if self.style.picture_wanted() else None
        return [
            self.session.card_request(
                row.card,
                row.prompt.toPlainText(),
                model=model,
                info=info,
                first=True,
                framing=framing if isinstance(row.card, Character) else "",
                style=style,
                style_reference=style_use,
            )
            for row in self.rows
            if row.wanted()
        ]

    def reject(self) -> None:
        if self._worker is not None:
            # The writer's call is stopped; close once its thread is done
            # rather than block here or destroy a running thread.
            self._closing = True
            self._worker.stop()
            self.status.setText("Stopping…")
            return
        super().reject()
