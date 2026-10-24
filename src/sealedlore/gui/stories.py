"""The story list (left dock).

Each row is a story's title, over up to two lines, and under it, smaller and
dimmer, how many messages it has (`_StoryRow`). Cut to one line, the author's
titles ("Between Stars: The Long Way Home", "Test · Sonnet 4.6 · Between Stars
long run") all read "Between Stars: T…" or "Test · Sonnet 4.6 …".
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, Signal
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

from sealedlore.gui import theme
from sealedlore.gui.start_page import Recent, when_played
from sealedlore.storage.library import story_listings


class StoryListPanel(QWidget):
    story_activated = Signal(str)
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
        # (id, label, updated) of every readable story, played last first.
        self._played: list[tuple[str, str, str]] = []
        # Phone access (gui/window_phone.py): the shared stories while it is
        # on, None while it is off; and which stories keep a map (never shared).
        self._phone_shared: set[str] | None = None
        self._maps: set[str] = set()
        self.root = root

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

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
        self._maps.clear()
        try:
            self._fill(selected_id)
        finally:
            self.refreshed.emit()

    def has_stories(self) -> bool:
        """Any story at all, readable or not."""
        return self.list.count() > 0

    def _fill(self, selected_id: str | None) -> None:
        for listing in story_listings(self.root):
            error = listing.error
            if error is None:
                count = listing.messages
                if listing.visual:
                    # A game on its map has no messages: where its clock stands.
                    when = listing.visual_when or "not begun yet"
                    label = f"{listing.title}\nVisual story · {when}"
                else:
                    label = f"{listing.title}\n{count} message{'s' if count != 1 else ''}"
                self._played.append((listing.id, label, listing.updated))
            else:
                # Shown rather than skipped: a story that vanished without a
                # word can't be looked into or deleted. Opening it says why.
                reason = (error.splitlines() or ["unreadable"])[0][:80]
                label = f"{listing.id}\n⚠ can't be read: {reason}"
            story_id = listing.id
            if listing.keeps_map:
                self._maps.add(story_id)
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, story_id)
            item.setData(PHONE_ROLE, self._phone_mark(story_id))
            item.setToolTip(label.split("\n", 1)[0])
            if error is not None:
                item.setForeground(QBrush(Qt.gray))
                item.setToolTip(error)
            self.list.addItem(item)
            if story_id == selected_id:
                self.list.setCurrentItem(item)

    def recent(self, count: int) -> list[Recent]:
        """The `count` stories played last, readable ones only, for the start
        page: title, and how many messages and when."""
        out = []
        for story_id, label, updated in self._played[:count]:
            title, _, messages = label.partition("\n")
            if messages.startswith("0 "):
                messages = "Not begun yet"
            played = when_played(updated)
            detail = f"{messages} · played {played}" if played else messages
            out.append(Recent(story_id, title, detail))
        return out

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
        return "" if story_id in self._maps else "on your phones"

    def menu_for(self, story_id: str) -> QMenu:
        """A story's right-click menu."""
        menu = QMenu(self)
        menu.addAction("Open", lambda: self.story_activated.emit(story_id))
        readable = any(played[0] == story_id for played in self._played)
        if self._phone_shared is not None and readable and story_id not in self._maps:
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
        if story_id:
            self.story_activated.emit(story_id)


# Lines a title may take before it is cut with "…".
TITLE_LINES = 2
# A row's phone mark ("on your phones"), drawn after its message count: the
# item's own text stays "Title\nN messages".
PHONE_ROLE = Qt.UserRole + 1
# The count's size beside the title's, and the space between them.
META_SCALE = 0.86
META_GAP = 2


class _StoryRow(QStyledItemDelegate):
    """A row of the story list: the item's text is "Title\nN messages"; the
    title wraps to `TITLE_LINES`, the rest is drawn smaller and dimmer.

    The row's box (padding, divider, selection) is the stylesheet's
    (`QListWidget::item`), drawn by the style; only the text is drawn here."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        opt = self._option(option, index)
        view = option.widget
        width = view.viewport().width() if view is not None else option.rect.width()
        # The style's box around one line of text: padding and divider.
        opt.text = "X"
        box = super().sizeHint(opt, index)
        opt.rect = QRect(0, 0, width, box.height())
        text = self._text_rect(opt)
        title, meta = self._parts(index)
        lines = self._title_lines(title, opt.font, text.width())
        height = box.height() - QFontMetrics(opt.font).height()
        height += len(lines) * QFontMetrics(opt.font).lineSpacing()
        if meta:
            height += META_GAP + QFontMetrics(self._meta_font(opt.font)).lineSpacing()
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
        if meta:
            font = self._meta_font(opt.font)
            small = QFontMetrics(font)
            painter.setFont(font)
            painter.setPen(ink if foreground is not None else theme.colour("text_dim"))
            painter.drawText(
                QRect(rect.left(), y + META_GAP, rect.width(), small.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                small.elidedText(meta, Qt.ElideRight, rect.width()),
            )
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
    def _parts(index: QModelIndex) -> tuple[str, str]:
        text = str(index.data(Qt.DisplayRole) or "")
        title, _, meta = text.partition("\n")
        meta = meta.replace("\n", " · ")
        phone = index.data(PHONE_ROLE)
        return title, f"{meta} · {phone}" if phone and meta else meta

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
