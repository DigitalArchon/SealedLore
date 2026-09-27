"""Story → Review settings…: say what isn't working, and choose which proposed changes to apply.

Two small dialogs around one call. The first takes the complaint; the window
runs the review on its worker like any other job; the second shows each
proposed change with what it replaces and why, and applies only the ones
ticked. Nothing changes until then — and what was applied can be undone from
the Story menu, and the review itself reopened, until the next one.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Sequence
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.authoring import (
    EVIDENCE_REPLIES,
    REVIEW_MAX_TOKENS,
    current_value,
    describe,
    format_value,
    review_markdown,
)
from sealedlore.engine.session import ReviewSizes
from sealedlore.gui.model_picker import Browse, ModelField
from sealedlore.gui.winprivacy import copy_text
from sealedlore.models.authoring import SettingsChange, SettingsReview
from sealedlore.models.config import ModelPrice
from sealedlore.storage.repository import StoryBundle

COMPLAINT_EXAMPLE = (
    "e.g. The replies focus too much on action and not enough on dialogue, and I want to "
    "know what other characters are thinking and saying even when they're off scene."
)
# Past this length a before/after pair is shown as a diff, not two walls of text.
LONG_TEXT = 160
# Lines of context either side of a change in the diff view: enough to see
# what a dropped paragraph sat between.
DIFF_CONTEXT = 3
# For the cost estimate: a review's answer is usually a few thousand tokens,
# however much room it is given.
TYPICAL_ANSWER_TOKENS = 4_000
PER_MILLION = 1_000_000


class ReviewRequestDialog(QDialog):
    def __init__(
        self,
        *,
        sizes: ReviewSizes,
        model: str,
        default_model: str,
        context_for: Callable[[str], int | None] | None = None,
        price_for: Callable[[str], ModelPrice | None] | None = None,
        output_limit_for: Callable[[str], int | None] | None = None,
        browse: Browse | None = None,
        last_complaint: str = "",
        clear_marks: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review settings")
        self.setMinimumWidth(560)
        self._sizes = sizes
        self._default_model = default_model
        self._context_for = context_for
        self._price_for = price_for
        self._output_limit_for = output_limit_for
        self._marked = len(sizes.marked)

        intro = QLabel(
            "Say what you'd like to be different. The model reviews this story's settings — "
            "world, style, cast and supporting characters, lore, outcome mode, world "
            "activity, the models, generation and budget — and the instructions the "
            "storyteller is actually given, then proposes changes. You choose which to "
            "apply; the story so far is never changed."
        )
        intro.setWordWrap(True)

        self.complaint = QPlainTextEdit()
        self.complaint.setPlaceholderText(COMPLAINT_EXAMPLE)
        self.complaint.setMinimumHeight(120)
        if last_complaint:
            # The last complaint, ready to send again or refine: a second
            # review is usually a second go at the same problem.
            self.complaint.setPlainText(last_complaint)
            self.complaint.selectAll()

        available = len(sizes.exchanges)
        self.replies = QSpinBox()
        self.replies.setRange(0, available)
        self.replies.setValue(min(EVIDENCE_REPLIES, available))
        self.replies.setSpecialValueText("None")
        self.replies.setSuffix(f" of {available}")
        self.replies.setEnabled(available > 0)
        self.replies.setToolTip(
            "The most recent replies, each with the turn of yours it answered. Lets the "
            "reviewer see the problem, not just hear about it."
        )
        all_replies = QPushButton("All")
        all_replies.setEnabled(available > 0)
        all_replies.clicked.connect(lambda: self.replies.setValue(available))
        replies_row = QHBoxLayout()
        replies_row.setContentsMargins(0, 0, 0, 0)
        replies_row.addWidget(self.replies, 1)
        replies_row.addWidget(all_replies)

        # Passages the author pointed at from the transcript ("Use as evidence
        # in a review"): sent whatever their age.
        self.marked = QLabel()
        self.marked.setObjectName("hintLabel")
        self.marked.setWordWrap(True)
        clear = QPushButton("Clear")
        clear.setToolTip("Forget the marked passages")

        def clear_marked() -> None:
            self._marked = 0
            self._sizes = replace(sizes, marked=())
            if clear_marks is not None:
                clear_marks()
            self._show_marked()
            self._show_size()

        clear.clicked.connect(clear_marked)
        self.clear_marks_button = clear
        marked_row = QHBoxLayout()
        marked_row.setContentsMargins(0, 0, 0, 0)
        marked_row.addWidget(self.marked, 1)
        marked_row.addWidget(clear)
        self._show_marked()

        self.whole_prompt = QCheckBox("Send the whole prompt the storyteller receives")
        self.whole_prompt.setToolTip(
            "Everything the storyteller is sent on its next turn — system prompt, chapter "
            "summaries, the recent story and the per-turn instructions — instead of the "
            "system prompt and per-turn instructions alone. Lets the reviewer read the "
            "story as the storyteller sees it, summaries included."
        )

        self.side_texts = QCheckBox("Let it change the side calls' prompts too")
        self.side_texts.setToolTip(
            "The review can always propose changes to the storyteller's own texts "
            "(File → Prompts (advanced)…). This adds the rest: the scene read, the plot's "
            "director and clock, chapters, lore choice, private scenes, pictures and "
            "questions. Long, and seldom the cause."
        )

        self.model = ModelField(model, browse)
        self.model.setPlaceholderText(default_model or "the story's model")

        self.size_label = QLabel()
        self.size_label.setObjectName("hintLabel")
        self.size_label.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Exchanges to include", replies_row)
        form.addRow("Marked passages", marked_row)
        form.addRow("", self.whole_prompt)
        form.addRow("", self.side_texts)
        form.addRow("Model", self.model)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Review")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.complaint, 1)
        layout.addLayout(form)
        layout.addWidget(self.size_label)
        layout.addWidget(buttons)

        self.replies.valueChanged.connect(self._show_size)
        self.whole_prompt.toggled.connect(self._show_size)
        self.side_texts.toggled.connect(self._show_size)
        self.model.textChanged.connect(self._show_size)
        self._show_size()

    def complaint_text(self) -> str:
        return self.complaint.toPlainText().strip()

    def model_text(self) -> str:
        return self.model.text().strip()

    def reply_count(self) -> int:
        return self.replies.value()

    def include_whole_prompt(self) -> bool:
        return self.whole_prompt.isChecked()

    def include_side_texts(self) -> bool:
        return self.side_texts.isChecked()

    def _show_marked(self) -> None:
        count = self._marked
        if count:
            self.marked.setText(
                f"{count} passage{'s' if count != 1 else ''} you marked in the transcript, "
                "sent as evidence whatever their age."
            )
        else:
            self.marked.setText(
                "None. Mark a passage from its ⋯ menu (Use as evidence in a review) to "
                "point the reviewer at it."
            )
        self.clear_marks_button.setEnabled(count > 0)

    def _show_size(self) -> None:
        tokens = self._sizes.total(
            self.reply_count(), self.include_whole_prompt(), self.include_side_texts()
        )
        text = f"About {tokens:,} tokens to send."
        if self.include_whole_prompt() and self._sizes.in_prompt and self.reply_count():
            carried = min(self._sizes.in_prompt, self.reply_count())
            text += f" The {carried} most recent are already in the prompt, so aren't sent twice."
        model = self.model_text() or self._default_model
        price = self._price_for(model) if self._price_for and model else None
        if price is not None:
            sending = tokens * price.prompt / PER_MILLION
            answer = TYPICAL_ANSWER_TOKENS * price.completion / PER_MILLION
            text += f" Roughly ${sending:.3f} to send, plus about ${answer:.3f} for the answer"
            limit_out = self._output_limit_for(model) if self._output_limit_for else None
            if limit_out:
                most = limit_out * price.completion / PER_MILLION
                text += f" (at most ${most:.2f} if it uses all {limit_out:,} tokens it may)"
            text += "."
        limit = self._context_for(model) if self._context_for and model else None
        if limit:
            text += f" {model} takes {limit:,}, with room kept for its answer."
            out = self._output_limit_for(model) if self._output_limit_for else None
            if tokens + min(REVIEW_MAX_TOKENS, out or REVIEW_MAX_TOKENS) > limit:
                text = "⚠ " + text + " Too much: the review will be refused."
        self.size_label.setText(text)

    def _accept(self) -> None:
        if self.complaint_text():
            self.accept()


def _before_after(before: object, after: object) -> QWidget:
    old, new = format_value(before), format_value(after)
    if max(len(old), len(new)) <= LONG_TEXT and "\n" not in old + new:
        label = QLabel(f"<b>Before:</b> {_escape(old)}<br><b>Proposed:</b> {_escape(new)}")
        label.setWordWrap(True)
        return label
    if before in (None, "", [], {}):
        view_text = new
    else:
        view_text = "\n".join(
            line
            for line in difflib.unified_diff(
                old.splitlines(), new.splitlines(), "now", "proposed", lineterm="", n=DIFF_CONTEXT
            )
        )
    view = QPlainTextEdit(view_text)
    view.setReadOnly(True)
    view.setObjectName("inspectorPayload")
    # As tall as the diff, within reason; a long one scrolls.
    lines = min(max(view_text.count("\n") + 1, 3), 14)
    view.setFixedHeight(lines * view.fontMetrics().lineSpacing() + 18)
    return view


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


HISTORY_NOTE = (
    "The reviewer thinks the story so far is part of the cause: the storyteller reads the "
    "recent story every turn, and it is the strongest signal it has. Settings alone may "
    "not turn it. After applying, you'll be offered a restart with the new settings and, "
    "where a changed character first appears later in the story, a branch from before that."
)


class ReviewResultDialog(QDialog):
    def __init__(
        self,
        review: SettingsReview,
        bundle: StoryBundle,
        parent: QWidget | None = None,
        *,
        complaint: str = "",
        model: str = "",
        applied_ids: Sequence[str] = (),
        read_only: bool = False,
    ) -> None:
        """`read_only` shows a past review as it was: what was applied is marked,
        and nothing can be applied again."""
        super().__init__(parent)
        self.setWindowTitle("Proposed changes" if not read_only else "Last review")
        self.setMinimumSize(680, 520)
        self._boxes: list[tuple[QCheckBox, SettingsChange]] = []
        self._read_only = read_only

        summary = QLabel(review.summary or "The reviewer gave no summary.")
        summary.setTextFormat(Qt.PlainText)
        summary.setWordWrap(True)

        body = QWidget()
        rows = QVBoxLayout(body)
        rows.setSpacing(10)
        if not review.changes:
            none = QLabel("No changes proposed.")
            none.setObjectName("hintLabel")
            rows.addWidget(none)
        for change in review.changes:
            rows.addWidget(self._row(change, bundle, applied=change.id in applied_ids))

        # The Director turn is not a setting: it goes to the composer, and the
        # author sends it or doesn't.
        self.director_box: QCheckBox | None = None
        if review.director_turn:
            frame = QFrame()
            frame.setObjectName("changeRow")
            layout = QVBoxLayout(frame)
            layout.setContentsMargins(10, 8, 10, 8)
            box = QCheckBox("Director turn — put it in the composer, ready to send")
            box.setToolTip(
                "An instruction to the storyteller about what happens from here, for "
                "when the story so far is the cause rather than a setting. Nothing is "
                "sent until you do."
            )
            box.setChecked(not read_only)
            box.setEnabled(not read_only)
            if read_only and "director" in applied_ids:
                box.setChecked(True)
                box.setText(box.text() + "  ·  sent to the composer")
            box.toggled.connect(self._sync)
            self.director_box = box
            layout.addWidget(box)
            text = QPlainTextEdit(review.director_turn)
            text.setReadOnly(True)
            text.setObjectName("inspectorPayload")
            lines = min(max(review.director_turn.count("\n") + 2, 3), 10)
            text.setFixedHeight(lines * text.fontMetrics().lineSpacing() + 18)
            layout.addWidget(text)
            rows.addWidget(frame)
        rows.addStretch(1)

        history = QLabel(HISTORY_NOTE)
        history.setObjectName("cannotFix")
        history.setWordWrap(True)
        history.setVisible(review.history_bound and not read_only)

        # Outside the scroll: often the most useful part of the answer, and
        # easy to miss below a long list of changes.
        cannot_fix = QLabel(
            "<b>Can't be fixed with settings</b><br>"
            + "<br>".join("• " + _escape(note) for note in review.cannot_fix)
        )
        cannot_fix.setObjectName("cannotFix")
        cannot_fix.setWordWrap(True)
        cannot_fix.setVisible(bool(review.cannot_fix))

        # Not the author's to act on: things in SealedLore's own fixed prompt
        # that no setting reaches. Copied out, they are a bug report.
        findings = QFrame()
        findings.setObjectName("cannotFix")
        findings_layout = QVBoxLayout(findings)
        findings_layout.setContentsMargins(0, 0, 0, 0)
        findings_text = QLabel(
            "<b>Findings about SealedLore itself</b> (fixed parts of the prompt no setting "
            "changes)<br>"
            + "<br>".join(
                "• "
                + _escape(finding.problem)
                + (f"<br>&nbsp;&nbsp;<i>{_escape(finding.where)}</i>" if finding.where else "")
                + (
                    f"<br>&nbsp;&nbsp;Suggestion: {_escape(finding.suggestion)}"
                    if finding.suggestion
                    else ""
                )
                for finding in review.app_findings
            )
        )
        findings_text.setWordWrap(True)
        findings_layout.addWidget(findings_text)
        findings.setVisible(bool(review.app_findings))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(body)

        self.cost_note = QLabel(
            "Changing the world, style, cast, world activity or model (or the lore, when "
            "it is sent whole) makes the next turn re-send the whole system prompt at full "
            "price, once. Applied changes can be undone from the Story menu until the next "
            "review, Settings save or edit."
        )
        self.cost_note.setObjectName("hintLabel")
        self.cost_note.setWordWrap(True)

        tools = QHBoxLayout()
        self.all_button = QPushButton("Tick all")
        self.all_button.clicked.connect(lambda: self._tick(True))
        self.none_button = QPushButton("Untick all")
        self.none_button.clicked.connect(lambda: self._tick(False))
        for button in (self.all_button, self.none_button):
            button.setEnabled(bool(review.changes) and not read_only)
            tools.addWidget(button)
        copy = QPushButton("Copy review")
        copy.setToolTip("Copy the whole review as Markdown: changes, what can't be fixed, findings")
        markdown = review_markdown(
            review, bundle, complaint=complaint, model=model, applied_ids=applied_ids
        )
        copy.clicked.connect(lambda: copy_text(markdown))
        tools.addWidget(copy)
        tools.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Close)
        self.apply_button = buttons.button(QDialogButtonBox.Ok)
        self.apply_button.setText("Apply selected")
        self.apply_button.setVisible(not read_only)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(summary)
        layout.addWidget(history)
        layout.addWidget(scroll, 1)
        layout.addWidget(cannot_fix)
        layout.addWidget(findings)
        layout.addWidget(self.cost_note)
        layout.addLayout(tools)
        layout.addWidget(buttons)
        self._sync()

    def _row(self, change: SettingsChange, bundle: StoryBundle, *, applied: bool) -> QWidget:
        frame = QFrame()
        frame.setObjectName("changeRow")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        label = describe(change)
        if self._read_only:
            label += "  ·  applied" if applied else "  ·  not applied"
        box = QCheckBox(label)
        # A change with a warning waits for the author to read it.
        box.setChecked(applied if self._read_only else change.warning is None)
        box.setEnabled(not self._read_only)
        box.toggled.connect(self._sync)
        self._boxes.append((box, change))
        layout.addWidget(box)
        if change.reason:
            reason = QLabel(change.reason)
            reason.setTextFormat(Qt.PlainText)
            reason.setObjectName("hintLabel")
            reason.setWordWrap(True)
            layout.addWidget(reason)
        if change.warning:
            warning = QLabel("⚠ " + change.warning[0].upper() + change.warning[1:])
            warning.setTextFormat(Qt.PlainText)
            warning.setObjectName("cannotFix")
            warning.setWordWrap(True)
            layout.addWidget(warning)
        if change.kind != "lore_disable":
            before = change.before if change.before is not None else current_value(bundle, change)
            layout.addWidget(_before_after(before, change.value))
        return frame

    def _tick(self, checked: bool) -> None:
        for box, _ in self._boxes:
            box.setChecked(checked)

    def _sync(self) -> None:
        self.apply_button.setEnabled(
            any(box.isChecked() for box, _ in self._boxes)
            or (self.director_box is not None and self.director_box.isChecked())
        )

    def selected_changes(self) -> Sequence[SettingsChange]:
        return [change for box, change in self._boxes if box.isChecked()]

    def send_director_turn(self) -> bool:
        return self.director_box is not None and self.director_box.isChecked()
