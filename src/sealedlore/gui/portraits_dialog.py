"""Portraits of the characters (engine/portraits.py, engine/session_portraits.py):
offered when a story starts and on the Cast page.

The prompt writer writes a prompt for each character from their card while
the dialog is open; the author sees every one, may change any of them and
untick whoever they don't want drawn, and chooses the model with its price.
Nothing is drawn until Draw: each ticked prompt is then sent exactly as it
stands (`requests`), as an ordinary picture, in the background.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.images import ImageRequest
from sealedlore.engine.portraits import portrait_size
from sealedlore.gui.image_jobs import ImageCatalog
from sealedlore.gui.model_picker import ModelField
from sealedlore.gui.ref_images import thumbnail_of
from sealedlore.gui.worker import GenerationWorker
from sealedlore.models.character import Character
from sealedlore.models.config import RECOMMENDED_IMAGE_MODELS, recommended_model
from sealedlore.providers.images import ImageModelInfo

THUMB_SIDE = 64
WRITING = "Writing…"


class _Row(QFrame):
    """One character: drawn or not, what they look like now, the prompt."""

    def __init__(self, character: Character, thumbnail) -> None:
        super().__init__()
        self.setObjectName("portraitRow")
        self.character = character
        self.tick = QCheckBox(character.name)
        self.tick.setChecked(True)
        self.prompt = QPlainTextEdit(WRITING)
        self.prompt.setEnabled(False)
        self.prompt.setFixedHeight(96)
        picture = QLabel()
        picture.setFixedSize(THUMB_SIDE, THUMB_SIDE)
        picture.setAlignment(Qt.AlignCenter)
        if thumbnail is not None and not thumbnail.isNull():
            picture.setPixmap(thumbnail)
            picture.setToolTip("Their portrait now: a new one goes first, this one stays")
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
        self.written = False

    def fill(self, prompt: str) -> None:
        self.written = True
        self.prompt.setPlainText(prompt)
        self.prompt.setEnabled(self.tick.isChecked())

    def wanted(self) -> bool:
        return self.written and self.tick.isChecked()


class PortraitsDialog(QDialog):
    def __init__(
        self,
        session,
        characters: Sequence[Character],
        *,
        catalog: ImageCatalog | None,
        browse: Callable[[str], str | None] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Portraits")
        self.setMinimumWidth(620)
        self.session = session
        self._catalog = catalog
        self._closing = False
        self._prompts: dict[str, str] = {}
        self._problem: str | None = None
        self._failure: str | None = None

        intro = QLabel(
            f"A portrait of each character, drawn from their card. "
            f"{session.image_prompt_model} is writing a prompt for each: read them, change "
            "any you like and untick anyone you'd rather not draw. Nothing is drawn until "
            "you press Draw. Each portrait becomes the character's first picture, shown "
            "large on the Cast page, and is kept in Media with its prompt and cost."
        )
        intro.setWordWrap(True)
        self.model = ModelField(
            session.config.portrait_model,
            browse,
            recommended=RECOMMENDED_IMAGE_MODELS["portrait_model"],
        )
        self.model_note = QLabel()
        self.model_note.setObjectName("hintLabel")
        self.model_note.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Drawn by", self.model)
        form.addRow(self.model_note)

        pictures = session.pictures
        self.rows: list[_Row] = []
        holder = QWidget()
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 6, 0)
        for character in characters:
            first = character.reference_images[0] if character.reference_images else None
            thumb = (
                thumbnail_of(pictures.read(first.file), THUMB_SIDE)
                if first is not None and pictures is not None
                else None
            )
            row = _Row(character, thumb)
            row.tick.toggled.connect(self._sync)
            self.rows.append(row)
            rows.addWidget(row)
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(holder)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumHeight(min(520, 120 * max(1, len(self.rows))))

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
        layout.addLayout(form)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.busy)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        self.model.textChanged.connect(self._sync)
        if catalog is not None:
            catalog.changed.connect(self._sync)
            catalog.ensure()
        self._thread: QThread | None = None
        self._worker: GenerationWorker | None = None
        self._write()
        self._sync()
        self.resize(700, 720)

    # --- writing the prompts ---------------------------------------------------------

    def _write(self) -> None:
        characters = [row.character for row in self.rows]

        def job():
            self._prompts, self._problem = self.session.write_portrait_prompts(characters)
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
        for row in self.rows:
            prompt = self._prompts.get(row.character.id)
            if prompt is not None:
                row.fill(prompt)
            else:
                row.prompt.setPlainText("")
        if self._failure is not None:
            self._problem = f"The prompts couldn't be written: {self._failure}"
        self._sync()

    def writing(self) -> bool:
        return self._worker is not None

    # --- the price and the button ---------------------------------------------------

    def model_info(self) -> ImageModelInfo | None:
        if self._catalog is None:
            return None
        model = self.model.text().strip()
        return next((m for m in self._catalog.models() if m.id == model), None)

    def _sync(self, *_args) -> None:
        model = self.model.text().strip()
        info = self.model_info()
        each = info.price(portrait_size(info)) if info is not None else None
        count = sum(row.wanted() for row in self.rows)
        known = recommended_model("portrait_model", model)
        price = f"about ${each:.3f} a portrait" if each else "price not listed"
        self.model_note.setText(
            f"{model or 'No model'}: {price}." + (f" {known.note}" if known is not None else "")
        )
        self.busy.setVisible(self.writing())
        if self.writing():
            self.status.setText("Writing the prompts…")
        else:
            total = f", about ${each * count:.2f} in all" if each and count else ""
            self.status.setText(
                (self._problem + " " if self._problem else "")
                + f"{count} portrait{'' if count == 1 else 's'} to draw{total}."
            )
        self.draw_button.setText(f"Draw {count}" if count else "Draw")
        self.draw_button.setEnabled(not self.writing() and count > 0 and bool(model))

    # --- the result -----------------------------------------------------------------

    def chosen_model(self) -> str:
        return self.model.text().strip()

    def requests(self) -> list[ImageRequest]:
        """One request a ticked character, each prompt exactly as it stands."""
        model, info = self.chosen_model(), self.model_info()
        return [
            self.session.portrait_request(
                row.character, row.prompt.toPlainText(), model=model, info=info
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
