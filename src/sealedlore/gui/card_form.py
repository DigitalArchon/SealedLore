"""One of the author's own characters, every field of it, in three forms for
the page's tabs (gui/collection_page.py):

- `CharacterForm`, the character as a story has them: the portrait and other
  pictures, name, aliases, summary, description, personality, voice and
  skills, laid out as a story's Cast page lays them out;
- `ChatForm`, what a SillyTavern card has for a chat with them: the scenario,
  the greetings, example messages and the card's own instructions;
- `AboutForm`, the card itself: who made it, its version and tags, its
  notes and sources, where it came from, and what it keeps for other apps.

Their lorebook and their card's other files have tabs of their own
(gui/lorebook_form.py, gui/card_files_form.py).

Each edits the character it is given in place and says so (`changed`); the
window keeps it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.fields import FormScroll, set_field_text
from sealedlore.gui.json_field import JsonField
from sealedlore.gui.portrait_view import PortraitView
from sealedlore.gui.ref_images import RefImageStrip, StoryPictures
from sealedlore.gui.sizing import GrowingText
from sealedlore.gui.skills_table import SkillsTable
from sealedlore.gui.start_page import when_played
from sealedlore.models.character import Character
from sealedlore.models.tavern import TavernFields

# The card's left column, the portrait and its other pictures, as the Cast
# page has it; wider here, with the whole window to fill.
PORTRAIT_COLUMN = 340
PORTRAIT_COLUMN_MIN = 200
MACROS_HINT = (
    "Write {{char}} for their name and {{user}} for whoever they talk to: the names go in "
    "when they join a story or a chat."
)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


def _tavern(character: Character) -> TavernFields:
    if character.tavern is None:
        character.tavern = TavernFields()
    return character.tavern


class CharacterForm(QWidget):
    """The character as a story has them (see the module)."""

    changed = Signal()
    # (what, picture id): "new", "again", "edit" or "set" on the portrait or
    # another picture, as the Cast page asks (gui/window_images.py).
    picture_requested = Signal(str, str)
    # Their pictures changed here (added, removed, labelled, made portrait).
    pictures_changed = Signal()

    def __init__(self, pictures: StoryPictures) -> None:
        super().__init__()
        self._pictures = pictures
        self._character: Character | None = None
        self._loading = False

        self.name = QLineEdit()
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText("comma-separated; the first is the card's nickname")
        self.canon = QLineEdit()
        self.canon.setPlaceholderText("e.g. 'Night of the Living Dead (1968)' — blank if original")
        self.summary = GrowingText(lines=2, most=8)
        self.summary.setPlaceholderText(
            "One or two lines, always in a story's prompt. Blank: the description's first sentence."
        )
        self.full_description = GrowingText(lines=6, most=30)
        self.full_description.setPlaceholderText(
            "Who they are: looks, history, what they want. A SillyTavern card's description."
        )
        self.personality = GrowingText(lines=2, most=10)
        self.personality.setPlaceholderText(
            "Optional: a few words, as a SillyTavern card has them."
        )
        self.voice_notes = GrowingText(lines=2, most=10)
        self.voice_notes.setPlaceholderText(
            "Speech patterns, verbal tics, register. Blank: a story takes their voice from the "
            "example messages (For chats)."
        )
        self.competence = GrowingText(lines=2, most=8)
        self.competence.setPlaceholderText(
            "Freeform: 'a veteran duellist, illiterate, hopeless at deception'"
        )
        self.skills = SkillsTable()
        self.skills.edited.connect(self._apply)
        add_skill = QPushButton("Add")
        add_skill.clicked.connect(lambda: self._character and self.skills.add_skill())
        remove_skill = QPushButton("Remove")
        remove_skill.clicked.connect(lambda: self.skills.remove_skill() and self._apply())
        skill_buttons = QHBoxLayout()
        skill_buttons.setContentsMargins(0, 0, 0, 0)
        skill_buttons.addWidget(add_skill)
        skill_buttons.addWidget(remove_skill)
        skill_buttons.addStretch(1)
        skills_box = QWidget()
        skills_layout = QVBoxLayout(skills_box)
        skills_layout.setContentsMargins(0, 0, 0, 0)
        skills_layout.addWidget(self.skills)
        skills_layout.addLayout(skill_buttons)
        self.player_available = QCheckBox("Available to play")
        self.player_available.setToolTip(
            "In a story they join: you can play them, and the storyteller voices them on the "
            "turns you don't. Off: they are the storyteller's."
        )
        self.author_only = QCheckBox("Never voiced by the storyteller")
        self.author_only.setToolTip(
            "In a story they join: yours on every turn, not only while you play them."
        )

        self.portrait = PortraitView(scene=False)
        self.others = RefImageStrip(self._pictures, "them", after_portrait=True, crop="portrait")
        self.portrait.draw_requested.connect(lambda: self._ask("new"))
        self.portrait.again_requested.connect(lambda: self._ask("again", first=True))
        self.portrait.edit_requested.connect(lambda: self._ask("edit", first=True))
        self.portrait.set_requested.connect(lambda: self._ask("set", first=True))
        self.portrait.add_requested.connect(self._add_portrait)
        self.portrait.remove_requested.connect(self._remove_portrait)
        self.portrait.labels_requested.connect(self._label_portrait)
        self.others.again_requested.connect(lambda ref: self._ask("again", ref))
        self.others.edit_requested.connect(lambda ref: self._ask("edit", ref))
        self.others.changed.connect(self._on_pictures_changed)

        for widget in (
            self.summary,
            self.full_description,
            self.personality,
            self.voice_notes,
            self.competence,
        ):
            widget.textChanged.connect(self._apply)
        for line in (self.name, self.aliases, self.canon):
            line.textChanged.connect(self._apply)
        self.player_available.toggled.connect(self._apply)
        self.author_only.toggled.connect(self._apply)

        pictures_column = QWidget()
        pictures_column.setMinimumWidth(PORTRAIT_COLUMN_MIN)
        pictures_column.setMaximumWidth(PORTRAIT_COLUMN)
        pictures_column.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        column = QVBoxLayout(pictures_column)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addWidget(self.portrait)
        column.addWidget(QLabel("Other pictures"))
        column.addWidget(self.others)
        column.addStretch(1)
        fields = QWidget()
        form = QFormLayout(fields)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Name", self.name)
        form.addRow("Aliases", self.aliases)
        form.addRow("From", self.canon)
        form.addRow("Summary", self.summary)
        form.addRow("Description", self.full_description)
        form.addRow("", _hint(MACROS_HINT))
        form.addRow("Personality", self.personality)
        form.addRow("Voice", self.voice_notes)
        form.addRow("Competence", self.competence)
        form.addRow("Skills", skills_box)
        form.addRow(self.player_available)
        form.addRow(self.author_only)
        host = QWidget()
        card = QHBoxLayout(host)
        card.setContentsMargins(0, 10, 8, 0)
        card.setSpacing(16)
        card.addWidget(pictures_column, 0, Qt.AlignTop)
        # At the top: beside a taller picture column the form's rows were
        # stretched apart.
        card.addWidget(fields, 1, Qt.AlignTop)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(FormScroll(host))

    # --- showing ------------------------------------------------------------------------

    @property
    def character(self) -> Character | None:
        return self._character

    def load(self, character: Character | None) -> None:
        self._character = character
        self.setEnabled(character is not None)
        self.others.set_refs(character.reference_images if character is not None else None)
        self.show_portrait()
        if character is None:
            return
        self._loading = True
        set_field_text(self.name, character.name)
        set_field_text(self.aliases, ", ".join(character.aliases))
        set_field_text(self.canon, character.canon or "")
        self.summary.setPlainText(character.summary or "")
        self.full_description.setPlainText(character.full_description or "")
        self.personality.setPlainText(character.personality or "")
        self.voice_notes.setPlainText(character.voice_notes or "")
        self.competence.setPlainText(character.competence.notes)
        self.skills.show_tiers(character.competence.tiers)
        self.player_available.setChecked(character.is_player_available)
        self.author_only.setChecked(character.author_only)
        self._loading = False

    def refresh_pictures(self) -> None:
        """Their pictures as they are now (one drawn has landed, say), the
        fields left as they are: the author may be typing in one."""
        character = self._character
        self.others.set_refs(character.reference_images if character is not None else None)
        self.show_portrait()

    def show_portrait(self) -> None:
        character = self._character
        first = character.reference_images[0] if character and character.reference_images else None
        store = self._pictures()
        data = store.read(first.file) if first is not None and store is not None else None
        caption = first.caption if first is not None and first.caption != "portrait" else ""
        if first is not None and (first.look or first.expression):
            labels = " · ".join(part for part in (first.look, first.expression) if part)
            caption = f"{caption}\n{labels}" if caption else labels
        self.portrait.show_portrait(data, caption, character.name if character else "")
        self.portrait.set_offered(character is not None)

    # --- editing ------------------------------------------------------------------------

    def _apply(self) -> None:
        character = self._character
        if self._loading or character is None:
            return
        character.name = self.name.text().strip() or character.name
        character.aliases = [a.strip() for a in self.aliases.text().split(",") if a.strip()]
        character.canon = self.canon.text().strip() or None
        character.summary = self.summary.toPlainText().strip() or None
        character.full_description = self.full_description.toPlainText().strip() or None
        character.personality = self.personality.toPlainText().strip() or None
        character.voice_notes = self.voice_notes.toPlainText().strip() or None
        character.competence.notes = self.competence.toPlainText().strip()
        character.competence.tiers = self.skills.tiers()
        character.is_player_available = self.player_available.isChecked()
        character.author_only = self.author_only.isChecked()
        self.changed.emit()

    def _ask(self, what: str, ref_id: str = "", *, first: bool = False) -> None:
        character = self._character
        if character is None:
            return
        if first and character.reference_images:
            ref_id = character.reference_images[0].id
        self.picture_requested.emit(what, ref_id)

    def _on_pictures_changed(self) -> None:
        self.show_portrait()
        self.pictures_changed.emit()

    def _add_portrait(self) -> None:
        if self.others.add_portrait():
            self._on_pictures_changed()

    def _remove_portrait(self) -> None:
        character = self._character
        if character is None or not character.reference_images:
            return
        shown = character.reference_images[0].id
        answer = QMessageBox.question(
            self,
            "Remove the portrait?",
            f"Take this portrait off {character.name}'s card? Their next picture becomes "
            "the portrait.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.others.remove_ref(shown)
            self._on_pictures_changed()

    def _label_portrait(self) -> None:
        character = self._character
        if character is None or not character.reference_images:
            return
        from sealedlore.gui.picture_labels import edit_labels

        if edit_labels(self, character.reference_images[0], character.reference_images):
            self._on_pictures_changed()


class GreetingsList(QWidget):
    """A card's alternate greetings: each listed by its first words, the one
    chosen written out below to change. `first` is the number the first one
    is called by (2 for the other greetings: Chat with them… calls the first
    message Greeting 1); `to_first` offers making one the first message."""

    changed = Signal()
    # (row): the greeting chosen, to be the first message instead.
    made_first = Signal(int)

    def __init__(self, *, first: int = 1, to_first: bool = False) -> None:
        super().__init__()
        self._greetings: list[str] = []
        self._loading = False
        self._first = first
        self.list = QListWidget()
        self.list.setObjectName("greetingsList")
        self.list.setMaximumHeight(140)
        self.list.currentRowChanged.connect(self._chosen)
        self.text = GrowingText(lines=4, most=16)
        self.text.setPlaceholderText("The greeting, as their first message.")
        self.text.textChanged.connect(self._edited)
        add = QPushButton("Add")
        add.clicked.connect(self._add)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        self.up_button = QPushButton("Move up")
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = QPushButton("Move down")
        self.down_button.clicked.connect(lambda: self._move(1))
        self.first_button = QPushButton("Make it the first message")
        self.first_button.setToolTip("It and the first message change places")
        self.first_button.clicked.connect(lambda: self.made_first.emit(self.list.currentRow()))
        self.first_button.setVisible(to_first)
        remove_key = QShortcut(QKeySequence(QKeySequence.Delete), self.list)
        remove_key.setContext(Qt.WidgetShortcut)
        remove_key.activated.connect(self._remove)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        for button in (
            add,
            self.remove_button,
            self.up_button,
            self.down_button,
            self.first_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.list)
        layout.addLayout(buttons)
        layout.addWidget(self.text)

    def set_greetings(self, greetings: list[str]) -> None:
        self._greetings = greetings
        self._fill()
        self._chosen(0 if greetings else -1)

    @staticmethod
    def _line(number: int, text: str) -> str:
        words = " ".join(text.split())
        return f"{number}. {words[:90] + '…' if len(words) > 90 else words or '(empty)'}"

    def _fill(self, row: int | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for number, text in enumerate(self._greetings, self._first):
            self.list.addItem(QListWidgetItem(self._line(number, text)))
        if row is not None and 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        self.list.setVisible(bool(self._greetings))

    def _chosen(self, row: int) -> None:
        self._loading = True
        if 0 <= row < len(self._greetings):
            self.list.blockSignals(True)
            self.list.setCurrentRow(row)
            self.list.blockSignals(False)
            self.text.setPlainText(self._greetings[row])
        else:
            self.text.setPlainText("")
        # Nothing to write in until one is chosen or added.
        chosen = 0 <= row < len(self._greetings)
        self.text.setVisible(chosen)
        self.remove_button.setEnabled(chosen)
        self.up_button.setEnabled(chosen and row > 0)
        self.down_button.setEnabled(chosen and row < len(self._greetings) - 1)
        self.first_button.setEnabled(chosen)
        self._loading = False

    def _edited(self) -> None:
        row = self.list.currentRow()
        if self._loading or not 0 <= row < len(self._greetings):
            return
        self._greetings[row] = self.text.toPlainText()
        item = self.list.item(row)
        if item is not None:
            item.setText(self._line(row + self._first, self._greetings[row]))
        self.changed.emit()

    def _add(self) -> None:
        self._greetings.append("")
        self._fill(len(self._greetings) - 1)
        self._chosen(len(self._greetings) - 1)
        self.text.setFocus()
        self.changed.emit()

    def _move(self, step: int) -> None:
        row = self.list.currentRow()
        to = row + step
        if not (0 <= row < len(self._greetings) and 0 <= to < len(self._greetings)):
            return
        greetings = self._greetings
        greetings[row], greetings[to] = greetings[to], greetings[row]
        self._fill(to)
        self._chosen(to)
        self.changed.emit()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if not 0 <= row < len(self._greetings) or not self.remove_button.isEnabled():
            return
        if self._greetings[row].strip():
            answer = QMessageBox.question(
                self,
                "Remove this greeting?",
                f"Take greeting {row + self._first} off their card?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self._greetings.pop(row)
        self._fill(min(row, len(self._greetings) - 1))
        self._chosen(min(row, len(self._greetings) - 1))
        self.changed.emit()


class ChatForm(QWidget):
    """What a SillyTavern card has for a chat with them (see the module)."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._character: Character | None = None
        self._loading = False
        self.scenario = GrowingText(lines=3, most=16)
        self.scenario.setPlaceholderText(
            "Where and when a chat with them is set, and what is going on."
        )
        self.first_mes = GrowingText(lines=4, most=20)
        self.first_mes.setPlaceholderText("Their first message in a chat: how it opens.")
        self.greetings = GreetingsList(first=2, to_first=True)
        self.greetings.changed.connect(self._apply)
        self.greetings.made_first.connect(self._swap_first)
        # SillyTavern's group chats only: never used here, kept for the card.
        self.group_greetings = GreetingsList()
        self.group_greetings.changed.connect(self._apply)
        self.mes_example = GrowingText(lines=4, most=20)
        self.mes_example.setPlaceholderText(
            "<START>\n{{user}}: Hello?\n{{char}}: Hands where I can see them."
        )
        self.system_prompt = GrowingText(lines=3, most=12)
        self.system_prompt.setPlaceholderText(
            "The card's own instructions for a chat. Blank: your own system prompt."
        )
        self.post_history = GrowingText(lines=2, most=10)
        self.post_history.setPlaceholderText(
            "Said after the conversation in every request, where it is weighed most."
        )
        for widget in (
            self.scenario,
            self.first_mes,
            self.mes_example,
            self.system_prompt,
            self.post_history,
        ):
            widget.textChanged.connect(self._apply)
        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(0, 10, 8, 0)
        form.addRow(
            _hint(
                "A SillyTavern card's fields for a one-to-one chat. A story has its own world, "
                "opening and rules, and never sends these; a simple chat with them starts from "
                "them. " + MACROS_HINT
            )
        )
        form.addRow("Scenario", self.scenario)
        form.addRow("First message", self.first_mes)
        form.addRow("Other greetings", self.greetings)
        form.addRow("Group chat greetings", self.group_greetings)
        form.addRow("", _hint("SillyTavern's greetings for its group chats only."))
        form.addRow("Example messages", self.mes_example)
        form.addRow("", _hint("<START> on its own line between one exchange and the next."))
        form.addRow("System prompt", self.system_prompt)
        form.addRow("After the chat", self.post_history)
        form.addRow("", _hint("SillyTavern's post-history instructions."))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(FormScroll(content))

    def load(self, character: Character | None) -> None:
        self._character = character
        self.setEnabled(character is not None)
        if character is None:
            return
        tavern = _tavern(character)
        self._loading = True
        self.scenario.setPlainText(tavern.scenario)
        self.first_mes.setPlainText(tavern.first_mes)
        self.greetings.set_greetings(tavern.alternate_greetings)
        self.group_greetings.set_greetings(tavern.group_only_greetings)
        self.mes_example.setPlainText(tavern.mes_example)
        self.system_prompt.setPlainText(tavern.system_prompt)
        self.post_history.setPlainText(tavern.post_history_instructions)
        self._loading = False

    def _apply(self) -> None:
        character = self._character
        if self._loading or character is None:
            return
        tavern = _tavern(character)
        tavern.scenario = self.scenario.toPlainText()
        tavern.first_mes = self.first_mes.toPlainText()
        # The list edits the card's own (GreetingsList keeps it).
        tavern.mes_example = self.mes_example.toPlainText()
        tavern.system_prompt = self.system_prompt.toPlainText()
        tavern.post_history_instructions = self.post_history.toPlainText()
        self.changed.emit()

    def _swap_first(self, row: int) -> None:
        """Greeting `row` and the first message change places."""
        tavern = _tavern(self._character)
        others = tavern.alternate_greetings
        if not 0 <= row < len(others):
            return
        tavern.first_mes, others[row] = others[row], tavern.first_mes
        self._loading = True
        self.first_mes.setPlainText(tavern.first_mes)
        self._loading = False
        self.greetings.set_greetings(others)
        self.greetings.list.setCurrentRow(row)
        self.changed.emit()


