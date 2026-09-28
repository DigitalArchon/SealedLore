"""New story from a premise: the model drafts the whole setup in one call.

The dialog runs its own worker, because there is no story yet for the
window's to belong to. It only drafts; the window saves the story, opens it
in Setup for the author to read, and then offers to start it.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.authoring import parse_generated
from sealedlore.engine.drafting import StoryDraft
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.gui.model_picker import Browse, ModelField
from sealedlore.gui.worker import GenerationWorker
from sealedlore.providers.base import ChatProvider

PREMISE_EXAMPLE = (
    "e.g. Four years after the dead rose. I play Jane, an ex-army medic who walks "
    "out of the snow into a small town that doesn't trust strangers. "
    "Bleak, tense, with room for dark humour."
)


class GenerateDialog(QDialog):
    def __init__(
        self,
        provider: ChatProvider,
        *,
        model: str,
        default_model: str,
        browse: Browse | None = None,
        texts: PromptTexts = DEFAULT_TEXTS,
        parent: QWidget | None = None,
        route_for: Callable[[str], dict] | None = None,
    ) -> None:
        """`route_for`: the authoring route for the model drafting."""
        super().__init__(parent)
        self.setWindowTitle("New story from a premise")
        self._route_for = route_for or (lambda _model: {})
        # The author's edits for every story: a draft has no story of its own yet.
        self.texts = texts
        self.setMinimumWidth(620)
        self.provider = provider
        self.default_model = default_model
        self.draft: StoryDraft | None = None
        self.warnings: list[str] = []
        self._thread: QThread | None = None
        self._worker: GenerationWorker | None = None
        self._received = 0
        self._failure: str | None = None
        self._closing = False

        self.premise = QPlainTextEdit()
        self.premise.setPlaceholderText(PREMISE_EXAMPLE)
        self.premise.setMinimumHeight(180)

        self.model = ModelField(model, browse)
        self.model.setPlaceholderText(default_model or "the story's model")
        model_hint = QLabel(
            "Blank uses your chat model. A stronger one (e.g. anthropic/claude-opus-5) "
            "drafts a richer setup; the story is still played with your chat model."
        )
        model_hint.setObjectName("hintLabel")
        model_hint.setWordWrap(True)

        intro = QLabel(
            "Describe the story you want: the setting, who you'll play, the tone. The model "
            "drafts the world, cast, lore, opening and style in one go. You can read and "
            "edit it all in Setup before you start, or use Story → Review settings… to "
            "have it changed."
        )
        intro.setWordWrap(True)

        # Indeterminate: a draft's length isn't known until it's written.
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setTextVisible(False)
        self.busy_bar.setMaximumHeight(6)
        self.busy_bar.hide()

        self.progress = QLabel()
        # Carries the endpoint's own error text; never render it as HTML.
        self.progress.setTextFormat(Qt.PlainText)
        self.progress.setObjectName("hintLabel")
        self.progress.setWordWrap(True)
        self.progress.hide()

        self.reply_view = QPlainTextEdit()
        self.reply_view.setReadOnly(True)
        self.reply_view.setObjectName("inspectorPayload")
        self.reply_view.setMaximumHeight(160)
        self.reply_view.hide()

        # Chosen before the draft, so the opening is written in them: a
        # second-person story drafted with a third-person opening is how the
        # author found the storyteller ignoring its person (Sept 2026).
        self.person = QComboBox()
        for label, value in (
            ("From the premise", None),
            ("First (I)", "first"),
            ("Second (you)", "second"),
            ("Third (he, she, they)", "third"),
        ):
            self.person.addItem(label, value)
        self.tense = QComboBox()
        for label, value in (("From the premise", None), ("Past", "past"), ("Present", "present")):
            self.tense.addItem(label, value)
        voice_hint = QLabel(
            "The opening is written in these, and they stay for the whole story once it "
            "begins. You can still change them in Setup before you start."
        )
        voice_hint.setObjectName("hintLabel")
        voice_hint.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Person", self.person)
        form.addRow("Tense", self.tense)
        form.addRow("", voice_hint)
        form.addRow("Model", self.model)
        form.addRow("", model_hint)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.generate_button = QPushButton("Draft the story")
        self.generate_button.setObjectName("sendButton")
        self.buttons.addButton(self.generate_button, QDialogButtonBox.ActionRole)
        self.generate_button.clicked.connect(self._generate)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.premise, 1)
        layout.addLayout(form)
        layout.addWidget(self.busy_bar)
        layout.addWidget(self.progress)
        layout.addWidget(self.reply_view)
        layout.addWidget(self.buttons)

    def model_text(self) -> str:
        return self.model.text().strip()

    def _generate(self) -> None:
        premise = self.premise.toPlainText()
        if not premise.strip():
            self._show_progress("Write a premise first.")
            return
        model = self.model_text() or self.default_model
        self.draft = StoryDraft(
            self.provider,
            premise,
            model,
            person=self.person.currentData(),
            tense=self.tense.currentData(),
            texts=self.texts,
            route=self._route_for(model),
        )
        self._received = 0
        self._failure = None
        self.reply_view.hide()
        self._set_running(True)
        self._show_progress(f"Drafting with {self.draft.model}… this takes a minute or two.")

        worker = GenerationWorker(self.draft.stream, provider=self.provider)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.text_delta.connect(self._on_delta)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        # Cleanup on the thread, never the worker's own finished (CONTRIBUTING.md).
        thread.finished.connect(self._on_finished)
        self._worker, self._thread = worker, thread
        thread.start()

    def _set_running(self, running: bool) -> None:
        self.busy_bar.setVisible(running)
        self.premise.setReadOnly(running)
        self.person.setEnabled(not running)
        self.tense.setEnabled(not running)
        self.model.setReadOnly(running)
        self.generate_button.setEnabled(not running)

    def _show_progress(self, text: str) -> None:
        self.progress.setText(text)
        self.progress.show()

    def _on_delta(self, text: str) -> None:
        self._received += len(text)
        # About six characters to a word, JSON punctuation included.
        self._show_progress(f"Drafting… about {self._received // 6:,} words so far.")

    def _on_failed(self, message: str) -> None:
        self._failure = message

    def _on_finished(self) -> None:
        self._worker, self._thread = None, None
        self._set_running(False)
        if self._closing:
            super().reject()
            return
        if self._failure is not None:
            self._show_progress(f"The call failed: {self._failure}")
            return
        if self.draft is None or not self.draft.text.strip():
            self._show_progress("Stopped before anything was drafted.")
            return
        try:
            _, self.warnings = parse_generated(self.draft.text)
        except ValueError as exc:
            completed = self.draft.completed
            if completed is not None and completed.finish_reason == "length":
                reason = "it ran past the length limit and was cut off"
            else:
                reason = str(exc)
            self._show_progress(
                f"The draft couldn't be used: {reason}. Its reply is below; drafting "
                "again usually works."
            )
            self.reply_view.setPlainText(self.draft.text)
            self.reply_view.show()
            return
        self.accept()

    def reject(self) -> None:
        if self._worker is not None:
            # Closing mid-draft aborts the call and creates no story. The stream
            # only notices at its next chunk, so close once the thread is done
            # rather than block here or destroy a running thread.
            self._closing = True
            self._worker.stop()
            self._show_progress("Stopping…")
            return
        super().reject()
