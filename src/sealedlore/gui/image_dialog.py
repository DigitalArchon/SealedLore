"""Generate image: write a prompt, show the author everything, send only what they approve.

The dialog is the approval. Everything the request will carry is on screen:
the model, the size, how many pictures, the price, the reference pictures in
the order they are numbered, and the whole prompt, which the author may
change freely. Generate builds the request from exactly that (the prompt's
text as it stands, not even trimmed) and nothing changes it afterwards.

Writing the prompt is a call on the story's own model (`write_image_prompt`),
run on the dialog's worker; the picture is drawn in the background after the
dialog closes (`gui.image_jobs`).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThread
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.image_prompt import ImagePromptDraft, mentioned_numbers
from sealedlore.engine.images import ImageRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.session_images import DEFAULT_MAX_REFS
from sealedlore.gui.image_jobs import ImageCatalog
from sealedlore.gui.image_picker import ImagePickerDialog, pick_image_model
from sealedlore.gui.model_picker import ModelField
from sealedlore.gui.ref_images import (
    PICTURE_FILTER,
    THUMB,
    import_reference,
    load_pixmap,
    thumbnail,
)
from sealedlore.gui.worker import GenerationWorker
from sealedlore.models.image import GeneratedImage, RefUse
from sealedlore.providers.images import ImageModelInfo, parse_image_models
from sealedlore.storage.images import story_path

DIRECTION_HINT = (
    "Optional. Blank pictures what is happening now. Or say what you want:\n"
    "“The current scene, with the focus on my character in combat.”\n"
    "“Ignore the background chatter: a portrait of her reaction to what I just said.”"
)


class ImageDialog(QDialog):
    def __init__(
        self,
        session: StorySession,
        *,
        anchor_id: str | None = None,
        prefill: GeneratedImage | None = None,
        catalog: ImageCatalog | None = None,
        browse: Callable[[str], str | None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Generate image")
        self._browse = browse
        self.setMinimumWidth(720)
        self.session = session
        self.anchor_id = anchor_id or session.story.active_leaf_id
        self.request: ImageRequest | None = None
        self._choices = session.image_ref_choices(self.anchor_id)
        self._refs: list[RefUse] = []
        # The author changed the references after the prompt was written.
        self._refs_edited = False
        self._written_with: list[str] | None = None
        self._worker: GenerationWorker | None = None
        self._thread: QThread | None = None
        self._closing = False
        self._draft: ImagePromptDraft | None = None
        self._failure: str | None = None
        self._models: list[ImageModelInfo] = parse_image_models(
            {"data": session.config.image_models}
        )
        self._catalog = catalog

        self._build()
        if prefill is not None:
            self._prefill(prefill)
        self._fill_models(prefill.model if prefill is not None else session.config.image_model)
        if prefill is not None:
            self._select_size(prefill.size)
        self._sync()
        if catalog is not None:
            catalog.changed.connect(self._on_listing)
            catalog.ensure()

    # --- layout -------------------------------------------------------------

    def _build(self) -> None:
        where = "the moment the story is at now"
        if self.anchor_id and self.anchor_id != self.session.story.active_leaf_id:
            where = "the moment the chosen passage ends on"
        intro = QLabel(
            f"A picture of {where}. A model that has read the story writes a prompt; you "
            "check and edit it, and nothing is sent until you press Generate. The picture "
            "is drawn in the background while you play on."
        )
        intro.setWordWrap(True)
        intro.setObjectName("hintLabel")
        # In a private scene the prompt is written by the private model, but
        # the picture is made elsewhere and kept with the story.
        self.private_warning = QLabel(
            "Private scene: the prompt is written by your private model, but the image "
            "model below is not private. The prompt and any "
            "reference pictures are sent to it, and the picture and its prompt are saved "
            "with the story on disk, even if the scene is kept in memory only (your "
            "direction above is then not saved)."
        )
        self.private_warning.setObjectName("warningLabel")
        self.private_warning.setWordWrap(True)
        tee_chat = self.session.tee_chat
        if tee_chat:
            self.private_warning.setText(
                "TEE chat: the prompt is written by the chat's own model, but the image "
                "model below is not private. The prompt and any reference pictures are sent "
                "to it, and the picture and its prompt are saved with the chat."
            )
        self.private_warning.setVisible(self.session.in_private or tee_chat)

        self.direction = QPlainTextEdit()
        self.direction.setPlaceholderText(DIRECTION_HINT)
        self.direction.setFixedHeight(84)
        self.write_button = QPushButton("Write prompt")
        self.write_button.clicked.connect(self._write)
        # Who writes the prompt: blank is the story model. Playtesting: the
        # setting was in Settings → Models only, so in play it couldn't be
        # found. A choice here is kept as that same setting.
        self.writer = ModelField(self.session.config.image_prompt_model or "", self._browse)
        self.writer.setPlaceholderText(self.session.model or "the story's model")
        if self.session.in_private:
            self.writer.setText(self.session.open_span.model)
            self.writer.setReadOnly(True)
        elif tee_chat:
            # A TEE chat's prompt is written by its own model (session_images).
            self.writer.setText(self.session.model)
            self.writer.setReadOnly(True)
        self.writer.textChanged.connect(lambda _text: self._show_writer())
        self._show_writer()
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setMaximumHeight(6)
        self.busy.setTextVisible(False)
        self.busy.hide()
        self.status = QLabel("")
        self.status.setObjectName("hintLabel")
        self.status.setWordWrap(True)
        self.status.hide()

        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "Press Write prompt, or write your own. This text is sent exactly as it is here."
        )
        self.prompt.setMinimumHeight(170)
        self.prompt.textChanged.connect(self._sync)

        self.refs = QListWidget()
        self.refs.setObjectName("refImageList")
        self.refs.setViewMode(QListWidget.IconMode)
        self.refs.setFlow(QListWidget.LeftToRight)
        self.refs.setWrapping(False)
        self.refs.setMovement(QListWidget.Static)
        self.refs.setIconSize(QSize(THUMB, THUMB))
        self.refs.setFixedHeight(THUMB + 44)
        self.refs.setGridSize(QSize(THUMB + 64, THUMB + 28))
        self.refs.setWordWrap(False)
        self.refs.setTextElideMode(Qt.ElideRight)
        self.refs.setSelectionMode(QAbstractItemView.SingleSelection)
        self.refs.currentRowChanged.connect(lambda _row: self._sync_ref_buttons())
        self.add_ref = QToolButton()
        self.add_ref.setText("Add…")
        self.add_ref.setPopupMode(QToolButton.InstantPopup)
        self.add_ref.setMenu(QMenu(self.add_ref))
        self.add_ref.menu().aboutToShow.connect(self._fill_add_menu)
        self.remove_ref = QToolButton()
        self.remove_ref.setText("Remove")
        self.remove_ref.clicked.connect(self._remove_ref)
        self.left_ref = QToolButton()
        self.left_ref.setText("◀")
        self.left_ref.setToolTip("Move earlier: a lower image number")
        self.left_ref.clicked.connect(lambda: self._move_ref(-1))
        self.right_ref = QToolButton()
        self.right_ref.setText("▶")
        self.right_ref.setToolTip("Move later: a higher image number")
        self.right_ref.clicked.connect(lambda: self._move_ref(1))
        ref_buttons = QHBoxLayout()
        ref_buttons.setContentsMargins(0, 0, 0, 0)
        for button in (self.add_ref, self.remove_ref, self.left_ref, self.right_ref):
            ref_buttons.addWidget(button)
        ref_buttons.addStretch(1)
        self.ref_note = QLabel("")
        self.ref_note.setObjectName("hintLabel")
        self.ref_note.setWordWrap(True)

        # Typed, or chosen with Browse… from a searchable table, as a text
        # model is (gui/image_picker.py).
        self.model = ModelField(
            "", self._browse_image_models if self._catalog is not None else None
        )
        self.model.setMinimumWidth(260)
        self.model.textChanged.connect(self._on_model_changed)
        self.size_choice = QComboBox()
        self.size_choice.setEditable(True)
        self.size_choice.currentTextChanged.connect(self._sync)
        self.count = QSpinBox()
        self.count.setRange(1, 4)
        self.count.setValue(self.session.config.image_count)
        self.count.valueChanged.connect(self._sync)
        self.price = QLabel("")
        self.price.setObjectName("hintLabel")
        settings = QHBoxLayout()
        settings.addWidget(QLabel("Model"))
        settings.addWidget(self.model, 1)
        settings.addWidget(QLabel("Size"))
        settings.addWidget(self.size_choice)
        settings.addWidget(QLabel("Pictures"))
        settings.addWidget(self.count)

        self.warning = QLabel("")
        self.warning.setObjectName("warningLabel")
        self.warning.setWordWrap(True)
        self.warning.hide()

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.generate_button = QPushButton("Generate")
        self.generate_button.setObjectName("sendButton")
        self.generate_button.setDefault(False)
        self.generate_button.setAutoDefault(False)
        self.buttons.addButton(self.generate_button, QDialogButtonBox.AcceptRole)
        self.generate_button.clicked.connect(self._approve)
        self.buttons.rejected.connect(self.reject)

        direction_row = QHBoxLayout()
        direction_row.addWidget(self.direction, 1)
        direction_row.addWidget(self.write_button, 0, Qt.AlignTop)

        form = QFormLayout()
        form.addRow("Direction", direction_row)
        form.addRow("Prompt written by", self.writer)
        form.addRow("", self.busy)
        form.addRow("", self.status)
        form.addRow("Prompt", self.prompt)
        refs_box = QVBoxLayout()
        refs_box.setContentsMargins(0, 0, 0, 0)
        refs_box.addWidget(self.refs)
        refs_box.addLayout(ref_buttons)
        refs_box.addWidget(self.ref_note)
        refs_host = QWidget()
        refs_host.setLayout(refs_box)
        form.addRow("References", refs_host)
        form.addRow("", settings)
        form.addRow("", self.price)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.private_warning)
        layout.addLayout(form)
        layout.addWidget(self.warning)
        layout.addWidget(self.buttons)

    def _prefill(self, image: GeneratedImage) -> None:
        open_span = self.session.open_span
        if image.private_span is None or (
            open_span is not None and open_span.id == image.private_span
        ):
            self.direction.setPlainText(image.direction)
        # A direction from a private scene that has ended is never offered to
        # another model's prompt writer: the approved prompt alone comes back.
        self.prompt.setPlainText(image.prompt)
        self._set_refs(list(image.references))
        self._written_with = [use.ref_id for use in image.references]

    # --- the image models ---------------------------------------------------

    def _info(self) -> ImageModelInfo | None:
        wanted = self.model.text().strip()
        return next((info for info in self._models if info.id == wanted), None)

    def _fill_models(self, selected: str) -> None:
        self.model.blockSignals(True)
        self.model.setText(selected)
        self.model.blockSignals(False)
        self._on_model_changed()

    def _browse_image_models(self, current: str) -> str | None:
        return pick_image_model(self._catalog, current, self)

    def _on_model_changed(self, *_args) -> None:
        info = self._info()
        self.model.setToolTip(
            ImagePickerDialog._describe(info)
            if info is not None
            else "The model that draws: type its id, or Browse… the endpoint's image models."
        )
        current = self.size_choice.currentText()
        self.size_choice.blockSignals(True)
        self.size_choice.clear()
        sizes = list(info.resolutions) if info is not None else []
        for size in sizes:
            price = info.price(size) if info is not None else None
            self.size_choice.addItem(size)
            if price is not None:
                self.size_choice.setItemData(
                    self.size_choice.count() - 1, f"${price:.3f} a picture", Qt.ToolTipRole
                )
        wanted = current or self.session.config.image_size
        if wanted not in sizes and sizes:
            wanted = (
                self.session.config.image_size
                if self.session.config.image_size in sizes
                else sizes[0]
            )
        self.size_choice.setCurrentText(wanted)
        self.size_choice.blockSignals(False)
        self.count.setMaximum(info.max_outputs if info is not None else 4)
        self._set_refs(self._refs)
        self._sync()

    def _select_size(self, size: str) -> None:
        self.size_choice.setCurrentText(size)

    def _on_listing(self) -> None:
        if self._catalog is None:
            return
        self._models = self._catalog.models
        self._fill_models(self.model.text().strip() or self.session.config.image_model)

    # --- references ---------------------------------------------------------

    def _max_refs(self) -> int:
        info = self._info()
        return info.max_inputs if info is not None else DEFAULT_MAX_REFS

    def _set_refs(self, refs: list[RefUse]) -> None:
        self._refs = refs
        self.refs.clear()
        folder = (self.session.story.id, self.session.root)
        for number, use in enumerate(refs, start=1):
            caption = f" ({use.caption})" if use.caption else ""
            item = QListWidgetItem(f"{number} · {use.owner_name}")
            item.setToolTip(f"image {number}: {use.owner_name}{caption}")
            try:
                item.setIcon(QIcon(thumbnail(story_path(folder[0], use.file, folder[1]))))
            except ValueError:
                pass
            self.refs.addItem(item)
        self._sync_ref_buttons()
        self._sync()

    def _sync_ref_buttons(self) -> None:
        row = self.refs.currentRow()
        # A picture can always be added from disk, so there is always a choice.
        self.add_ref.setEnabled(self._max_refs() > 0)
        self.remove_ref.setEnabled(row >= 0)
        self.left_ref.setEnabled(row > 0)
        self.right_ref.setEnabled(0 <= row < len(self._refs) - 1)

    def _fill_add_menu(self) -> None:
        menu = self.add_ref.menu()
        menu.clear()
        used = {use.ref_id for use in self._refs}
        for choice in self._choices:
            if choice.use.ref_id in used:
                continue
            use = choice.use
            label = use.owner_name + (f" — {use.caption}" if use.caption else "")
            action = menu.addAction(label + ("  (in the scene)" if choice.present else ""))
            action.triggered.connect(lambda _checked=False, u=use: self._add(u))
        # Any picture can be added, in a story as in a chat: one that belongs
        # to no card (a place, an object, a pose) had no way in. It was a
        # chat's alone, since a chat has no cards to hold pictures.
        if not menu.isEmpty():
            menu.addSeparator()
        menu.addAction("Add a picture from disk…").triggered.connect(self._add_from_disk)

    def _add_from_disk(self) -> None:
        """The story's own reference picture, on no card: copied into the
        story, kept on it (`Story.reference_images`), and attached here."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Add a reference picture", str(Path.home()), PICTURE_FILTER
        )
        if not path:
            return
        session = self.session
        try:
            ref = import_reference(Path(path), session.story.id, session.root)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Couldn't add the picture", str(exc))
            return
        session.story.reference_images.append(ref)
        session.save()
        self._choices = session.image_ref_choices(self.anchor_id)
        added = next((c.use for c in self._choices if c.use.ref_id == ref.id), None)
        if added is not None:
            self._add(added)

    def _add(self, use: RefUse) -> None:
        self._refs_edited = True
        self._set_refs([*self._refs, use])

    def _remove_ref(self) -> None:
        row = self.refs.currentRow()
        if row >= 0:
            self._refs_edited = True
            self._set_refs([use for i, use in enumerate(self._refs) if i != row])

    def _move_ref(self, step: int) -> None:
        row = self.refs.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < len(self._refs):
            return
        refs = list(self._refs)
        refs[row], refs[target] = refs[target], refs[row]
        self._refs_edited = True
        self._set_refs(refs)
        self.refs.setCurrentRow(target)

    # --- writing the prompt ---------------------------------------------------

    def writer_model(self) -> str | None:
        """The writer chosen here, None for the story model (or in private, or
        in a TEE chat, whose own model writes)."""
        if self.session.in_private or self.session.tee_chat:
            return None
        return self.writer.text().strip() or None

    def _writer_name(self) -> str:
        if self.session.in_private:
            return self.session.open_span.model
        return self.writer_model() or self.session.model

    def _show_writer(self) -> None:
        name = self._writer_name()
        if self.session.in_private:
            tip = f"Your private model, {name}, writes the prompt from the scene."
        elif name == self.session.model:
            tip = f"Ask {name} to write the prompt from the story (one call on its cached prompt)."
        else:
            tip = (
                f"Ask {name} to write the prompt from the story. It isn't the story's model, "
                "so it reads the whole story without the storyteller's cache."
            )
        self.write_button.setToolTip(tip)
        self.writer.setToolTip(
            tip if self.session.in_private else tip + " Blank: the story's model."
        )

    def _write(self) -> None:
        if self._worker is not None:
            return
        direction = self.direction.toPlainText().strip()
        fixed = list(self._refs) if self._refs_edited else None
        max_refs = self._max_refs()
        session = self.session
        anchor = self.anchor_id
        model = self.writer_model()
        self._draft, self._failure = None, None

        def job():
            self._draft = session.write_image_prompt(
                direction, anchor_id=anchor, max_refs=max_refs, fixed=fixed, model=model
            )
            return
            yield

        writer_provider = session.private_provider if session.in_private else session.provider
        worker = GenerationWorker(job, provider=writer_provider)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.failed.connect(self._on_write_failed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_written)
        self._worker, self._thread = worker, thread
        self._set_running(True)
        self._show_status(f"Writing the prompt with {self._writer_name()}…")
        thread.start()

    def _on_write_failed(self, message: str) -> None:
        self._failure = message

    def _on_written(self) -> None:
        self._worker, self._thread = None, None
        self._set_running(False)
        if self._closing:
            super().reject()
            return
        if self._failure is not None:
            self._show_status(f"The prompt couldn't be written: {self._failure}")
            return
        draft = self._draft
        if draft is None:
            self._show_status("Stopped before a prompt was written.")
            return
        self.prompt.setPlainText(draft.prompt)
        self._refs_edited = False
        self._set_refs(list(draft.references))
        self._written_with = [use.ref_id for use in draft.references]
        note = "Check the prompt and the pictures, change anything, then Generate."
        if draft.dropped:
            note += (
                f" (Left out references it named that weren't offered: {', '.join(draft.dropped)}.)"
            )
        self._show_status(note)

    def _set_running(self, running: bool) -> None:
        self.busy.setVisible(running)
        self.write_button.setEnabled(not running)
        self.generate_button.setEnabled(not running and bool(self.prompt.toPlainText().strip()))
        self.direction.setReadOnly(running)
        self.prompt.setReadOnly(running)

    def _show_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.show()

    # --- checks and approval --------------------------------------------------

    def _problems(self) -> list[str]:
        problems: list[str] = []
        info = self._info()
        text = self.prompt.toPlainText()
        if info is not None and self._refs and info.max_inputs == 0:
            problems.append(
                f"{info.name} takes no reference pictures: they won't be sent. Remove them, "
                "or choose a model that takes them."
            )
        elif info is not None and len(self._refs) > info.max_inputs:
            problems.append(f"{info.name} takes at most {info.max_inputs} reference pictures.")
        numbers = mentioned_numbers(text)
        beyond = sorted(n for n in numbers if n > len(self._sent_refs()))
        if beyond:
            names = ", ".join(f"image {n}" for n in beyond)
            count = len(self._sent_refs())
            attached = f"{count} picture{'s are' if count != 1 else ' is'} attached"
            problems.append(f"The prompt mentions {names}, but {attached}.")
        if (
            self._written_with is not None
            and [use.ref_id for use in self._refs] != self._written_with
            and numbers
        ):
            problems.append(
                "The pictures changed since the prompt was written; it refers to them by "
                "number, so check it matches (or press Write prompt again)."
            )
        return problems

    def _sent_refs(self) -> list[RefUse]:
        info = self._info()
        if info is not None and info.max_inputs == 0:
            return []
        return list(self._refs)

    def _sync(self, *_args) -> None:
        info = self._info()
        size = self.size_choice.currentText().strip()
        n = self.count.value()
        price = info.price(size) if info is not None else None
        if price is not None:
            self.price.setText(
                f"${price:.3f} a picture × {n} = about ${price * n:.3f}, as listed by the endpoint"
            )
            self.generate_button.setText(f"{self._generate_word()} (~${price * n:.3f})")
        else:
            self.price.setText("Price unknown: the endpoint doesn't list this model.")
            self.generate_button.setText(self._generate_word())
        accepts = info is None or info.max_inputs > 0
        if not self._choices:
            self.ref_note.setText(
                "No reference pictures in this story yet. Add them to characters and lore "
                "entries (Cast and Lore tabs) to keep their look."
            )
        elif not accepts:
            self.ref_note.setText("This model takes no reference pictures.")
        else:
            self.ref_note.setText(
                "Sent with the prompt, numbered as shown. The prompt refers to them as "
                "“image 1”, “image 2”…"
            )
        problems = self._problems()
        self.warning.setText("\n".join(problems))
        self.warning.setVisible(bool(problems))
        running = self._worker is not None
        self.generate_button.setEnabled(
            not running and bool(self.prompt.toPlainText().strip()) and bool(size)
        )
        self._sync_ref_buttons()

    def _generate_word(self) -> str:
        return "Generate anyway" if self.session.in_private else "Generate"

    def _approve(self) -> None:
        text = self.prompt.toPlainText()
        size = self.size_choice.currentText().strip()
        model = self.model.text().strip()
        if not text.strip() or not size or not model:
            return
        info = self._info()
        self.request = ImageRequest(
            story_id=self.session.story.id,
            model=model,
            # Exactly what is on screen.
            prompt=text,
            size=size,
            n=self.count.value(),
            references=tuple(self._sent_refs()),
            anchor_node_id=self.anchor_id,
            direction=self.direction.toPlainText().strip(),
            listed_price=info.price(size) if info is not None else None,
            private_span=self.session.open_span.id if self.session.in_private else None,
            # A memory-only scene's direction describes the scene: not written down.
            direction_kept=not (
                self.session.in_private and self.session.open_span.keep == "memory"
            ),
        )
        self.accept()

    def reject(self) -> None:
        if self._worker is not None:
            self._closing = True
            self._worker.stop()
            self._show_status("Stopping…")
            return
        super().reject()


