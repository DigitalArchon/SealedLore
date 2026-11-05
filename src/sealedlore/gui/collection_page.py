"""Your characters (File → Your characters…, the start page, the Cast page):
every character the author keeps for any story or chat, in the whole window,
as the cast board shows a story's (gui/cast_board.py).

The author (Nov 4 2026): "something similar to the Cast Board, but globally,
so the user can manage all their characters, dozens or more, outside of a
story", each with every field of a SillyTavern V3 card. The page lists them
(searched, by tag, sorted), shows everyone's portraits, and shows a
character's card in four tabs: the character as a story has them, their
pictures as the cast board's sheet of looks and expressions, what a card has
for a chat, and the card itself. A character's versions are listed once and
chosen above the card. Keeping, drawing and opening stories are the
window's (gui/window_collection.py).
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui import text_size, theme
from sealedlore.gui.card_files_form import FilesForm
from sealedlore.gui.card_form import AboutForm, CharacterForm, ChatForm
from sealedlore.gui.cast_board import CastBoard, Thumbs, scaled_font
from sealedlore.gui.lorebook_form import LorebookForm
from sealedlore.gui.ref_images import StoryPictures
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.models.character import Character
from sealedlore.storage import collection

PAGE_LABEL = "Your characters"
EVERYONE = "Everyone"
ALL_TAGS = "All tags"
SORTS = ("By name", "Changed last", "Added last")
# The people beside the names; everyone's portraits on the overview, 2:3.
PEOPLE_ICON = QSize(40, 60)
TILE = QSize(150, 225)
CARD_ROLE = Qt.UserRole + 1
# What each tab is for, as their tooltips say.
TABS = (
    ("Card", "The character as a story has them: what a story's storyteller is told"),
    ("Pictures", "Their pictures as a sheet of looks and expressions, as the cast board has it"),
    ("For chats", "What a SillyTavern card has for a one-to-one chat with them"),
    ("Lorebook", "Their own lore, every entry to read, change, add or remove"),
    ("Files", "Every other file on their card: a background, a greeting's pictures, sounds"),
    ("About", "The card itself: who made it, its version, tags and sources, other apps' settings"),
)
# On a name in the list: their portrait's file, and whether it is shown yet.
PORTRAIT_ROLE = Qt.UserRole + 10
# The list's column at 100% text.
LEFT_WIDTH = 300
# Character cards, by their files' endings (dropped on the page).
CARD_SUFFIXES = (".png", ".json", ".charx")
# The actions the page asks the window for (`action_requested`).
NEW, IMPORT, LINK, EXPRESSIONS = "new", "import", "link", "expressions"
EXPORT_PNG, EXPORT_CHARX, EXPORT_JSON = "export_png", "export_charx", "export_json"
DUPLICATE, VERSION, RENAME_VERSION, DELETE = "duplicate", "version", "rename_version", "delete"
STORY_FROM_CARD = "story_from_card"
ADD_TO_STORY = "add_to_story"
CHAT, DRAFT = "chat", "draft"
CHAT_LABEL = "Chat with them…"
DRAFT_LABEL = "Draft a story with them…"
ADD_LABEL = "Add to the story"
SCENARIO_STORY_LABEL = "A story from their card's own scenario…"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


class PersonDelegate(QStyledItemDelegate):
    """A character on the overview: their portrait filling the tile, their
    name and a line about them over a shade at the bottom."""

    def __init__(self, thumbs: Thumbs, parent=None) -> None:
        super().__init__(parent)
        self.thumbs = thumbs

    def sizeHint(self, _option, _index) -> QSize:  # noqa: N802 - Qt naming
        return TILE + QSize(12, 12)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: D102
        character, line = index.data(CARD_ROLE) or (None, "")
        rect = QRectF(option.rect.adjusted(6, 6, -6, -6))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        painter.fillPath(path, theme.colour("panel"))
        painter.setClipPath(path)
        first = character.reference_images[0] if character and character.reference_images else None
        if first is not None:
            pixmap = self.thumbs.get(first.file, TILE.width(), TILE.height())
            if pixmap is not None and not pixmap.isNull():
                painter.drawPixmap(rect.topLeft().toPoint(), pixmap)
        else:
            painter.setPen(theme.colour("text_soft"))
            painter.drawText(rect, Qt.AlignCenter, "No portrait")
        if character is not None:
            name_font = scaled_font(option.font, 1.05)
            name_font.setBold(True)
            small = scaled_font(option.font, 0.85)
            painter.setFont(name_font)
            name_height = painter.fontMetrics().height()
            band = name_height + (painter.fontMetrics().height() if line else 0) + 12
            shade = QRectF(rect.left(), rect.bottom() - band, rect.width(), band)
            painter.fillRect(shade, theme.colour("window", 200))
            painter.setPen(theme.colour("text_bright"))
            inner = shade.adjusted(6, 5, -6, -5)
            width = int(inner.width())
            painter.drawText(
                inner,
                Qt.AlignHCenter | Qt.AlignTop,
                painter.fontMetrics().elidedText(character.name, Qt.ElideRight, width),
            )
            if line:
                painter.setFont(small)
                painter.setPen(theme.colour("text_soft"))
                painter.drawText(
                    inner,
                    Qt.AlignHCenter | Qt.AlignBottom,
                    painter.fontMetrics().elidedText(line, Qt.ElideRight, width),
                )
        painter.setClipping(False)
        if option.state & (QStyle.State_MouseOver | QStyle.State_Selected):
            painter.setPen(QPen(theme.colour("primary"), 2))
            painter.drawPath(path)
        painter.restore()


class CollectionPage(QWidget):
    """The page (see the module): the window gives it the characters
    (`set_characters`) and acts on what it asks for; edits to a card's fields
    it makes on the card and says so (`changed`)."""

    back_requested = Signal()
    # (what, the character shown's id, or "" for none): see the names above.
    action_requested = Signal(str, str)
    # (character id): its fields changed here; the window keeps them.
    changed = Signal(str)
    # (character id): its pictures changed here (added, removed, labelled).
    pictures_changed = Signal(str)
    # (what, character id, picture id): "new", "again", "edit" or "set".
    picture_requested = Signal(str, str, str)
    # (what, character id): "import", "export" or "remove", its own lorebook.
    lorebook_requested = Signal(str, str)
    # "save" or "picture", which of the card's files, and the character's id.
    file_requested = Signal(str, int, str)
    # (paths, character id): files dropped on their Files tab, to add.
    files_dropped = Signal(list, str)
    # (paths): character cards dropped on the list or the overview, to keep.
    cards_dropped = Signal(list)
    # (character id, "" for everyone): another one shown; the window fills
    # the sheet of their pictures.
    character_shown = Signal(str)

    def __init__(self, pictures: StoryPictures, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("collectionPage")
        # Esc in a field gives the page the focus first (main_window._on_escape).
        self.setFocusPolicy(Qt.ClickFocus)
        self._pictures = pictures
        self._thumbs = Thumbs(pictures, self._thumbs_ready)
        self._characters: list[Character] = []
        self._current: str | None = None
        # The character the forms hold: loaded again only for another one,
        # never under the author's typing (a picture landing, the list redone).
        self._loaded: Character | None = None
        self._extra = ""
        # A story is open beside the page (Add to the story is offered).
        self._story_open = False

        # --- the list of them, on the left ---
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search your characters")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self._fill())
        self.tag = QComboBox()
        self.tag.setToolTip("Only the characters with this tag (About → Tags)")
        self.tag.currentIndexChanged.connect(lambda _index: self._fill())
        self.sort = QComboBox()
        self.sort.addItems(list(SORTS))
        self.sort.currentIndexChanged.connect(lambda _index: self._fill())
        self.people = QListWidget()
        self.people.setObjectName("boardPeople")
        self.people.setIconSize(PEOPLE_ICON)
        self.people.setSpacing(2)
        # A long line of tags is cut short, never a sideways scrollbar.
        self.people.setWordWrap(False)
        self.people.setTextElideMode(Qt.ElideRight)
        self.people.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Rows with no portrait keep their names in line with the rest.
        blank = QPixmap(PEOPLE_ICON)
        blank.fill(Qt.transparent)
        self._blank_icon = QIcon(blank)
        self.people.currentItemChanged.connect(self._on_person)
        delete_key = QShortcut(QKeySequence(QKeySequence.Delete), self.people)
        delete_key.setContext(Qt.WidgetShortcut)
        delete_key.activated.connect(lambda: self._act(DELETE))
        self.new_button = QPushButton("New character")
        self.new_button.setObjectName("sendButton")
        self.new_button.setToolTip("A character of your own, from a blank card")
        self.new_button.clicked.connect(lambda: self.action_requested.emit(NEW, ""))
        self.import_button = QToolButton()
        self.import_button.setText("Import")
        self.import_button.setPopupMode(QToolButton.InstantPopup)
        import_menu = QMenu(self.import_button)
        import_menu.addAction("Character cards from files…").triggered.connect(
            lambda: self.action_requested.emit(IMPORT, "")
        )
        import_menu.addAction("A character card from a link…").triggered.connect(
            lambda: self.action_requested.emit(LINK, "")
        )
        self.expressions_action = import_menu.addAction("Expression pictures from a zip…")
        self.expressions_action.setToolTip(
            "SillyTavern's expression pictures for the character shown (a zip of pictures "
            "named by expression): added to their pictures, labelled"
        )
        self.expressions_action.triggered.connect(
            lambda: self.action_requested.emit(EXPRESSIONS, self._current or "")
        )
        import_menu.setToolTipsVisible(True)
        self.import_button.setMenu(import_menu)
        self.import_button.setToolTip(
            "SillyTavern character cards (PNG, CHARX or JSON), as SillyTavern, Chub and "
            "RisuRealm share them"
        )
        left_buttons = QHBoxLayout()
        left_buttons.setContentsMargins(0, 0, 0, 0)
        left_buttons.addWidget(self.new_button)
        left_buttons.addWidget(self.import_button)
        left_buttons.addStretch(1)
        filters = QHBoxLayout()
        filters.setContentsMargins(0, 0, 0, 0)
        filters.addWidget(self.tag, 1)
        filters.addWidget(self.sort, 1)
        left = QWidget()
        self._left = left
        self._fit_left()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(self.search)
        left_layout.addLayout(filters)
        left_layout.addWidget(self.people, 1)
        left_layout.addLayout(left_buttons)

        # --- the header over the right side ---
        self.title = QLabel(PAGE_LABEL)
        self.title.setObjectName("boardTitle")
        self.version = QComboBox()
        self.version.setToolTip(
            "The versions of this character, each a whole card of its own: the main card, "
            "and any you made from it"
        )
        self.version.currentIndexChanged.connect(self._on_version)
        self.chat_button = QPushButton(CHAT_LABEL)
        self.chat_button.setObjectName("sendButton")
        self.chat_button.setToolTip(
            "A simple chat with them: their card as its first message, their own system "
            "prompt, and a greeting of theirs to open it, each yours to change first"
        )
        self.chat_button.clicked.connect(lambda: self._act(CHAT))
        self.draft_button = QPushButton(DRAFT_LABEL)
        self.draft_button.setToolTip(
            "Say what story you want with them, in what world: the model drafts its world, "
            "the other characters, lore and opening around their card"
        )
        self.draft_button.clicked.connect(lambda: self._act(DRAFT))
        self.add_button = QPushButton(ADD_LABEL)
        self.add_button.setToolTip(
            "Into the story that is open, with their pictures: what the story changes of them "
            "can come back to this card"
        )
        self.add_button.clicked.connect(lambda: self._act(ADD_TO_STORY))
        self.add_button.hide()
        self.export_button = QToolButton()
        self.export_button.setText("Export")
        self.export_button.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(self.export_button)
        self.png_action = export_menu.addAction("As a PNG card, with the portrait…")
        self.png_action.triggered.connect(lambda: self._act(EXPORT_PNG))
        # Always offered: the only card that keeps every picture and every
        # file of theirs (a card's own files count, not only pictures).
        self.charx_action = export_menu.addAction("As a CHARX card, with every picture and file…")
        self.charx_action.triggered.connect(lambda: self._act(EXPORT_CHARX))
        export_menu.addAction("As a JSON card…").triggered.connect(lambda: self._act(EXPORT_JSON))
        export_menu.setToolTipsVisible(True)
        self.export_button.setMenu(export_menu)
        self.export_button.setToolTip("This character as a SillyTavern card")
        self.more_button = QToolButton()
        self.more_button.setText("More")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        more_menu = QMenu(self.more_button)
        self.story_action = more_menu.addAction(SCENARIO_STORY_LABEL)
        self.story_action.setToolTip(
            "A new story from this card as a SillyTavern card makes one: its scenario as the "
            "world, a greeting as the opening, and you as the other character"
        )
        self.story_action.triggered.connect(lambda: self._act(STORY_FROM_CARD))
        more_menu.addSeparator()
        more_menu.addAction("New version of this character…").triggered.connect(
            lambda: self._act(VERSION)
        )
        self.rename_action = more_menu.addAction("Rename this version…")
        self.rename_action.triggered.connect(lambda: self._act(RENAME_VERSION))
        more_menu.addAction("Duplicate as a new character").triggered.connect(
            lambda: self._act(DUPLICATE)
        )
        more_menu.addSeparator()
        self.delete_action = more_menu.addAction("Delete…")
        self.delete_action.triggered.connect(lambda: self._act(DELETE))
        more_menu.setToolTipsVisible(True)
        self.more_button.setMenu(more_menu)
        self.back_button = QPushButton("Back to the story")
        self.back_button.setToolTip("Esc")
        self.back_button.clicked.connect(self.back_requested)
        header = WrapRow(group_spacing=12)
        header.add_group([self.title])
        self.version_label = QLabel("Version")
        header.add_group([self.version_label, self.version])
        header.add_group([self.chat_button, self.draft_button, self.add_button])
        header.add_group([self.export_button, self.more_button])
        header.add_group([self.back_button], right=True)
        self.stats = QLabel()
        self.stats.setObjectName("hintLabel")
        self.stats.setWordWrap(True)

        # --- everyone, or one of them ---
        self.overview = QListWidget()
        self.overview.setObjectName("collectionOverview")
        self.overview.setViewMode(QListWidget.IconMode)
        self.overview.setFlow(QListWidget.LeftToRight)
        self.overview.setWrapping(True)
        self.overview.setResizeMode(QListWidget.Adjust)
        self.overview.setMovement(QListWidget.Static)
        self.overview.setUniformItemSizes(True)
        self.overview.setSelectionMode(QAbstractItemView.SingleSelection)
        self.overview.setMouseTracking(True)
        self.overview.setCursor(Qt.PointingHandCursor)
        self.overview.setGridSize(TILE + QSize(12, 12))
        self.overview.setItemDelegate(PersonDelegate(self._thumbs, self.overview))
        self.overview.itemActivated.connect(self._tile_chosen)
        self.overview.itemClicked.connect(self._tile_chosen)
        self.empty = QLabel(
            "No characters yet.\n\nMake one with New character (below, left), or Import "
            "SillyTavern cards: PNG, CHARX or JSON files, or a link to a card on Chub or "
            "RisuRealm. Card files can be dropped here too."
        )
        self.empty.setObjectName("hintLabel")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        overview_page = QWidget()
        overview_layout = QVBoxLayout(overview_page)
        overview_layout.setContentsMargins(0, 0, 0, 0)
        overview_layout.addWidget(self.empty)
        overview_layout.addWidget(self.overview, 1)

        self.card_form = CharacterForm(pictures)
        self.card_form.changed.connect(self._card_changed)
        self.card_form.name.editingFinished.connect(self._name_finished)
        self.card_form.pictures_changed.connect(
            lambda: self._current and self.pictures_changed.emit(self._current)
        )
        self.card_form.picture_requested.connect(
            lambda what, ref_id: (
                self._current and self.picture_requested.emit(what, self._current, ref_id)
            )
        )
        self.board = CastBoard(pictures)
        # One character's sheet: the page lists the people and goes back.
        for widget in (self.board.people, self.board.back_button, self.board.title):
            widget.hide()
        self.chat_form = ChatForm()
        self.chat_form.changed.connect(self._counted_changed)
        self.lorebook_form = LorebookForm()
        self.lorebook_form.changed.connect(self._counted_changed)
        self.lorebook_form.lorebook_requested.connect(
            lambda what: self._current and self.lorebook_requested.emit(what, self._current)
        )
        self.files_form = FilesForm(self._thumbs)
        self.files_form.changed.connect(self._counted_changed)
        self.files_form.files_dropped.connect(
            lambda paths: self._current and self.files_dropped.emit(paths, self._current)
        )
        self.files_form.greetings_changed.connect(self._card_text_renamed)
        self.files_form.file_requested.connect(
            lambda what, index: (
                self._current and self.file_requested.emit(what, index, self._current)
            )
        )
        self.about_form = AboutForm()
        self.about_form.changed.connect(self._about_changed)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("collectionTabs")
        for widget, (name, tip) in zip(self._tab_forms(), TABS, strict=True):
            index = self.tabs.addTab(widget, name)
            self.tabs.setTabToolTip(index, tip)

        self.pages = QStackedWidget()
        self.pages.addWidget(overview_page)
        self.pages.addWidget(self.tabs)

        right = QVBoxLayout()
        right.setSpacing(8)
        right.addLayout(header)
        right.addWidget(self.stats)
        right.addWidget(self.pages, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)
        layout.addWidget(left)
        layout.addLayout(right, 1)
        for target in (self.people.viewport(), self.overview.viewport(), self.empty):
            target.setAcceptDrops(True)
            target.installEventFilter(self)
        self._show()

    def _fit_left(self) -> None:
        """The list's column, wider with the text (View → Text size): at 150%
        a fixed 300px cut "New character" short."""
        self._left.setFixedWidth(round(LEFT_WIDTH * text_size.scale()))

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange) and hasattr(self, "_left"):
            self._fit_left()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        # Shown only in the whole window, the story's panels put away (as the
        # cast board): it must not widen the story's place in the stack.
        return QSize(0, 0)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(0, 0)

    # --- what the window gives it -----------------------------------------------------------

    def set_characters(self, characters: Sequence[Character]) -> None:
        """Every character kept, versions too. The one shown stays shown (or
        its main card, if it went)."""
        self._characters = list(characters)
        self._fill_tags()
        if self._current is not None and self._card(self._current) is None:
            self._current = None
        self._fill()
        self._show()

    def _fill_tags(self) -> None:
        tags = collection.tags_of(self._characters)
        chosen = self.tag.currentText()
        self.tag.blockSignals(True)
        self.tag.clear()
        self.tag.addItem(ALL_TAGS)
        self.tag.addItems(tags)
        self.tag.setCurrentText(chosen if chosen in tags else ALL_TAGS)
        self.tag.blockSignals(False)
        self.tag.setVisible(bool(tags))

    def set_story_open(self, story_open: bool) -> None:
        self._story_open = story_open
        card = self._card(self._current)
        self.add_button.setVisible(card is not None and story_open)

    def set_back_words(self, words: str) -> None:
        self.back_button.setText(words)

    def set_stats(self, words: str) -> None:
        """A line for everyone (how many, what drawing their pictures cost)."""
        self._extra = words
        self._show_stats()

    def show_character(self, card_id: str | None) -> None:
        """A character's card (a version's, if that is the id), or everyone."""
        self._current = card_id if card_id and self._card(card_id) is not None else None
        self._select_person()
        self._show()

    def show_tab(self, name: str) -> None:
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == name:
                self.tabs.setCurrentIndex(index)

    @property
    def current(self) -> str | None:
        return self._current

    def _tab_forms(self) -> tuple[QWidget, ...]:
        return (
            self.card_form,
            self.board,
            self.chat_form,
            self.lorebook_form,
            self.files_form,
            self.about_form,
        )

    def _forms(self) -> tuple:
        """Every form that edits the card shown (the board is filled apart)."""
        return (
            self.card_form,
            self.chat_form,
            self.lorebook_form,
            self.files_form,
            self.about_form,
        )

    def show_book(self) -> None:
        """Their lorebook again, after the window changed it whole."""
        self.lorebook_form.load(self._card(self._current))
        self._show_stats()

    def show_files(self, chosen: int | None = None) -> None:
        """Their card's files again, after the window added or took one off."""
        self.files_form.load(self._card(self._current), chosen=chosen)
        self._show_stats()

    def current_character(self) -> Character | None:
        return self._card(self._current)

    def refresh_pictures(self) -> None:
        """Pictures changed under the page (one drawn has landed): shown
        anew, the fields left as they are. A picture's file never changes
        under its name, so what was read stays read."""
        self.card_form.refresh_pictures()
        self._fill()

    def _card_text_renamed(self) -> None:
        """A file renamed in the card's text (from the Files tab): the forms
        that show that text read it again."""
        card = self._card(self._current)
        self.chat_form.load(card)
        self.card_form.load(card)

    def card_changed(self, card_id: str) -> None:
        """The card shown was changed from outside the page (a story saved its
        changes to it): the forms read it again, or the next keystroke in one
        would write their old text back over the change."""
        card = self._card(self._current)
        if card is None or card.id != card_id:
            return
        for form in self._forms():
            form.load(card)
        self._show()

    def forget_forms(self) -> None:
        """The forms loaded afresh the next time a card shows: its fields
        may have changed while the page was put away."""
        self._loaded = None
        for form in self._forms():
            form.load(None)

    def forget_pictures(self) -> None:
        self._thumbs.forget()

    def forget_file(self, file: str) -> None:
        self._thumbs.forget_file(file)

    # --- the list -------------------------------------------------------------------------

    def _card(self, card_id: str | None) -> Character | None:
        return next((c for c in self._characters if c.id == card_id), None)

    def _shown_mains(self) -> list[Character]:
        """The main cards to list: a family shows when any of its cards
        answers the search and has the tag chosen."""
        words = self.search.text().strip()
        tag = self.tag.currentText() if self.tag.currentIndex() > 0 else ""
        shown = []
        for main in collection.mains(self._characters):
            family = collection.family_of(main, self._characters)
            if words and not any(collection.matches(card, words) for card in family):
                continue
            if tag and not any(collection.has_tag(card, tag) for card in family):
                continue
            shown.append(main)
        sort = self.sort.currentIndex()
        if sort == 1:
            shown.sort(key=lambda c: collection.info(c).changed_at, reverse=True)
        elif sort == 2:
            shown.sort(key=lambda c: collection.info(c).added_at, reverse=True)
        else:
            shown.sort(key=lambda c: c.name.casefold())
        return shown

    def _line(self, main: Character) -> str:
        """Under a name: how many versions, the tags."""
        family = collection.family_of(main, self._characters)
        parts = []
        if len(family) > 1:
            parts.append(_plural(len(family), "version"))
        tags = main.tavern.tags if main.tavern else []
        if tags:
            parts.append(", ".join(tags[:3]))
        return " · ".join(parts)

    def _fill(self) -> None:
        shown = self._shown_mains()
        self.people.blockSignals(True)
        self.people.clear()
        everyone = QListWidgetItem(f"{EVERYONE}\n{_plural(len(shown), 'character')}")
        everyone.setData(Qt.UserRole, None)
        everyone.setIcon(self._blank_icon)
        self.people.addItem(everyone)
        for main in shown:
            line = self._line(main)
            item = QListWidgetItem(f"{main.name}\n{line}" if line else main.name)
            item.setData(Qt.UserRole, main.id)
            first = main.reference_images[0] if main.reference_images else None
            # Read a few a tick (Thumbs), filled in as they come: read all at
            # once, a large collection held the page before it showed.
            item.setData(PORTRAIT_ROLE, first.file if first is not None else "")
            item.setIcon(self._blank_icon)
            item.setToolTip(f"{main.name}\n{line}" if line else main.name)
            self.people.addItem(item)
        self.people.blockSignals(False)
        self._people_icons()
        self._select_person()
        self.overview.clear()
        for main in shown:
            item = QListWidgetItem()
            item.setData(CARD_ROLE, (main, self._line(main)))
            item.setData(Qt.UserRole, main.id)
            item.setToolTip(f"{main.name}: click to see their card")
            self.overview.addItem(item)
        self.empty.setVisible(not self._characters)
        self.overview.setVisible(bool(self._characters))
        self._show_stats()

    def _select_person(self) -> None:
        card = self._card(self._current)
        main_id = collection.main_of(card, self._characters).id if card is not None else None
        self.people.blockSignals(True)
        for row in range(self.people.count()):
            if self.people.item(row).data(Qt.UserRole) == main_id:
                self.people.setCurrentRow(row)
                break
        else:
            self.people.setCurrentRow(-1)
        self.people.blockSignals(False)

    def _on_person(self, item: QListWidgetItem | None, _previous=None) -> None:
        if item is not None:
            self.show_character(item.data(Qt.UserRole))

    def _tile_chosen(self, item: QListWidgetItem) -> None:
        self.show_character(item.data(Qt.UserRole))

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        """Character cards dropped on the list or the overview are kept, as
        Import does (SillyTavern takes cards so)."""
        kind = event.type()
        if kind in (QEvent.DragEnter, QEvent.DragMove, QEvent.Drop) and watched in (
            self.people.viewport(),
            self.overview.viewport(),
            self.empty,
        ):
            paths = [
                url.toLocalFile()
                for url in event.mimeData().urls()
                if url.isLocalFile() and url.toLocalFile().lower().endswith(CARD_SUFFIXES)
            ]
            if not paths:
                return False
            event.acceptProposedAction()
            if kind == QEvent.Drop:
                self.cards_dropped.emit(paths)
            return True
        return super().eventFilter(watched, event)

    def _thumbs_ready(self) -> None:
        self.overview.viewport().update()
        self.files_form.refresh_file_icons()
        self._people_icons()

    def _people_icons(self) -> None:
        """Each name's portrait, once it has been read."""
        for row in range(self.people.count()):
            item = self.people.item(row)
            file = item.data(PORTRAIT_ROLE)
            if not file or item.data(PORTRAIT_ROLE + 1):
                continue
            pixmap = self._thumbs.get(file, PEOPLE_ICON.width(), PEOPLE_ICON.height())
            if pixmap is not None:
                item.setData(PORTRAIT_ROLE + 1, True)
                if not pixmap.isNull():
                    item.setIcon(QIcon(pixmap))

    # --- one of them ----------------------------------------------------------------------

    def _show(self) -> None:
        card = self._card(self._current)
        if card is not self._loaded:
            self._loaded = card
            self.character_shown.emit(card.id if card is not None else "")
        for widget in (self.chat_button, self.draft_button, self.export_button, self.more_button):
            widget.setVisible(card is not None)
        self.add_button.setVisible(card is not None and self._story_open)
        self.expressions_action.setEnabled(card is not None)
        if card is None:
            self.title.setText(PAGE_LABEL)
            self.version.hide()
            self.version_label.hide()
            self.pages.setCurrentIndex(0)
            for form in (self.card_form, self.chat_form, self.about_form):
                form.load(None)
            self._show_stats()
            return
        self.title.setText(card.name)
        family = collection.family_of(card, self._characters)
        self.version.blockSignals(True)
        self.version.clear()
        for other in family:
            self.version.addItem(collection.version_name(other, self._characters), other.id)
        self.version.setCurrentIndex(max(0, self.version.findData(card.id)))
        self.version.blockSignals(False)
        self.version.setVisible(len(family) > 1)
        self.version_label.setVisible(len(family) > 1)
        main = family[0] is card
        self.rename_action.setEnabled(not main)
        self.rename_action.setToolTip(
            "What this version is called"
            if not main
            else "The main card has no version name: choose a version above to rename it"
        )
        self.png_action.setEnabled(bool(card.reference_images))
        self.png_action.setToolTip(
            "Their portrait with the card inside it, as SillyTavern shares cards"
            if card.reference_images
            else "A PNG card is their portrait: give them one first (Card tab)"
        )
        if self.card_form.character is not card:
            for form in self._forms():
                form.load(card)
        self.pages.setCurrentIndex(1)
        self._show_stats()

    def _show_stats(self) -> None:
        card = self._card(self._current)
        if card is None:
            count = len(collection.mains(self._characters))
            shown = len(self._shown_mains())
            kept = (
                _plural(count, "character")
                if shown == count
                else f"{shown} of {_plural(count, 'character')} shown"
            )
            words = (
                f"{kept}, kept for any story or chat. Click one to see their card." if count else ""
            )
        else:
            parts = [_plural(len(card.reference_images), "picture")]
            files = len(card.tavern.assets) if card.tavern else 0
            if files:
                parts.append(_plural(files, "file") + " on the card")
            greetings = len(card.tavern.greetings) if card.tavern else 0
            if greetings:
                parts.append(_plural(greetings, "greeting"))
            book = card.tavern.book_entries if card.tavern else 0
            if book:
                parts.append(f"{book} lore entr{'y' if book == 1 else 'ies'}")
            words = " · ".join(parts)
        self.stats.setText(f"{words}  {self._extra}".strip())

    def _on_version(self, index: int) -> None:
        card_id = self.version.itemData(index)
        if card_id:
            self.show_character(card_id)

    def _act(self, what: str) -> None:
        if self._current is not None:
            self.action_requested.emit(what, self._current)

    def _card_changed(self) -> None:
        card = self._card(self._current)
        if card is None:
            return
        self.title.setText(card.name)
        item = self.people.currentItem()
        if item is not None and collection.is_main(card, self._characters):
            line = self._line(card)
            item.setText(f"{card.name}\n{line}" if line else card.name)
        self.changed.emit(card.id)

    def _name_finished(self) -> None:
        """A name typed in full: kept in place in a list sorted by name, and
        a field left blank shows the name the card keeps."""
        card = self._card(self._current)
        if card is None:
            return
        if not self.card_form.name.text().strip():
            self.card_form.name.setText(card.name)
        if self.sort.currentIndex() == 0:
            self._fill()

    def _counted_changed(self) -> None:
        """A change the line over the tabs counts (lore entries, files)."""
        if self._current is not None:
            self._show_stats()
            self.changed.emit(self._current)

    def _about_changed(self) -> None:
        card = self._card(self._current)
        if card is None:
            return
        # The list and the tiles show the tags, and filter by them: filled
        # again when the tags changed, not at every key typed in the maker's
        # notes (each fill goes through every card: slow with hundreds).
        listed = [self.tag.itemText(row) for row in range(1, self.tag.count())]
        main = collection.main_of(card, self._characters)
        line = self._line(main)
        item = self.people.currentItem()
        shown = item.text() if item is not None else None
        if list(collection.tags_of(self._characters)) != listed or shown not in (
            None,
            f"{main.name}\n{line}" if line else main.name,
        ):
            self._fill_tags()
            self._fill()
        self.changed.emit(card.id)
