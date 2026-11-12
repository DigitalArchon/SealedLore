"""Story setup: the world, the opening, and where a playthrough starts.

Human testing put the world's rules into a lore entry — retrieved by
similarity into the uncached tail, so only sometimes there — and the premise
into the first Director turn. The world bible already had a place in the
cached system block (§6.1); it just had no editor. This is it.

Edits apply only on Save, so Cancel really cancels, except on the Characters
tab: it is the Cast page itself, lent by the window (gui/window_cast.py),
whose cards are saved as they are made, as they always were there. Before
it, the full setup had no characters at all (the author, Oct 2026: from the
new story guide's "Skip to the full setup…", a story's cast was a status-bar
line away, on another page).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.routing import config_role_model
from sealedlore.gui.composer import (
    AGENCY_LABELS,
    AGENCY_TIPS,
    WORLD_ACTIVITY_LABELS,
    WORLD_ACTIVITY_TIPS,
)
from sealedlore.gui.fields import line_edit, set_field_text
from sealedlore.gui.model_picker import BrowseRoute, ModelField, Qualify, RouteEndpoint
from sealedlore.gui.picture_style import PictureStyleField
from sealedlore.gui.saved_prompts import SavedPrompts, SavedPromptsButton
from sealedlore.gui.sizing import GrowingText, enter_only_on_buttons, size_to_screen
from sealedlore.gui.start_presence import StartPresence
from sealedlore.gui.style_panel import StylePanel
from sealedlore.models.character import Character
from sealedlore.models.config import RECOMMENDED_TEXT_MODELS, Config
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, Node
from sealedlore.models.scene import SceneState
from sealedlore.models.story import STORY_MODEL_FIELDS, Story
from sealedlore.providers.tee import is_tee

# How full the map's rooms are (models/area.py `Density`).
DENSITY_LABELS = {
    "sparse": "Sparse: the bare essentials",
    "normal": "Normal",
    "full": "Full: lived in, cluttered",
}


GENERATE_LABEL = "The model writes the opening from these notes"
NOTES_TAB = "Storyteller's notes"
# The storyteller's notes a model wrote (a draft, a follow-up), not yet read.
NOTES_UNREAD = "The storyteller has notes for this story that you have not read ({words})."
SHOW_NOTES = "Show them"
CLEAR_NOTES = "Clear them"
AS_WRITTEN_LABEL = "Use exactly as written — this is the opening passage"


# A story's models as Settings → Models lists them: role, label, what it does.
MODEL_ROLES = (
    ("story", "Story model", "The storyteller: writes every passage."),
    ("summarisation", "Summarisation model", "Writes the chapters as the story is archived."),
    ("authoring", "Authoring model", "Reviews this story's settings."),
    ("scene", "Scene model", "Reads the scene after every passage."),
    ("plot", "Plot model", "Decides when the plot's events happen."),
    ("lore", "Lore model", "Picks lore after each passage, when a lorebook is too large to send."),
    ("image_prompt", "Image prompt writer", "Writes the image prompt from the story."),
)
# What a simple chat calls (engine/chat.py), and what it calls its own model.
CHAT_MODEL_ROLES = ("story", "summarisation", "image_prompt")
# The story's fields Save writes (`SetupDialog._write`): what Cancel would
# lose is told by comparing them with what they were as Setup opened.
WRITTEN = {
    "title",
    "world_bible",
    "storyteller_notes",
    "storyteller_notes_by",
    "setup",
    "defaults",
    "style",
    "picture_style",
    "image_style",
    "private_prompt",
    "chat_prompt",
    "chat_tail",
}
ALL_IN_SETTINGS = "Every model here goes with Settings → Models, and follows it when it changes."
SOME_OF_ITS_OWN = "The filled-in ones are this story's alone; the rest go with Settings → Models."


@dataclass(frozen=True)
class ModelChoices:
    """What the Models tab offers: the settings it goes with, and from the
    window, the model list and each model's routes."""

    config: Config
    browse_route: BrowseRoute | None = None
    route_endpoint: RouteEndpoint | None = None
    hosts_of: Callable[[str], tuple[str, ...] | None] | None = None
    qualify: Qualify | None = None


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
        characters: QWidget | None = None,
        models: ModelChoices | None = None,
        describe_style: Callable[[], tuple[str, bytes, str] | None] | None = None,
        saved_prompts: SavedPrompts | None = None,
        hidden: frozenset[str] = frozenset(),
    ) -> None:
        """`cast`: the story's own list, read again whenever the Starting
        scene tab is shown, since `characters` (the Cast page, lent) may
        have changed it. `models`: for the Models tab, shown with it.
        `saved_prompts`: the system prompts kept for chats."""
        self._saved_prompts = saved_prompts
        super().__init__(parent)
        self.setWindowTitle("Story setup")
        self.texts = texts
        # Words for a picture's style (gui/style_from_picture.py), the window's.
        self._describe_style = describe_style
        self.setMinimumSize(640, 560)
        size_to_screen(self, 0.62 if characters is not None else 0.5, 0.82)
        self.story = story
        # Whom the plot keeps out until an event brings them: not offered to
        # pin at the start (a spoiler), unless spoilers are shown.
        self._hidden = hidden
        self._live_cast = cast
        self.cast = [c for c in cast if c.id not in hidden]
        self.first_message = first_message
        self.model_fields: dict[str, ModelField] = {}
        self._models = models

        self.tabs = QTabWidget()
        if story.chat:
            # A simple chat's setup is its name and its system prompt.
            self.tabs.addTab(self._chat_about_tab(), "About")
            self.tabs.addTab(self._chat_prompt_tab(), "System prompt")
            # A TEE chat's every call is its own model's, moved by its button.
            if models is not None and not is_tee(story.defaults.main_model or ""):
                self.tabs.addTab(self._models_tab(models), "Models")
            self._add_buttons()
            return
        self.tabs.addTab(self._about_tab(), "About")
        self.tabs.addTab(self._world_tab(), "World")
        self.tabs.addTab(self._notes_tab(), NOTES_TAB)
        if characters is not None:
            self.tabs.addTab(self._characters_tab(characters), "Characters")
        self.tabs.addTab(self._style_tab(), "Style")
        self.tabs.addTab(self._opening_tab(), "Opening")
        self.scene_tab = self._scene_tab()
        self.tabs.addTab(self.scene_tab, "Starting scene")
        self.tabs.addTab(self._private_tab(), "Private scenes")
        if models is not None:
            self.tabs.addTab(self._models_tab(models), "Models")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self._add_buttons()

    def _add_buttons(self) -> None:
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        for button in buttons.buttons():
            # Enter in a field (the title) never saves and closes Setup.
            button.setAutoDefault(False)
            button.setDefault(False)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        # What Save would write as Setup opened, to tell before Cancel or Esc
        # whether anything would be lost (`_written`, once the dialog shows).
        self._opened: tuple | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)

    # --- tabs -------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # Once filled in: what changes from here on is the author's doing.
        if self._opened is None:
            QTimer.singleShot(0, self, self._remember_opened)

    def _remember_opened(self) -> None:
        if self._opened is None:
            self._opened = self._written()

    def _written(self) -> tuple:
        """What Save would write, as the fields stand: `_write` on a copy of
        the story, so nothing changes. Compared with what it was as Setup
        opened, it tells whatever way a change was made: typed, chosen, or
        set by a button. A watch on the fields saw only typing and choosing,
        so From a picture…, Style's Edit text, a model chosen with Recommended
        or Browse…, and Use the models in Settings were dropped by Cancel
        without a word. The lent Cast page is not Setup's: its cards are kept
        as they are made."""
        story = self.story.model_copy(deep=True)
        self._write(story)
        written = story.model_dump(include=WRITTEN)
        picture = None if story.chat else self.picture_style.picture_choice()
        return written, picture

    def edited(self) -> bool:
        """Whether Save would write anything other than what Setup opened with."""
        return self._opened is not None and self._written() != self._opened

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Enter in a field never presses a button for it, and on a button
        presses that one only (gui/sizing.py `enter_only_on_buttons`)."""
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)

    def reject(self) -> None:
        """Cancel, Esc or closing: changes made here are asked about first."""
        if self.edited():
            answer = QMessageBox.question(
                self,
                "Discard your changes?",
                "Close Setup without saving what you changed?",
                QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Discard:
                return
        super().reject()

    def _characters_tab(self, panel: QWidget) -> QWidget:
        self._lent_panel = panel
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(
            _hint(
                "The cast board's cards themselves: as many or as few characters as you "
                "like, in as much or as little detail. The more you put on their cards, the "
                "more say you have in who they are. Set in a world that already exists? Fill "
                "with lore characters… writes cards for its people. Cards are kept as you "
                "make them, as on the cast board: Cancel doesn't take them back. Their "
                "pictures as sheets of looks and expressions are on the cast board."
            )
        )
        layout.addWidget(panel, 1)
        panel.show()
        return widget

    def _on_tab_changed(self, index: int) -> None:
        """Back on the Starting scene with a cast that changed meanwhile: the
        roster and the suggestion list it, choices kept."""
        if self.tabs.widget(index) is not self.scene_tab:
            return
        live = [c for c in self._live_cast if c.id not in self._hidden]
        if [(c.id, c.name, c.is_player_available) for c in live] == [
            (c.id, c.name, c.is_player_available) for c in self.cast
        ]:
            return
        present, absent = self._pinned()
        suggested = self.suggested.currentData() if self.suggested is not None else None
        self.cast = live
        fresh = self._scene_tab(chosen=(present, absent, suggested))
        index = self.tabs.indexOf(self.scene_tab)
        self.tabs.blockSignals(True)
        self.tabs.removeTab(index)
        self.scene_tab.deleteLater()
        self.scene_tab = fresh
        self.tabs.insertTab(index, fresh, "Starting scene")
        self.tabs.setCurrentIndex(index)
        self.tabs.blockSignals(False)

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

    def _models_tab(self, models: ModelChoices) -> QWidget:
        """This story's own models (the author, Oct 31 2026). Blank goes with
        Settings → Models and follows it: the model it goes to is in grey."""
        story = self.story
        widget = QWidget()
        form = QFormLayout(widget)
        form.addRow(
            _hint(
                "Which model does each job in this chat. Blank uses the one in grey, from "
                "Settings → Models. Its own model stays until you change it."
                if story.chat
                else "Which model does each job in this story. Blank uses the one in grey, "
                "from Settings → Models, and follows it when that changes. Choose one here "
                "to use it for this story alone; the Stories list marks a story that does."
            )
        )
        for role, label, tip in MODEL_ROLES:
            if story.chat and role not in CHAT_MODEL_ROLES:
                continue
            # A story with no plot has no plot calls.
            if role == "plot" and story.plot is None:
                continue
            field = ModelField(
                story.defaults.own_model(role) or "",
                route_endpoint=models.route_endpoint,
                browse_route=models.browse_route,
                hosts_of=models.hosts_of,
                recommended=RECOMMENDED_TEXT_MODELS.get(
                    f"{role}_model" if role != "story" else role, ()
                ),
                qualify_recommended=models.qualify,
            )
            field.setPlaceholderText(config_role_model(models.config, role)[1])
            field.setToolTip(tip)
            field.set_route(self._own_route(role))
            field.textChanged.connect(self._sync_models)
            self.model_fields[role] = field
            form.addRow("Model" if story.chat and role == "story" else label, field)
        self.reset_models = QPushButton("Use the models in Settings")
        self.reset_models.setToolTip(
            "Empty every field here. Nothing is saved until you press Save."
        )
        self.reset_models.clicked.connect(self._use_settings_models)
        form.addRow(self.reset_models)
        self.models_note = _hint("")
        form.addRow(self.models_note)
        self._sync_models()
        return widget

    def _own_route(self, role: str):
        defaults = self.story.defaults
        if role == "story" and self.story.chat:
            return defaults.main_route
        return defaults.model_routes.get(role)

    def _use_settings_models(self) -> None:
        for field in self.model_fields.values():
            field.setText("")
            field.set_route(None)

    def _sync_models(self, *_args) -> None:
        own = any(field.text().strip() for field in self.model_fields.values())
        self.reset_models.setEnabled(own)
        self.models_note.setText(SOME_OF_ITS_OWN if own else ALL_IN_SETTINGS)

    def _save_models(self, story: Story) -> None:
        defaults = story.defaults
        for role, field in self.model_fields.items():
            model = field.text().strip() or None
            route = field.effective_route() if model else None
            if role == "story" and story.chat:
                # A chat always has a model of its own: blank is today's default.
                model = model or field.placeholderText() or None
                defaults.main_route = route
            elif route is None:
                defaults.model_routes.pop(role, None)
            else:
                defaults.model_routes[role] = route
            setattr(defaults, STORY_MODEL_FIELDS[role], model)

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
        self.private_notes = GrowingText(prompt.notes, lines=3, most=10)
        self.private_notes.setPlaceholderText(
            "Added to the rules in any mode, e.g. 'slow pacing, mostly dialogue, fade to black'."
        )
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
        if self._saved_prompts is not None:
            layout.addWidget(
                SavedPromptsButton(
                    self._saved_prompts,
                    self.chat_prompt,
                    in_memory=lambda: self.story.chat_keep == "memory",
                ),
                0,
                Qt.AlignLeft,
            )
        layout.addWidget(
            _hint(
                "Sent at the top of every request, exactly as written, and cached: a change "
                "costs one cache miss on the next message. Nothing else is added to it."
            )
        )
        layout.addWidget(QLabel("Tail"))
        self.chat_tail = GrowingText(self.story.chat_tail, lines=3, most=10)
        self.chat_tail.setPlaceholderText(
            "e.g. Reply in British English, in two short paragraphs at most."
        )
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
        self.picture_style = PictureStyleField(
            self.story.picture_style,
            self.story.image_style,
            texts=self.texts,
            from_picture=self._describe_style,
            picture=bool(self.story.style_picture),
        )
        form.addRow("Picture style", self.picture_style)
        form.addRow(
            _hint(
                "How every picture of this story looks: its portraits, its lore and places, and "
                "pictures of the story. Given to whoever writes a picture's prompt; never to "
                "the storyteller."
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
                "should happen next: those belong in Characters, Style and Opening."
            )
        )
        return widget

    def _notes_tab(self) -> QWidget:
        """The storyteller's notes (`Story.storyteller_notes`): what only the
        storyteller knows. Notes a model wrote (a draft's, a follow-up's) are
        its secrets from the author too, so they stay out of sight, a line
        saying how long they are in their place, until the author asks here
        to see them; left unread and unchanged, they are still unread the
        next time Setup opens."""
        story = self.story
        notes = (story.storyteller_notes or "").strip()
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(
            _hint(
                "The storyteller's secrets: the truth behind what the story has not shown "
                "yet, and what its people want and hide. The storyteller holds to them, and "
                "gives them up only as your character finds them out. Write your own, or "
                "leave what a draft wrote unread."
            )
        )
        # Unread: a line and two buttons where the notes would be.
        self.notes_unread = QWidget()
        unread = QVBoxLayout(self.notes_unread)
        unread.setContentsMargins(0, 0, 0, 0)
        count = len(notes.split())
        self.notes_unread_label = QLabel(
            NOTES_UNREAD.format(words=f"{count:,} word{'' if count == 1 else 's'}")
        )
        self.notes_unread_label.setWordWrap(True)
        unread.addWidget(self.notes_unread_label)
        self.show_notes = QPushButton(SHOW_NOTES)
        self.show_notes.setToolTip("Read them here, and change them if you like")
        self.show_notes.clicked.connect(self._show_notes)
        self.clear_notes = QPushButton(CLEAR_NOTES)
        self.clear_notes.setToolTip(
            "Take them out of the story without reading them. Nothing is saved until you "
            "press Save."
        )
        self.clear_notes.clicked.connect(self._clear_notes)
        buttons = QHBoxLayout()
        buttons.addWidget(self.show_notes)
        buttons.addWidget(self.clear_notes)
        buttons.addStretch(1)
        unread.addLayout(buttons)
        # A wrapped label given the spare room would draw its line mid-page.
        unread.addStretch(1)
        layout.addWidget(self.notes_unread, 1)

        self.notes = GrowingText(lines=6, most=24, fill=True)
        self.notes.setPlaceholderText(
            "What is really going on, as plain facts: what is under the floor, who took "
            "the money and why, what each of the central people wants and what they hide. "
            "Leave it empty for a story with no secrets."
        )
        layout.addWidget(self.notes, 1)
        self._notes_read = not (notes and story.storyteller_notes_by == "model")
        if self._notes_read:
            self.notes.setPlainText(notes)
        self.notes.setVisible(self._notes_read)
        self.notes_unread.setVisible(not self._notes_read)
        layout.addWidget(
            _hint(
                "Sent to the storyteller with every turn, after the world, and cached: a "
                "change costs one cache miss, takes hold from the next passage and needs no "
                "new playthrough. In play no other model is sent them. What your character "
                "already knows belongs in World or Lore; this is for what they would have to "
                "find out."
            )
        )
        return widget

    def _show_notes(self) -> None:
        """Show them: the author asked, here, to read what a model wrote."""
        self._reveal_notes((self.story.storyteller_notes or "").strip())

    def _clear_notes(self) -> None:
        """Clear them, unread: asked first, and only saved with Setup."""
        answer = QMessageBox.question(
            self,
            "Clear the storyteller's notes?",
            "The story's secrets go unread, and the storyteller is left to make up what "
            "the story leaves open, perhaps differently each time. You can write notes of "
            "your own in their place. Nothing is cleared until you save Setup.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self._reveal_notes("")

    def _reveal_notes(self, text: str) -> None:
        self._notes_read = True
        self.notes.setPlainText(text)
        self.notes_unread.hide()
        self.notes.show()
        self.notes.setFocus()

    def _write_notes(self, story: Story) -> None:
        """The notes as the box has them, when they differ from the story's:
        then they are the author's text. Unread, or read and left as they
        were, a model's notes stay a model's, and unread the next time."""
        if not self._notes_read:
            return
        text = self.notes.toPlainText().strip()
        if text != (story.storyteller_notes or "").strip():
            story.storyteller_notes = text or None
            story.storyteller_notes_by = "author"

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

    def _scene_tab(
        self, *, chosen: tuple[list[str], list[str], str | None] | None = None
    ) -> QWidget:
        """`chosen`: (pinned present, pinned absent, suggested), for the tab
        built again for a cast that changed, which keeps the location, time
        and situation boxes as they are typed."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        start = self.story.setup.starting_scene
        present, absent, suggested_id = chosen or (
            start.present_character_ids,
            self.story.setup.absent_at_start,
            self.story.setup.suggested_character_id,
        )

        form = QFormLayout()
        if chosen is None:
            self.location = line_edit(start.location or "")
            self.time_of_day = line_edit(start.time_of_day or "")
            self.situation = GrowingText(start.situation or "", lines=2, most=8)
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
                present,
                absent,
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
            index = self.suggested.findData(suggested_id)
            self.suggested.setCurrentIndex(max(index, 0))
            suggested.addRow("Suggested character to play", self.suggested)
            layout.addLayout(suggested)
        if not self.cast:
            layout.addWidget(
                _hint(
                    "This story has no cast yet. Once it has (the Characters tab, or the "
                    "cast board, People → Cast board…), choose here or when you start who is "
                    "there at the start and which character to suggest playing. Until "
                    "then the opening places everyone."
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
        """(pinned present, pinned not present); as they were with no cast to
        show. Whom the plot hides isn't shown, so their pins are kept as they
        were: read from the rows alone, Save dropped them."""
        setup = self.story.setup
        if self.presence is None:
            return list(setup.starting_scene.present_character_ids), list(setup.absent_at_start)
        present, absent = self.presence.pinned()
        hidden = self._hidden
        present += [i for i in setup.starting_scene.present_character_ids if i in hidden]
        absent += [i for i in setup.absent_at_start if i in hidden]
        # Someone taken out on the Characters tab meanwhile is still a row
        # here until the tab is shown again: never pinned back into the story.
        live = self._live_ids()
        return [i for i in present if i in live], [i for i in absent if i in live]

    def _live_ids(self) -> set[str]:
        return {character.id for character in self._live_cast}

    def character_removed(self, *, untouched: bool) -> None:
        """Someone taken out on the Characters tab (the lent card list): the
        story has let them go from whatever begins it, and Save writes nothing
        of them back. Cancel asks only about what was changed here: when
        nothing had been (`untouched`), what Setup opened with is what it
        holds now."""
        if untouched and self._opened is not None:
            self._opened = self._written()

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
        self._write(self.story)
        self.accept()

    def _write(self, story: Story) -> None:
        """The fields into `story`: the story itself on Save, a copy to tell
        what Save would write (`_written`)."""
        story.title = self.title.text().strip() or story.title
        if story.chat:
            story.setup.description = self.description.toPlainText().strip() or None
            story.chat_prompt = self.chat_prompt.toPlainText().strip()
            story.chat_tail = self.chat_tail.toPlainText().strip()
            self._save_models(story)
            return
        story.world_bible = self.world.toPlainText().strip() or None
        self._write_notes(story)
        setup = story.setup
        setup.description = self.description.toPlainText().strip() or None
        story.defaults.agency_mode = self.agency.currentData()
        story.defaults.world_activity = self.world_activity.currentData()
        story.style = self.style
        story.picture_style, story.image_style = self.picture_style.value()
        story.private_prompt.mode = self.private_mode.currentData()
        story.private_prompt.notes = self.private_notes.toPlainText().strip()
        story.private_prompt.custom = self.private_custom.toPlainText().strip()
        setup.opening_text = self.opening.toPlainText().strip()
        setup.opening_mode = "as_written" if self.as_written.isChecked() else "generate"
        setup.starting_scene = self._starting_scene()
        setup.absent_at_start = self._pinned()[1]
        if self.suggested is not None:
            suggested = self.suggested.currentData()
            setup.suggested_character_id = suggested if suggested in self._live_ids() else None
        self._save_models(story)