class AboutForm(QWidget):
    """The card itself (see the module)."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._character: Character | None = None
        self._loading = False
        self.creator = QLineEdit()
        self.version = QLineEdit()
        self.version.setPlaceholderText("The card's own version, as its maker numbered it")
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("comma-separated: Your characters can be listed by them")
        self.creator_notes = GrowingText(lines=3, most=16)
        self.creator_notes.setPlaceholderText("What the card's maker says of it.")
        self.source = GrowingText(lines=2, most=8)
        self.source.setPlaceholderText(
            "Where the card is from, one a line: an address or an id (the V3 card's source)."
        )
        for line in (self.creator, self.version, self.tags):
            line.textChanged.connect(self._apply)
        for text in (self.creator_notes, self.source):
            text.textChanged.connect(self._apply)
        # The rest of the card, for other apps: nothing on it out of reach.
        self.notes_elsewhere = JsonField(
            "Maker's notes in other languages (JSON)",
            tip='Each language\'s code and its notes: {"de": "…", "ja": "…"}',
        )
        self.notes_elsewhere.edited.connect(self._notes_elsewhere_edited)
        self.extensions = JsonField(
            "Other apps' settings (JSON)",
            tip="What SillyTavern, Chub, RisuAI and others keep on the card (`extensions`). "
            "SealedLore's own are added when it is exported.",
        )
        self.extensions.edited.connect(self._extensions_edited)
        self.came_from = QLabel()
        self.came_from.setWordWrap(True)
        self.came_from.setTextFormat(Qt.PlainText)
        self.dates = QLabel()
        self.dates.setObjectName("hintLabel")
        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(0, 10, 8, 0)
        form.addRow("Made by", self.creator)
        form.addRow("Maker's version", self.version)
        form.addRow("Tags", self.tags)
        form.addRow("Maker's notes", self.creator_notes)
        form.addRow("Sources", self.source)
        form.addRow("Came from", self.came_from)
        form.addRow("", self.dates)
        form.addRow("", self.notes_elsewhere)
        form.addRow("", self.extensions)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(FormScroll(content))

    def load(self, character: Character | None) -> None:
        self._character = character
        self.setEnabled(character is not None)
        if character is None:
            return
        tavern = _tavern(character)
        self._loading = True
        set_field_text(self.creator, tavern.creator)
        set_field_text(self.version, tavern.character_version)
        set_field_text(self.tags, ", ".join(tavern.tags))
        self.creator_notes.setPlainText(tavern.creator_notes)
        self.source.setPlainText("\n".join(tavern.source))
        self.notes_elsewhere.set_value(dict(tavern.creator_notes_multilingual))
        self.extensions.set_value(dict(tavern.extensions))
        self._loading = False
        kept = character.collection
        self.came_from.setText((kept.came_from if kept else "") or "Made here")
        if kept is not None:
            added, changed = when_played(kept.added_at), when_played(kept.changed_at)
            self.dates.setText(f"Added {added} · changed {changed}")

    def _apply(self) -> None:
        character = self._character
        if self._loading or character is None:
            return
        tavern = _tavern(character)
        tavern.creator = self.creator.text().strip()
        tavern.character_version = self.version.text().strip()
        tavern.tags = [tag.strip() for tag in self.tags.text().split(",") if tag.strip()]
        tavern.creator_notes = self.creator_notes.toPlainText()
        tavern.source = [
            line.strip() for line in self.source.toPlainText().splitlines() if line.strip()
        ]
        self.changed.emit()

    def _notes_elsewhere_edited(self, value: dict) -> None:
        if self._character is not None:
            _tavern(self._character).creator_notes_multilingual = value
            self.changed.emit()

    def _extensions_edited(self, value: dict) -> None:
        if self._character is not None:
            _tavern(self._character).extensions = value
            self.changed.emit()
