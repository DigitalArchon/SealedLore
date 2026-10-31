"""The story list (left dock).

Each row is a story's title, over up to two lines, and under it, smaller and
dimmer, what kind it is, how many messages it has, whether it has models of its
own, and when it was started and last played (`_StoryRow`). Cut to one line, the
author's titles ("Between Stars: The Long Way Home", "Test · Sonnet 4.6 ·
Between Stars long run") all read "Between Stars: T…" or "Test · Sonnet 4.6 …".

Above them, a search of everything in every story (storage/story_search.py, on a
thread of its own: a library takes about a second) and the order they are in.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QModelIndex, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPalette,
    QShortcut,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.routing import differing_models
from sealedlore.gui import theme
from sealedlore.gui.start_page import Recent, when_played
from sealedlore.models.config import Config, model_label
from sealedlore.storage.library import StoryListing, story_listings
from sealedlore.storage.repository import VISUAL_ELSEWHERE
from sealedlore.storage.story_search import TITLE, StoryHit, search_stories
from sealedlore.threads import BackgroundThread

# After the last key: every story is read for each search.
SEARCH_DELAY_MS = 250
SORTS = {"played": "Last played", "title": "Title"}
OWN_MODELS = "own models"
ROLE_NAMES = {
    "story": "Story",
    "summarisation": "Summaries",
    "authoring": "Authoring",
    "scene": "Scene read",
    "plot": "Plot",
    "lore": "Lore picks",
    "image_prompt": "Picture prompts",
}


class _SearchSignals(QObject):
    """From the search's thread: (which search, a story found) and (which
    search, finished). A plain thread's signals reach the panel queued."""

    found = Signal(int, object)
    finished = Signal(int)


def _dates(listing: StoryListing) -> str:
    if not listing.started:
        made = when_played(listing.made)
        return f"Made {made}" if made else ""
    started, played = when_played(listing.started), when_played(listing.played)
    return f"Started {started} · Played {played}"


def _models_tip(models: dict[str, str]) -> str:
    lines = (f"  {ROLE_NAMES[role]}: {model_label(model)}" for role, model in models.items())
    return "Its own models (Story → Setup → Models):\n" + "\n".join(lines)