class FitPicture(QWidget):
    """A picture scaled to fit the widget, both ways."""

    def __init__(self, pixmap: QPixmap) -> None:
        super().__init__()
        self._pixmap = pixmap

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return self._pixmap.size().scaled(1100, 800, Qt.KeepAspectRatio)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        if self._pixmap.isNull():
            return
        scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        painter = QPainter(self)
        painter.drawPixmap(
            (self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2, scaled
        )


class ImageViewer(QDialog):
    """A picture full size, with its prompt."""

    def __init__(self, image: GeneratedImage, path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Picture · {image.model.rsplit('/', 1)[-1]} · {image.size}")
        self._path = path
        picture = FitPicture(load_pixmap(path))
        prompt = QPlainTextEdit(image.prompt)
        prompt.setReadOnly(True)
        prompt.setMaximumHeight(110)
        refs = ", ".join(
            f"image {i}: {use.owner_name}" for i, use in enumerate(image.references, start=1)
        )
        details = QLabel(refs or "No reference pictures.")
        details.setObjectName("hintLabel")
        details.setWordWrap(True)
        save = QPushButton("Save as…")
        save.clicked.connect(self._save)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(details, 1)
        row.addWidget(save)
        row.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(picture, 1)
        layout.addWidget(prompt)
        layout.addLayout(row)
        self.resize(1000, 800)

    def _save(self) -> None:
        save_picture_as(self, self._path)


def save_picture_as(parent: QWidget, path: Path) -> None:
    target, _ = QFileDialog.getSaveFileName(
        parent, "Save picture", str(Path.home() / path.name), f"Pictures (*{path.suffix})"
    )
    if target:
        try:
            Path(target).write_bytes(path.read_bytes())
        except OSError as exc:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(parent, "Couldn't save", str(exc))
