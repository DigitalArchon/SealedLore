"""Generate video: the price first, then everything the request will carry,
and only what the author approves is sent.

Video costs far more than pictures (a picture is cents; a few seconds of
video are tens of cents to tens of dollars), and it is charged when it is
sent, whatever happens after. So the dialog:

- says what this video will cost, in dollars and in pictures' worth, before
  anything else, and makes the author tick that they accept it: a price it
  could not work out must be accepted as unknown, in so many words;
- warns every time when the video key is also the chat's or the pictures'
  (the author's rule: allowed, but only typed in by hand, and warned about);
- shows every setting the model takes, from its own listing, and sends
  nothing the listing doesn't offer (`VideoModelInfo.clean`);
- has the prompt written for video by the same prompt writer as pictures,
  told the length, the sound and the frames, and sends the prompt exactly as
  it stands on screen.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.session import StorySession
from sealedlore.engine.video_price import VideoQuote, pictures_worth, quote_video
from sealedlore.engine.videos import VideoRequest
from sealedlore.gui.model_picker import ModelField, _Row
from sealedlore.gui.ref_images import PICTURE_FILTER, import_reference, thumbnail_of
from sealedlore.gui.video_jobs import VideoCatalog
from sealedlore.gui.worker import GenerationWorker
from sealedlore.models.image import RefUse
from sealedlore.models.video import GeneratedVideo
from sealedlore.providers.videos import VideoModelInfo, VideoParam

DIRECTION_HINT = (
    "Optional. Blank shows what is happening now, in motion. Or say what you want:\n"
    "“She turns from the window as the door opens.”\n"
    "“A slow pan across the harbour at dawn, no people.”"
)
NO_FRAME = "(none)"
ADD_FROM_DISK = "Add a picture from disk…"


def cost_words(quote: VideoQuote) -> str:
    if quote.amount is None:
        return "Price unknown"
    return f"${quote.amount:.2f}"


class VideoDialog(QDialog):
    def __init__(
        self,
        session: StorySession,
        *,
        anchor_id: str | None = None,
        prefill: GeneratedVideo | None = None,
        start: RefUse | None = None,
        catalog: VideoCatalog | None = None,
        browse: Callable[[str], str | None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Generate video")
        self.setMinimumWidth(760)
        self.session = session
        self.catalog = catalog
        self._browse = browse
        self.anchor_id = anchor_id or session.story.active_leaf_id
        self.request: VideoRequest | None = None
        self._frames = session.video_frame_choices(self.anchor_id)
        self._controls: dict[str, QWidget] = {}
        self._worker: GenerationWorker | None = None
        self._thread: QThread | None = None
        self._closing = False
        self._written: str | None = None
        self._failure: str | None = None
        # WaveSpeed's own quote, for the settings it was asked about.
        self._remote_quote: tuple[str, float | None, str] | None = None
        media = session.config.videos()
        self._shared_key = bool(media and media.shared_key)
        self._api = media.api if media is not None else ""

        self._build()
        wanted = prefill.model if prefill is not None else session.config.video_model
        self.model.blockSignals(True)
        self.model.setText(wanted)
        self.model.blockSignals(False)
        self._on_model_changed()
        if prefill is not None:
            self._prefill(prefill)
        if start is not None:
            self._add_frame_choice(start)
            self._select_frame(self.start_choice, start)
        self._sync()
        if catalog is not None:
            catalog.changed.connect(self._on_listing)
            catalog.priced.connect(self._on_priced)
            catalog.ensure()

    # --- layout -------------------------------------------------------------------

    def _build(self) -> None:
        self.cost_banner = QLabel("")
        self.cost_banner.setObjectName("warningLabel")
        self.cost_banner.setWordWrap(True)
        self.shared_warning = QLabel(
            "⚠ The video key is the same as your chat or picture key (Settings → Video). "
            "Its spending can't be capped for video alone. A key of its own, with a daily "
            "limit set on it at the service, is the safe way."
        )
        self.shared_warning.setObjectName("warningLabel")
        self.shared_warning.setWordWrap(True)
        self.shared_warning.setVisible(self._shared_key)
        self.private_warning = QLabel(
            "Private scene: the prompt is written by your private model, but the video "
            "model is not private. The prompt and any frames are sent to it, and the video "
            "is kept with the story."
        )
        self.private_warning.setObjectName("warningLabel")
        self.private_warning.setWordWrap(True)
        self.private_warning.setVisible(self.session.in_private or self.session.tee_chat)

        self.model = ModelField("", self._browse_models if self.catalog is not None else None)
        self.model.setMinimumWidth(300)
        self.model.textChanged.connect(self._on_model_changed)
        self.model_note = QLabel("")
        self.model_note.setObjectName("hintLabel")
        self.model_note.setWordWrap(True)
        self.settings_host = QWidget()
        self.settings_form = QFormLayout(self.settings_host)
        self.settings_form.setContentsMargins(0, 0, 0, 0)

        self.start_choice = QComboBox()
        self.end_choice = QComboBox()
        for combo in (self.start_choice, self.end_choice):
            combo.currentIndexChanged.connect(lambda _i, c=combo: self._on_frame_chosen(c))
        self.start_thumb = QLabel()
        self.end_thumb = QLabel()
        self._fill_frames()
        frames = QHBoxLayout()
        frames.setContentsMargins(0, 0, 0, 0)
        for label, combo, thumb in (
            ("Starts on", self.start_choice, self.start_thumb),
            ("Ends on", self.end_choice, self.end_thumb),
        ):
            column = QVBoxLayout()
            column.addWidget(QLabel(label))
            column.addWidget(combo)
            column.addWidget(thumb)
            frames.addLayout(column, 1)
        self.frames_host = QWidget()
        self.frames_host.setLayout(frames)
        self.frames_note = QLabel("")
        self.frames_note.setObjectName("hintLabel")
        self.frames_note.setWordWrap(True)

        self.direction = QPlainTextEdit()
        self.direction.setPlaceholderText(DIRECTION_HINT)
        self.direction.setFixedHeight(76)
        self.write_button = QPushButton("Write prompt")
        self.write_button.clicked.connect(self._write)
        self.writer = ModelField(self.session.config.image_prompt_model or "", self._browse)
        self.writer.setPlaceholderText(self.session.model or "the story's model")
        if self.session.in_private:
            self.writer.setText(self.session.open_span.model)
            self.writer.setReadOnly(True)
        elif self.session.tee_chat:
            self.writer.setText(self.session.model)
            self.writer.setReadOnly(True)
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
        self.prompt.setMinimumHeight(140)
        self.prompt.textChanged.connect(self._sync)

        self.price = QLabel("")
        self.price.setObjectName("priceLabel")
        self.price.setWordWrap(True)
        self.accept_cost = QCheckBox()
        self.accept_cost.toggled.connect(self._on_accepted)
        # The price the author ticked for: a change of price clears the tick.
        self._accepted_price: tuple[float | None, str] | None = None

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.generate_button = QPushButton("Generate video")
        self.generate_button.setObjectName("sendButton")
        self.generate_button.setAutoDefault(False)
        self.buttons.addButton(self.generate_button, QDialogButtonBox.AcceptRole)
        self.generate_button.clicked.connect(self._approve)
        self.buttons.rejected.connect(self.reject)

        direction_row = QHBoxLayout()
        direction_row.addWidget(self.direction, 1)
        direction_row.addWidget(self.write_button, 0, Qt.AlignTop)
        form = QFormLayout()
        form.addRow("Model", self.model)
        form.addRow("", self.model_note)
        form.addRow("Settings", self.settings_host)
        form.addRow("Frames", self.frames_host)
        form.addRow("", self.frames_note)
        form.addRow("Direction", direction_row)
        form.addRow("Prompt written by", self.writer)
        form.addRow("", self.busy)
        form.addRow("", self.status)
        form.addRow("Prompt", self.prompt)

        layout = QVBoxLayout(self)
        layout.addWidget(self.cost_banner)
        layout.addWidget(self.shared_warning)
        layout.addWidget(self.private_warning)
        layout.addLayout(form)
        layout.addWidget(self.price)
        layout.addWidget(self.accept_cost)
        layout.addWidget(self.buttons)

    def _prefill(self, video: GeneratedVideo) -> None:
        open_span = self.session.open_span
        if video.private_span is None or (
            open_span is not None and open_span.id == video.private_span
        ):
            self.direction.setPlainText(video.direction)
        self.prompt.setPlainText(video.prompt)
        self._apply_settings(video.settings)
        for use, combo in (
            (video.start_frame, self.start_choice),
            (video.end_frame, self.end_choice),
        ):
            if use is not None:
                self._add_frame_choice(use)
                self._select_frame(combo, use)

    # --- the model and its settings ----------------------------------------------

    def _models(self) -> list[VideoModelInfo]:
        return self.catalog.models if self.catalog is not None else []

    def _info(self) -> VideoModelInfo | None:
        wanted = self.model.text().strip()
        return next((info for info in self._models() if info.id == wanted), None)

    def _browse_models(self, current: str) -> str | None:
        return pick_video_model(self.catalog, current, self)

    def _on_listing(self) -> None:
        self._on_model_changed()

    def _on_model_changed(self, *_args) -> None:
        info = self._info()
        chosen = self.settings() or dict(self.session.config.video_params)
        while self.settings_form.rowCount():
            self.settings_form.removeRow(0)
        self._controls = {}
        if info is None:
            loading = self.catalog is not None and self.catalog.loading
            self.model_note.setText(
                "Loading the endpoint's video models…"
                if loading
                else "This model isn't in the endpoint's list (Browse… shows what is). Its "
                "settings and price can't be known, so it can't be sent from here."
            )
            self._sync_frames()
            self._sync()
            return
        notes = [info.description] if info.description else []
        if info.nsfw:
            notes.append("The service lists this model as allowing adult content.")
        self.model_note.setText(" ".join(notes)[:400])
        for param in info.params:
            control = self._control(param)
            self._controls[param.key] = control
            label = param.label
            self.settings_form.addRow(label, control)
            if param.description:
                control.setToolTip(param.description)
        self._apply_settings({**info.defaults(), **chosen})
        self._sync_frames()
        self._sync()

    def _control(self, param: VideoParam) -> QWidget:
        if param.kind == "select":
            combo = QComboBox()
            for value, label in param.options:
                combo.addItem(label, value)
            combo.currentIndexChanged.connect(self._sync)
            return combo
        if param.kind == "switch":
            box = QCheckBox()
            box.toggled.connect(self._sync)
            return box
        if param.kind == "number":
            spin = QDoubleSpinBox()
            spin.setDecimals(0 if param.key == "seed" else 2)
            spin.setRange(
                param.minimum if param.minimum is not None else -1,
                param.maximum if param.maximum is not None else 2_147_483_647,
            )
            spin.setSpecialValueText("—")
            spin.setValue(spin.minimum())
            spin.valueChanged.connect(self._sync)
            return spin
        edit = QLineEdit()
        edit.textChanged.connect(self._sync)
        return edit

    def _apply_settings(self, values: dict[str, Any]) -> None:
        for key, control in self._controls.items():
            if key not in values or values[key] is None:
                continue
            value = values[key]
            control.blockSignals(True)
            if isinstance(control, QComboBox):
                index = control.findData(str(value))
                if index >= 0:
                    control.setCurrentIndex(index)
            elif isinstance(control, QCheckBox):
                control.setChecked(bool(value))
            elif isinstance(control, QDoubleSpinBox):
                try:
                    control.setValue(float(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(control, QLineEdit):
                control.setText(str(value))
            control.blockSignals(False)

    def settings(self) -> dict[str, Any]:
        """What the controls say, by the model's own names."""
        values: dict[str, Any] = {}
        for key, control in self._controls.items():
            if isinstance(control, QComboBox):
                values[key] = control.currentData()
            elif isinstance(control, QCheckBox):
                values[key] = control.isChecked()
            elif isinstance(control, QDoubleSpinBox):
                if control.value() != control.minimum():
                    number = control.value()
                    values[key] = int(number) if control.decimals() == 0 else number
            elif isinstance(control, QLineEdit) and control.text().strip():
                values[key] = control.text()
        return values

    def sent_settings(self) -> dict[str, Any]:
        info = self._info()
        return info.clean(self.settings()) if info is not None else {}

    # --- frames ------------------------------------------------------------------

    def _fill_frames(self) -> None:
        for combo in (self.start_choice, self.end_choice):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(NO_FRAME, None)
            for use in self._frames:
                caption = f" — {use.caption}" if use.caption else ""
                combo.addItem(f"{use.owner_name}{caption}", use)
            combo.addItem(ADD_FROM_DISK, "disk")
            combo.setCurrentIndex(0)
            if isinstance(current, RefUse):
                self._select_frame(combo, current)
            combo.blockSignals(False)
        self._show_thumb(self.start_choice, self.start_thumb)
        self._show_thumb(self.end_choice, self.end_thumb)

    def _add_frame_choice(self, use: RefUse) -> None:
        if all(existing.file != use.file for existing in self._frames):
            self._frames.append(use)
            self._fill_frames()

    def _select_frame(self, combo: QComboBox, use: RefUse) -> None:
        for index in range(combo.count()):
            data = combo.itemData(index)
            if isinstance(data, RefUse) and data.file == use.file:
                combo.setCurrentIndex(index)
                return

    def _on_frame_chosen(self, combo: QComboBox) -> None:
        if combo.currentData() == "disk":
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
            self._add_from_disk(combo)
        self._show_thumb(self.start_choice, self.start_thumb)
        self._show_thumb(self.end_choice, self.end_thumb)
        self._sync()

    def _add_from_disk(self, combo: QComboBox) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "A picture for the video", str(Path.home()), PICTURE_FILTER
        )
        if not path:
            return
        session = self.session
        try:
            ref = import_reference(Path(path), session.pictures)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Couldn't add the picture", str(exc))
            return
        session.story.reference_images.append(ref)
        session.save()
        use = RefUse(
            owner_kind="story",
            owner_id=session.story.id,
            owner_name=ref.caption or "Added picture",
            ref_id=ref.id,
            file=ref.file,
            caption=ref.caption,
        )
        self._add_frame_choice(use)
        self._select_frame(combo, use)

    def _show_thumb(self, combo: QComboBox, thumb: QLabel) -> None:
        use = combo.currentData()
        if isinstance(use, RefUse):
            thumb.setPixmap(thumbnail_of(self.session.pictures.read(use.file)))
        else:
            thumb.clear()

    def frame(self, combo: QComboBox) -> RefUse | None:
        data = combo.currentData()
        return data if isinstance(data, RefUse) and combo.isEnabled() else None

    def _sync_frames(self) -> None:
        info = self._info()
        start = info is not None and info.start_frame
        end = info is not None and info.end_frame
        self.start_choice.setEnabled(start)
        self.end_choice.setEnabled(end)
        if info is None:
            self.frames_note.setText("")
        elif not start:
            self.frames_note.setText("This model makes video from the prompt alone.")
        elif not end:
            self.frames_note.setText(
                "The video starts on the picture chosen (its first frame is that picture). "
                "This model takes no end frame."
            )
        else:
            self.frames_note.setText(
                "The video starts on the first picture and ends on the second, if chosen."
            )
        if info is not None and not info.text_to_video and self.frame(self.start_choice) is None:
            self.frames_note.setText(
                self.frames_note.text() + " It needs a start frame: choose one."
            )

    # --- the price ---------------------------------------------------------------

    def quote(self) -> VideoQuote:
        info = self._info()
        if info is None:
            return VideoQuote(None, "the model isn't in the endpoint's list")
        settings = self.sent_settings()
        if info.api == "wavespeed":
            key = repr((info.id, sorted(settings.items())))
            if self._remote_quote is not None and self._remote_quote[0] == key:
                return VideoQuote(self._remote_quote[1], self._remote_quote[2])
            if self.catalog is not None:
                self._asked_quote = key
                self.catalog.price(info.id, settings)
            return VideoQuote(None, "asking WaveSpeed for its price…")
        return quote_video(info, settings, start_frame=self.frame(self.start_choice) is not None)

    def _on_priced(self, model_id: str, amount, basis: str) -> None:
        key = getattr(self, "_asked_quote", None)
        if key is not None:
            self._remote_quote = (key, amount, basis)
            self._sync()

    # --- writing the prompt ------------------------------------------------------

    def writer_model(self) -> str | None:
        if self.session.in_private or self.session.tee_chat:
            return None
        return self.writer.text().strip() or None

    def _write(self) -> None:
        if self._worker is not None:
            return
        info = self._info()
        settings = self.sent_settings()
        audio_key = info.audio_key if info is not None else None
        audio = bool(settings.get(audio_key)) if audio_key else None
        session = self.session
        direction = self.direction.toPlainText().strip()
        anchor = self.anchor_id
        start, end = self.frame(self.start_choice), self.frame(self.end_choice)
        model = self.writer_model()
        duration = settings.get("duration") or settings.get("seconds")
        self._written, self._failure = None, None

        def job():
            self._written = session.write_video_prompt(
                direction,
                anchor_id=anchor,
                duration=duration,
                audio=audio,
                start=start,
                end=end,
                model=model,
            )
            return
            yield

        provider = session.private_provider if session.in_private else session.provider
        worker = GenerationWorker(job, provider=provider)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.failed.connect(self._on_write_failed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_written)
        self._worker, self._thread = worker, thread
        self._set_running(True)
        name = model or (session.open_span.model if session.in_private else session.model)
        self._show_status(f"Writing a video prompt with {name}…")
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
        if self._written is None:
            self._show_status("Stopped before a prompt was written.")
            return
        self.prompt.setPlainText(self._written)
        self._show_status(
            "Check the prompt and the settings, change anything, then Generate. If you change "
            "the length, sound or frames, Write prompt again: the prompt is written for them."
        )

    def _set_running(self, running: bool) -> None:
        self.busy.setVisible(running)
        self.write_button.setEnabled(not running)
        self.direction.setReadOnly(running)
        self.prompt.setReadOnly(running)
        self._sync()

    def _show_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.show()

    # --- checks and approval -----------------------------------------------------

    def _problems(self) -> list[str]:
        info = self._info()
        if info is None:
            return ["Choose a model from the endpoint's list."]
        problems = []
        if not info.text_to_video and self.frame(self.start_choice) is None:
            problems.append(f"{info.name} needs a start frame.")
        if not self.prompt.toPlainText().strip():
            problems.append("There is no prompt yet.")
        return problems

    def _sync(self, *_args) -> None:
        quote = self.quote()
        info = self._info()
        if quote.amount is not None:
            worth = pictures_worth(quote.amount)
            self.cost_banner.setText(
                f"This video will cost about ${quote.amount:.2f}, as much as ~{worth} "
                "pictures. Video is far more expensive than pictures, and it is charged when "
                "it is sent, even if you stop waiting or delete it."
            )
            self.price.setText(f"<b>{cost_words(quote)}</b> — {quote.basis}.")
            self.accept_cost.setText(f"I accept that this video costs about ${quote.amount:.2f}")
            self.generate_button.setText(f"Generate video (${quote.amount:.2f})")
        else:
            self.cost_banner.setText(
                "Video is far more expensive than pictures, and it is charged when it is sent, "
                "even if you stop waiting or delete it. The price of this one couldn't be "
                "worked out before sending."
            )
            self.price.setText(f"<b>Price unknown</b> — {quote.basis}.")
            self.accept_cost.setText("I accept sending this without knowing what it will cost")
            self.generate_button.setText("Generate video (price unknown)")
        if self.accept_cost.isChecked() and self._accepted_price != (quote.amount, quote.basis):
            # Ticked for another price: tick it again for this one.
            self.accept_cost.blockSignals(True)
            self.accept_cost.setChecked(False)
            self.accept_cost.blockSignals(False)
            self._accepted_price = None
        problems = self._problems()
        running = self._worker is not None
        self.accept_cost.setEnabled(info is not None)
        self.generate_button.setEnabled(
            not running and not problems and self.accept_cost.isChecked()
        )
        self.generate_button.setToolTip("\n".join(problems))

    def _on_accepted(self, checked: bool) -> None:
        quote = self.quote()
        self._accepted_price = (quote.amount, quote.basis) if checked else None
        self._sync()

    def _approve(self) -> None:
        info = self._info()
        text = self.prompt.toPlainText()
        if info is None or self._problems() or not self.accept_cost.isChecked():
            return
        if self._shared_key:
            answer = QMessageBox.warning(
                self,
                "The video key is shared",
                "This video goes on the same key as your chat or pictures, so nothing at the "
                "service caps what video spends on it. Send it anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        in_private = self.session.in_private
        self.request = VideoRequest(
            story_id=self.session.story.id,
            model=info,
            # Exactly what is on screen.
            prompt=text,
            settings=self.sent_settings(),
            anchor_node_id=self.anchor_id,
            start_frame=self.frame(self.start_choice),
            end_frame=self.frame(self.end_choice),
            direction=self.direction.toPlainText().strip(),
            quote=self.quote().amount,
            private_span=self.session.open_span.id if in_private else None,
            direction_kept=not (in_private and self.session.open_span.keep == "memory"),
        )
        self.accept()

    def reject(self) -> None:
        if self._worker is not None:
            self._closing = True
            self._worker.stop()
            self._show_status("Stopping…")
            return
        super().reject()

    def done(self, result: int) -> None:
        if self.catalog is not None:
            for signal, slot in (
                (self.catalog.changed, self._on_listing),
                (self.catalog.priced, self._on_priced),
            ):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        super().done(result)


# --- choosing a video model --------------------------------------------------------------

COLUMNS = ("Model", "Name", "Frames", "Sound", "Seconds", "5 s at its default")
FRAMES, SOUND, SECONDS, PRICE = 2, 3, 4, 5


def _frames_words(info: VideoModelInfo) -> str:
    if info.start_frame and info.end_frame:
        return "start, end"
    if info.start_frame:
        return "start" if info.text_to_video else "start (needed)"
    return "none"


def _seconds_words(info: VideoModelInfo) -> str:
    param = info.param("duration")
    values = [v for v, _ in param.options] if param is not None else []
    if not values:
        return ""
    return values[0] if len(values) == 1 else f"{values[0]}–{values[-1]}"


def sample_quote(info: VideoModelInfo) -> VideoQuote:
    settings = info.clean({"duration": "5"})
    return quote_video(info, settings)


class VideoPickerDialog(QDialog):
    def __init__(
        self, catalog: VideoCatalog, *, current: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose a video model")
        self.resize(1040, 560)
        self.catalog = catalog
        self.current = current.strip()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search — e.g. “seedance”, “1080p”, “audio”")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.table = QTreeWidget()
        self.table.setObjectName("modelTable")
        self.table.setColumnCount(len(COLUMNS))
        self.table.setHeaderLabels(list(COLUMNS))
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(True)
        header = self.table.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.resizeSection(0, 320)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in (FRAMES, SOUND, SECONDS, PRICE):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.itemDoubleClicked.connect(lambda *_: self._accept())
        self.table.currentItemChanged.connect(self._sync)
        self.detail = QLabel()
        self.detail.setObjectName("hintLabel")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.PlainText)
        self.status = QLabel()
        self.status.setObjectName("hintLabel")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(lambda: self.catalog.ensure(force=True))
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Use this model")
        self.buttons.addButton(self.refresh_button, QDialogButtonBox.ResetRole)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.detail)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)
        self.catalog.changed.connect(self._populate)
        self._populate()
        self.catalog.ensure()
        self.search.setFocus()

    def done(self, result: int) -> None:
        try:
            self.catalog.changed.disconnect(self._populate)
        except (RuntimeError, TypeError):
            pass
        super().done(result)

    def _models(self) -> dict[str, VideoModelInfo]:
        return {info.id: info for info in self.catalog.models}

    def _populate(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clear()
        for info in self._models().values():
            quote = sample_quote(info)
            row = _Row(
                [
                    info.id,
                    info.name,
                    _frames_words(info),
                    "yes" if info.audio_key else "",
                    _seconds_words(info),
                    f"${quote.amount:.2f}" if quote.amount is not None else "unknown",
                ]
            )
            row.setData(0, Qt.UserRole + 1, info.id)
            row.setData(PRICE, Qt.UserRole, quote.amount if quote.amount is not None else 1e9)
            row.setTextAlignment(PRICE, Qt.AlignRight | Qt.AlignVCenter)
            for column in range(len(COLUMNS)):
                row.setToolTip(column, info.description or info.name)
            self.table.addTopLevelItem(row)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.refresh_button.setEnabled(not self.catalog.loading)
        self._filter()
        for index in range(self.table.topLevelItemCount()):
            row = self.table.topLevelItem(index)
            if row.data(0, Qt.UserRole + 1) == self.current:
                self.table.setCurrentItem(row)
                self.table.scrollToItem(row, QAbstractItemView.PositionAtCenter)

    def _filter(self) -> None:
        models = self._models()
        words = self.search.text().lower().split()
        shown = 0
        for index in range(self.table.topLevelItemCount()):
            row = self.table.topLevelItem(index)
            info = models.get(row.data(0, Qt.UserRole + 1))
            haystack = ""
            if info is not None:
                options = " ".join(v for p in info.params for v, _ in p.options)
                sound = "audio sound" if info.audio_key else ""
                haystack = f"{info.id} {info.name} {info.description} {options} {sound}".lower()
            hidden = info is None or not all(word in haystack for word in words)
            row.setHidden(hidden)
            shown += not hidden
        total = len(models)
        if self.catalog.loading:
            self.status.setText("Loading the endpoint's video models…")
        elif not total:
            self.status.setText("No video models listed yet: Refresh fetches them.")
        else:
            self.status.setText(
                f"{total} video models" if shown == total else f"{shown} of {total} video models"
            )
        self._sync()

    def _sync(self, *_args) -> None:
        chosen = self.selected_model()
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(chosen is not None)
        info = self._models().get(chosen or "")
        if info is None:
            self.detail.setText("")
            return
        quote = sample_quote(info)
        price = (
            f"5 seconds at its default settings: ${quote.amount:.2f} ({quote.basis})."
            if quote.amount is not None
            else f"Price unknown: {quote.basis}."
        )
        self.detail.setText(f"{info.description or info.name}\n{price}")

    def selected_model(self) -> str | None:
        item = self.table.currentItem()
        if item is None or item.isHidden():
            return None
        return item.data(0, Qt.UserRole + 1)

    def _accept(self) -> None:
        if self.selected_model() is not None:
            self.accept()


def pick_video_model(catalog: VideoCatalog, current: str, parent: QWidget | None) -> str | None:
    dialog = VideoPickerDialog(catalog, current=current, parent=parent)
    try:
        return dialog.selected_model() if dialog.exec() == QDialog.Accepted else None
    finally:
        dialog.deleteLater()
