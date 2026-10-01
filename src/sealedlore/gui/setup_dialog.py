"""Story setup: the world, the opening, and where a playthrough starts.

Human testing put the world's rules into a lore entry — retrieved by
similarity into the uncached tail, so only sometimes there — and the premise
into the first Director turn. The world bible already had a place in the
cached system block (§6.1); it just had no editor. This is it.

Edits apply only on Save, so Cancel really cancels.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.gui.composer import (
    AGENCY_LABELS,
    AGENCY_TIPS,
    WORLD_ACTIVITY_LABELS,
    WORLD_ACTIVITY_TIPS,
)
from sealedlore.gui.fields import line_edit, set_field_text
from sealedlore.gui.start_presence import StartPresence
from sealedlore.gui.style_panel import StylePanel
from sealedlore.models.character import Character
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, Node
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story

GENERATE_LABEL = "The model writes the opening from these notes"
AS_WRITTEN_LABEL = "Use exactly as written — this is the opening passage"


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


class SetupDialog(QDialog):
    def __init__(
        self,
        story: Story,
        cast: Sequence[Character],
        *,
        first_message: Node | None = None,
        texts: PromptTexts = DEFAULT_TEXTS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Story setup")
        self.texts = texts
        self.setMinimumSize(640, 560)
        self.story = story
        self.cast = list(cast)
        self.first_message = first_message

        self.tabs = QTabWidget()
        if story.chat:
            # A simple chat's setup is its name and its system prompt.
            self.tabs.addTab(self._chat_about_tab(), "About")
            self.tabs.addTab(self._chat_prompt_tab(), "System prompt")
            self._add_buttons()
            return
        self.tabs.addTab(self._about_tab(), "About")
        self.tabs.addTab(self._world_tab(), "World")
        self.tabs.addTab(self._style_tab(), "Style")
        self.tabs.addTab(self._opening_tab(), "Opening")
        self.tabs.addTab(self._scene_tab(), "Starting scene")
        self.tabs.addTab(self._private_tab(), "Private scenes")

        self._add_buttons()

    def _add_buttons(self) -> None:
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)

    # --- tabs -------------------------------------------------------------

    def _style_tab(self) -> QWidget:
        """The Style page's form, here too: a new story's opening is written in
        this style, and before this the only place to set it was the inspector,
        once the story had begun (the author's report, Sept 2026). It edits a
        copy, applied on Save, so Cancel leaves the story's style as it was."""
        self.style = self.story.style.model_copy(deep=True)
        self.style_panel = StylePanel()
        self.style_panel.set_style(
            self.style, started=self.first_message is not None, texts=self.texts
        )
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            _hint(
                "How passages read: length, perspective, person and tense, and the rest. "
                "The same settings as the Style page beside the story; set them here and "
                "the opening is written this way. Person and tense are fixed once the "
                "story has begun, the rest can change at any time."
            )
        )
        layout.addWidget(self.style_panel, 1)
        return page

    def _private_tab(self) -> QWidget:
        """How the private model is told to write. Only the rules are the
        author's: the story's facts and the per-turn reminders always go."""
        prompt = self.story.private_prompt
        widget = QWidget()
        form = QFormLayout(widget)
        self.private_mode = QComboBox()
        self.private_mode.addItem("Compact rules (for smaller models)", "compact")
        self.private_mode.addItem("The story's full rules", "full")
        self.private_mode.addItem("My own rules", "custom")
        self.private_mode.setCurrentIndex(max(self.private_mode.findData(prompt.mode), 0))
        self.private_notes = QPlainTextEdit(prompt.notes)
        self.private_notes.setPlaceholderText(
            "Added to the rules in any mode, e.g. 'slow pacing, mostly dialogue, fade to black'."
        )
        self.private_notes.setMaximumHeight(90)
        self.private_custom = QPlainTextEdit(prompt.custom)
        self.private_custom.setPlaceholderText("Your rules, in place of the story's.")
        start = QPushButton("Start from the compact rules")
        start.clicked.connect(
            lambda: self.private_custom.setPlainText(self.texts["private.rules"].strip())
        )
        self.private_mode.currentIndexChanged.connect(self._sync_private_mode)
        self._private_start = start
        form.addRow("Rules", self.private_mode)
        form.addRow("For private scenes", self.private_notes)
        form.addRow("Own rules", self.private_custom)
        form.addRow("", start)
        form.addRow(
            _hint(
                "Whatever you choose, the private model is always given the story's world, "
                "cast, lore, scene and history, and each turn is told who you are playing, "
                "who is present and how long to write, so your rules change how it writes, "
                "not who is who."
            )
        )
        self._sync_private_mode()
        return widget

    def _sync_private_mode(self) -> None:
        custom = self.private_mode.currentData() == "custom"
        self.private_custom.setEnabled(custom)
        self._private_start.setEnabled(custom)

    def _chat_about_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self.title = line_edit(self.story.title)
        self.description = QPlainTextEdit(self.story.setup.description or "")
        self.description.setPlaceholderText("What this chat is for. Not sent to the model.")
        form.addRow("Title", self.title)
        form.addRow("Description", self.description)
        return widget

    def _chat_prompt_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.chat_prompt = QPlainTextEdit(self.story.chat_prompt)
        self.chat_prompt.setPlaceholderText("e.g. You are a helpful assistant. Answer concisely.")
        layout.addWidget(self.chat_prompt, 1)
        layout.addWidget(
            _hint(
                "Sent at the top of every request, exactly as written, and cached: a change "
                "costs one cache miss on the next message. Nothing else is added to it."
            )
        )
        layout.addWidget(QLabel("Tail"))
        self.chat_tail = QPlainTextEdit(self.story.chat_tail)
        self.chat_tail.setPlaceholderText(
            "e.g. Reply in British English, in two short paragraphs at most."
        )
        self.chat_tail.setMaximumHeight(110)
        layout.addWidget(self.chat_tail)
        layout.addWidget(
            _hint(
                "Added after your message at the end of every request, where a model "
                "weighs it most: a style reminder or a standing rule. It isn't saved into "
                "the conversation, so it never piles up in the history, and it costs no "
                "cache miss when you change it. Blank sends nothing."
            )
        )
        return widget

    def _about_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self.title = line_edit(self.story.title)
        self.description = QPlainTextEdit(self.story.setup.description or "")
        self.description.setPlaceholderText(
            "What this story is, for anyone starting it. Not sent to the model."
        )
        self.agency = QComboBox()
        for mode, label in AGENCY_LABELS.items():
            self.agency.addItem(label, mode)
            self.agency.setItemData(self.agency.count() - 1, AGENCY_TIPS[mode], Qt.ToolTipRole)
        self.agency.setCurrentIndex(max(self.agency.findData(self.story.defaults.agency_mode), 0))
        self.world_activity = QComboBox()
        for level, label in WORLD_ACTIVITY_LABELS.items():
            self.world_activity.addItem(label, level)
            self.world_activity.setItemData(
                self.world_activity.count() - 1, WORLD_ACTIVITY_TIPS[level], Qt.ToolTipRole
            )
        self.world_activity.setCurrentIndex(
            max(self.world_activity.findData(self.story.defaults.world_activity), 0)
        )
        form.addRow("Title", self.title)
        form.addRow("Description", self.description)
        form.addRow("Outcomes", self.agency)
        form.addRow(
            _hint(
                "How the outcome of your character's actions is decided, unless you choose "
                "otherwise for a turn in the bottom bar."
            )
        )
        form.addRow("World", self.world_activity)
        form.addRow(
            _hint(
                "How much happens that you didn't start: people acting on their own aims, "
                "someone arriving, an earlier choice catching up."
            )
        )
        self.image_style = line_edit(self.story.image_style)
        self.image_style.setPlaceholderText("e.g. painterly science-fiction concept art")
        form.addRow("Picture style", self.image_style)
        form.addRow(
            _hint(
                "How generated pictures of this story should look. Given to whoever writes "
                "an image prompt; never to the storyteller."
            )
        )
        return widget

    def _world_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.world = QPlainTextEdit(self.story.world_bible or "")
        self.world.setPlaceholderText(
            "The setting and its rules: what exists, how things work, what is true "
            "throughout the story."
        )
        layout.addWidget(self.world, 1)
        layout.addWidget(
            _hint(
                "Sent with every turn and cached, so it is always there and cheap to "
                "repeat. Changing it costs one cache miss. Details that only matter "
                "now and then belong in Lore instead."
            )
        )
        # Playtesting (Sept 2026): a world text saying one side "has no
        # knowledge that" the other has some number of worlds put the number
        # in that side's characters' mouths; a style note didn't stop it,
        # rewording the sentence did.
        layout.addWidget(
            _hint(
                "How to write it: state facts about the world, each once, in the place "
                "it belongs. When one side doesn't know something, name the gap without "
                "the fact: “Hellsville has no knowledge of the Northern Camp”, not "
                "“Hellsville doesn't know the Northern Camp holds four hundred people”. "
                "A fact written into a sentence about someone reads as something they "
                "know, and their characters will quote it; a note elsewhere won't undo "
                "that. Leave out who plays whom, how passages should read and what "
                "should happen next: those belong in Cast, Style and Opening."
            )
        )
        return widget

    def _opening_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.opening = QPlainTextEdit(self.story.setup.opening_text)
        self.opening.setPlaceholderText(
            "How the story begins. Notes for the model to write up, or the opening passage itself."
        )
        self.generate = QRadioButton(GENERATE_LABEL)
        self.as_written = QRadioButton(AS_WRITTEN_LABEL)
        group = QButtonGroup(widget)
        group.addButton(self.generate)
        group.addButton(self.as_written)
        as_written = self.story.setup.opening_mode == "as_written"
        (self.as_written if as_written else self.generate).setChecked(True)

        layout.addWidget(self.opening, 1)
        # The storyteller writes the first passage in the voice it reads, so
        # an opening in another person or tense lets the story slip into it.
        self.voice_hint = _hint("")
        layout.addWidget(self.voice_hint)
        self.style_panel.changed.connect(self._show_voice_hint)
        self._show_voice_hint()
        layout.addWidget(self.generate)
        layout.addWidget(self.as_written)
        layout.addWidget(
            _hint(
                "Written up, the notes go to the model as a Director instruction and "
                "Regenerate gives a different opening. As written, the text becomes the "
                "first passage and no call is made."
            )
        )
        if self.first_message is not None:
            # Human testing: on a story already under way, the opening looked
            # like the one thing that couldn't be changed. It can, for a new
            # playthrough; saving a change offers one.
            layout.addWidget(
                _hint(
                    "This story has already begun, so a new opening (or starting scene) "
                    "starts a new playthrough: saving offers one, and the story so far "
                    "is kept as it is."
                )
            )
            copy = QPushButton("Use the story's first message")
            copy.setToolTip("Fill this in from the message the story actually began with")
            copy.clicked.connect(self._copy_first_message)
            layout.addWidget(copy, 0, Qt.AlignLeft)
        return widget

    def _show_voice_hint(self) -> None:
        voice = ", ".join(
            part
            for part in (
                f"{self.style.person} person" if self.style.person else "",
                f"{self.style.tense} tense" if self.style.tense else "",
            )
            if part
        )
        self.voice_hint.setText(
            f"Write it in the person and tense the story is told in ({voice}, from the "
            "Style tab): an opening in another voice can pull the first passages toward it."
            if voice
            else "Write it in the person and tense you want the story told in, and set "
            "them on the Style tab: an opening in another voice can pull the first "
            "passages toward it."
        )

    def _scene_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        start = self.story.setup.starting_scene

        form = QFormLayout()
        self.location = line_edit(start.location or "")
        self.time_of_day = line_edit(start.time_of_day or "")
        self.situation = QPlainTextEdit(start.situation or "")
        self.situation.setMaximumHeight(72)
        form.addRow("Location", self.location)
        form.addRow("Time", self.time_of_day)
        form.addRow("Situation", self.situation)
        # A plot story's time is the plot clock's (prompt.scene_time_shown).
        form.setRowVisible(self.time_of_day, self.story.plot is None)
        layout.addLayout(form)

        # A new story's cast is usually made after its Setup (the author's
        # report, Sept 2026): with nobody to list, say where this is chosen.
        self.presence: StartPresence | None = None
        self.suggested: QComboBox | None = None
        if self.cast:
            layout.addWidget(QLabel("Who is there at the start"))
            self.presence = StartPresence(
                self.cast,
                start.present_character_ids,
                self.story.setup.absent_at_start,
                max_height=None,
            )
            layout.addWidget(self.presence, 1)
        playable = [character for character in self.cast if character.is_player_available]
        if playable:
            suggested = QFormLayout()
            self.suggested = QComboBox()
            self.suggested.addItem("No suggestion", None)
            for character in playable:
                self.suggested.addItem(character.name, character.id)
            index = self.suggested.findData(self.story.setup.suggested_character_id)
            self.suggested.setCurrentIndex(max(index, 0))
            suggested.addRow("Suggested character to play", self.suggested)
            layout.addLayout(suggested)
        if not self.cast:
            layout.addWidget(
                _hint(
                    "This story has no cast yet. Once it has (the Cast page beside the "
                    "story), choose here or when you start who is there at the start and "
                    "which character to suggest playing. Until then the opening places "
                    "everyone."
                )
            )
            layout.addStretch(1)

        if self.first_message is not None:
            current = QPushButton("Use the current scene")
            current.setToolTip("Copy the Scene tab's location, time, situation and roster")
            current.clicked.connect(self._copy_current_scene)
            layout.addWidget(current, 0, Qt.AlignLeft)
        layout.addWidget(
            _hint(
                "Where the story begins, and where a Restart begins again. Once it has "
                "begun, the scene as it stands is edited in the Scene tab."
            )
        )
        return widget

    # --- helpers ----------------------------------------------------------

    def _copy_first_message(self) -> None:
        node = self.first_message
        if node is None:
            return
        self.opening.setPlainText(node.content)
        # A Director turn was notes for the model; anything else was prose.
        generated = node.kind == "user" and node.speaker_id == DIRECTOR_SPEAKER_ID
        (self.generate if generated else self.as_written).setChecked(True)

    def _copy_current_scene(self) -> None:
        self._show_scene(self.story.scene)

    def _show_scene(self, scene: SceneState) -> None:
        set_field_text(self.location, scene.location or "")
        set_field_text(self.time_of_day, scene.time_of_day or "")
        self.situation.setPlainText(scene.situation or "")
        if self.presence is not None:
            self.presence.show_roster(scene.present_character_ids)

    def _pinned(self) -> tuple[list[str], list[str]]:
        """(pinned present, pinned not present); as they were with no cast to show."""
        if self.presence is None:
            setup = self.story.setup
            return list(setup.starting_scene.present_character_ids), list(setup.absent_at_start)
        return self.presence.pinned()

    def _starting_scene(self) -> SceneState:
        present, _ = self._pinned()
        # Only what this tab shows changes. Built afresh, the starting scene
        # lost who is nearby (a drafted story sets it), its privacy and any
        # non-cast people present on every Save, even one that only touched
        # the World.
        return self.story.setup.starting_scene.model_copy(
            deep=True,
            update={
                "location": self.location.text().strip() or None,
                "time_of_day": self.time_of_day.text().strip() or None,
                "situation": self.situation.toPlainText().strip() or None,
                "present_character_ids": present,
            },
        )

    def _save(self) -> None:
        story = self.story
        story.title = self.title.text().strip() or story.title
        if story.chat:
            story.setup.description = self.description.toPlainText().strip() or None
            story.chat_prompt = self.chat_prompt.toPlainText().strip()
            story.chat_tail = self.chat_tail.toPlainText().strip()
            self.accept()
            return
        story.world_bible = self.world.toPlainText().strip() or None
        setup = story.setup
        setup.description = self.description.toPlainText().strip() or None
        story.defaults.agency_mode = self.agency.currentData()
        story.defaults.world_activity = self.world_activity.currentData()
        story.style = self.style
        story.image_style = self.image_style.text().strip()
        story.private_prompt.mode = self.private_mode.currentData()
        story.private_prompt.notes = self.private_notes.toPlainText().strip()
        story.private_prompt.custom = self.private_custom.toPlainText().strip()
        setup.opening_text = self.opening.toPlainText().strip()
        setup.opening_mode = "as_written" if self.as_written.isChecked() else "generate"
        setup.starting_scene = self._starting_scene()
        setup.absent_at_start = self._pinned()[1]
        if self.suggested is not None:
            setup.suggested_character_id = self.suggested.currentData()
        self.accept()
