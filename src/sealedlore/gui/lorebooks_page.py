"""Lorebooks (File → Lorebooks…, the start page): every lorebook the author
keeps for any story, in the whole window, as Your characters shows their
characters (gui/collection_page.py).

The author (Nov 20 2026): a lorebook on its own, not a character's, kept,
changed, imported and exported, and a story started from it with anyone from
Your characters. The page lists them (searched, sorted) and shows one with
its entries in the editor a character's own lorebook has
(gui/lorebook_form.py); keeping, exporting and starting stories are the
window's (gui/window_lorebooks.py).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui import text_size
from sealedlore.gui.fields import set_field_text
from sealedlore.gui.lorebook_form import LorebookForm
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.models.lorebook import StoredLorebook
from sealedlore.storage import lorebooks

PAGE_LABEL = "Lorebooks"
SORTS = ("By name", "Changed last", "Added last")
LEFT_WIDTH = 300
BOOK_ROLE = Qt.UserRole + 1
# The actions the page asks the window for (`action_requested`).
NEW, IMPORT, LINK, EXPORT, DUPLICATE, DELETE = (
    "new",
    "import",
    "link",
    "export",
    "duplicate",
    "delete",
)
STORY, PEOPLE, INTO_STORY = "story", "people", "into_story"
SCOPE_WORDS = (
    "Lorebooks are kept for any story: a world's lore to start stories in, with anyone from "
    "Your characters. A story takes a copy of the entries, so what you change here reaches "
    "only stories made from it afterwards."
)
FORM_INTRO = (
    "Every entry of the lorebook, to read, change, add or remove. A story uses an entry's "
    "keywords, its text and whether it is always sent or switched off; the rest is kept "
    "for SillyTavern and other apps, and goes out again when the lorebook is exported."
)
EMPTY_WORDS = (
    "No lorebooks yet. Import one: a SillyTavern lorebook, one NovelAI, Agnai or RisuAI "
    "made, or the lorebook on a character card. Or start a new one and write its entries."
)
STORY_LABEL = "New story from this lorebook…"
PEOPLE_LABEL = "Find the people…"
INTO_STORY_LABEL = "Add to the open story…"
# The name is saved this long after the last key.
NAME_DELAY_MS = 400


class _Shelved:
    """A kept lorebook as the editor holds a book (gui/lorebook_form.py)."""

    def __init__(self, book: StoredLorebook) -> None:
        self.stored = book

    @property
    def character_book(self) -> dict[str, Any] | None:
        return self.stored.book or None

    @character_book.setter
    def character_book(self, book: dict[str, Any] | None) -> None:
        self.stored.book = book or {}


class LorebooksPage(QWidget):
    back_requested = Signal()
    # (what, book id): NEW, IMPORT… for the window to do.
    action_requested = Signal(str, str)
    # The book shown was changed (its name, an entry): the window saves it.
    changed = Signal(str)
    # Lorebook files dropped on the page, to import.
    files_dropped = Signal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("lorebooksPage")
        self.setFocusPolicy(Qt.ClickFocus)
        self._books: list[StoredLorebook] = []
        self._current: str | None = None
        self._loaded: StoredLorebook | None = None
        self._story_open = False

        # --- the list, on the left ---
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search your lorebooks")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self._fill())
        self.sort = QComboBox()
        self.sort.addItems(list(SORTS))
        self.sort.currentIndexChanged.connect(lambda _index: self._fill())
        self.list = QListWidget()
        self.list.setObjectName("lorebookList")
        self.list.setWordWrap(True)
        self.list.setSpacing(2)
        self.list.currentItemChanged.connect(self._on_chosen)
        delete_key = QShortcut(QKeySequence(QKeySequence.Delete), self.list)
        delete_key.setContext(Qt.WidgetShortcut)
        delete_key.activated.connect(lambda: self._act(DELETE))
        self.new_button = QPushButton("New lorebook")
        self.new_button.setObjectName("sendButton")
        self.new_button.setToolTip("An empty lorebook, to write the entries of")
        self.new_button.clicked.connect(lambda: self.action_requested.emit(NEW, ""))
        self.import_button = QToolButton()
        self.import_button.setText("Import")
        self.import_button.setPopupMode(QToolButton.InstantPopup)
        import_menu = QMenu(self.import_button)
        import_menu.addAction("Lorebooks from files…").triggered.connect(
            lambda: self.action_requested.emit(IMPORT, "")
        )
        import_menu.addAction("A lorebook from a link…").triggered.connect(
            lambda: self.action_requested.emit(LINK, "")
        )
        import_menu.setToolTipsVisible(True)
        self.import_button.setMenu(import_menu)
        self.import_button.setToolTip(
            "SillyTavern lorebooks (JSON), NovelAI's, Agnai's and RisuAI's, or the lorebook "
            "on a character card"
        )
        left_buttons = WrapRow(spacing=6, group_spacing=6)
        left_buttons.add_group([self.new_button])
        left_buttons.add_group([self.import_button])
        left = QWidget()
        self._left = left
        self._fit_left()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.sort)
        left_layout.addWidget(self.list, 1)
        left_layout.addLayout(left_buttons)

        # --- the header over the right side ---
        self.title = QLabel(PAGE_LABEL)
        self.title.setObjectName("boardTitle")
        self.story_button = QPushButton(STORY_LABEL)
        self.story_button.setObjectName("sendButton")
        self.story_button.setToolTip(
            "A new story set in this lorebook's world: its entries become the story's lore, "
            "and you choose who you play and who else comes from Your characters"
        )
        self.story_button.clicked.connect(lambda: self._act(STORY))
        self.people_button = QPushButton(PEOPLE_LABEL)
        self.people_button.setToolTip(
            "The authoring model reads the entries and finds the people they describe, to "
            "keep in Your characters, and points out instructions written for the storyteller"
        )
        self.people_button.clicked.connect(lambda: self._act(PEOPLE))
        self.into_story_button = QPushButton(INTO_STORY_LABEL)
        self.into_story_button.setToolTip(
            "Its entries into the open story's lore: you choose which, and can change them "
            "for that story alone"
        )
        self.into_story_button.clicked.connect(lambda: self._act(INTO_STORY))
        self.export_button = QPushButton("Export…")
        self.export_button.setToolTip(
            "The lorebook as a SillyTavern lorebook file, every setting it came with kept"
        )
        self.export_button.clicked.connect(lambda: self._act(EXPORT))
        self.more_button = QToolButton()
        self.more_button.setText("More")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        more = QMenu(self.more_button)
        more.addAction("Duplicate").triggered.connect(lambda: self._act(DUPLICATE))
        more.addAction("Delete…").triggered.connect(lambda: self._act(DELETE))
        self.more_button.setMenu(more)
        self.back_button = QPushButton("Back to the story")
        self.back_button.setToolTip("Esc")
        self.back_button.clicked.connect(self.back_requested)
        header = WrapRow(group_spacing=12)
        header.add_group([self.title])
        header.add_group([self.story_button, self.people_button, self.into_story_button])
        header.add_group([self.export_button, self.more_button])
        header.add_group([self.back_button], right=True)

        self.name = QLineEdit()
        self.name.setPlaceholderText("The lorebook's name")
        self.name.textEdited.connect(lambda _text: self._name_timer.start())
        self.name.editingFinished.connect(self._name_finished)
        self._name_timer = QTimer(self)
        self._name_timer.setSingleShot(True)
        self._name_timer.setInterval(NAME_DELAY_MS)
        self._name_timer.timeout.connect(self._name_finished)
        self.stats = QLabel("")
        self.stats.setObjectName("hintLabel")
        self.stats.setWordWrap(True)
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_label = QLabel("Name")
        name_row.addWidget(name_label)
        name_row.addWidget(self.name, 1)
        self.form = LorebookForm(
            intro_text=FORM_INTRO, own_name=False, whose="this", book_buttons=False
        )
        self.form.changed.connect(self._form_changed)
        book_page = QWidget()
        book_layout = QVBoxLayout(book_page)
        book_layout.setContentsMargins(0, 0, 0, 0)
        book_layout.setSpacing(6)
        book_layout.addLayout(name_row)
        book_layout.addWidget(self.stats)
        book_layout.addWidget(self.form, 1)
        self.empty = QLabel(EMPTY_WORDS)
        self.empty.setObjectName("hintLabel")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.empty)
        self.pages.addWidget(book_page)

        self.scope = QLabel(SCOPE_WORDS)
        self.scope.setObjectName("scopeNote")
        self.scope.setWordWrap(True)
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addLayout(header)
        right.addWidget(self.scope)
        right.addWidget(self.pages, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)
        layout.addWidget(left)
        layout.addLayout(right, 1)
        for target in (self.list.viewport(), self.empty):
            target.setAcceptDrops(True)
            target.installEventFilter(self)
        self._show()

    def _fit_left(self) -> None:
        self._left.setFixedWidth(round(LEFT_WIDTH * text_size.scale()))

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange) and hasattr(self, "_left"):
            self._fit_left()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        # Shown only in the whole window: it must not widen the story's place.
        return QSize(0, 0)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(0, 0)

    # --- what the window gives it ---------------------------------------------------------

    def set_books(self, books: Sequence[StoredLorebook]) -> None:
        self._books = list(books)
        if self._current not in {book.id for book in self._books}:
            self._current = self._books[0].id if self._books else None
        self._fill()

    def set_story_open(self, story_open: bool) -> None:
        self._story_open = story_open
        self._sync_buttons()

    def set_back_words(self, words: str) -> None:
        self.back_button.setText(words)

    def show_book(self, book_id: str | None) -> None:
        if book_id is not None:
            self._current = book_id
            self.search.clear()
        self._fill()

    def current(self) -> str | None:
        return self._current

    def current_book(self) -> StoredLorebook | None:
        return self._book(self._current)

    def forget_form(self) -> None:
        """Load the book shown again (it was changed from outside the page)."""
        self._loaded = None
        self._show()

    def refresh_stats(self) -> None:
        book = self.current_book()
        if book is not None:
            self._say_stats(book)
            item = self.list.currentItem()
            if item is not None:
                item.setText(self._line(book))

    # --- the list ---------------------------------------------------------------------------

    def _book(self, book_id: str | None) -> StoredLorebook | None:
        return next((book for book in self._books if book.id == book_id), None)

    def _line(self, book: StoredLorebook) -> str:
        found = lorebooks.stats(book.book)
        return f"{book.name}\n{found.entries} entries · {found.words:,} words"

    def _shown(self) -> list[StoredLorebook]:
        words = self.search.text().casefold().split()
        shown = [b for b in self._books if all(w in lorebooks.searchable(b) for w in words)]
        sort = self.sort.currentIndex()
        if sort == 1:
            shown.sort(key=lambda book: book.changed_at, reverse=True)
        elif sort == 2:
            shown.sort(key=lambda book: book.added_at, reverse=True)
        else:
            shown.sort(key=lambda book: book.name.casefold())
        return shown

    def _fill(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        chosen = None
        for book in self._shown():
            item = QListWidgetItem(self._line(book))
            item.setData(BOOK_ROLE, book.id)
            item.setToolTip(book.came_from or book.name)
            self.list.addItem(item)
            if book.id == self._current:
                chosen = item
        if chosen is None and self.list.count():
            chosen = self.list.item(0)
            self._current = chosen.data(BOOK_ROLE)
        if chosen is not None:
            self.list.setCurrentItem(chosen)
        self.list.blockSignals(False)
        self._show()

    def _on_chosen(self, item: QListWidgetItem | None, _previous=None) -> None:
        if item is None:
            return
        self._name_finished()
        self._current = item.data(BOOK_ROLE)
        self._show()

    def _show(self) -> None:
        book = self.current_book()
        self.pages.setCurrentIndex(0 if book is None else 1)
        self._sync_buttons()
        if book is None:
            self._loaded = None
            self.form.load_book(None)
            return
        if book is not self._loaded:
            self._loaded = book
            set_field_text(self.name, book.name)
            self.form.load_book(_Shelved(book))
        self._say_stats(book)

    def _say_stats(self, book: StoredLorebook) -> None:
        parts = [lorebooks.stats_words(book.book)]
        if book.came_from:
            parts.append(f"from {book.came_from}")
        self.stats.setText(" · ".join(parts))

    def _sync_buttons(self) -> None:
        book = self.current_book()
        has = book is not None
        entries = has and bool(lorebooks.stats(book.book).entries)
        for button in (self.story_button, self.export_button, self.more_button):
            button.setEnabled(has)
        self.people_button.setEnabled(entries)
        self.into_story_button.setVisible(self._story_open)
        self.into_story_button.setEnabled(entries)

    # --- edits ------------------------------------------------------------------------------

    def _act(self, what: str) -> None:
        if self._current is not None:
            self._name_finished()
            self.action_requested.emit(what, self._current)

    def _name_finished(self) -> None:
        self._name_timer.stop()
        book = self._loaded
        if book is None:
            return
        name = self.name.text().strip()
        if name and name != book.name:
            book.name = name
            self.changed.emit(book.id)
            item = self.list.currentItem()
            if item is not None:
                item.setText(self._line(book))

    def _form_changed(self) -> None:
        book = self._loaded
        if book is not None:
            self._say_stats(book)
            item = self.list.currentItem()
            if item is not None:
                item.setText(self._line(book))
            self.changed.emit(book.id)

    # --- files dropped ----------------------------------------------------------------------

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        kind = event.type()
        if kind in (QEvent.DragEnter, QEvent.DragMove) and event.mimeData().hasUrls():
            event.acceptProposedAction()
            return True
        if kind == QEvent.Drop and event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return True
        return super().eventFilter(watched, event)
