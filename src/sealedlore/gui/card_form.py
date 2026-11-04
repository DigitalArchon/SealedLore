"""One of the author's own characters, every field of it, in three forms for
the page's tabs (gui/collection_page.py):

- `CharacterForm`, the character as a story has them: the portrait and other
  pictures, name, aliases, summary, description, personality, voice and
  skills, laid out as a story's Cast page lays them out;
- `ChatForm`, what a SillyTavern card has for a chat with them: the scenario,
  the greetings, example messages and the card's own instructions;
- `AboutForm`, the card itself: who made it, its version and tags, its
  notes, its own lorebook, where it came from.

Each edits the character it is given in place and says so (`changed`); the
window keeps it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.fields import FormScroll, set_field_text
from sealedlore.gui.portrait_view import PortraitView
from sealedlore.gui.ref_images import RefImageStrip, StoryPictures
from sealedlore.gui.sizing import GrowingText
from sealedlore.gui.skills_table import SkillsTable
from sealedlore.gui.start_page import when_played
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.tavern import TavernFields
from sealedlore.storage.collection import files_said
from sealedlore.storage.tavern_lore import lore_from_book

if TYPE_CHECKING:
    from sealedlore.gui.cast_board import Thumbs

# The card's left column, the portrait and its other pictures, as the Cast
# page has it; wider here, with the whole window to fill.
PORTRAIT_COLUMN = 340
# A card's other files on the About tab, as thumbnails.
FILE_ICON = 72
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
    chosen written out below to change."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._greetings: list[str] = []
        self._loading = False
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
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(add)
        buttons.addWidget(self.remove_button)
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
        for number, text in enumerate(self._greetings, 1):
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
        self.text.setVisible(0 <= row < len(self._greetings))
        self.remove_button.setEnabled(0 <= row < len(self._greetings))
        self._loading = False

    def _edited(self) -> None:
        row = self.list.currentRow()
        if self._loading or not 0 <= row < len(self._greetings):
            return
        self._greetings[row] = self.text.toPlainText()
        item = self.list.item(row)
        if item is not None:
            item.setText(self._line(row + 1, self._greetings[row]))
        self.changed.emit()

    def _add(self) -> None:
        self._greetings.append("")
        self._fill(len(self._greetings) - 1)
        self._chosen(len(self._greetings) - 1)
        self.text.setFocus()
        self.changed.emit()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if not 0 <= row < len(self._greetings):
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
        self.greetings = GreetingsList()
        self.greetings.changed.connect(self._apply)
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


class AboutForm(QWidget):
    """The card itself (see the module)."""

    changed = Signal()
    # "import", "export" or "remove": the card's own lorebook.
    lorebook_requested = Signal(str)
    # "save", "picture" or "whole", and which of the card's files (its place
    # in `tavern.assets`): saved where the author says, made one of their
    # pictures, or a picture seen full size.
    file_requested = Signal(str, int)

    def __init__(self, thumbs: Thumbs | None = None) -> None:
        super().__init__()
        self._character: Character | None = None
        self._loading = False
        self._thumbs = thumbs
        self.creator = QLineEdit()
        self.version = QLineEdit()
        self.version.setPlaceholderText("The card's own version, as its maker numbered it")
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("comma-separated: Your characters can be listed by them")
        self.creator_notes = GrowingText(lines=3, most=16)
        self.creator_notes.setPlaceholderText("What the card's maker says of it.")
        for line in (self.creator, self.version, self.tags):
            line.textChanged.connect(self._apply)
        self.creator_notes.textChanged.connect(self._apply)
        self.book = QLabel()
        self.book.setWordWrap(True)
        self.book_import = QPushButton("Import a lorebook…")
        self.book_import.clicked.connect(lambda: self.lorebook_requested.emit("import"))
        self.book_export = QPushButton("Export…")
        self.book_export.clicked.connect(lambda: self.lorebook_requested.emit("export"))
        self.book_remove = QPushButton("Remove")
        self.book_remove.clicked.connect(lambda: self.lorebook_requested.emit("remove"))
        book_buttons = QHBoxLayout()
        book_buttons.setContentsMargins(0, 0, 0, 0)
        for button in (self.book_import, self.book_export, self.book_remove):
            book_buttons.addWidget(button)
        book_buttons.addStretch(1)
        # Its entries, read as a story will take them (storage/tavern_lore.py),
        # and the chosen one's keywords and text: the count alone said there
        # was lore and showed none of it (the author, Nov 4 2026).
        self._book: list[LoreEntry] = []
        self.book_list = QListWidget()
        self.book_list.setObjectName("cardBook")
        self.book_list.currentRowChanged.connect(self._show_entry)
        self.book_text = GrowingText(lines=3, most=16)
        self.book_text.setReadOnly(True)
        book_box = QWidget()
        # As tall as it is, as the files below: spare height went to the label.
        book_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.book.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        book_layout = QVBoxLayout(book_box)
        book_layout.setContentsMargins(0, 0, 0, 0)
        book_layout.addWidget(self.book)
        book_layout.addWidget(self.book_list)
        book_layout.addWidget(self.book_text)
        book_layout.addLayout(book_buttons)
        self.files_said = QLabel()
        self.files_said.setWordWrap(True)
        self.files = QListWidget()
        self.files.setObjectName("cardFiles")
        self.files.setViewMode(QListView.IconMode)
        self.files.setMovement(QListView.Static)
        self.files.setResizeMode(QListView.Adjust)
        self.files.setWrapping(True)
        self.files.setIconSize(QSize(FILE_ICON, FILE_ICON))
        self.files.setGridSize(QSize(FILE_ICON + 28, FILE_ICON + 36))
        self.files.setFixedHeight(2 * (FILE_ICON + 36) + 12)
        self.files.setSelectionMode(QListWidget.SingleSelection)
        self.files.itemSelectionChanged.connect(self._sync_file_buttons)
        # A click on a picture shows it full size, and leaves it chosen for
        # the buttons below.
        self.files.itemClicked.connect(self._file_clicked)
        self.file_save = QPushButton("Save as…")
        self.file_save.setToolTip("Save this file of the card's where you choose")
        self.file_save.clicked.connect(lambda: self._file_action("save"))
        self.file_picture = QPushButton("Add to their pictures")
        self.file_picture.setToolTip(
            "A copy among their pictures (Card and Pictures tabs), to use as any of theirs; "
            "the card keeps its own"
        )
        self.file_picture.clicked.connect(lambda: self._file_action("picture"))
        file_buttons = QHBoxLayout()
        file_buttons.setContentsMargins(0, 0, 0, 0)
        for button in (self.file_save, self.file_picture):
            file_buttons.addWidget(button)
        file_buttons.addStretch(1)
        files_box = QWidget()
        # As tall as it is: spare height in the page went to its label.
        files_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.files_said.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        files_layout = QVBoxLayout(files_box)
        files_layout.setContentsMargins(0, 0, 0, 0)
        files_layout.addWidget(self.files_said)
        files_layout.addWidget(self.files)
        files_layout.addLayout(file_buttons)
        self.came_from = QLabel()
        self.came_from.setWordWrap(True)
        self.came_from.setTextFormat(Qt.PlainText)
        self.dates = QLabel()
        self.dates.setObjectName("hintLabel")
        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(0, 10, 8, 0)
        form.addRow("Made by", self.creator)
        form.addRow("Card version", self.version)
        form.addRow("Tags", self.tags)
        form.addRow("Maker's notes", self.creator_notes)
        form.addRow("Lorebook", book_box)
        form.addRow("Files on the card", files_box)
        form.addRow("Came from", self.came_from)
        form.addRow("", self.dates)
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
        self._loading = False
        self.show_book()
        self.show_files()
        kept = character.collection
        self.came_from.setText((kept.came_from if kept else "") or "Made here")
        if kept is not None:
            added, changed = when_played(kept.added_at), when_played(kept.changed_at)
            self.dates.setText(f"Added {added} · changed {changed}")

    def show_book(self) -> None:
        character = self._character
        entries = _tavern(character).book_entries if character is not None else 0
        self.book.setText(
            f"{entries} entr{'y' if entries == 1 else 'ies'}: offered as lore when they join a "
            "story."
            if entries
            else "None. A card's own lore goes into a story's lore when they join it."
        )
        self.book_export.setEnabled(bool(entries))
        self.book_remove.setEnabled(bool(entries))
        book = _tavern(character).character_book if character is not None else None
        self._book = lore_from_book(book).entries if book else []
        self.book_list.clear()
        for entry in self._book:
            marks = (["always sent"] if entry.always_on else []) + (
                ["switched off"] if not entry.enabled else []
            )
            item = QListWidgetItem(" · ".join([entry.title, *marks]))
            item.setToolTip(", ".join(entry.keywords) or "No keywords")
            self.book_list.addItem(item)
        shown = bool(self._book)
        self.book_list.setVisible(shown)
        self.book_text.setVisible(shown)
        if shown:
            rows = min(len(self._book), 6)
            frame = 2 * self.book_list.frameWidth()
            self.book_list.setFixedHeight(rows * self.book_list.sizeHintForRow(0) + frame + 4)
            self.book_list.setCurrentRow(0)
        else:
            self.book_text.clear()

    def _show_entry(self, row: int) -> None:
        entry = self._book[row] if 0 <= row < len(self._book) else None
        if entry is None:
            self.book_text.clear()
            return
        keywords = ", ".join(entry.keywords) or "none (sent only when chosen as relevant)"
        if entry.always_on:
            keywords = "always sent"
        self.book_text.setPlainText(f"Keywords: {keywords}\n\n{entry.content}")

    def show_files(self) -> None:
        """The card's other files (a background, pictures its greetings show,
        sounds), each by its name, a picture as a thumbnail."""
        character = self._character
        assets = _tavern(character).assets if character is not None else []
        self.files.clear()
        self.files_said.setText(
            files_said(assets)
            if assets
            else "None besides their pictures. A card's background, the pictures its "
            "greetings show and its sounds are kept here, and go out with it as a CHARX."
        )
        style = self.style()
        for index, asset in enumerate(assets):
            item = QListWidgetItem(asset.name or asset.type)
            where = asset.uri if not asset.held else f"{asset.ext.upper()} file"
            tip = f"{asset.name or '(no name)'} · {asset.type} · {where}"
            if asset.held and asset.picture:
                tip += "\nClick to see it full size."
            item.setToolTip(tip)
            item.setData(Qt.UserRole, index)
            if not asset.held:
                item.setIcon(style.standardIcon(QStyle.SP_DriveNetIcon))
            elif not asset.picture:
                item.setIcon(style.standardIcon(QStyle.SP_FileIcon))
            self.files.addItem(item)
        self.files.setVisible(bool(assets))
        self.refresh_file_icons()
        self._sync_file_buttons()

    def refresh_file_icons(self) -> None:
        """Thumbnails as they are read (a card may have hundreds)."""
        character = self._character
        if self._thumbs is None or character is None:
            return
        assets = _tavern(character).assets
        for row in range(self.files.count()):
            item = self.files.item(row)
            index = item.data(Qt.UserRole)
            asset = assets[index] if 0 <= index < len(assets) else None
            if asset is None or not asset.held or not asset.picture:
                continue
            pixmap = self._thumbs.get(asset.file, FILE_ICON, FILE_ICON)
            if pixmap is not None:
                item.setIcon(QIcon(pixmap))

    def _file_clicked(self, item: QListWidgetItem) -> None:
        character = self._character
        assets = _tavern(character).assets if character is not None else []
        index = item.data(Qt.UserRole)
        if 0 <= index < len(assets) and assets[index].held and assets[index].picture:
            self.file_requested.emit("whole", index)

    def _chosen_file(self) -> int | None:
        item = self.files.currentItem()
        return item.data(Qt.UserRole) if item is not None and item.isSelected() else None

    def _sync_file_buttons(self) -> None:
        character = self._character
        index = self._chosen_file()
        assets = _tavern(character).assets if character is not None else []
        asset = assets[index] if index is not None and 0 <= index < len(assets) else None
        self.file_save.setEnabled(asset is not None and asset.held)
        self.file_picture.setEnabled(asset is not None and asset.held and asset.picture)
        self.file_save.setVisible(bool(assets))
        self.file_picture.setVisible(bool(assets))

    def _file_action(self, action: str) -> None:
        index = self._chosen_file()
        if index is not None:
            self.file_requested.emit(action, index)

    def _apply(self) -> None:
        character = self._character
        if self._loading or character is None:
            return
        tavern = _tavern(character)
        tavern.creator = self.creator.text().strip()
        tavern.character_version = self.version.text().strip()
        tavern.tags = [tag.strip() for tag in self.tags.text().split(",") if tag.strip()]
        tavern.creator_notes = self.creator_notes.toPlainText()
        self.changed.emit()