class StoryListPanel(QWidget):
    story_activated = Signal(str)
    # A search's result opened: (story id, what to look for in it; "" for
    # nothing Find in story could go to).
    found_story_activated = Signal(str, str)
    # The order chosen, to be kept (`Config.stories_sort`).
    sort_changed = Signal(str)
    new_story_requested = Signal()
    # Right-click menu (and the Delete key); each carries the story id.
    duplicate_settings_requested = Signal(str)
    duplicate_story_requested = Signal(str)
    delete_requested = Signal(str)
    open_folder_requested = Signal(str)
    # (story id, shared): Share with phone, while phone access is on.
    share_requested = Signal(str, bool)
    # The list was read again (a story made, deleted, played).
    refreshed = Signal()

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        # Every readable story, played last first.
        self._played: list[StoryListing] = []
        # The settings, for which of a story's own models differ from them.
        self._config: Callable[[], Config | None] = lambda: None
        # The search in force: its words, its results so far, and its thread.
        self._query = ""
        self._hits: dict[str, StoryHit] = {}
        self._search = 0
        self._stop: threading.Event | None = None
        self._thread: BackgroundThread | None = None
        self._results_in = False
        # Phone access (gui/window_phone.py): the shared stories while it is
        # on, None while it is off; and which stories keep a map (never shared).
        self._phone_shared: set[str] | None = None
        # Stories that can't be opened: never marked as on the phones.
        self._unreadable: set[str] = set()
        self.root = root

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.search = QLineEdit()
        self.search.setObjectName("storySearch")
        self.search.setPlaceholderText("Search every story…")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip(
            "Every message on every branch, the chapters, your Questions, the world, the "
            "opening and the cards, in every story. All the words must be in the story."
        )
        self.search.textChanged.connect(self._search_typed)
        layout.addWidget(self.search)
        self._search_wait = QTimer(self)
        self._search_wait.setSingleShot(True)
        self._search_wait.setInterval(SEARCH_DELAY_MS)
        self._search_wait.timeout.connect(self._start_search)
        self._signals = _SearchSignals(self)
        self._signals.found.connect(self._on_found)
        self._signals.finished.connect(self._on_search_finished)
        self.search_note = QLabel()
        self.search_note.setObjectName("hintLabel")
        self.search_note.setWordWrap(True)
        self.search_note.hide()
        layout.addWidget(self.search_note)

        order = QHBoxLayout()
        order.setContentsMargins(0, 0, 0, 0)
        caption = QLabel("Sort by")
        caption.setObjectName("hintLabel")
        self.sort = QComboBox()
        for value, label in SORTS.items():
            self.sort.addItem(label, value)
        self.sort.currentIndexChanged.connect(self._sort_chosen)
        order.addWidget(caption)
        order.addWidget(self.sort, 1)
        layout.addLayout(order)

        self.list = QListWidget()
        self.list.setObjectName("storyList")
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        # A long title wraps to a second line and is cut with "…" after it
        # (the tooltip has it whole), rather than a horizontal scrollbar
        # under a dock this narrow. Rows are measured again at each width.
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setResizeMode(QListView.Adjust)
        self.list.setItemDelegate(_StoryRow(self.list))
        # Activated alone: a double-click is also an activation (or a single
        # click, where the desktop says so), and with both connected one
        # double-click opened the story twice, the second cancelling what
        # the first had already sent for its map (Oct 19 2026).
        self.list.itemActivated.connect(self._activate)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_menu)
        delete_key = QShortcut(QKeySequence.Delete, self.list)
        delete_key.setContext(Qt.WidgetShortcut)
        delete_key.activated.connect(self._delete_current)
        layout.addWidget(self.list, 1)

        new_button = QPushButton("New…")
        new_button.clicked.connect(self.new_story_requested)
        layout.addWidget(new_button)

    def refresh(self, *, selected_id: str | None = None) -> None:
        """Rebuild the list from `story.json` alone (and a count of the
        messages), most recently played first.

        This runs after every job; loading and validating every story's whole
        bundle here grew with the library and was paid on every turn.
        """
        self.list.clear()
        self._played.clear()
        self._unreadable.clear()
        try:
            self._fill(selected_id)
        finally:
            self.refreshed.emit()
        # What it found may have changed with whatever this follows.
        if self._query and self.isVisible():
            self._search_wait.start()

    def use_config(self, config: Callable[[], Config | None]) -> None:
        """Where the settings are read from, as they stand at each refresh."""
        self._config = config

    def set_sort(self, sort: str) -> None:
        """The order kept from last time, without saying it changed."""
        self.sort.blockSignals(True)
        self.sort.setCurrentIndex(max(self.sort.findData(sort), 0))
        self.sort.blockSignals(False)

    def _sort_chosen(self) -> None:
        self.sort_changed.emit(self.sort.currentData())
        current = self.list.currentItem()
        self.refresh(selected_id=current.data(Qt.UserRole) if current is not None else None)

    def has_stories(self) -> bool:
        """Any story at all, readable or not."""
        return self.list.count() > 0

    def _fill(self, selected_id: str | None) -> None:
        listings = story_listings(self.root)
        self._played = [listing for listing in listings if listing.error is None]
        if self.sort.currentData() == "title":
            # Unreadable ones stay last, as in the other order.
            listings.sort(key=lambda listing: (listing.error is not None, listing.title.casefold()))
        config = self._config()
        for listing in listings:
            error = listing.error
            own: dict[str, str] = {}
            if error is None:
                count = listing.messages
                own = differing_models(listing.models, config) if config is not None else {}
                words = f"{count} message{'s' if count != 1 else ''}" if count else "Not begun"
                mark = f" · {OWN_MODELS}" if own else ""
                label = f"{listing.title}\n{listing.kind} · {words}{mark}\n{_dates(listing)}"
            elif error == VISUAL_ELSEWHERE:
                label = f"{listing.title}\nA Visual Story, not played here"
            else:
                # Shown rather than skipped: a story that vanished without a
                # word can't be looked into or deleted. Opening it says why.
                reason = (error.splitlines() or ["unreadable"])[0][:80]
                label = f"{listing.id}\n⚠ can't be read: {reason}"
            story_id = listing.id
            if error is not None:
                self._unreadable.add(story_id)
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, story_id)
            item.setData(PHONE_ROLE, self._phone_mark(story_id))
            item.setToolTip(listing.title + (f"\n\n{_models_tip(own)}" if own else ""))
            if error is not None:
                item.setForeground(QBrush(Qt.gray))
                item.setToolTip(error)
            self.list.addItem(item)
            if story_id == selected_id:
                self.list.setCurrentItem(item)
        self._show_results()

    def recent(self, count: int) -> list[Recent]:
        """The `count` stories played last, readable ones only, for the start
        page: title, and how many messages and when."""
        out = []
        for listing in self._played[:count]:
            messages = listing.messages
            words = f"{messages} message{'s' if messages != 1 else ''}"
            words = words if messages else "Not begun yet"
            played = when_played(listing.last_active)
            detail = f"{words} · played {played}" if played else words
            out.append(Recent(listing.id, listing.title, detail))
        return out

    # --- the search ------------------------------------------------------------

    def _search_typed(self, text: str) -> None:
        self._search_wait.stop()
        if text.strip():
            self._search_wait.start()
        else:
            self._cancel_search()
            self._query = ""
            self._hits = {}
            self._show_results()

    def _cancel_search(self) -> None:
        self._search += 1
        if self._stop is not None:
            self._stop.set()
            self._stop = None
        self._thread = None

    def stop_search(self) -> None:
        """Before the window closes: the thread reads files and nothing else,
        and ends at the next text it comes to."""
        self._search_wait.stop()
        thread = self._thread
        self._cancel_search()
        if thread is not None:
            thread.join(2)

    def _start_search(self) -> None:
        query = self.search.text().strip()
        self._cancel_search()
        self._query = query
        self._results_in = False
        self.search_note.setText("Searching…")
        self.search_note.show()
        config = self._config()
        spoilers = config.show_plot_spoilers if config is not None else False
        stop = threading.Event()
        search = self._search
        root, signals = self.root, self._signals

        def run() -> None:
            try:
                for hit in search_stories(query, root, spoilers=spoilers, stop=stop):
                    signals.found.emit(search, hit)
                signals.finished.emit(search)
            except RuntimeError:
                pass  # the panel has gone

        self._stop = stop
        self._thread = BackgroundThread(target=run, name="story-search")
        self._thread.start()

    def _on_found(self, search: int, hit: StoryHit) -> None:
        if search != self._search:
            return
        if not self._results_in:
            # What was shown stays until there is something better to show.
            self._results_in = True
            self._hits = {}
        self._hits[hit.story_id] = hit
        self._show_results()

    def _on_search_finished(self, search: int) -> None:
        if search != self._search:
            return
        self._stop, self._thread = None, None
        if not self._results_in:
            self._hits = {}
            self._results_in = True
        self._show_results()
        found = len(self._hits)
        self.search_note.setText(
            f"{found} {'story' if found == 1 else 'stories'} with “{self._query}”"
            if found
            else f"No story has “{self._query}”."
        )

    def _show_results(self) -> None:
        """Each row shown if it holds what is being searched for, with the
        words around the match."""
        searching = bool(self._query)
        shows_results = searching and self._results_in
        for row in range(self.list.count()):
            item = self.list.item(row)
            hit = self._hits.get(item.data(Qt.UserRole))
            item.setHidden(shows_results and hit is None)
            words = f"{hit.where}: {hit.snippet}" if hit is not None and hit.where != TITLE else ""
            item.setData(FOUND_ROLE, words if searching else "")
        # A row's height follows the words under it, and changed data alone
        # doesn't measure it again.
        self.list.doItemsLayout()
        if not searching:
            self.search_note.hide()

    def _show_menu(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        story_id = item.data(Qt.UserRole) if item is not None else None
        if not story_id:
            return
        self.list.setCurrentItem(item)
        self.menu_for(story_id).exec(self.list.viewport().mapToGlobal(position))

    def set_phone(self, shared: set[str] | None) -> None:
        """The stories shared with phones, while phone access is on (None:
        off): marked in their rows, and Share with phone in their menus."""
        self._phone_shared = None if shared is None else set(shared)
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setData(PHONE_ROLE, self._phone_mark(item.data(Qt.UserRole)))

    def _phone_mark(self, story_id: str) -> str:
        if self._phone_shared is None or story_id not in self._phone_shared:
            return ""
        return "" if story_id in self._unreadable else "on your phones"

    def menu_for(self, story_id: str) -> QMenu:
        """A story's right-click menu."""
        menu = QMenu(self)
        menu.addAction("Open", lambda: self.story_activated.emit(story_id))
        readable = any(listing.id == story_id for listing in self._played)
        if self._phone_shared is not None and readable:
            shared = story_id in self._phone_shared
            share = menu.addAction("Share with phone")
            share.setCheckable(True)
            share.setChecked(shared)
            share.setToolTip("Phones paired for phone access may see and play it")
            share.toggled.connect(lambda on: self.share_requested.emit(story_id, on))
        # On an unreadable story too: that's where its files most need a look.
        open_folder = menu.addAction(
            "Open folder", lambda: self.open_folder_requested.emit(story_id)
        )
        open_folder.setToolTip("The story's files, in your file manager")
        menu.addSeparator()
        duplicate_settings = menu.addAction(
            "Duplicate settings only", lambda: self.duplicate_settings_requested.emit(story_id)
        )
        duplicate_settings.setToolTip(
            "A new, unplayed story with the same world, cast, lore, style and opening"
        )
        duplicate_story = menu.addAction(
            "Duplicate entire story", lambda: self.duplicate_story_requested.emit(story_id)
        )
        duplicate_story.setToolTip("Everything: every branch, summary and question, and its costs")
        menu.setToolTipsVisible(True)
        menu.addSeparator()
        menu.addAction("Delete…", lambda: self.delete_requested.emit(story_id))
        return menu

    def _delete_current(self) -> None:
        item = self.list.currentItem()
        story_id = item.data(Qt.UserRole) if item is not None else None
        if story_id:
            self.delete_requested.emit(story_id)

    def _activate(self, item: QListWidgetItem) -> None:
        story_id = item.data(Qt.UserRole)
        if not story_id:
            return
        hit = self._hits.get(story_id) if self._query else None
        if hit is not None:
            self.found_story_activated.emit(story_id, hit.find)
        else:
            self.story_activated.emit(story_id)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # Stories may have changed while the list was put away.
        if self._query:
            self._search_wait.start()


# Lines a title may take before it is cut with "…".
TITLE_LINES = 2
# A row's phone mark ("on your phones"), drawn after what it says of itself.
PHONE_ROLE = Qt.UserRole + 1
# The words around a search's match in this story.
FOUND_ROLE = Qt.UserRole + 2
# The count's size beside the title's, and the space between them.
META_SCALE = 0.86
META_GAP = 2


class _StoryRow(QStyledItemDelegate):
    """A row of the story list: the item's text is the title, then a line each
    of what it says of itself. The title wraps to `TITLE_LINES`, the rest is
    drawn smaller and dimmer, wrapping between its " · " parts where a line
    won't fit, and a search's words under it wrap to `TITLE_LINES` too.

    The row's box (padding, divider, selection) is the stylesheet's
    (`QListWidget::item`), drawn by the style; only the text is drawn here."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        opt = self._option(option, index)
        view = option.widget
        width = view.viewport().width() if view is not None else option.rect.width()
        # The style's box around one line of text: padding and divider. Not
        # through the delegate's sizeHint, which reads the item's whole text
        # again: every line of it counted twice.
        opt.text = "X"
        style = view.style() if view is not None else QApplication.style()
        box = style.sizeFromContents(QStyle.CT_ItemViewItem, opt, QSize(), view)
        opt.rect = QRect(0, 0, width, box.height())
        text = self._text_rect(opt)
        title, meta = self._parts(index)
        lines = self._title_lines(title, opt.font, text.width())
        height = box.height() - QFontMetrics(opt.font).height()
        height += len(lines) * QFontMetrics(opt.font).lineSpacing()
        small = self._meta_lines(meta, self._meta_font(opt.font), text.width())
        if small:
            spacing = QFontMetrics(self._meta_font(opt.font)).lineSpacing()
            height += META_GAP + len(small) * spacing
        return QSize(width, height)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        opt = self._option(option, index)
        title, meta = self._parts(index)
        opt.text = ""
        widget = option.widget
        style = widget.style() if widget is not None else None
        if style is None:
            return
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
        rect = self._text_rect(opt)
        selected = bool(opt.state & QStyle.State_Selected)
        foreground = index.data(Qt.ForegroundRole)
        if foreground is not None:
            ink = QBrush(foreground).color()
        elif selected:
            ink = theme.colour("text_selected")
        else:
            ink = opt.palette.color(QPalette.Text)
        painter.save()
        painter.setFont(opt.font)
        painter.setPen(ink)
        metrics = QFontMetrics(opt.font)
        y = rect.top()
        for line in self._title_lines(title, opt.font, rect.width()):
            painter.drawText(
                QRect(rect.left(), y, rect.width(), metrics.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                line,
            )
            y += metrics.lineSpacing()
        font = self._meta_font(opt.font)
        small = QFontMetrics(font)
        painter.setFont(font)
        painter.setPen(ink if foreground is not None else theme.colour("text_dim"))
        y += META_GAP
        for line in self._meta_lines(meta, font, rect.width()):
            painter.drawText(
                QRect(rect.left(), y, rect.width(), small.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                small.elidedText(line, Qt.ElideRight, rect.width()),
            )
            y += small.lineSpacing()
        painter.restore()

    def _option(self, option: QStyleOptionViewItem, index: QModelIndex) -> QStyleOptionViewItem:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        return opt

    @staticmethod
    def _text_rect(opt: QStyleOptionViewItem) -> QRect:
        widget = opt.widget
        if widget is None:
            return opt.rect
        return widget.style().subElementRect(QStyle.SE_ItemViewItemText, opt, widget)

    @staticmethod
    def _parts(index: QModelIndex) -> tuple[str, list[str]]:
        """The title, and the lines under it."""
        title, *meta = str(index.data(Qt.DisplayRole) or "").split("\n")
        meta = [line for line in meta if line]
        phone = index.data(PHONE_ROLE)
        if phone and meta:
            meta[0] = f"{meta[0]} · {phone}"
        found = index.data(FOUND_ROLE)
        return title, [*meta, found] if found else meta

    @classmethod
    def _meta_lines(cls, meta: list[str], font: QFont, width: int) -> list[str]:
        """Each line as it fits `width`: broken at its " · " parts, and a part
        that alone doesn't fit (the words a search found) wrapped."""
        metrics = QFontMetrics(font)
        out: list[str] = []
        for line in meta:
            if width <= 0 or metrics.horizontalAdvance(line) <= width:
                out.append(line)
                continue
            current = ""
            for part in line.split(" · "):
                joined = f"{current} · {part}" if current else part
                if metrics.horizontalAdvance(joined) <= width:
                    current = joined
                    continue
                if current:
                    out.append(current)
                current = part
            if metrics.horizontalAdvance(current) <= width:
                out.append(current)
            else:
                out += cls._title_lines(current, font, width)
        return out

    @staticmethod
    def _meta_font(font: QFont) -> QFont:
        small = QFont(font)
        if font.pixelSize() > 0:
            small.setPixelSize(max(1, round(font.pixelSize() * META_SCALE)))
        else:
            small.setPointSizeF(font.pointSizeF() * META_SCALE)
        return small

    @staticmethod
    def _title_lines(title: str, font: QFont, width: int) -> list[str]:
        """The title as it wraps at `width`, cut with "…" past TITLE_LINES."""
        if width <= 0:
            return [title]
        layout = QTextLayout(title, font)
        layout.setTextOption(QTextOption(Qt.AlignLeft))
        option = layout.textOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        starts: list[int] = []
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            starts.append(line.textStart())
        layout.endLayout()
        starts = starts or [0]
        lines = [
            title[start : (starts[n + 1] if n + 1 < len(starts) else len(title))].rstrip()
            for n, start in enumerate(starts[:TITLE_LINES])
        ]
        if len(starts) > TITLE_LINES:
            rest = title[starts[TITLE_LINES - 1] :]
            lines[-1] = QFontMetrics(font).elidedText(rest, Qt.ElideRight, width)
        return lines
