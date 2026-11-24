"""The lorebook, and what it injected last turn.

Retrieval is the one part of the prompt the author doesn't write directly, so
the panel's second job is transparency: which entries went in on the last turn,
why, and — when embeddings weren't used — that it was keywords alone.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.retrieval import RetrievalReport
from sealedlore.gui.fields import FormScroll, set_field_text, set_spin_value
from sealedlore.gui.lore_rules import ABOUT, NAMES
from sealedlore.gui.plain_text import plain_tip
from sealedlore.gui.portrait_view import PortraitView
from sealedlore.gui.ref_images import CardIcons, RefImageStrip, StoryPictures
from sealedlore.gui.sizing import GrowingText
from sealedlore.gui.tavern_dialog import (
    LOREBOOK_EXPORT_LABEL,
    LOREBOOK_IMPORT_LABEL,
    LOREBOOK_LABEL,
    LOREBOOK_LINK_LABEL,
)
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.models.lore import LoreEntry
from sealedlore.storage.tavern_lore import uses_tavern_settings

PEOPLE_LABEL = "Find the people…"
ALWAYS_ON_LABEL = "Always-on entries…"
FROM_LOREBOOKS_LABEL = "From Lorebooks…"
TO_LOREBOOKS_LABEL = "Keep in Lorebooks…"
# The pictures beside the titles in the list.
LIST_ICON = 28
# The most entries the list shows before it scrolls, and the report's list.
LIST_ROWS = 8
REPORT_ROWS = 6
# The report's row when lore from the full entry was ranked by words alone.
IN_FULL_HEAD = "Lore from the full entries"


FIRING_SEALEDLORE = NAMES["sealedlore"]
FIRING_TAVERN = NAMES["sillytavern"]
REEMBED_TIP = (
    "Rebuild every vector. Vectors are keyed by the entry's text and the model, so an "
    "edited entry or a changed model re-embeds by itself; this is for an endpoint that "
    "changed what it serves under the same model id"
)
FIRING_TIP = (
    "SealedLore's rules: a small lorebook is sent whole, a large one chosen per turn as "
    "Settings → Lore says.\n\nSillyTavern's rules: each turn's entries fire exactly by "
    "SillyTavern's own rules and each entry's own settings from its lorebook (keys and "
    "secondary keys, order, recursion, sticky, cooldown, delay, inclusion groups, "
    "probability, its budget). A constant entry nothing can stop is sent with the world, "
    "cached; the rest go with the turn, in SillyTavern's order. Entries written here "
    "fire by their keywords."
)


def unused_here(entries: list[LoreEntry]) -> int:
    """How many enabled entries carry SillyTavern settings SealedLore's own
    rules don't use."""
    return sum(entry.enabled and uses_tavern_settings(entry) for entry in entries)


