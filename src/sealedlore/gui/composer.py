"""The bottom bar: who you are, who speaks, who the model may voice. See §3.1, §10.

Holding a character and speaking as one are deliberately separate controls.
Live testing settled it: dropping a Director note does not hand your character
back, so the lock has to stay in force while the speaker changes.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.dice import DIFFICULTY_ORDER
from sealedlore.engine.response_style import PRESET_ORDER, PRESETS, label_for
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.models.character import Character
from sealedlore.models.node import (
    DIRECTOR_SPEAKER_ID,
    NARRATOR_SPEAKER_ID,
    AgencyMode,
    Difficulty,
    Node,
    NpcScope,
    ResponseStyle,
)
from sealedlore.models.scene import SceneState
from sealedlore.models.story import DEFAULT_RESPONSE_STYLE, WorldActivity

NOBODY = "__nobody__"
STORY_DEFAULT = "__story_default__"
# Not a speaker the engine knows: the window routes it to `StorySession.ask`.
QUESTION = "__question__"

# Human testing showed "Narrator" vs "Director" isn't self-evident: a turn that
# was plainly the held character acting went in as a first-person Director
# message. The labels say which side of the fiction the text lands on. Only the
# composer uses these; ids, prompt tags and transcript headers are unchanged.
NARRATION_LABEL = "Narration — story prose, in the fiction"
DIRECTION_LABEL = "Direction — instruction, outside the fiction"
NARRATION_TIP = (
    "You write the story's own prose. It becomes part of the story as fact, "
    "not tied to any character."
)
DIRECTION_TIP = (
    "An instruction to the model about where the story should go. It is obeyed, "
    "but it isn't prose and no character hears it."
)

# Human testing: a "why did nobody react?" sent as Direction came back as
# narration and an in-story excuse, because a turn can only ever be prose.
QUESTION_LABEL = "Question — ask the model, out of character"
# The story's own length and outcome head their lists, value first: "Story
# default — Descriptive (recommended)" was cut to "Story default — D" at
# 1366px, hiding the one word that mattered.
DEFAULT_SUFFIX = " (story default)"
QUESTION_TIP = (
    "Ask about the story, a character or the model's choices, and get a plain answer. "
    "It is shown as a question and never becomes part of the story."
)
TURN_PLACEHOLDER = "Write the next turn…  (Enter to send, Shift+Enter for a new line)"
QUESTION_PLACEHOLDER = "Ask the model about the story…  (Enter to ask)"
NO_STORY_PLACEHOLDER = "Open a story, or start one, to write here."
CHAT_PLACEHOLDER = "Write a message…  (Enter to send, Shift+Enter for a new line)"

AGENCY_LABELS: dict[AgencyMode, str] = {
    "fiat": "Fiat",
    "plausible": "Plausible",
    "contested": "Contested",
    "dice": "Dice",
}
AGENCY_TIPS: dict[AgencyMode, str] = {
    "fiat": "What your character attempts succeeds exactly as written, with no added catch.",
    "plausible": "The model may scale an implausible result down, but never to failure.",
    "contested": "The model judges success or failure against your character's competence.",
    "dice": "The app rolls d100 against your character's skill; the model narrates the result.",
}
GENERAL_SKILL = "__general__"

WORLD_ACTIVITY_LABELS: dict[WorldActivity, str] = {
    "quiet": "Quiet",
    "normal": "Normal",
    "eventful": "Eventful",
}
WORLD_ACTIVITY_TIPS: dict[WorldActivity, str] = {
    "quiet": ("The world answers you but rarely intrudes. Scenes move at the pace you set them."),
    "normal": (
        "People pursue their own aims off the page, anyone with a stake in what is happening "
        "acts on it, and earlier choices catch up on their own schedule."
    ),
    "eventful": (
        "Most passages bring something you didn't start: an arrival, news, a complication "
        "out of what the story already established."
    ),
}

SCOPE_LABELS: list[tuple[str, NpcScope]] = [
    ("All present NPCs", "all"),
    ("Only these…", "selected"),
    ("Model's choice", "model_choice"),
]


PRIVATE_TIP = (
    "Private scene: play the next part on your private model. Nothing from it reaches "
    "the story's model or any other; when you end it, the private model writes a summary "
    "for you to approve, and only that goes back into the story."
)
CHAT_PRIVATE_TIP = (
    "Private: hold the next part of the chat on your private model. Nothing from it "
    "reaches the chat's own model or any other; when you end it, the private model writes "
    "a summary for you to approve, and only that goes back into the chat."
)
CHAT_END_PRIVATE_TIP = (
    "End the private part: the private model summarises it for you to approve, or you "
    "can end it without a summary. Settings and other chats and stories are paused until "
    "it ends."
)
END_PRIVATE_TIP = (
    "End the private scene: the private model summarises it for you to approve, or you "
    "can end it without a summary. Settings, other stories and branches are paused until "
    "it ends."
)


class TurnEditor(QPlainTextEdit):
    """Enter sends, Shift+Enter starts a new line — the chat convention.

    Ctrl+Enter is left alone so the window's Send shortcut keeps working. The
    newline is inserted explicitly: Qt's own Shift+Enter inserts a Unicode line
    separator, which `toPlainText()` hands back as U+2028 rather than "\n".
    """

    submitted = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            modifiers = event.modifiers() & ~Qt.KeypadModifier
            if modifiers == Qt.NoModifier:
                self.submitted.emit()
                return
            if modifiers == Qt.ShiftModifier:
                self.textCursor().insertText("\n")
                self.ensureCursorVisible()
                return
        super().keyPressEvent(event)


class Composer(QWidget):
    send_requested = Signal()
    stop_requested = Signal()
    regenerate_requested = Signal()
    image_requested = Signal()
    # The author pressed Private scene: True to begin one, False to end it.
    private_requested = Signal(bool)
    held_character_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._cast: list[Character] = []
        self._scope_ids: set[str] = set()

        self.held = QComboBox()
        self.held.setObjectName("heldSelector")
        self.held.setMinimumWidth(150)
        self.held.currentIndexChanged.connect(self._on_held_changed)

        self.speaker = QComboBox()
        self._compact(self.speaker, 16)

        self.scope = QComboBox()
        for label, _ in SCOPE_LABELS:
            self.scope.addItem(label)
        self.scope.currentIndexChanged.connect(self._sync_scope_button)

        self.scope_button = QToolButton()
        self.scope_button.setText("none")
        self.scope_button.setPopupMode(QToolButton.InstantPopup)
        self.scope_menu = QMenu(self.scope_button)
        self.scope_button.setMenu(self.scope_menu)
        self.scope_button.hide()

        self.length = QComboBox()
        self._compact(self.length, 11)
        self.length.setToolTip(
            "Length of the passages from here on. Your choice stays until you change it "
            "back; Regenerate uses it too. The story's own default is the first entry, "
            "and is set in the Style tab."
        )
        self.set_default_length(DEFAULT_RESPONSE_STYLE)

        # Agency (§4): a one-off override, like Length. Skill and difficulty
        # only appear when this turn will actually be rolled.
        self.agency = QComboBox()
        self._compact(self.agency, 11)
        self.agency.setToolTip(
            "How the outcome of your character's actions is decided. Your choice stays "
            "until you change it back; Regenerate uses it too. The story's own default is "
            "the first entry, and is set in Story → Setup."
        )
        self._default_agency: AgencyMode = "contested"
        self.skill = QComboBox()
        self._compact(self.skill, 11)
        self.skill.setToolTip("Which of your character's skills this roll uses")
        self.difficulty = QComboBox()
        for level in DIFFICULTY_ORDER:
            self.difficulty.addItem(level, level)
        self.difficulty.setCurrentIndex(DIFFICULTY_ORDER.index("standard"))
        self._compact(self.difficulty, 8)
        self._fit_popup(self.difficulty)
        self.difficulty.setToolTip("How hard the attempt is. Resets to standard after each roll.")
        # One caption for both: a caption each pushed the window past a
        # laptop's width (1,441px) once the dice controls appeared.
        self.roll_caption = self._caption("Roll")
        # Last skill picked per character: a pilot rolls piloting turn after turn.
        self._skill_memory: dict[str, str] = {}
        self._held_id: str | None = None

        self.ooc_toggle = QPushButton("OOC")
        self.ooc_toggle.setCheckable(True)
        self.ooc_toggle.setToolTip(
            "Out-of-character addendum: authoritative fact or instruction, not dialogue. "
            "Attached to this turn; same authority as Direction."
        )
        self.ooc_toggle.toggled.connect(self._on_ooc_toggled)

        # A private scene: played on the private model, handed back as a
        # summary. Where it is kept is chosen beside it, before it begins, and
        # locked (showing the choice) while it runs.
        self.private_toggle = QPushButton("Private")
        self.private_toggle.setObjectName("privateToggle")
        self.private_toggle.setCheckable(True)
        self.private_toggle.setToolTip(PRIVATE_TIP)
        self.private_toggle.clicked.connect(self._on_private_clicked)
        self._private_offered = True
        self._keep_fixed: str | None = None
        self.private_keep = QComboBox()
        self.private_keep.addItem("In memory", "memory")
        self.private_keep.addItem("On disk", "disk")
        self.private_keep.setItemData(
            0,
            "The scene's messages are never written to disk; when the app closes they are "
            "gone, and only the approved summary stays.",
            Qt.ToolTipRole,
        )
        self.private_keep.setItemData(
            1, "The scene's messages are saved with the story, marked private.", Qt.ToolTipRole
        )
        self._compact(self.private_keep, 9)
        # Capped at its longest entry like the others: uncapped, it took every
        # spare pixel of the row and squeezed Length and Outcome short.
        self._fit_popup(self.private_keep)

        # Two rows, because one ran out of width: adding Length pushed the
        # window's minimum past 1,570px. The first row is who is in play; the
        # second is how this one turn is shaped: length, outcome mode, and the
        # dice controls when this turn will be rolled. Each wraps when the
        # window is narrower than it (a larger text size), a caption staying
        # with its control.
        who = WrapRow()
        captions = [self._caption(text) for text in ("Playing", "Speaking as", "Model voices")]
        who.add_group([captions[0], self.held])
        who.add_group([captions[1], self.speaker])
        who.add_group([captions[2], self.scope, self.scope_button])
        # What a simple chat has none of (set_chat_mode): who is in play.
        self._who_widgets = [*captions, self.held, self.speaker, self.scope]

        shape = WrapRow()
        self.length_caption = self._caption("Length")
        shape.add_group([self.length_caption, self.length])
        self.agency_caption = self._caption("Outcome")
        shape.add_group([self.agency_caption, self.agency])
        shape.add_group([self.roll_caption, self.skill, self.difficulty])
        shape.add_group([self.private_toggle, self.private_keep, self.ooc_toggle], right=True)

        controls = QVBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        controls.addLayout(who)
        controls.addLayout(shape)

        self.ooc = TurnEditor()
        self.ooc.setObjectName("oocInput")
        self.ooc.setPlaceholderText(
            "Treated as true and as instruction — not spoken, and no character hears it."
        )
        self.ooc.setMaximumHeight(72)
        self.ooc.hide()
        self.ooc.submitted.connect(self.send_requested)

        self.input = TurnEditor()
        self.input.setObjectName("input")
        self.input.setPlaceholderText(TURN_PLACEHOLDER)
        self.input.submitted.connect(self.send_requested)
        self.input.setMaximumHeight(140)

        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("sendButton")
        self.send_button.clicked.connect(self.send_requested)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_requested)
        self.regenerate_button = QPushButton("Regenerate")
        self.regenerate_button.clicked.connect(self.regenerate_requested)
        self.image_button = QPushButton("Image…")
        self.image_button.setToolTip(
            "Generate a picture of what is happening now (Ctrl+Shift+I). You see and approve "
            "the prompt first; it is drawn in the background while you play on."
        )
        self.image_button.clicked.connect(self.image_requested)

        buttons = QVBoxLayout()
        buttons.setSpacing(6)
        buttons.addWidget(self.send_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.regenerate_button)
        buttons.addWidget(self.image_button)

        entry = QHBoxLayout()
        entry.setContentsMargins(0, 0, 0, 0)
        entry.setSpacing(10)
        entry.addWidget(self.input, 1)
        entry.addLayout(buttons)

        # A simple chat has none of the story's controls (set_chat_mode).
        self.controls_host = QWidget()
        self.controls_host.setLayout(controls)
        self._chat = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.controls_host)
        layout.addWidget(self.ooc)
        layout.addLayout(entry)

        self.speaker.currentIndexChanged.connect(self._sync_question_mode)
        self.speaker.currentIndexChanged.connect(self._sync_dice)
        self.agency.currentIndexChanged.connect(self._sync_dice)
        self.skill.currentIndexChanged.connect(self._remember_skill)
        self.set_default_agency("contested")

    @staticmethod
    def _compact(combo: QComboBox, characters: int) -> None:
        """Size a combo to its selection, not its longest item.

        A QComboBox asks for the width of its widest entry, and the explanatory
        speaker labels are long: left alone they widened the bottom bar enough
        to squeeze the Stories dock to a sliver. The popup still shows every
        label in full.
        """
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(characters)
        combo.view().setTextElideMode(Qt.ElideNone)
        # Small when space is short, but free to grow into spare width up to
        # its longest entry (capped in _fit_popup), so a wide window doesn't
        # still cut an entry short.
        combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().changeEvent(event)
        # A new text size (View → Text size) changes every entry's width: the
        # caps set when the lists were filled would cut them short.
        # After the event: the combos may not have their new font yet.
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            QTimer.singleShot(0, self._refit_popups)

    def _refit_popups(self) -> None:
        for combo in (
            self.speaker,
            self.length,
            self.agency,
            self.skill,
            self.difficulty,
            self.private_keep,
        ):
            self._fit_popup(combo)

    @staticmethod
    def _fit_popup(combo: QComboBox) -> None:
        view = combo.view()
        view.setMinimumWidth(view.sizeHintForColumn(0) + 32)
        if combo.sizePolicy().horizontalPolicy() == QSizePolicy.Expanding:
            widest = max(
                (
                    combo.fontMetrics().horizontalAdvance(combo.itemText(i))
                    for i in range(combo.count())
                ),
                default=0,
            )
            combo.setMaximumWidth(max(widest + 56, combo.minimumSizeHint().width()))

    def _caption(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("composerCaption")
        return label

    # --- population -------------------------------------------------------

    def clear_cast(self) -> None:
        """No story open: nobody to play or speak as. Emptied rather than set
        to Nobody / Narration, which the next story would keep as a choice."""
        for combo in (self.held, self.speaker):
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)

    def refresh(
        self,
        cast: Sequence[Character],
        scene: SceneState,
        held_id: str | None,
        *,
        default_length: ResponseStyle | None = None,
    ) -> None:
        """Repopulate the selectors from story state, preserving choices."""
        if default_length is not None:
            self.set_default_length(default_length)
        self._cast = list(cast)
        present_ids = set(scene.present_character_ids)
        # The combo's own data, not speaker_id(): that falls back to the
        # narrator when the combo is empty, and a story then opened as
        # "Playing John / Speaking as Narration" (seen live).
        current = self.speaker.currentData()
        previous_speaker = str(current) if current else None

        self.held.blockSignals(True)
        self.held.clear()
        self.held.addItem("Nobody", NOBODY)
        for character in self._cast:
            if character.is_player_available:
                self.held.addItem(character.name, character.id)
        self._select(self.held, held_id or NOBODY)
        self.held.blockSignals(False)

        self.speaker.blockSignals(True)
        self.speaker.clear()
        self.speaker.addItem(NARRATION_LABEL, NARRATOR_SPEAKER_ID)
        self.speaker.setItemData(0, NARRATION_TIP, Qt.ToolTipRole)
        self.speaker.addItem(DIRECTION_LABEL, DIRECTOR_SPEAKER_ID)
        self.speaker.setItemData(1, DIRECTION_TIP, Qt.ToolTipRole)
        for character in self._cast:
            if character.id in present_ids:
                self.speaker.addItem(character.name, character.id)
        self.speaker.addItem(QUESTION_LABEL, QUESTION)
        self.speaker.setItemData(self.speaker.count() - 1, QUESTION_TIP, Qt.ToolTipRole)
        # Speaking as the character you hold is the common case.
        self._select(self.speaker, previous_speaker or held_id or NARRATOR_SPEAKER_ID)
        self.speaker.blockSignals(False)
        self._sync_question_mode()
        self._fit_popup(self.speaker)

        self._scope_ids &= {character.id for character in self._cast}
        self._rebuild_scope_menu(present_ids, held_id)
        self._held_id = held_id
        self._fill_skills()
        self._sync_scope_button()

    def _select(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def select_speaker(self, speaker_id: str) -> None:
        self._select(self.speaker, speaker_id)

    def set_default_length(self, default: ResponseStyle) -> None:
        """Rebuild the length list, naming the story default so it's never a mystery."""
        current = self.length.currentData()
        self.length.blockSignals(True)
        self.length.clear()
        self.length.addItem(f"{label_for(default)}{DEFAULT_SUFFIX}", STORY_DEFAULT)
        for key in PRESET_ORDER:
            if key == "custom":
                # Custom wording belongs to the story; a one-off wants a preset.
                continue
            self.length.addItem(PRESETS[key].label, key)
            self.length.setItemData(self.length.count() - 1, PRESETS[key].summary, Qt.ToolTipRole)
        self._select(self.length, current or STORY_DEFAULT)
        self.length.blockSignals(False)
        self._fit_popup(self.length)

    def _rebuild_scope_menu(self, present_ids: set[str], held_id: str | None) -> None:
        self.scope_menu.clear()
        for character in self._cast:
            if character.id not in present_ids or character.id == held_id:
                continue
            action = QAction(character.name, self.scope_menu)
            action.setCheckable(True)
            action.setChecked(character.id in self._scope_ids)
            action.setData(character.id)
            action.toggled.connect(self._on_scope_toggled)
            self.scope_menu.addAction(action)

    def _on_scope_toggled(self, checked: bool) -> None:
        action = self.sender()
        if not isinstance(action, QAction):
            return
        character_id = action.data()
        if checked:
            self._scope_ids.add(character_id)
        else:
            self._scope_ids.discard(character_id)
        self._sync_scope_button()

    def _sync_scope_button(self) -> None:
        selected = self.npc_scope() == "selected" and not getattr(self, "_chat", False)
        self.scope_button.setVisible(selected)
        names = [character.name for character in self._cast if character.id in self._scope_ids]
        self.scope_button.setText(", ".join(names) if names else "none")

    # --- agency and dice ----------------------------------------------------

    def set_default_agency(self, default: AgencyMode) -> None:
        current = self.agency.currentData()
        self._default_agency = default
        self.agency.blockSignals(True)
        self.agency.clear()
        self.agency.addItem(f"{AGENCY_LABELS[default]}{DEFAULT_SUFFIX}", None)
        for mode, label in AGENCY_LABELS.items():
            self.agency.addItem(label, mode)
            self.agency.setItemData(self.agency.count() - 1, AGENCY_TIPS[mode], Qt.ToolTipRole)
        index = self.agency.findData(current) if current else 0
        self.agency.setCurrentIndex(max(index, 0))
        self.agency.blockSignals(False)
        self._fit_popup(self.agency)
        self._sync_dice()

    def _held_character(self) -> Character | None:
        return next((c for c in self._cast if c.id == self._held_id), None)

    def _fill_skills(self) -> None:
        held = self._held_character()
        self.skill.blockSignals(True)
        self.skill.clear()
        self.skill.addItem("General (competent)", GENERAL_SKILL)
        if held is not None:
            for domain, tier in held.competence.tiers.items():
                self.skill.addItem(f"{domain} — {tier}", domain)
        remembered = self._skill_memory.get(held.id) if held else None
        self._select(self.skill, remembered or GENERAL_SKILL)
        self.skill.blockSignals(False)
        self._fit_popup(self.skill)
        self._sync_dice()

    def _remember_skill(self) -> None:
        if self._held_id:
            self._skill_memory[self._held_id] = self.skill.currentData() or GENERAL_SKILL

    def will_roll(self) -> bool:
        """Dice only resolve the held character acting — never Direction or Narration."""
        return (
            self.agency_mode() == "dice"
            and self._held_id is not None
            and self.speaker_id() == self._held_id
        )

    def _sync_dice(self) -> None:
        rolling = self.will_roll() and not self.is_question() and not self._chat
        for widget in (self.roll_caption, self.skill, self.difficulty):
            widget.setVisible(rolling)

    def agency_override(self) -> AgencyMode | None:
        return self.agency.currentData()

    def agency_mode(self) -> AgencyMode:
        return self.agency_override() or self._default_agency

    def dice_domain(self) -> str | None:
        value = self.skill.currentData()
        return None if value in (None, GENERAL_SKILL) else value

    def difficulty_level(self) -> Difficulty:
        return self.difficulty.currentData() or "standard"

    def _sync_question_mode(self) -> None:
        """A question isn't a turn: length and OOC don't apply, and Send says Ask."""
        asking = self.is_question()
        self.send_button.setText("Ask" if asking else "Send")
        self.input.setPlaceholderText(
            QUESTION_PLACEHOLDER if asking else CHAT_PLACEHOLDER if self._chat else TURN_PLACEHOLDER
        )
        if self._chat:
            return  # a chat shows none of these (set_chat_mode)
        for widget in (
            self.length_caption,
            self.length,
            self.agency_caption,
            self.agency,
            self.ooc_toggle,
        ):
            widget.setVisible(not asking)
        self._sync_dice()
        if asking and self.ooc_toggle.isChecked():
            self.ooc_toggle.setChecked(False)

    def is_question(self) -> bool:
        return not self._chat and self.speaker.currentData() == QUESTION

    def set_chat_mode(self, on: bool) -> None:
        """A simple chat (engine/chat.py): the text box and its buttons, none of
        a story's controls; nothing asked out of character, no OOC note."""
        self._chat = on
        self.set_private_offered(True)
        for widget in self._who_widgets:
            widget.setVisible(not on)
        if on:
            self.ooc_toggle.setChecked(False)
            self.ooc.hide()
            for widget in (
                self.scope_button,
                self.length_caption,
                self.length,
                self.agency_caption,
                self.agency,
                self.roll_caption,
                self.skill,
                self.difficulty,
                self.ooc_toggle,
            ):
                widget.hide()
        else:
            self._sync_scope_button()
        self._sync_question_mode()
        self._sync_private_tips()

    def set_private_offered(self, offered: bool) -> None:
        """Whether a part can be held in private: not in a chat on a TEE
        model, which is private throughout. The Private button is all of the
        controls a chat shows, so without it the whole row goes."""
        self._private_offered = offered
        self.private_toggle.setVisible(offered)
        self.private_keep.setVisible(offered and self._keep_fixed is None)
        self.controls_host.setVisible(offered or not self._chat)

    def fix_private_keep(self, keep: str | None) -> None:
        """Where a private part is kept, when there is no choice to make: a
        chat kept in memory only keeps its private parts in memory too, so the
        In memory / On disk selector goes. None gives the choice back."""
        self._keep_fixed = keep
        self.private_keep.setVisible(self._private_offered and keep is None)

    def _sync_private_tips(self) -> None:
        active = self.private_toggle.isChecked()
        if self._chat:
            tip = CHAT_END_PRIVATE_TIP if active else CHAT_PRIVATE_TIP
        else:
            tip = END_PRIVATE_TIP if active else PRIVATE_TIP
        self.private_toggle.setToolTip(tip)

    def set_private_look(self, on: bool) -> None:
        """A TEE chat's input is tinted as a private scene's is."""
        for widget in (self.input, self.ooc):
            widget.setProperty("private", "true" if on else "false")
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _on_held_changed(self) -> None:
        self.held_character_changed.emit(self.held_character_id() or "")

    def _on_ooc_toggled(self, checked: bool) -> None:
        self.ooc.setVisible(checked)
        if checked:
            self.ooc.setFocus()
        else:
            self.ooc.clear()

    # --- current turn -----------------------------------------------------

    def held_character_id(self) -> str | None:
        value = self.held.currentData()
        return None if value in (None, NOBODY) else str(value)

    def speaker_id(self) -> str:
        value = self.speaker.currentData()
        return str(value) if value else NARRATOR_SPEAKER_ID

    def npc_scope(self) -> NpcScope:
        return SCOPE_LABELS[self.scope.currentIndex()][1]

    def npc_scope_ids(self) -> tuple[str, ...]:
        if self.npc_scope() != "selected":
            return ()
        return tuple(character.id for character in self._cast if character.id in self._scope_ids)

    def response_style(self) -> ResponseStyle | None:
        """The one-off length for this turn, or None for the story default."""
        value = self.length.currentData()
        return None if value in (None, STORY_DEFAULT) else value

    def text(self) -> str:
        return self.input.toPlainText().strip()

    def ooc_text(self) -> str | None:
        if not self.ooc_toggle.isChecked():
            return None
        return self.ooc.toPlainText().strip() or None

    def restore_turn(self, node: Node) -> None:
        """Put a sent turn back in the composer, as it was sent (Take back).

        Everything the author chose for it comes back with the text: who spoke,
        the OOC note, the length and outcome, the dice settings. Anything the
        node doesn't record keeps whatever the composer currently shows.
        """
        meta = node.meta
        self.input.setPlainText(node.content)
        self.ooc_toggle.setChecked(bool(node.ooc))
        self.ooc.setPlainText(node.ooc or "")
        if meta.controlled_character_id:
            self._select(self.held, meta.controlled_character_id)
        self._select(self.speaker, node.speaker_id)
        self._select(self.length, meta.response_style or STORY_DEFAULT)
        index = self.agency.findData(meta.agency_mode) if meta.agency_mode else 0
        self.agency.setCurrentIndex(max(index, 0))
        if meta.difficulty:
            self.difficulty.setCurrentIndex(DIFFICULTY_ORDER.index(meta.difficulty))
        if meta.dice_domain:
            skill = self.skill.findData(meta.dice_domain)
            if skill >= 0:
                self.skill.setCurrentIndex(skill)
        self.focus_input()

    def set_direction(self, text: str) -> None:
        """Put a Director turn in the composer, ready to send.

        A settings review proposes one when the story so far, not the
        settings, is the cause; it is never sent by anything but the author.
        """
        self.input.setPlainText(text.strip())
        self.ooc_toggle.setChecked(False)
        self.ooc.clear()
        self._select(self.speaker, DIRECTOR_SPEAKER_ID)
        self._sync_question_mode()
        self.focus_input()

    def reset_turn_shape(self) -> None:
        """Back to the story's defaults — for when a different story opens.

        Length and outcome stick across turns, but they are this session's
        choices, not the story's: another story's defaults are its own.
        """
        self._select(self.length, STORY_DEFAULT)
        self.agency.setCurrentIndex(0)
        self.difficulty.setCurrentIndex(DIFFICULTY_ORDER.index("standard"))

    def clear_turn(self) -> None:
        self.input.clear()
        self.ooc.clear()
        # Length and outcome stay as the author left them. They used to reset
        # to the story default after every send, so choosing a longer passage
        # meant choosing it again on the next turn and the one after; the only
        # way to keep it was to change the story default. The story default is
        # still the first entry and marked as the recommended one.
        # The dice difficulty does reset: it describes one attempt, not a
        # standing preference. (The skill is remembered per character.)
        self.difficulty.setCurrentIndex(DIFFICULTY_ORDER.index("standard"))

    def set_busy(
        self,
        busy: bool,
        *,
        has_story: bool,
        can_regenerate: bool,
        background: bool = False,
        queued: bool = False,
    ) -> None:
        """`background`: the passage is in and only reads remain, so the author
        may write and send their next turn, which waits for them (`queued`)."""
        writable = has_story and (not busy or (background and not queued))
        self.input.setEnabled(writable)
        if not has_story:
            self.input.setPlaceholderText(NO_STORY_PLACEHOLDER)
        else:
            self._sync_question_mode()
        self.ooc.setEnabled(writable)
        self.send_button.setEnabled(writable)
        self.send_button.setToolTip(
            "Sends when the background reads finish" if busy and writable else ""
        )
        self.stop_button.setEnabled(busy)
        self.regenerate_button.setEnabled(has_story and not busy and can_regenerate)
        self.image_button.setEnabled(has_story and not busy)
        self.private_toggle.setEnabled(has_story and not busy)
        self.private_keep.setEnabled(has_story and not busy and not self.private_toggle.isChecked())
        # Taking up a character moves the scene (StorySession.hold), which is
        # the worker's to change until the job is over.
        self.held.setEnabled(has_story and not busy)
        for widget in (
            self.speaker,
            self.scope,
            self.scope_button,
            self.length,
            self.agency,
            self.skill,
            self.difficulty,
            self.ooc_toggle,
        ):
            widget.setEnabled(writable)

    def _on_private_clicked(self, checked: bool) -> None:
        # The window decides: until it says the scene has begun or ended, the
        # button shows the state as it was.
        self.private_toggle.setChecked(not checked)
        self.private_requested.emit(checked)

    def set_private_state(self, active: bool, keep: str | None = None) -> None:
        self.private_toggle.setChecked(active)
        if keep is not None:
            index = self.private_keep.findData(keep)
            if index >= 0:
                self.private_keep.setCurrentIndex(index)
        self.private_keep.setEnabled(not active)
        self.private_toggle.setText("End private" if active else "Private")
        self._sync_private_tips()
        # A tinted input says, while you type, where this turn is going.
        for widget in (self.input, self.ooc):
            widget.setProperty("private", "true" if active else "false")
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def keep_choice(self) -> str:
        return self._keep_fixed or self.private_keep.currentData() or "memory"

    @property
    def keep_fixed(self) -> bool:
        return self._keep_fixed is not None

    def focus_input(self) -> None:
        self.input.setFocus(Qt.OtherFocusReason)
