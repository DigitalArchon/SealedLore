"""The style aid: a form that writes the style block, with a live preview. See §8.2.

Everything here lands in the system block, so every change costs one cache miss
on the next turn — the panel says so rather than letting it be a surprise.

Hand-editing the preview detaches it from the form (§8.2) until "Reset to form".
Length control survives a detach: the per-turn length directive and the final
`REMEMBER:` line still follow the preset, because those live in the tail.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.authoring import FIXED_ONCE_STARTED, style_fixed
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.response_style import PRESET_ORDER, PRESETS
from sealedlore.engine.rules import (
    render_perspective_block,
    render_style_block,
    without_length_line,
)
from sealedlore.gui.fields import FormScroll, set_field_text
from sealedlore.models.story import StyleDirectives

STYLE_HEADING = "# STYLE\n\n"

# Named stops for the freeform fields. The combos stay editable, so these are
# suggestions rather than the only words allowed; blank means "not set".
DENSITY_STOPS = ["", "terse", "lean", "balanced", "rich", "florid"]
BALANCE_STOPS = ["", "mostly dialogue", "balanced", "mostly narration"]
PACING_STOPS = ["", "brisk", "measured", "slow and lingering"]
RATING_STOPS = ["", "all ages", "teen", "mature", "adult"]


def _editable_combo(stops: list[str]) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(True)
    combo.addItems(stops)
    combo.lineEdit().setPlaceholderText("not set")
    return combo


def _choice_combo(choices: list[tuple[str, str | None]]) -> QComboBox:
    combo = QComboBox()
    for label, value in choices:
        combo.addItem(label, value)
    return combo


PERSPECTIVE_SUMMARIES = {
    "whole_story": "Cutaways allowed; nobody elsewhere perceives your scene.",
    "with_character": "Only what your character perceives; no cutaways.",
}


class StylePanel(QWidget):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._style: StyleDirectives | None = None
        self._texts: PromptTexts = DEFAULT_TEXTS
        self._loading = False
        self._started = False

        # --- length ------------------------------------------------------
        self.length = QComboBox()
        for key in PRESET_ORDER:
            self.length.addItem(PRESETS[key].label, key)
        self.length_summary = QLabel()
        self.length_summary.setObjectName("hintLabel")
        self.length_summary.setWordWrap(True)
        self.custom_length = QLineEdit()
        self.custom_length.setPlaceholderText("e.g. 'two tight paragraphs, dialogue-led'")

        # --- perspective -------------------------------------------------
        self.perspective = QComboBox()
        self.perspective.addItem("Follow the whole story", "whole_story")
        self.perspective.setItemData(
            0,
            "Cutaways to other places and people are allowed. Characters elsewhere still "
            "can't perceive your scene except through a channel the story has set up.",
            Qt.ToolTipRole,
        )
        self.perspective.addItem("Stay with my character", "with_character")
        self.perspective.setItemData(
            1,
            "Only what your character can perceive. No cutaways; anyone elsewhere is off the page.",
            Qt.ToolTipRole,
        )
        self.perspective_summary = QLabel()
        self.perspective_summary.setObjectName("hintLabel")
        self.perspective_summary.setWordWrap(True)

        # --- the rest of §8.2 --------------------------------------------
        self.person = _choice_combo(
            [("not set", None), ("first", "first"), ("second", "second"), ("third", "third")]
        )
        self.tense = _choice_combo([("not set", None), ("past", "past"), ("present", "present")])
        # Fixed once the story has begun (authoring.FIXED_ONCE_STARTED): shown,
        # greyed, for reference.
        self.fixed_hint = QLabel()
        self.fixed_hint.setObjectName("hintLabel")
        self.fixed_hint.setWordWrap(True)
        self.fixed_hint.hide()
        self.density = _editable_combo(DENSITY_STOPS)
        self.balance = _editable_combo(BALANCE_STOPS)
        self.pacing = _editable_combo(PACING_STOPS)
        self.rating = _editable_combo(RATING_STOPS)
        self.limits = QLineEdit()
        self.limits.setPlaceholderText("e.g. 'no graphic gore; fade to black'")
        self.forbidden = QPlainTextEdit()
        self.forbidden.setPlaceholderText("One per line — words or clichés to avoid")
        self.forbidden.setMaximumHeight(70)
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText(
            "Anything else, in plain words — e.g. 'let the narration show what other "
            "characters are thinking'"
        )
        self.notes.setMaximumHeight(90)

        self.length.currentIndexChanged.connect(self._apply_form)
        self.perspective.currentIndexChanged.connect(self._apply_form)
        self.custom_length.textChanged.connect(self._apply_form)
        self.person.currentIndexChanged.connect(self._apply_form)
        self.tense.currentIndexChanged.connect(self._apply_form)
        for combo in (self.density, self.balance, self.pacing, self.rating):
            combo.currentTextChanged.connect(self._apply_form)
        self.limits.textChanged.connect(self._apply_form)
        self.forbidden.textChanged.connect(self._apply_form)
        self.notes.textChanged.connect(self._apply_form)

        # Length has its own host: it must stay live when the block is detached,
        # and a disabled parent would disable it along with everything else.
        length_host = QWidget()
        length_form = QFormLayout(length_host)
        length_form.setContentsMargins(0, 0, 0, 0)
        length_form.addRow("Length", self.length)
        length_form.addRow("", self.length_summary)
        length_form.addRow("Custom length", self.custom_length)
        # Perspective is its own system section, so it too survives a hand edit.
        length_form.addRow("Perspective", self.perspective)
        length_form.addRow("", self.perspective_summary)

        self.form_host = QWidget()
        form = QFormLayout(self.form_host)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Person", self.person)
        form.addRow("Tense", self.tense)
        form.addRow("", self.fixed_hint)
        form.addRow("Prose density", self.density)
        form.addRow("Dialogue / narration", self.balance)
        form.addRow("Pacing", self.pacing)
        form.addRow("Content rating", self.rating)
        form.addRow("Content limits", self.limits)
        form.addRow("Avoid", self.forbidden)
        form.addRow("Other notes", self.notes)

        # --- preview -----------------------------------------------------
        caption = QLabel("STYLE BLOCK SENT TO THE MODEL")
        caption.setObjectName("composerCaption")
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("stylePreview")
        self.preview.setReadOnly(True)
        self.preview.textChanged.connect(self._apply_detached_text)

        # Perspective is its own section of the prompt, not part of the style
        # block, so Edit text can't delete the presence rules it carries. Human
        # testing: with only the style block previewed, switching perspective
        # looked like it did nothing.
        perspective_caption = QLabel("PERSPECTIVE SECTION SENT TO THE MODEL")
        perspective_caption.setObjectName("composerCaption")
        self.perspective_preview = QPlainTextEdit()
        self.perspective_preview.setObjectName("stylePreview")
        self.perspective_preview.setReadOnly(True)
        self.perspective_preview.setMinimumHeight(150)
        perspective_note = QLabel("Set by Perspective above; not changed by Edit text.")
        perspective_note.setObjectName("hintLabel")
        perspective_note.setWordWrap(True)

        self.detach_button = QPushButton("Edit text")
        self.detach_button.setToolTip("Hand-edit the block; the form stops driving it")
        self.detach_button.clicked.connect(self._detach)
        self.reset_button = QPushButton("Reset to form")
        self.reset_button.clicked.connect(self._reattach)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.detach_button)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)

        self.detached_note = QLabel(
            "Hand-edited: the form above no longer drives this block, except its first "
            "line: the length is always the Length setting's. Perspective has its own "
            "section below."
        )
        self.detached_note.setObjectName("hintLabel")
        self.detached_note.setWordWrap(True)

        cost = QLabel(
            "Everything here is part of the cached prefix: a change costs one cache "
            "miss on the next turn."
        )
        cost.setObjectName("hintLabel")
        cost.setWordWrap(True)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)
        body_layout.addWidget(length_host)
        body_layout.addWidget(self.form_host)
        body_layout.addWidget(caption)
        body_layout.addWidget(self.preview, 1)
        body_layout.addWidget(self.detached_note)
        body_layout.addLayout(buttons)
        body_layout.addWidget(perspective_caption)
        body_layout.addWidget(self.perspective_preview)
        body_layout.addWidget(perspective_note)
        body_layout.addWidget(cost)

        scroll = FormScroll(body)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(scroll)

    # --- population -------------------------------------------------------

    def set_style(
        self,
        style: StyleDirectives,
        *,
        started: bool | None = None,
        texts: PromptTexts | None = None,
    ) -> None:
        """Show `style`; `started` (kept when None) says whether the story has
        begun, which fixes person and tense. `texts` (kept when None) are the
        story's prompt texts in force, which the previews show."""
        self._style = style
        if texts is not None:
            self._texts = texts
        if started is not None:
            self._started = started
        self._loading = True
        self._select(self.length, style.response_style)
        set_field_text(self.custom_length, style.length_target or "")
        self._select(self.perspective, style.perspective)
        self._select(self.person, style.person)
        self._select(self.tense, style.tense)
        set_field_text(self.density, style.prose_density or "")
        set_field_text(self.balance, style.dialogue_narration_balance or "")
        set_field_text(self.pacing, style.pacing or "")
        set_field_text(self.rating, style.content_rating or "")
        set_field_text(self.limits, style.content_limits or "")
        self.forbidden.setPlainText("\n".join(style.forbidden_phrases))
        self.notes.setPlainText(style.notes or "")
        self._loading = False
        self._sync()
        self._apply_fixed()

    def set_started(self, started: bool) -> None:
        """The story began (or a new one opened). Only a change re-locks, so a
        person just chosen for a story that had none can still be corrected
        until the story is next shown."""
        if started != self._started:
            self._started = started
            self._apply_fixed()

    def _apply_fixed(self) -> None:
        style = self._style
        if style is None:
            return
        fixed = [f for f in FIXED_ONCE_STARTED if style_fixed(style, f, self._started)]
        self.person.setEnabled("person" not in fixed)
        self.tense.setEnabled("tense" not in fixed)
        unset = self._started and any(getattr(style, f) is None for f in FIXED_ONCE_STARTED)
        lines = []
        if fixed:
            lines.append(
                "Person and tense were set when the story began and stay for all of it. "
                "To tell it another way, use Restart or Duplicate settings."
            )
        if unset:
            lines.append(
                "Not set yet: choose what the story is already written in. Once set, it "
                "stays for the rest of the story."
            )
        self.fixed_hint.setText(" ".join(lines))
        self.fixed_hint.setVisible(bool(lines))

    def _select(self, combo: QComboBox, value: str | None) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    # --- editing ----------------------------------------------------------

    def _apply_form(self) -> None:
        if self._loading or self._style is None:
            return
        style = self._style
        style.response_style = self.length.currentData()
        style.length_target = self.custom_length.text().strip() or None
        style.perspective = self.perspective.currentData()
        style.person = self.person.currentData()
        style.tense = self.tense.currentData()
        style.prose_density = self.density.currentText().strip() or None
        style.dialogue_narration_balance = self.balance.currentText().strip() or None
        style.pacing = self.pacing.currentText().strip() or None
        style.content_rating = self.rating.currentText().strip() or None
        style.content_limits = self.limits.text().strip() or None
        style.forbidden_phrases = [
            line.strip() for line in self.forbidden.toPlainText().splitlines() if line.strip()
        ]
        style.notes = self.notes.toPlainText().strip() or None
        self._sync()
        self.changed.emit()

    def _apply_detached_text(self) -> None:
        if self._loading or self._style is None or not self._style.detached:
            return
        self._style.custom_text = self.preview.toPlainText()
        self.changed.emit()

    def _detach(self) -> None:
        if self._style is None:
            return
        # Start from exactly what the form produced, so detaching changes nothing
        # until the author actually edits.
        # The length line stays the form's (Length above), so it isn't copied.
        self._style.custom_text = without_length_line(
            render_style_block(self._style, self._texts).removeprefix(STYLE_HEADING)
        )
        self._style.detached = True
        self._sync()
        self.preview.setFocus()
        self.changed.emit()

    def _reattach(self) -> None:
        if self._style is None:
            return
        self._style.detached = False
        self._style.custom_text = None
        self._sync()
        self.changed.emit()

    def _sync(self) -> None:
        """Bring enablement, the summary line and the preview in line with the model."""
        style = self._style
        if style is None:
            return
        detached = style.detached

        self.form_host.setEnabled(not detached)
        self.custom_length.setEnabled(style.response_style == "custom")
        self.length_summary.setText(PRESETS[style.response_style].summary)
        self.perspective_summary.setText(PERSPECTIVE_SUMMARIES[style.perspective])
        self.detached_note.setVisible(detached)
        self.detach_button.setEnabled(not detached)
        self.reset_button.setEnabled(detached)

        self._loading = True
        if detached:
            self.preview.setReadOnly(False)
            # An older hand edit may still carry its length line; it is never sent.
            text = without_length_line(style.custom_text or "")
            if self.preview.toPlainText() != text:
                self.preview.setPlainText(text)
        else:
            self.preview.setReadOnly(True)
            self.preview.setPlainText(
                render_style_block(style, self._texts).removeprefix(STYLE_HEADING)
            )
        self.perspective_preview.setPlainText(
            render_perspective_block(style.perspective, self._texts)
        )
        self._loading = False