class LorePanel(QWidget):
    changed = Signal()
    reembed_requested = Signal()
    # A SillyTavern lorebook (storage/tavern_lore.py) read into this story's
    # lore, or the lore written out as one.
    lorebook_import_requested = Signal()
    lorebook_link_requested = Signal()
    lorebook_export_requested = Signal()
    # Lorebooks kept for any story (gui/window_lorebooks.py): one brought in,
    # or the lore kept as one; and the people the lore describes found.
    from_lorebooks_requested = Signal()
    to_lorebooks_requested = Signal()
    people_requested = Signal()
    # (entry id, picture id or "", what): a picture drawn for the entry:
    # "new", or "again" or "edit" of that picture (as on the Cast page).
    picture_requested = Signal(str, str, str)
    # How the entries are chosen each turn: "sealedlore" or "sillytavern"
    # (engine/tavern_firing.py), and SillyTavern's settings asked for.
    firing_changed = Signal(str)
    tavern_settings_requested = Signal()
    # The always-on entries looked over (gui/always_on_dialog.py), and one
    # entry's condensed version written (engine/session_lore_condense.py).
    always_on_requested = Signal()
    condense_requested = Signal(str)

    def __init__(self, pictures: StoryPictures | None = None) -> None:
        super().__init__()
        self._pictures: StoryPictures = pictures or (lambda: None)
        self._entries: list[LoreEntry] = []
        self._hidden: set[str] = set()
        self._held_back: set[str] = set()
        self._current: LoreEntry | None = None
        self._loading = False
        # Keeps the author's own condensed version of an entry (the window's:
        # the session's `set_condensed_lore`).
        self.keep_condensed: Callable[[LoreEntry, str], None] = lambda _entry, _text: None

        self.list = QListWidget()
        self.list.setObjectName("loreList")
        # Each title beside its picture, small.
        self.list.setIconSize(QSize(LIST_ICON, LIST_ICON))
        self._icons = CardIcons(self._pictures, LIST_ICON)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.currentItemChanged.connect(self._on_selection_changed)

        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add_entry)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove_entry)

        self.tavern_button = QToolButton()
        self.tavern_button.setText(LOREBOOK_LABEL)
        self.tavern_button.setToolTip(
            "Lorebooks as SillyTavern and Chub share them: bring one into this story's lore, "
            "or save the lore as one"
        )
        self.tavern_button.setPopupMode(QToolButton.InstantPopup)
        tavern_menu = QMenu(self.tavern_button)
        tavern_menu.addAction(LOREBOOK_IMPORT_LABEL).triggered.connect(
            lambda _checked=False: self.lorebook_import_requested.emit()
        )
        tavern_menu.addAction(LOREBOOK_LINK_LABEL).triggered.connect(
            lambda _checked=False: self.lorebook_link_requested.emit()
        )
        tavern_menu.addAction(LOREBOOK_EXPORT_LABEL).triggered.connect(
            lambda _checked=False: self.lorebook_export_requested.emit()
        )
        tavern_menu.addSeparator()
        tavern_menu.addAction(FROM_LOREBOOKS_LABEL).triggered.connect(
            lambda _checked=False: self.from_lorebooks_requested.emit()
        )
        tavern_menu.addAction(TO_LOREBOOKS_LABEL).triggered.connect(
            lambda _checked=False: self.to_lorebooks_requested.emit()
        )
        self.tavern_button.setMenu(tavern_menu)
        self.people_button = QPushButton(PEOPLE_LABEL)
        self.people_button.setToolTip(
            "The authoring model reads the lore and finds the people it describes, for you to "
            "make cast or supporting cards of; and points out instructions written for the "
            "storyteller, to move into the style's notes"
        )
        self.people_button.clicked.connect(self.people_requested)

        # Wraps rather than widens the page: in one row, Find the people…
        # held the Lore page 117px wider, and the window past 1,282px with
        # Windows' fonts (the lab's Windows 11, Nov 20 2026).
        buttons = WrapRow(spacing=6, group_spacing=6)
        for button in (add_button, self.remove_button, self.tavern_button, self.people_button):
            buttons.add_group([button])
        self._buttons_row = buttons

        # How the entries are chosen each turn: SealedLore's ways (Settings →
        # Lore), or exactly as SillyTavern fires a lorebook's.
        self.firing = QComboBox()
        self.firing.addItem(FIRING_SEALEDLORE, "sealedlore")
        self.firing.addItem(FIRING_TAVERN, "sillytavern")
        self.firing.setToolTip(FIRING_TIP)
        self.firing.currentIndexChanged.connect(self._on_firing)
        self.tavern_settings_button = QPushButton("SillyTavern settings…")
        self.tavern_settings_button.setToolTip(
            "SillyTavern's World Info settings for this story: scan depth, budget, recursion, "
            "minimum activations, case, whole words, group scoring"
        )
        self.tavern_settings_button.clicked.connect(self.tavern_settings_requested)
        # Wraps too: on one line it held the page ~90px wider than before.
        firing_row = WrapRow(spacing=6, group_spacing=6)
        firing_row.add_group([QLabel("Lore fires by"), self.firing])
        firing_row.add_group([self.tavern_settings_button])
        self._firing_row = firing_row
        # What the rules chosen do, and what of the lore they leave unused:
        # the choice made plain where it is made (the author, Nov 21 2026).
        self.firing_about = QLabel()
        self.firing_about.setObjectName("hintLabel")
        self.firing_about.setWordWrap(True)
        self.firing_about.setTextFormat(Qt.PlainText)
        # Always-on lore against the lore budget, said when it is over, with
        # the way to choose which go optional or condensed.
        self.always_on_note = QLabel()
        self.always_on_note.setObjectName("warningLabel")
        self.always_on_note.setWordWrap(True)
        self.always_on_note.setTextFormat(Qt.PlainText)
        self.always_on_note.hide()
        self.always_on_button = QPushButton(ALWAYS_ON_LABEL)
        self.always_on_button.setToolTip(
            "Every always-on entry and how much it takes: make some optional (sent by their "
            "keywords), or write shorter versions to send when they are too much"
        )
        self.always_on_button.clicked.connect(self.always_on_requested)
        firing_row.add_group([self.always_on_button])

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)
        top_layout.addLayout(firing_row)
        top_layout.addWidget(self.firing_about)
        top_layout.addWidget(self.always_on_note)
        top_layout.addWidget(self.list, 1)
        top_layout.addLayout(buttons)
        self._top_layout = top_layout

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self._build_editor())
        splitter.addWidget(self._build_injection_view())
        # The entry, with its picture at the top, is the long part: it takes
        # what the list and the report don't need (`_fit_panes`). At fixed
        # shares the list showed two entries and a half whatever the
        # lorebook held, and an empty report a third of the page.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([150, 520, 160])
        self._splitter = splitter
        # Dragged by the author, the panes stay as they put them.
        self._dragged = False
        splitter.splitterMoved.connect(self._on_dragged)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter)

    def _build_editor(self) -> QWidget:
        self.title = QLineEdit()
        # Where an entry brought in came from (a character's own lorebook, a
        # lorebook file): said over its fields, so lore a card or a book
        # brought is told from lore written here.
        self.came_from = QLabel()
        self.came_from.setObjectName("hintLabel")
        self.came_from.setWordWrap(True)
        self.came_from.setTextFormat(Qt.PlainText)
        self.content = GrowingText(lines=6)
        self.content.setPlaceholderText("The text injected into the prompt, verbatim.")
        self.keywords = QLineEdit()
        self.keywords.setPlaceholderText("comma-separated; matched as whole words")
        self.always_on = QCheckBox("Always include")
        self.enabled = QCheckBox("Enabled")
        self.until_mentioned = QCheckBox("Hold back until I mention it")
        self.until_mentioned.setToolTip(
            "For what hasn't happened yet. No model sees this entry until one of your own "
            "turns or Directions names it (its title or a keyword) on the branch you're "
            "playing. The storyteller naming it doesn't count, nor does a private scene."
        )
        # A shorter version beside the entry, never in its place: sent only
        # when always-on lore is more than the lore budget holds.
        self.condensed = GrowingText(lines=3)
        self.condensed.setPlaceholderText(
            "None yet. Sent in place of the whole only when the always-on entries are more "
            "than the lore budget holds."
        )
        self.condensed_state = QLabel()
        self.condensed_state.setObjectName("hintLabel")
        self.condensed_state.setWordWrap(True)
        self.condensed_state.setTextFormat(Qt.PlainText)
        self.condense_button = QPushButton("Write a condensed version")
        self.condense_button.setToolTip(
            "The authoring model writes a shorter version of this entry, kept beside it"
        )
        self.condense_button.clicked.connect(
            lambda: self._current is not None and self.condense_requested.emit(self._current.id)
        )
        self.priority = QSpinBox()
        self.priority.setRange(-100, 100)
        self.priority.setToolTip("Higher priority survives the lore token cap first")
        # The entry's first picture large, the others small under it, as a
        # character's portrait is on the Cast page.
        self.picture = PortraitView(person=False)
        self.pictures = RefImageStrip(self._pictures, "it", after_portrait=True, crop="picture")
        self.pictures.changed.connect(self._on_pictures_changed)
        self.picture.draw_requested.connect(lambda: self._ask("new"))
        self.picture.again_requested.connect(lambda: self._ask("again", first=True))
        self.picture.edit_requested.connect(lambda: self._ask("edit", first=True))
        self.picture.add_requested.connect(self.pictures.add_portrait)
        self.picture.remove_requested.connect(self._remove_picture)
        self.pictures.again_requested.connect(lambda ref: self._ask("again", ref))
        self.pictures.edit_requested.connect(lambda ref: self._ask("edit", ref))

        self.title.textChanged.connect(self._apply_edits)
        self.content.textChanged.connect(self._apply_edits)
        self.keywords.textChanged.connect(self._apply_edits)
        self.always_on.toggled.connect(self._apply_edits)
        self.enabled.toggled.connect(self._apply_edits)
        self.until_mentioned.toggled.connect(self._apply_edits)
        self.priority.valueChanged.connect(self._apply_edits)
        self.condensed.textChanged.connect(self._condensed_edited)

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(0, 0, 0, 0)
        # Heads the entry: under its picture it read as one of the picture's.
        form.addRow(self.came_from)
        # The words first, the pictures under them (the author, Nov 22 2026:
        # "text first"); a landscape picture on top put the entry's title and
        # content below the fold.
        form.addRow("Title", self.title)
        form.addRow("Content", self.content)
        form.addRow("Keywords", self.keywords)
        form.addRow("Priority", self.priority)
        form.addRow(self.always_on)
        form.addRow(self.enabled)
        form.addRow(self.until_mentioned)
        self._condensed_label = QLabel("Condensed")
        form.addRow(self._condensed_label, self.condensed)
        condensed_row = QHBoxLayout()
        condensed_row.setContentsMargins(0, 0, 0, 0)
        condensed_row.addWidget(self.condensed_state, 1)
        condensed_row.addWidget(self.condense_button)
        self._condensed_row = QWidget()
        self._condensed_row.setLayout(condensed_row)
        form.addRow(self._condensed_row)
        self._form = form
        form.addRow(self.picture)
        # Full width, under its label, as on the Cast page.
        form.addRow(QLabel("Other pictures"))
        form.addRow(self.pictures)

        self.empty_hint = QLabel("Add an entry, or select one to edit.")
        self.empty_hint.setObjectName("hintLabel")
        self.empty_hint.setWordWrap(True)
        form.addRow(self.empty_hint)

        scroll = FormScroll(form_host)
        self._form_host = form_host
        return scroll

    def _build_injection_view(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        caption = QLabel("SENT WITH THE LAST TURN")
        caption.setObjectName("composerCaption")
        self._caption = caption

        self.status = QLabel("Nothing sent yet.")
        self.status.setObjectName("hintLabel")
        self.status.setWordWrap(True)

        self.injected = QListWidget()
        self.injected.setObjectName("injectionList")
        self.injected.setSelectionMode(QAbstractItemView.NoSelection)

        self.reembed_button = QPushButton("Re-embed lorebook")
        self.reembed_button.setToolTip(REEMBED_TIP)
        # There is an embeddings endpoint, and why the button is off all the
        # same ("": it isn't): a private scene is open (`set_reembed_paused`).
        self._embeddings_available = True
        self._reembed_paused = ""
        self.reembed_button.clicked.connect(self.reembed_requested)
        # Beside the caption, not a row of its own the page's width.
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.addWidget(caption)
        heading.addStretch(1)
        heading.addWidget(self.reembed_button)

        layout.addLayout(heading)
        layout.addWidget(self.status)
        layout.addWidget(self.injected, 1)
        # An empty list under "Nothing sent yet." said nothing twice.
        self.injected.hide()
        self._report_layout = layout
        return host

    # --- the panes' heights ---------------------------------------------

    def _on_dragged(self, *_args) -> None:
        self._dragged = True

    def _rows_tall(self, view: QListWidget, rows: int) -> int:
        row = view.sizeHintForRow(0) if view.count() else -1
        if row <= 0:
            row = max(LIST_ICON, view.fontMetrics().height()) + 12
        return rows * row + 2 * view.frameWidth()

    def _list_height(self) -> int:
        """The list as tall as its entries, three to eight, its buttons, and
        the rules row above it with what they do."""
        rows = min(max(self.list.count(), 3), LIST_ROWS)
        width = max(self.list.width(), 1)
        buttons = self._buttons_row.heightForWidth(width)
        firing = self._firing_row.heightForWidth(width)
        about = 0 if self.firing_about.isHidden() else self.firing_about.heightForWidth(width)
        if not self.always_on_note.isHidden():
            about += self._top_layout.spacing() + self.always_on_note.heightForWidth(width)
        spacing = self._top_layout.spacing()
        return (
            self._rows_tall(self.list, rows)
            + spacing
            + buttons
            + spacing
            + firing
            + spacing
            + max(about, 0)
        )

    def _report_height(self) -> int:
        """The report as tall as what it says, its list six rows at most."""
        layout = self._report_layout
        width = max(self._splitter.width(), 200)
        heading = max(self._caption.sizeHint().height(), self.reembed_button.sizeHint().height())
        tall = layout.contentsMargins().top() + heading + layout.spacing()
        tall += self.status.heightForWidth(width)
        if not self.injected.isHidden():
            tall += layout.spacing() + self._rows_tall(
                self.injected, min(self.injected.count(), REPORT_ROWS)
            )
        return tall

    def _fit_panes(self) -> None:
        if self._dragged:
            return
        total = self._splitter.height() - 2 * self._splitter.handleWidth()
        if total <= 0:
            return
        # Neither may take the entry's room: a third each at most.
        top = min(self._list_height(), total // 3)
        bottom = min(self._report_height(), total // 3)
        self._splitter.setSizes([top, max(total - top - bottom, 0), bottom])

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit_panes()

    # --- population -------------------------------------------------------

    def set_entries(self, entries: list[LoreEntry], *, hidden: set[str] | None = None) -> None:
        """`hidden`: ids the plot hasn't revealed, left off unless the author asked."""
        self._entries = entries
        self._say_firing()
        if hidden is not None:
            self._hidden = hidden
        selected = self._current.id if self._current else None
        self._loading = True
        self.list.clear()
        for entry in entries:
            if entry.id in self._hidden:
                continue
            item = QListWidgetItem(self._label(entry))
            item.setData(Qt.UserRole, entry.id)
            if entry.came_from:
                item.setToolTip(plain_tip(f"From {entry.came_from}"))
            item.setIcon(self._icons.icon(entry.reference_images))
            self.list.addItem(item)
            if entry.id == selected:
                self.list.setCurrentItem(item)
        self._loading = False
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._on_selection_changed(self.list.currentItem(), None)
        self._fit_panes()

    def set_held_back(self, ids: set[str]) -> None:
        """Which "until mentioned" entries you haven't named yet on this branch:
        only the labels change, so an entry being edited is left alone."""
        if ids == self._held_back:
            return
        self._held_back = set(ids)
        by_id = {entry.id: entry for entry in self._entries}
        for row in range(self.list.count()):
            item = self.list.item(row)
            entry = by_id.get(item.data(Qt.UserRole))
            if entry is not None:
                item.setText(self._label(entry))

    def _label(self, entry: LoreEntry) -> str:
        marks = []
        if entry.came_from:
            marks.append(f"from {entry.came_from}")
        if entry.always_on:
            marks.append("always on")
        if not entry.enabled:
            marks.append("disabled")
        elif entry.until_mentioned:
            marks.append("held back" if entry.id in self._held_back else "mentioned")
        suffix = f"  ({', '.join(marks)})" if marks else ""
        return f"{entry.title or '(untitled)'}{suffix}"

    def show_retrieval(self, report: RetrievalReport | None) -> None:
        """§7: which entries were injected on the last turn, and why."""
        self._show_retrieval(report)
        self.injected.setVisible(self.injected.count() > 0)
        self._fit_panes()

    def _show_retrieval(self, report: RetrievalReport | None) -> None:
        self.injected.clear()
        if report is None:
            self.status.setText("Nothing sent yet.")
            return
        if report.mode == "whole":
            for entry in report.standing:
                shorter = ", condensed" if entry.id in report.condensed else ""
                self.injected.addItem(f"{entry.title} — sent every turn{shorter}")
            self.status.setText(
                f"All {len(report.standing)} entries are sent every turn with the world, "
                "cached. A lorebook this size costs less whole than selected from."
            )
            return

        for entry in report.standing:
            shorter = ", condensed" if entry.id in report.condensed else ""
            self.injected.addItem(f"{entry.title} — always on, sent with the world{shorter}")
        for injection in report.injected:
            self.injected.addItem(f"{injection.name} — {injection.describe()}")
        for injection in report.dropped:
            self.injected.addItem(f"{injection.name} — left out: no room left this turn")
        if report.in_full_note:
            # The parts of the entries sent condensed, ranked by words alone
            # this turn: their vectors are made ahead, and weren't yet.
            item = QListWidgetItem(f"{IN_FULL_HEAD} — {report.in_full_note}")
            item.setForeground(Qt.gray)
            self.injected.addItem(item)
        # Fired as SillyTavern fires: what didn't, and SillyTavern's reason.
        for entry, why in report.not_fired:
            item = QListWidgetItem(f"{entry.title} — not fired: {why}")
            item.setForeground(Qt.gray)
            self.injected.addItem(item)
        # Similarity scores cluster, so the threshold can only be tuned by
        # seeing what sat just under it.
        for entry, score in report.near_misses:
            if report.misses_ranked:
                # The turn's next matches: what more entries per turn would send.
                said = "a next match, not sent" + (f" (similarity {score:.2f})" if score else "")
            else:
                said = f"below threshold ({score:.2f})"
            item = QListWidgetItem(f"{entry.title} — {said}")
            item.setForeground(Qt.gray)
            self.injected.addItem(item)

        count = f"{len(report.injected)} entries, ~{report.tokens:,} tokens"
        if report.selector == "sillytavern" and report.fallback_reason:
            self.status.setText(f"Not fired this turn: {report.fallback_reason}.")
            return
        if report.selector == "sillytavern":
            self.status.setText(
                f"{count} · fired as SillyTavern would, within its budget of "
                f"{report.tavern_budget:,} tokens"
            )
            return
        if report.selector == "picker":
            self.status.setText(
                f"{count} · picked after the last passage, and what your turn names"
            )
            return
        if report.selector == "jev":
            self.status.setText(f"{count} · scored by Jev, and what your turn names")
            return
        fell_back = (
            f"Similarity this turn ({report.selector_note}). " if report.selector_note else ""
        )
        # Your turn named no entry, so the keywords reached back this far.
        reach = report.keyword_reach
        if reach:
            count += f" · keywords from the last {reach} exchange{'s' if reach > 1 else ''}"
        if report.fallback_reason:
            self.status.setText(
                f"{fell_back}Keyword matching only — embeddings unavailable "
                f"({report.fallback_reason})."
            )
        elif report.used_embeddings:
            self.status.setText(f"{fell_back}{count} · semantic + keyword")
        else:
            self.status.setText(f"{fell_back}{count} · keyword matching")

    def set_embeddings_available(self, available: bool) -> None:
        self._embeddings_available = available
        self._sync_reembed()

    def set_reembed_paused(self, why: str) -> None:
        """Re-embed lorebook off, its tooltip saying `why`, while a private
        scene is open ("" once it has ended): the embeddings endpoint isn't
        called then. Kept apart from whether there is an endpoint, so a
        refresh of the page mid-scene doesn't bring the button back."""
        self._reembed_paused = why
        self._sync_reembed()

    def _sync_reembed(self) -> None:
        self.reembed_button.setEnabled(self._embeddings_available and not self._reembed_paused)
        self.reembed_button.setToolTip(self._reembed_paused or REEMBED_TIP)

    # --- editing ----------------------------------------------------------

    def _on_selection_changed(self, current: QListWidgetItem | None, _previous=None) -> None:
        if self._loading:
            return
        entry_id = current.data(Qt.UserRole) if current else None
        self._current = next((e for e in self._entries if e.id == entry_id), None)
        self._load_editor()

    def _ask(self, what: str, ref_id: str = "", *, first: bool = False) -> None:
        entry = self._current
        if entry is None:
            return
        if first and entry.reference_images:
            ref_id = entry.reference_images[0].id
        self.picture_requested.emit(entry.id, ref_id, what)

    def _remove_picture(self) -> None:
        entry = self._current
        if entry is None or not entry.reference_images:
            return
        # The one shown when asked, as on the Cast page.
        shown = entry.reference_images[0].id
        answer = QMessageBox.question(
            self,
            "Remove the picture?",
            f"Take this picture off “{entry.title}”? Its next picture takes its place. "
            "Pictures drawn for the story stay in Media.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes and self._current is not None:
            if self._current.id == entry.id:
                self.pictures.remove_ref(shown)

    def _on_pictures_changed(self) -> None:
        if self._current is not None:
            self._show_picture()
            self.changed.emit()

    def _show_picture(self) -> None:
        entry = self._current
        first = entry.reference_images[0] if entry and entry.reference_images else None
        store = self._pictures()
        data = store.read(first.file) if first is not None and store is not None else None
        caption = first.caption if first is not None and first.caption != "portrait" else ""
        self.picture.show_portrait(data, caption, entry.title if entry is not None else "")
        self.picture.set_offered(entry is not None)
        item = self.list.currentItem()
        if item is not None and entry is not None:
            item.setIcon(self._icons.icon(entry.reference_images))

    def refresh_pictures(self) -> None:
        """The entries' pictures as they are now, nothing else (as the Cast
        page's `refresh_pictures`)."""
        entries = {e.id: e for e in self._entries}
        for row in range(self.list.count()):
            item = self.list.item(row)
            entry = entries.get(item.data(Qt.UserRole))
            if entry is not None:
                item.setIcon(self._icons.icon(entry.reference_images))
        current = self._current
        self.pictures.set_refs(current.reference_images if current is not None else None)
        self._show_picture()

    def _load_editor(self) -> None:
        entry = self._current
        enabled = entry is not None
        self._form_host.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)
        self.empty_hint.setVisible(not enabled)
        self.pictures.set_refs(entry.reference_images if entry is not None else None)
        self._show_picture()
        source = entry.came_from if entry is not None else ""
        self.came_from.setText(f"From {source}." if source else "")
        self.came_from.setVisible(bool(source))
        if entry is None:
            return

        self._loading = True
        set_field_text(self.title, entry.title)
        self.content.setPlainText(entry.content)
        set_field_text(self.keywords, ", ".join(entry.keywords))
        set_spin_value(self.priority, entry.priority)
        self.always_on.setChecked(entry.always_on)
        self.enabled.setChecked(entry.enabled)
        self.until_mentioned.setChecked(entry.until_mentioned)
        self.condensed.setPlainText(entry.condensed or "")
        self._loading = False
        self._show_condensed()

    def _show_condensed(self) -> None:
        """The condensed version's rows: for an always-on entry, or one that
        has a condensed version; its state said under it."""
        entry = self._current
        shown = entry is not None and (entry.always_on or bool(entry.condensed))
        self._form.setRowVisible(self.condensed, shown)
        self._form.setRowVisible(self._condensed_row, shown)
        if not shown:
            return
        if entry.condensed and not entry.condensed_now():
            said = (
                "Out of date: the entry changed since it was written. Never sent until "
                "written again."
            )
        elif entry.condensed:
            words = len(entry.condensed.split())
            said = f"{words} words, of the entry's {len(entry.content.split())}."
        else:
            said = ""
        self.condensed_state.setText(said)
        self.condense_button.setText(
            "Write it again" if entry.condensed else "Write a condensed version"
        )

    def _condensed_edited(self) -> None:
        """The author's own words, current for the entry as it stands: kept by
        the session (`keep_condensed`: StorySession.set_condensed_lore)."""
        if self._loading or self._current is None:
            return
        self.keep_condensed(self._current, self.condensed.toPlainText())
        self._show_condensed()
        self.changed.emit()

    def set_always_on(self, note: str, any_on: bool) -> None:
        """What always-on lore takes against the lore budget, when over."""
        self.always_on_note.setText(note)
        self.always_on_note.setVisible(bool(note))
        self.always_on_button.setVisible(any_on)
        self._fit_panes()

    def _apply_edits(self) -> None:
        if self._loading or self._current is None:
            return
        entry = self._current
        entry.title = self.title.text().strip()
        entry.content = self.content.toPlainText()
        entry.keywords = [word.strip() for word in self.keywords.text().split(",") if word.strip()]
        entry.priority = self.priority.value()
        entry.always_on = self.always_on.isChecked()
        entry.enabled = self.enabled.isChecked()
        entry.until_mentioned = self.until_mentioned.isChecked()
        self._show_condensed()

        item = self.list.currentItem()
        if item is not None:
            item.setText(self._label(entry))
        self.changed.emit()

    def _add_entry(self) -> None:
        entry = LoreEntry(title="New entry", content="")
        self._entries.append(entry)
        self._current = entry
        self.set_entries(self._entries)
        self._select(entry.id)
        self.title.setFocus()
        self.title.selectAll()
        self.changed.emit()

    def _remove_entry(self) -> None:
        if self._current is None:
            return
        answer = QMessageBox.question(
            self,
            "Remove this entry?",
            f"Take “{self._current.title or 'this entry'}” out of the story's lore?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._entries.remove(self._current)
        self._current = None
        self.set_entries(self._entries)
        self.changed.emit()

    def select_entry(self, entry_id: str) -> None:
        self._select(entry_id)

    def _select(self, entry_id: str) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(Qt.UserRole) == entry_id:
                self.list.setCurrentItem(item)
                return

    # --- how the entries fire ------------------------------------------------

    def set_firing(self, mode: str) -> None:
        """Show the story's choice without telling anyone it changed."""
        self.firing.blockSignals(True)
        self.firing.setCurrentIndex(max(0, self.firing.findData(mode)))
        self.firing.blockSignals(False)
        self.tavern_settings_button.setEnabled(mode == "sillytavern")
        self._say_firing()

    def _say_firing(self) -> None:
        """One short line under the choice, the rules' whole account in its
        tooltip: under SillyTavern's, that each entry's settings are used;
        under SealedLore's, how many entries carry settings they leave
        unused, or nothing. Longer, it took the list's room."""
        mode = self.firing.currentData() or "sealedlore"
        unused = unused_here(self._entries) if mode == "sealedlore" else 0
        if mode == "sillytavern":
            said = "Each entry fires by its own SillyTavern settings."
        elif unused:
            said = (
                f"{unused} {'entry has' if unused == 1 else 'entries have'} SillyTavern "
                "settings these rules don't use."
            )
        else:
            said = ""
        self.firing_about.setText(said)
        self.firing_about.setToolTip(plain_tip(ABOUT[mode]))
        self.firing_about.setHidden(not said)

    def _on_firing(self, _index: int) -> None:
        mode = self.firing.currentData()
        self.tavern_settings_button.setEnabled(mode == "sillytavern")
        self._say_firing()
        self.firing_changed.emit(mode)
