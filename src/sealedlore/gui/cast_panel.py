"""Cast management.

Deliberately lightweight: the author never enters numeric stats. Competence is
freeform prose, plus optional per-skill tiers (§4.1) — the author's ruling on
a character's ceiling, which dice mode turns into odds. Nothing here is
required; with no tiers the prose carries it.

Two tiers share one list. The cast is the author's: on the roster, holdable,
in the cached system block. Supporting characters are people the story
introduced, carded for continuity and shown to the model only when mentioned.
The model suggests them; nothing joins either tier without the author's say.

Laid out as Your characters is (the author, Nov 7 2026: the People page, the
Cast page and the cast board were three places for one thing): the list down
the left, and the character shown in tabs on the right, the card here and
their pictures the cast board's sheet (added by the window's cast board page,
gui/story_cast_page.py, which holds the panel in the whole window). Lent to
the new story guide and Setup, it is the list and the card alone.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.card_condense import fingerprint
from sealedlore.engine.look_sheet import keep_order
from sealedlore.gui import text_size, theme
from sealedlore.gui.banners import CastRoomBanner
from sealedlore.gui.cast_board import SAVE_LABEL
from sealedlore.gui.cast_room import CastRoom, banner_text, card_note, supporting_note
from sealedlore.gui.fields import FormScroll, set_field_text
from sealedlore.gui.lore_cast_dialog import FILL_LABEL
from sealedlore.gui.portrait_view import PortraitView
from sealedlore.gui.ref_images import CardIcons, RefImageStrip, StoryPictures
from sealedlore.gui.sizing import GrowingText
from sealedlore.gui.skills_table import SkillsTable
from sealedlore.gui.tavern_dialog import (
    CARD_EXPORT_LABEL,
    CARD_IMPORT_LABEL,
    CARD_LINK_LABEL,
    EXPRESSIONS_LABEL,
    TAVERN_LABEL,
)
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.models.character import Character
from sealedlore.models.supporting import CharacterSuggestion

HEADER_ROLE = Qt.UserRole + 1
# The list's first entry in the whole window: everyone's pictures.
EVERYONE_ROLE = Qt.UserRole + 2
EVERYONE_LABEL = "Everyone"
COLLECTION_ADD_LABEL = "From Your characters…"
CONDENSE_LABEL = "Condense this card"
CONDENSE_AGAIN_LABEL = "Write it again"
MODEL_MARK = "  ✦"
CARD_TAB = "Card"
# The width of the card's left column: the portrait, its other pictures.
PORTRAIT_COLUMN = 300
PORTRAIT_COLUMN_MIN = 160
# The portraits beside the names in the list.
LIST_ICON = 40
# The list's column at 100% text, as Your characters' (gui/collection_page.py).
LEFT_WIDTH = 320
# A simple chat's characters are for their pictures alone (the author, Nov 7
# 2026): their card says so, and shows only what drawing them reads.
CHAT_HINT = (
    "In a chat, characters are here for their pictures alone: nothing on their card is "
    "sent to the chat's model. Their description is what their portrait is drawn from."
)


class CastPanel(QWidget):
    changed = Signal()
    # (character id, the name before): the rosters hold supporting cards by name.
    renamed = Signal(str, str)
    promote_requested = Signal(str)
    demote_requested = Signal(str)
    accept_requested = Signal(str, bool)
    dismiss_requested = Signal(str)
    scan_requested = Signal()
    infer_requested = Signal(str)
    # (character id, picture id or "", what): a picture drawn for the card:
    # "new" (a portrait), "again" or "edit" (of that picture), "scene".
    picture_requested = Signal(str, str, str)
    # (character id): a portrait chosen from a file went first on the card.
    portrait_added = Signal(str)
    # Cards for a published world's people, written by a model.
    lore_fill_requested = Signal()
    # SillyTavern character cards (storage/tavern_cards.py): one read into
    # this story, or (character id) one written out.
    card_import_requested = Signal()
    card_link_requested = Signal()
    # (character id) a SillyTavern expression pack for this character.
    expressions_requested = Signal(str)
    # The cast board in the whole window, on this character ("": everyone).
    board_requested = Signal(str)
    card_export_requested = Signal(str)
    # From the author's own characters (gui/window_card_link.py): add some,
    # save some of this story's there (the one shown ticked, "" for none), and
    # a character's line about their card (its button, by character id).
    collection_add_requested = Signal()
    collection_save_requested = Signal(str)
    link_requested = Signal(str)
    # Another character is shown ("" for none).
    current_changed = Signal(str)
    # (character id, or "" for every card waiting) a condensed card written.
    condense_requested = Signal(str)
    # Settings → Context, where the cast's room is set.
    cast_room_requested = Signal()

    def __init__(self, pictures: StoryPictures | None = None) -> None:
        super().__init__()
        self._pictures: StoryPictures = pictures or (lambda: None)
        self._cast: list[Character] = []
        self._hidden: set[str] = set()
        self._supporting: list[Character] = []
        self._suggestions: list[CharacterSuggestion] = []
        self._current: Character | None = None
        self._loading = False
        # How the cast fits its room in the prompt (gui/cast_room.py).
        self._room: CastRoom | None = None

        self.list = QListWidget()
        self.list.setObjectName("castList")
        # Each name beside its portrait, small.
        self.list.setIconSize(QSize(LIST_ICON, LIST_ICON))
        self.list.setMinimumHeight(LIST_ICON * 3)
        self._icons = CardIcons(self._pictures, LIST_ICON)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.currentItemChanged.connect(self._on_selection_changed)

        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add_character)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove_character)
        self.move_button = QPushButton("Move to supporting")
        self.move_button.clicked.connect(self._move_character)

        # Each its own group: the list's column is narrow, and in pairs they
        # were cut short at its edge.
        buttons = WrapRow(spacing=6, group_spacing=6)
        for button in (add_button, self.remove_button, self.move_button):
            buttons.add_group([button])
        # Its own line: a fourth button abreast widened the Inspector.
        self.fill_button = QPushButton(FILL_LABEL)
        self.fill_button.setToolTip(
            "For a story set in a world that already exists: the model writes cards for "
            "its characters, and you change or remove any of them"
        )
        self.fill_button.clicked.connect(self.lore_fill_requested)
        self.tavern_button = QToolButton()
        self.tavern_button.setText(TAVERN_LABEL)
        self.tavern_button.setToolTip(
            "Character cards as SillyTavern, Chub and JanitorAI share them: bring one into "
            "this story, or save a character as one"
        )
        self.tavern_button.setPopupMode(QToolButton.InstantPopup)
        tavern_menu = QMenu(self.tavern_button)
        tavern_menu.addAction(CARD_IMPORT_LABEL).triggered.connect(
            lambda _checked=False: self.card_import_requested.emit()
        )
        tavern_menu.addAction(CARD_LINK_LABEL).triggered.connect(
            lambda _checked=False: self.card_link_requested.emit()
        )
        self.card_export_action = tavern_menu.addAction(CARD_EXPORT_LABEL)
        self.card_export_action.triggered.connect(self._export_card)
        self.expressions_action = tavern_menu.addAction(EXPRESSIONS_LABEL)
        self.expressions_action.setToolTip(
            "SillyTavern's expression sprites for this character (a zip of pictures named "
            "by expression): added to their pictures, labelled"
        )
        self.expressions_action.triggered.connect(
            lambda _checked=False: self.expressions_requested.emit(
                self._current.id if self._current is not None else ""
            )
        )
        self.tavern_button.setMenu(tavern_menu)
        # The author's own characters, kept outside any story.
        self.collection_button = QPushButton(COLLECTION_ADD_LABEL)
        self.collection_button.setToolTip(
            "Characters you keep in Your characters (File → Your characters…): each comes "
            "with their pictures, and what this story changes of them can go back to their card"
        )
        self.collection_button.clicked.connect(self.collection_add_requested)
        # The other way: this story's characters kept there, one or everyone.
        # Before, only an unexplained button over one character's name did it.
        self.save_button = QPushButton(SAVE_LABEL)
        self.save_button.setToolTip(
            "Keep any of this story's characters, or all of them, in Your characters with "
            "their pictures, to join any story; each stays linked to this story's"
        )
        self.save_button.clicked.connect(
            lambda: self.collection_save_requested.emit(self._current.id if self._current else "")
        )
        # One line while the panel has the room, wrapping when it is narrow:
        # a line of its own took the list's room for names.
        fill_row = WrapRow(spacing=6, group_spacing=6)
        for button in (
            self.collection_button,
            self.save_button,
            self.fill_button,
            self.tavern_button,
        ):
            fill_row.add_group([button])

        # The list down the left, as tall as the page, its buttons and the
        # suggestions under it; the character on the right, in tabs (the
        # card; the window adds their pictures). In the Inspector the list
        # sat over the card, with room for a few names (the author, Oct 2026).
        side = QWidget()
        side.setObjectName("castSide")
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(6)
        side_layout.addWidget(self.list, 1)
        side_layout.addLayout(buttons)
        side_layout.addLayout(fill_row)
        self.suggestions_box = self._build_suggestions()
        side_layout.addWidget(self.suggestions_box)
        self.side = side
        self._fit_side()

        self.tabs = QTabWidget()
        self.tabs.setObjectName("castTabs")
        self.tabs.setDocumentMode(True)
        # Lent to the guide or Setup it is the card alone: no tab to choose.
        self.tabs.setTabBarAutoHide(True)
        self.tabs.addTab(self._build_editor(), CARD_TAB)
        self.tabs.setTabToolTip(
            0, "What the storyteller is told of them, who voices them, and their portrait"
        )

        # The cast over its room in the prompt: said above everything.
        self.room_banner = CastRoomBanner()
        self.room_banner.room_requested.connect(self.cast_room_requested)
        self.room_banner.condense_requested.connect(lambda: self.condense_requested.emit(""))

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        body.addWidget(side)
        body.addWidget(self.tabs, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.room_banner)
        layout.addLayout(body, 1)
        # Everyone's pictures as the list's first entry (the window's page).
        self._everyone = False
        # A simple chat's characters, for their pictures alone (`set_chat_mode`).
        self._chat = False

    def _build_editor(self) -> QWidget:
        self.name = QLineEdit()
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText("comma-separated")
        self.summary = GrowingText(lines=2, most=8)
        self.summary.setPlaceholderText("One or two lines. Always included in the prompt.")
        self.full_description = GrowingText(lines=4, most=18)
        self.full_description.setPlaceholderText("Longer sheet. Included when the budget allows.")
        # A SillyTavern card's own field, told with the description.
        self.personality = GrowingText(lines=1, most=6)
        self.personality.setPlaceholderText(
            "Optional: a few words, as a SillyTavern card has them. Told with the description."
        )
        self.voice_notes = GrowingText(lines=2, most=8)
        self.voice_notes.setPlaceholderText("Speech patterns, verbal tics, register.")
        self.competence = GrowingText(lines=2, most=8)
        self.competence.setPlaceholderText(
            "Freeform: 'a veteran duellist, illiterate, hopeless at deception'"
        )
        self.player_available = QCheckBox("Available to play")
        self.player_available.setToolTip(
            "On: you can hold this character, and the storyteller voices them on the turns "
            "you don't.\nOff: they are the storyteller's — it writes their speech, thoughts "
            "and choices in full, and they no longer appear in the Speaking as list."
        )
        self.author_only = QCheckBox("Never voiced by the storyteller")
        self.author_only.setToolTip(
            "Yours on every turn, not only while you hold them. The storyteller is told "
            "never to write their speech, thoughts or decisions."
        )
        self.keep_whole = QCheckBox("Always send the whole card")
        self.keep_whole.setToolTip(
            "For a main character. When the cast's cards don't fit their room in the "
            "prompt (Settings → Context), the others are condensed first and this one "
            "never is."
        )
        # The card in fewer words, sent in its place while the cast's cards
        # don't fit their room (engine/cast_fit.py); the author may edit it.
        self.condensed = GrowingText(lines=3, most=14)
        self.condensed.setPlaceholderText(
            "Written by the model when the cast's cards don't fit their room in the prompt."
        )
        self.condensed_note = QLabel()
        self.condensed_note.setObjectName("hintLabel")
        self.condensed_note.setWordWrap(True)
        self.condense_button = QPushButton(CONDENSE_AGAIN_LABEL)
        self.condense_button.setToolTip(
            "Have the model condense this card from what it says now, in the background"
        )
        self.condense_button.clicked.connect(
            lambda: self._current and self.condense_requested.emit(self._current.id)
        )
        self.condensed_box = QWidget()
        condensed_layout = QVBoxLayout(self.condensed_box)
        condensed_layout.setContentsMargins(0, 0, 0, 0)
        condensed_layout.setSpacing(4)
        condensed_layout.addWidget(self.condensed_note)
        condensed_layout.addWidget(self.condensed)
        condensed_layout.addWidget(self.condense_button, 0, Qt.AlignLeft)

        self.skills = SkillsTable()
        self.skills.edited.connect(self._apply_edits)
        add_skill = QPushButton("Add")
        add_skill.clicked.connect(self._add_skill)
        self.remove_skill = QPushButton("Remove")
        self.remove_skill.clicked.connect(self._remove_skill)
        self.suggest_skills = QPushButton("Suggest skills")
        self.suggest_skills.setToolTip(
            "Ask the model to draft skills and tiers from this sheet; you accept or discard them"
        )
        self.suggest_skills.clicked.connect(
            lambda: self._current and self.infer_requested.emit(self._current.id)
        )
        skill_buttons = QHBoxLayout()
        skill_buttons.setContentsMargins(0, 0, 0, 0)
        skill_buttons.addWidget(add_skill)
        skill_buttons.addWidget(self.remove_skill)
        skill_buttons.addStretch(1)
        skills_box = QWidget()
        self._skills_box = skills_box
        skills_layout = QVBoxLayout(skills_box)
        skills_layout.setContentsMargins(0, 0, 0, 0)
        skills_layout.addWidget(self.skills)
        skills_layout.addLayout(skill_buttons)
        # Its own line: three buttons abreast made this the form's widest row,
        # and the inspector had to widen (squeezing the Stories dock) to fit it.
        skills_layout.addWidget(self.suggest_skills, 0, Qt.AlignLeft)
        self.canon = QLineEdit()
        self.canon.setPlaceholderText("e.g. 'Night of the Living Dead (1968)' — blank if original")
        # The portrait large, at the top of the card; the other pictures
        # small under the sheet (the author, Oct 15 2026).
        self.portrait = PortraitView()
        self.pictures = RefImageStrip(self._pictures, "them", after_portrait=True, crop="portrait")
        # Their sheet's rows and columns stay where they are as pictures change.
        self.pictures.before_change = self._keep_sheet_order
        self.portrait.draw_requested.connect(lambda: self._ask("new"))
        self.portrait.again_requested.connect(lambda: self._ask("again", first=True))
        self.portrait.edit_requested.connect(lambda: self._ask("edit", first=True))
        self.portrait.scene_requested.connect(lambda: self._ask("scene"))
        self.portrait.add_requested.connect(self._add_portrait)
        self.portrait.remove_requested.connect(self._remove_portrait)
        self.portrait.labels_requested.connect(self._label_portrait)
        self.portrait.set_requested.connect(lambda: self._ask("set", first=True))
        self.pictures.again_requested.connect(lambda ref: self._ask("again", ref))
        self.pictures.edit_requested.connect(lambda ref: self._ask("edit", ref))
        self.pictures.changed.connect(self._on_pictures_changed)
        self.origin_note = QLabel("Card drafted by the model. Check it.")
        self.origin_note.setObjectName("hintLabel")
        self.origin_note.setWordWrap(True)

        for widget in (
            self.summary,
            self.full_description,
            self.personality,
            self.voice_notes,
            self.competence,
        ):
            widget.textChanged.connect(self._apply_edits)
        self.name.textChanged.connect(self._apply_edits)
        self.aliases.textChanged.connect(self._apply_edits)
        self.canon.textChanged.connect(self._apply_edits)
        self.player_available.toggled.connect(self._apply_edits)
        self.player_available.toggled.connect(self._sync_voicing)
        self.author_only.toggled.connect(self._apply_edits)
        self.keep_whole.toggled.connect(self._apply_edits)
        self.condensed.textChanged.connect(self._condensed_edited)

        # The portrait at the left of the card, tall, with the other pictures
        # under it; the card's text on the right (the author, Oct 15 2026:
        # most screens are wide).
        pictures_column = QWidget()
        # Its full width where there is room, narrower where there isn't.
        pictures_column.setMinimumWidth(PORTRAIT_COLUMN_MIN)
        pictures_column.setMaximumWidth(PORTRAIT_COLUMN)
        pictures_column.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        column = QVBoxLayout(pictures_column)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addWidget(self.portrait)
        column.addWidget(QLabel("Other pictures"))
        column.addWidget(self.pictures)
        column.addStretch(1)
        fields = QWidget()
        form = QFormLayout(fields)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Name", self.name)
        form.addRow("Aliases", self.aliases)
        form.addRow("From", self.canon)
        form.addRow(self.origin_note)
        form.addRow("Summary", self.summary)
        form.addRow("Description", self.full_description)
        form.addRow("Personality", self.personality)
        form.addRow("Voice", self.voice_notes)
        form.addRow("Competence", self.competence)
        form.addRow("Skills", skills_box)
        form.addRow(self.player_available)
        form.addRow(self.author_only)
        form.addRow(self.keep_whole)
        form.addRow("Condensed", self.condensed_box)
        self._form = form
        form_host = QWidget()
        card = QHBoxLayout(form_host)
        card.setContentsMargins(0, 0, 0, 0)
        card.setSpacing(12)
        card.addWidget(pictures_column, 0, Qt.AlignTop)
        # At the top: in the whole window the card is shorter than the page,
        # and the spare height went to the skills table.
        card.addWidget(fields, 1, Qt.AlignTop)

        # In a simple chat: what a chat's characters are for.
        self.chat_hint = QLabel(CHAT_HINT)
        self.chat_hint.setObjectName("hintLabel")
        self.chat_hint.setWordWrap(True)
        self.chat_hint.hide()
        form.insertRow(0, self.chat_hint)
        self.empty_hint = QLabel("Add a character, or select one to edit.")
        self.empty_hint.setObjectName("hintLabel")
        self.empty_hint.setWordWrap(True)
        form.insertRow(0, self.empty_hint)
        # Where the character came from among the author's own, and what
        # changed (gui/window_card_link.py fills it).
        self.link_words = QLabel()
        self.link_words.setObjectName("hintLabel")
        self.link_words.setWordWrap(True)
        self.link_words.setTextFormat(Qt.PlainText)
        self.link_button = QPushButton()
        self.link_button.clicked.connect(
            lambda: self._current and self.link_requested.emit(self._current.id)
        )
        self.link_box = QWidget()
        link_layout = QVBoxLayout(self.link_box)
        link_layout.setContentsMargins(0, 0, 0, 4)
        link_layout.setSpacing(4)
        link_layout.addWidget(self.link_words)
        link_layout.addWidget(self.link_button, 0, Qt.AlignLeft)
        self.link_box.hide()
        form.insertRow(1, self.link_box)

        scroll = FormScroll(form_host)
        self._form_host = form_host
        return scroll

    def _build_suggestions(self) -> QWidget:
        self.suggestion_list = QListWidget()
        self.suggestion_list.setObjectName("suggestionList")
        self.suggestion_list.setMaximumHeight(110)
        self.suggestion_list.currentItemChanged.connect(self._sync_suggestion_buttons)

        self.accept_button = QPushButton("Add")
        self.accept_button.setToolTip(
            "Keep as a supporting character: shown to the model when mentioned"
        )
        self.accept_button.clicked.connect(lambda: self._accept(False))
        self.accept_cast_button = QPushButton("Add to cast")
        self.accept_cast_button.setToolTip(
            "Put on the roster and in every prompt; costs one cache miss"
        )
        self.accept_cast_button.clicked.connect(lambda: self._accept(True))
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.setToolTip("Don't suggest this name again")
        self.dismiss_button.clicked.connect(self._dismiss)
        self.scan_button = QPushButton("Scan recent story")
        self.scan_button.setToolTip("Ask the model who in the recent prose isn't on file yet")
        self.scan_button.clicked.connect(self.scan_requested)

        self.suggestion_hint = QLabel(
            "New characters are suggested when turns are archived, or scan now."
        )
        self.suggestion_hint.setObjectName("hintLabel")
        self.suggestion_hint.setWordWrap(True)

        caption = QLabel("SUGGESTED FROM THE STORY")
        caption.setObjectName("composerCaption")
        caption.setToolTip(self.suggestion_hint.text())
        self.scan_button.setToolTip(self.scan_button.toolTip() + ". " + self.suggestion_hint.text())
        header = WrapRow(spacing=6, group_spacing=6)
        header.add_group([caption])
        header.add_group([self.scan_button])
        suggestion_buttons = WrapRow(spacing=6, group_spacing=6)
        for button in (self.accept_button, self.accept_cast_button, self.dismiss_button):
            suggestion_buttons.add_group([button])

        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.suggestion_list)
        layout.addWidget(self.suggestion_hint)
        layout.addLayout(suggestion_buttons)
        return box

    # --- population -------------------------------------------------------

    def set_cast(
        self,
        cast: list[Character],
        supporting: list[Character] | None = None,
        suggestions: list[CharacterSuggestion] | None = None,
        *,
        hidden: set[str] | None = None,
    ) -> None:
        """`hidden`: ids the plot hasn't brought in yet, left off the list unless
        the author has chosen to see the plot (they're still edited in place)."""
        self._cast = cast
        if hidden is not None:
            self._hidden = hidden
        if supporting is not None:
            self._supporting = supporting
        if suggestions is not None:
            self._suggestions = suggestions
        selected = self._current.id if self._current else None
        everyone_was = self.everyone_shown()
        self._loading = True
        self.list.clear()
        if self._everyone:
            everyone = QListWidgetItem(EVERYONE_LABEL)
            everyone.setData(EVERYONE_ROLE, True)
            everyone.setToolTip("Everyone's pictures, each with all of theirs")
            font = QFont(self.list.font())
            font.setBold(True)
            everyone.setFont(font)
            self.list.addItem(everyone)
            if everyone_was:
                self.list.setCurrentItem(everyone)
        sections = [("IN THIS CHAT" if self._chat else "CAST", self._cast)]
        if self._supporting:
            sections.append(("SUPPORTING — FROM THE STORY", self._supporting))
        for title, characters in sections:
            self._add_header(title)
            for character in characters:
                if character.id in self._hidden:
                    continue
                label = character.name or "(unnamed)"
                if character.origin == "model":
                    label += MODEL_MARK
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, character.id)
                item.setIcon(self._icon(character))
                if character.canon:
                    item.setToolTip(character.canon)
                self.list.addItem(item)
                if character.id == selected:
                    self.list.setCurrentItem(item)
        self._loading = False
        self.save_button.setEnabled(
            any(c.id not in self._hidden for c in (*self._cast, *self._supporting))
        )
        self._show_suggestions()
        first = next(
            (
                row
                for row in range(self.list.count())
                if self.list.item(row).data(Qt.UserRole) is not None
            ),
            -1,
        )
        if self.list.currentItem() is None and first >= 0:
            self.list.setCurrentRow(first)
        else:
            self._on_selection_changed(self.list.currentItem(), None)

    # --- the list's column -----------------------------------------------

    def _fit_side(self) -> None:
        """The list's column, wider with the text (View → Text size)."""
        self.side.setFixedWidth(round(LEFT_WIDTH * text_size.scale()))

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange) and hasattr(self, "side"):
            self._fit_side()

    def set_chat_mode(self, on: bool) -> None:
        """A simple chat's characters (pictures alone) or a story's: in a
        chat, only what drawing them reads, and nothing a story keeps."""
        self._chat = on
        for field in (
            self.canon,
            self.personality,
            self.voice_notes,
            self.competence,
            self._skills_box,
            self.player_available,
            self.author_only,
            self.keep_whole,
            self.condensed_box,
        ):
            self._form.setRowVisible(field, not on)
        self._form.setRowVisible(self.chat_hint, on)
        for widget in (
            self.move_button,
            self.fill_button,
            self.tavern_button,
            self.suggestions_box,
            self.room_banner,
        ):
            widget.setVisible(not on)
        self.set_cast(self._cast)

    @property
    def chat_mode(self) -> bool:
        return self._chat

    def set_everyone(self, on: bool) -> None:
        """Whether the list begins with Everyone (the whole window's page:
        everyone's pictures), or not (lent to the guide or Setup)."""
        if on != self._everyone:
            self._everyone = on
            self.set_cast(self._cast)

    def everyone_shown(self) -> bool:
        item = self.list.currentItem()
        return item is not None and bool(item.data(EVERYONE_ROLE))

    def show_everyone(self) -> None:
        """Everyone chosen in the list (when it has them)."""
        for row in range(self.list.count()):
            if self.list.item(row).data(EVERYONE_ROLE):
                self.list.setCurrentRow(row)
                return

    def _icon(self, character: Character) -> QIcon:
        return self._icons.icon(character.reference_images)

    def refresh_pictures(self) -> None:
        """The cards' pictures as they are now (one just landed): the list's
        icons, the portrait and the others, nothing else. Reloaded whole, the
        card put the cursor at the start of the field being typed in and
        lost its undo."""
        cards = {c.id: c for c in (*self._cast, *self._supporting)}
        for row in range(self.list.count()):
            item = self.list.item(row)
            character = cards.get(item.data(Qt.UserRole))
            if character is not None:
                item.setIcon(self._icon(character))
        current = self._current
        self.pictures.set_refs(current.reference_images if current is not None else None)
        self._show_portrait()

    def _ask(self, what: str, ref_id: str = "", *, first: bool = False) -> None:
        character = self._current
        if character is None:
            return
        if first and character.reference_images:
            ref_id = character.reference_images[0].id
        self.picture_requested.emit(character.id, ref_id, what)

    def add_portrait_to(self, character_id: str) -> bool:
        """Choose a file to be this character's portrait (the People page's
        Choose a picture…): their card selected, then as Choose a file…."""
        self.select_character(character_id)
        if self._current is None or self._current.id != character_id:
            return False
        before = len(self._current.reference_images)
        self._add_portrait()
        return len(self._current.reference_images) > before

    def _add_portrait(self) -> None:
        character = self._current
        if self.pictures.add_portrait() and character is not None:
            self.portrait_added.emit(character.id)

    def _remove_portrait(self) -> None:
        character = self._current
        if character is None or not character.reference_images:
            return
        # The one shown when asked: a portrait landing while the question is
        # up goes first, and "the first" was then the new one.
        shown = character.reference_images[0].id
        answer = QMessageBox.question(
            self,
            "Remove the portrait?",
            f"Take this portrait off {character.name}'s card? Their next picture becomes "
            "the portrait. Pictures drawn for the story stay in Media.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes and self._current is not None:
            if self._current.id == character.id:
                self.pictures.remove_ref(shown)

    def _keep_sheet_order(self) -> None:
        if self._current is not None:
            keep_order(self._current)

    def _label_portrait(self) -> None:
        character = self._current
        if character is None or not character.reference_images:
            return
        from sealedlore.gui.picture_labels import edit_labels

        keep_order(character)
        if edit_labels(self, character.reference_images[0], character.reference_images):
            self._on_pictures_changed()

    def _show_portrait(self) -> None:
        character = self._current
        first = character.reference_images[0] if character and character.reference_images else None
        store = self._pictures()
        data = store.read(first.file) if first is not None and store is not None else None
        name = character.name if character is not None else ""
        caption = first.caption if first is not None and first.caption != "portrait" else ""
        if first is not None and (first.look or first.expression):
            labels = " · ".join(part for part in (first.look, first.expression) if part)
            caption = f"{caption}\n{labels}" if caption else labels
        self.portrait.show_portrait(data, caption, name)
        self.portrait.set_offered(character is not None, character is not None)
        item = self.list.currentItem()
        if item is not None and character is not None:
            item.setIcon(self._icon(character))

    def _add_header(self, title: str) -> None:
        item = QListWidgetItem(title)
        item.setData(HEADER_ROLE, True)
        item.setFlags(Qt.NoItemFlags)
        # QSS can't select list items by role, so the header is styled here.
        font = QFont(self.list.font())
        # The stylesheet sets sizes in pixels, which leaves pointSizeF() at -1.
        if font.pixelSize() > 0:
            font.setPixelSize(max(9, round(font.pixelSize() * 0.8)))
        elif font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * 0.8)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 1)
        item.setFont(font)
        item.setForeground(theme.colour("text_placeholder"))
        self.list.addItem(item)

    def _show_suggestions(self) -> None:
        self.suggestion_list.clear()
        for suggestion in self._suggestions:
            label = suggestion.name + (f" — {suggestion.canon}" if suggestion.canon else "")
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, suggestion.id)
            item.setToolTip(suggestion.description or "No description.")
            self.suggestion_list.addItem(item)
        has_any = bool(self._suggestions)
        self.suggestion_list.setVisible(has_any)
        # Said in the caption's tooltip rather than a line of its own: the
        # list above and the portrait below want the room.
        self.suggestion_hint.setVisible(False)
        if has_any:
            self.suggestion_list.setCurrentRow(0)
        self._sync_suggestion_buttons()

    def _sync_suggestion_buttons(self, *_args) -> None:
        chosen = self.suggestion_list.currentItem() is not None and bool(self._suggestions)
        for button in (self.accept_button, self.accept_cast_button, self.dismiss_button):
            button.setVisible(bool(self._suggestions))
            button.setEnabled(chosen)

    def _chosen_suggestion(self) -> str | None:
        item = self.suggestion_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _accept(self, to_cast: bool) -> None:
        suggestion_id = self._chosen_suggestion()
        if suggestion_id:
            self.accept_requested.emit(suggestion_id, to_cast)

    def _dismiss(self) -> None:
        suggestion_id = self._chosen_suggestion()
        if suggestion_id:
            self.dismiss_requested.emit(suggestion_id)

    def _sync_voicing(self) -> None:
        """ "Never voiced" only means something for a character you can play.

        With "Available to play" off the character is the storyteller's, which
        is the opposite setting; leaving the box live would offer a state where
        nobody writes them.
        """
        playable = self.player_available.isChecked()
        self.author_only.setEnabled(playable)
        if not playable and self.author_only.isChecked():
            self.author_only.setChecked(False)

    def _is_supporting(self, character: Character | None) -> bool:
        return character is not None and any(c is character for c in self._supporting)

    def _on_selection_changed(self, current: QListWidgetItem | None, _previous=None) -> None:
        if self._loading:
            return
        character_id = current.data(Qt.UserRole) if current else None
        self._current = next(
            (c for c in (*self._cast, *self._supporting) if c.id == character_id), None
        )
        self._load_editor()

    @property
    def current(self) -> Character | None:
        return self._current

    def set_link(self, words: str, button: str, tip: str = "") -> None:
        """The line about the shown character's card ("" hides it) and what
        its button does (`tip`, said in its tooltip)."""
        self.link_words.setText(words)
        self.link_words.setVisible(bool(words))
        self.link_button.setText(button)
        self.link_button.setToolTip(tip)
        self.link_button.setVisible(bool(button))
        self.link_box.setVisible(bool(words or button) and self._current is not None)

    def _load_editor(self) -> None:
        character = self._current
        self.current_changed.emit(character.id if character is not None else "")
        enabled = character is not None
        self._form_host.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)
        self.move_button.setEnabled(enabled)
        self.move_button.setText(
            "Move to cast" if self._is_supporting(character) else "Move to supporting"
        )
        self.empty_hint.setVisible(not enabled)
        self.origin_note.setVisible(bool(character and character.origin == "model"))
        self.pictures.set_refs(character.reference_images if character is not None else None)
        self._show_portrait()
        if character is None:
            return

        self._loading = True
        set_field_text(self.name, character.name)
        set_field_text(self.aliases, ", ".join(character.aliases))
        self.summary.setPlainText(character.summary or "")
        self.full_description.setPlainText(character.full_description or "")
        self.personality.setPlainText(character.personality or "")
        self.voice_notes.setPlainText(character.voice_notes or "")
        self.competence.setPlainText(character.competence.notes)
        self.player_available.setChecked(character.is_player_available)
        self.author_only.setChecked(character.author_only)
        self.keep_whole.setChecked(character.keep_whole)
        self.condensed.setPlainText(character.condensed or "")
        self._sync_voicing()
        set_field_text(self.canon, character.canon or "")
        self._show_skills(character)
        self._loading = False
        self._show_condensed()

    # --- editing ----------------------------------------------------------

    def _apply_edits(self) -> None:
        if self._loading or self._current is None:
            return
        character = self._current
        old_name = character.name
        character.name = self.name.text().strip()
        if character.name and character.name != old_name:
            self.renamed.emit(character.id, old_name)
        character.aliases = [
            alias.strip() for alias in self.aliases.text().split(",") if alias.strip()
        ]
        character.summary = self.summary.toPlainText().strip() or None
        character.full_description = self.full_description.toPlainText().strip() or None
        character.personality = self.personality.toPlainText().strip() or None
        character.voice_notes = self.voice_notes.toPlainText().strip() or None
        character.competence.notes = self.competence.toPlainText().strip()
        character.is_player_available = self.player_available.isChecked()
        character.author_only = self.author_only.isChecked()
        character.keep_whole = self.keep_whole.isChecked()
        character.canon = self.canon.text().strip() or None
        character.competence.tiers = self._skills_from_table()

        item = self.list.currentItem()
        if item is not None:
            mark = MODEL_MARK if character.origin == "model" else ""
            item.setText((character.name or "(unnamed)") + mark)
        self.changed.emit()

    def _condensed_edited(self) -> None:
        """The author's own words for the condensed card: kept as written,
        and current for the card as it stands (they wrote it from it)."""
        if self._loading or self._current is None:
            return
        text = self.condensed.toPlainText().strip()
        self._current.condensed = text or None
        self._current.condensed_from = fingerprint(self._current) if text else None
        self.changed.emit()

    def set_room(self, room: CastRoom | None) -> None:
        """How the cast fits its room in the prompt (None: not said, as in a
        chat): the banner, and the shown card's condensed text."""
        self._room = room
        text = banner_text(room) if room is not None else ""
        can_condense = room is not None and any(
            cid not in room.writing and cid not in room.failures for cid in room.fit.waiting
        )
        self.room_banner.show_room(text, can_condense=can_condense)
        self._show_condensed()

    def _show_condensed(self) -> None:
        character = self._current
        cast_card = character is not None and not self._is_supporting(character)
        if character is None:
            note = ""
        elif cast_card:
            note = card_note(self._room, character)
        else:
            note = supporting_note(self._room, character)
        # A chat's characters are never sent: no room, nothing condensed.
        shown = character is not None and bool(note or character.condensed) and not self._chat
        self._form.setRowVisible(self.condensed_box, shown)
        self._form.setRowVisible(self.keep_whole, cast_card and not self._chat)
        if not shown:
            return
        self.condensed_note.setText(note)
        self.condensed_note.setVisible(bool(note))
        room = self._room
        writing = room is not None and character.id in room.writing
        self.condense_button.setEnabled(not writing)
        self.condense_button.setText(
            CONDENSE_AGAIN_LABEL if character.condensed else CONDENSE_LABEL
        )

    def _on_pictures_changed(self) -> None:
        if self._current is not None:
            self._show_portrait()
            self.changed.emit()

    def add_character(self) -> None:
        """Add, from elsewhere (the People page's Add a character…)."""
        self._add_character()

    def _export_card(self) -> None:
        if self._current is not None:
            self.card_export_requested.emit(self._current.id)

    def _add_character(self) -> None:
        character = Character(name="New character")
        self._cast.append(character)
        self._current = character
        self.set_cast(self._cast)
        self._select(character.id)
        # Their card, to name them: added from Everyone or their pictures,
        # the name to type in was on a tab not showing.
        self.tabs.setCurrentIndex(0)
        self.name.setFocus()
        self.name.selectAll()
        self.changed.emit()

    def _remove_character(self) -> None:
        if self._current is None:
            return
        text = f"Take {self._current.name} out of the story? Their card goes, pictures and all."
        tavern = self._current.tavern
        if tavern is not None and tavern.book_entries:
            # Once in, the entries a card's lorebook made are the story's own:
            # nothing says which they were, so they stay, and the author is told.
            text += (
                "\n\nThe lore that came in with their card stays: remove it on the Lore page "
                "if the story no longer needs it."
            )
        answer = QMessageBox.question(
            self, "Remove this character?", text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if answer != QMessageBox.Yes:
            return
        tier = self._supporting if self._is_supporting(self._current) else self._cast
        tier.remove(self._current)
        self._current = None
        self.set_cast(self._cast)
        self.changed.emit()

    # --- skills (§4.1) ----------------------------------------------------

    def _show_skills(self, character: Character) -> None:
        self.skills.show_tiers(character.competence.tiers)

    def _skills_from_table(self) -> dict:
        return self.skills.tiers()

    def _add_skill(self) -> None:
        if self._current is not None:
            self.skills.add_skill()

    def _remove_skill(self) -> None:
        if self.skills.remove_skill():
            self._apply_edits()

    def show_suggested_skills(self, character_id: str, tiers: dict) -> None:
        """Put accepted suggestions in the table, where they can still be edited."""
        if self._current is None or self._current.id != character_id:
            self._select(character_id)
        if self._current is None:
            return
        self._current.competence.tiers = dict(tiers)
        self._loading = True
        self._show_skills(self._current)
        self._loading = False
        self.changed.emit()

    def _move_character(self) -> None:
        if self._current is None:
            return
        if self._is_supporting(self._current):
            self.promote_requested.emit(self._current.id)
        else:
            self.demote_requested.emit(self._current.id)

    def _select(self, character_id: str) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(Qt.UserRole) == character_id:
                self.list.setCurrentItem(item)
                return

    def select_character(self, character_id: str) -> None:
        self._select(character_id)
