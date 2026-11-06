"""The plot file editor: a plot file's parts as a tree, a form for each, and
the importer's verdict on the whole.

A plot file (SampleStories/PLOT_FORMAT.md) is easy to write and easy to get
wrong: a fact spelt two ways, a condition the importer can't read, a place
named in an event but never given a section. The editor holds the file as a
`PlotDocument` and the author never types the structure: sections and items
come from the tree, conditions and lists from pickers filled with what the
file already has. What the editor writes is the rendered document, and what
it checks is that same text run through the importer, so the problems list
is exactly what File → Import would say, each against the item to fix.

The tree is in two halves (the author, Nov 2026: which parts were the plot
and which only writing a story wasn't clear): the story, with what every
story has, plot or none (its title and settings, world, opening, characters,
places and lore), and the plot, with what a plot adds (its clock, facts and
events). Every page says which half it is in.

It is its own window and needs no story open. Saving writes the `.md`;
"Start a story from this file" hands the saved file to the main window's
import, which is the same path a hand-written file takes.

It also opens a story (File → Open a story…, or the main window's Story →
Edit the plot in the plot editor…) to add a plot to it or change its plot,
filled in with the story's own world, cast and lore (engine/story_plot.py).
The story's parts are shown as the story has them and are changed where
they always are (Setup, the Cast and Lore pages); what is the plot's is
edited here: its facts and events, which entries are its places, and the
people, places and lore it keeps hidden until an event brings them in. Save
gives the plot to the story through the main window (`PlotEditorHost`), the
story's one writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.plot_file import (
    CHARACTER,
    EVENT,
    FACT,
    FRONT,
    LORE,
    NOTES,
    OPENING,
    PLACE,
    WORLD,
    DocProblem,
    ItemRef,
    RenderedPlot,
    check_plot_document,
    describe_where,
    read_plot_document,
    render_plot_markdown,
)
from sealedlore.engine.plot_md import PLOT_SUFFIX
from sealedlore.engine.story_plot import document_from_story
from sealedlore.gui import theme
from sealedlore.gui.guides import (
    FORMAT_LABEL,
    PLOT_FORMAT_GUIDE,
    PLOT_WRITING_GUIDE,
    WRITING_LABEL,
    WRITING_TIP,
    open_guide,
)
from sealedlore.gui.plot_editor_pages import (
    CharacterPage,
    EntryPage,
    FactPage,
    OpeningPage,
    Page,
    PlotPage,
    SectionPage,
    StoryPage,
    WayPage,
    WorldPage,
)
from sealedlore.gui.plot_pictures import copy_pictures
from sealedlore.gui.sizing import size_to_screen
from sealedlore.models.plot_file import (
    CharacterDoc,
    EntryDoc,
    EventDoc,
    FactDoc,
    PictureDoc,
    PlotDocument,
    VariantDoc,
)
from sealedlore.storage.repository import StoryBundle

REF_ROLE = Qt.UserRole + 1
# Tree headers: what each holds, and what "Add" makes under it. Timeline and
# optional events share one list in the document; the header says which.
HEADER = "header"
TIMELINE = "timeline"
OPTIONAL = "events"
# The plot half's own row: its clock.
PLOT = "plot"
STORY_HALF = "The story"
PLOT_HALF = "The plot"
_GROUPS: list[tuple[str, str, str, list[tuple[str, str]]]] = [
    (
        FRONT,
        STORY_HALF,
        "What every story has, with a plot or without: its title and settings, world, "
        "opening, characters, places and lore",
        [
            (WORLD, "World"),
            (OPENING, "Opening"),
            (CHARACTER, "Characters"),
            (PLACE, "Places"),
            (LORE, "Lore"),
        ],
    ),
    (
        PLOT,
        PLOT_HALF,
        "What a plot adds: the facts the program keeps, and the events that happen when "
        "the time and the moment are right",
        [(FACT, "Facts"), (TIMELINE, "Timeline"), (OPTIONAL, "Optional events")],
    ),
]
_SECTION_NAMES = {section: name for *_, sections in _GROUPS for section, name in sections}
_GROUP_ABOUT = {(HEADER, group, -1): about for group, _, about, _ in _GROUPS}
# What goes in each section, for its heading's page.
_SECTION_ABOUT = {
    CHARACTER: (
        "Everyone the story has a card for. A character the plot keeps hidden reaches no "
        "model, and no page, until an event brings them in."
    ),
    PLACE: (
        "Somewhere the story can be. Each is a lore entry, and also a place the program "
        "follows the player's character between: events test “at” and “visited” against "
        "the places, so a place is part of the plot too."
    ),
    LORE: (
        "Background the storyteller needs now and then: a history, a people, how a thing "
        "works. Never somewhere to be. The plot can keep an entry hidden until an event "
        "reveals it."
    ),
    FACT: (
        "What the plot keeps track of in the program's own records, not just in the prose. "
        "Events test them and set them."
    ),
    TIMELINE: (
        "Events with a window of days. Each happens within its window when the moment "
        "suits, or resolves one way or another once the window closes."
    ),
    OPTIONAL: (
        "Events with conditions but no fixed days: each happens once its conditions hold "
        "and the moment suits, or never."
    ),
}
_PLOT_SECTIONS = {PLOT, FACT, TIMELINE, OPTIONAL, EVENT}
_LISTS = (CHARACTER, PLACE, LORE, FACT, TIMELINE, OPTIONAL)
CHECK_DELAY_MS = 250


class PlotEditorHost(Protocol):
    """What the editor needs of the main window to work on a story
    (gui/window_plot.py). Each says why itself when it can't."""

    def plot_editor_stories(self) -> list[tuple[str, str]]:
        """(story id, label) for every story that can have a plot."""
        ...

    def plot_editor_bundle(self, story_id: str) -> StoryBundle | None:
        """The story, open in the main window, or None."""
        ...

    def plot_editor_save(self, story_id: str, document: PlotDocument) -> list[str] | None:
        """The document's plot given to the story: what couldn't be done, or None."""
        ...


@dataclass
class EditedStory:
    """The story the editor works on (story mode)."""

    id: str
    title: str
    # Begun: someone it has shown can't be hidden any more.
    begun: bool
    # Whom it begins with: hidden, the opening would have them and not.
    starts_with: frozenset[str]
    had_plot: bool


class PlotEditorWindow(QMainWindow):
    # The saved file the author wants a story started from.
    start_requested = Signal(object)

    def __init__(
        self, parent: QWidget | None = None, *, host: PlotEditorHost | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.Window)
        self.setMinimumSize(960, 640)
        size_to_screen(self, 0.75, 0.88)
        self.host = host
        self.document = PlotDocument()
        self.path: Path | None = None
        # The story being worked on, in place of a file (story mode).
        self.story: EditedStory | None = None
        self.modified = False
        self.rendered: RenderedPlot | None = None
        self.problems: list[DocProblem] = []
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(CHECK_DELAY_MS)
        self._check_timer.timeout.connect(self.check_now)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setObjectName("plotTree")
        self.tree.currentItemChanged.connect(self._on_tree_selection)

        self.add_button = QPushButton("Add")
        self.add_button.clicked.connect(self._add)
        self.variant_button = QPushButton("Add variant")
        self.variant_button.setToolTip("Another way this event can go, chosen by its conditions")
        self.variant_button.clicked.connect(self._add_variant)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        self.up_button = QPushButton("↑")
        self.up_button.setObjectName("smallButton")
        self.up_button.setToolTip("Move up")
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = QPushButton("↓")
        self.down_button.setObjectName("smallButton")
        self.down_button.setToolTip("Move down")
        self.down_button.clicked.connect(lambda: self._move(1))
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        for button in (
            self.add_button,
            self.variant_button,
            self.remove_button,
            self.up_button,
            self.down_button,
        ):
            buttons.addWidget(button)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.tree, 1)
        left_layout.addLayout(buttons)

        self.pages = QStackedWidget()
        self._pages: dict[str, Page] = {
            FRONT: StoryPage(),
            WORLD: WorldPage(),
            OPENING: OpeningPage(),
            PLOT: PlotPage(),
            CHARACTER: CharacterPage(self._folder, self.save),
            PLACE: EntryPage(is_place=True, folder=self._folder, save_first=self.save),
            LORE: EntryPage(is_place=False, folder=self._folder, save_first=self.save),
            FACT: FactPage(),
            EVENT: WayPage(is_event=True),
            "variant": WayPage(is_event=False),
        }
        self.empty_page = QLabel("Choose something on the left, or add one.")
        self.empty_page.setAlignment(Qt.AlignCenter)
        self.pages.addWidget(self.empty_page)
        self.section_page = SectionPage()
        self.pages.addWidget(self.section_page)
        for page in self._pages.values():
            page.changed.connect(self._on_page_changed)
            page.renamed.connect(self._on_renamed)
            self.pages.addWidget(page)
        for section in (PLACE, LORE):
            page = self._pages[section]
            assert isinstance(page, EntryPage)
            page.place_toggled.connect(self._on_place_toggled)

        self.problem_list = QListWidget()
        self.problem_list.setObjectName("plotProblems")
        self.problem_list.itemActivated.connect(self._on_problem_activated)
        self.problem_list.itemClicked.connect(self._on_problem_activated)
        self.summary = QLabel()
        self.summary.setObjectName("hintLabel")
        problems = QWidget()
        problems_layout = QVBoxLayout(problems)
        problems_layout.setContentsMargins(0, 0, 0, 0)
        problems_layout.addWidget(self.summary)
        problems_layout.addWidget(self.problem_list, 1)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(left)
        top.addWidget(self.pages)
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setSizes([280, 680])
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(problems)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([480, 140])
        # Which story the editor is on, in story mode.
        self.story_label = QLabel()
        self.story_label.setObjectName("hintLabel")
        self.story_label.setWordWrap(True)
        self.story_label.hide()
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.story_label)
        layout.addWidget(splitter)
        self.setCentralWidget(central)

        self._build_actions()
        self._rebuild_tree(select=(HEADER, FRONT, -1))
        self.check_now()
        self._refresh_title()

    # --- actions ---------------------------------------------------------------

    def _build_actions(self) -> None:
        menu = self.menuBar().addMenu("&File")

        def action(label: str, handler, shortcut=None, tip: str = "") -> QAction:
            item = QAction(label, self)
            if shortcut is not None:
                item.setShortcut(shortcut)
            if tip:
                item.setStatusTip(tip)
            item.triggered.connect(handler)
            menu.addAction(item)
            return item

        self.new_action = action("&New", self.new_file, QKeySequence.New)
        self.open_action = action("&Open…", self.open_file, QKeySequence.Open)
        self.open_story_action = action(
            "Open a &story…",
            self.open_story_chosen,
            tip="A story of yours, its world and cast filled in: add a plot to it, or change "
            "its plot",
        )
        self.open_story_action.setVisible(self.host is not None)
        self.save_action = action("&Save", self.save, QKeySequence.Save)
        self.save_as_action = action("Save &as…", self.save_as, QKeySequence.SaveAs)
        menu.addSeparator()
        action("&Preview file…", self.preview, tip="The Markdown this will be saved as")
        self.start_action = action(
            "Start a s&tory from this file",
            self.start_story,
            tip="Save, then import it as a new story",
        )
        menu.addSeparator()
        action("&Close", self.close, QKeySequence.Close)
        # The guides, where plots are written: what a file may hold, and how
        # to word it so the director and the reads take it as meant.
        guides = self.menuBar().addMenu("&Help")
        format_guide = QAction(FORMAT_LABEL, self)
        format_guide.setStatusTip("What a plot file may hold, part by part")
        format_guide.triggered.connect(lambda: open_guide(self, PLOT_FORMAT_GUIDE))
        guides.addAction(format_guide)
        writing_guide = QAction(WRITING_LABEL, self)
        writing_guide.setStatusTip(WRITING_TIP)
        writing_guide.triggered.connect(lambda: open_guide(self, PLOT_WRITING_GUIDE))
        guides.addAction(writing_guide)
        self._sync_mode()

    def _sync_mode(self) -> None:
        """The menu and the line above the tree for a file, or for a story."""
        story = self.story
        self.save_action.setText("&Save to the story" if story else "&Save")
        self.save_action.setStatusTip(
            f"Give “{story.title}” this plot" if story else "Save the plot file"
        )
        self.save_as_action.setText("Save a copy as a plot &file…" if story else "Save &as…")
        self.start_action.setVisible(story is None)
        self.story_label.setVisible(story is not None)
        if story is not None:
            self.story_label.setText(
                f"The plot of your story “{story.title}”. Its world, opening and cast are "
                "filled in from the story and changed there (Story → Setup…, the Cast and "
                "Lore pages); here you write the plot, and the people, places and lore it "
                "keeps hidden. Save to the story when it is ready."
            )

    def _folder(self) -> Path | None:
        """Where the file's pictures are kept from: None until it is saved,
        and for a story (its pictures are its own, on the Cast page)."""
        return self.path.parent if self.path is not None and self.story is None else None

    def _pictures(self) -> list[PictureDoc]:
        """Every picture the file names: its cards' and its background."""
        document = self.document
        owners = [*document.characters, *document.places, *document.lore]
        pictures = [picture for owner in owners for picture in owner.pictures]
        if document.background.strip():
            pictures.append(PictureDoc(file=document.background.strip()))
        return pictures

    def _refresh_title(self) -> None:
        mark = "*" if self.modified else ""
        if self.story is not None:
            self.setWindowTitle(f"{self.story.title}{mark} — its plot — Plot file editor")
            return
        name = self.path.name if self.path else "Untitled"
        self.setWindowTitle(f"{name}{mark} — Plot file editor")

    def _set_modified(self, modified: bool) -> None:
        self.modified = modified
        self._refresh_title()

    # --- the tree ----------------------------------------------------------------

    def _items(self, section: str) -> list:
        document = self.document
        if section == CHARACTER:
            return document.characters
        if section == PLACE:
            return document.places
        if section == LORE:
            return document.lore
        if section == FACT:
            return document.facts
        return document.events

    def _label(self, section: str, item) -> str:
        if section == FACT:
            return item.name or "(unnamed)"
        if section == EVENT:
            return item.title or "(untitled)"
        name = item.name or "(unnamed)"
        if self.story is not None and section in (CHARACTER, PLACE, LORE) and not item.story_id:
            return f"{name}  (new)"
        return name

    def _rebuild_tree(self, *, select: ItemRef | None) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        headers: dict[str, QTreeWidgetItem] = {}
        for group, label, about, sections in _GROUPS:
            half = QTreeWidgetItem([label])
            half.setData(0, REF_ROLE, (HEADER, group, -1))
            half.setToolTip(0, about)
            font = QFont(half.font(0))
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() * 1.1)
            half.setFont(0, font)
            self.tree.addTopLevelItem(half)
            for section, name in sections:
                header = QTreeWidgetItem([name])
                header.setData(0, REF_ROLE, (HEADER, section, -1))
                bold = QFont(header.font(0))
                bold.setBold(True)
                header.setFont(0, bold)
                half.addChild(header)
                headers[section] = header
        for section in (CHARACTER, PLACE, LORE, FACT):
            for index, item in enumerate(self._items(section)):
                row = QTreeWidgetItem([self._label(section, item)])
                row.setData(0, REF_ROLE, (section, index, -1))
                headers[section].addChild(row)
        for index, event in enumerate(self.document.events):
            row = QTreeWidgetItem([self._label(EVENT, event)])
            row.setData(0, REF_ROLE, (EVENT, index, -1))
            headers[TIMELINE if event.timeline else OPTIONAL].addChild(row)
            for variant_index, variant in enumerate(event.variants):
                child = QTreeWidgetItem([variant.title or "(untitled)"])
                child.setData(0, REF_ROLE, (EVENT, index, variant_index))
                row.addChild(child)
        self.tree.expandAll()
        self.tree.blockSignals(False)
        self._mark_problems()
        self._select(select)

    def _find(self, ref: ItemRef | None) -> QTreeWidgetItem | None:
        if ref is None:
            return None
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.data(0, REF_ROLE) == ref:
                return item
            stack.extend(item.child(i) for i in range(item.childCount()))
        return None

    def _select(self, ref: ItemRef | None) -> None:
        item = self._find(ref)
        if item is None and ref is not None and ref[0] in (FRONT, WORLD, OPENING, NOTES):
            item = self._find((HEADER, FRONT if ref[0] == NOTES else ref[0], -1))
        if item is None:
            item = self.tree.topLevelItem(0)
        self.tree.setCurrentItem(item)
        self._on_tree_selection(item, None)

    def current_ref(self) -> tuple | None:
        item = self.tree.currentItem()
        return item.data(0, REF_ROLE) if item is not None else None

    def _on_tree_selection(self, item: QTreeWidgetItem | None, _previous) -> None:
        ref = item.data(0, REF_ROLE) if item is not None else None
        self._show(ref)
        self._sync_buttons(ref)

    def _show(self, ref: tuple | None) -> None:
        if ref is None:
            self.pages.setCurrentWidget(self.empty_page)
            return
        kind, index, variant = ref
        if kind == HEADER:
            if index in (FRONT, WORLD, OPENING, PLOT):
                page = self._pages[index]
                page.load(self.document, self.document)
                self._dress(page, index, None)
                self.pages.setCurrentWidget(page)
            else:
                half = PLOT_HALF if index in _PLOT_SECTIONS else STORY_HALF
                self.section_page.show_section(
                    f"{half} · {_SECTION_NAMES.get(index, '')}", _SECTION_ABOUT.get(index, "")
                )
                self.pages.setCurrentWidget(self.section_page)
            return
        items = self._items(kind)
        if not 0 <= index < len(items):
            self.pages.setCurrentWidget(self.empty_page)
            return
        item = items[index]
        if kind == EVENT and variant >= 0:
            page = self._pages["variant"]
            assert isinstance(page, WayPage)
            page.load_way(item.variants[variant], item, self.document)
        elif kind == EVENT:
            page = self._pages[EVENT]
            assert isinstance(page, WayPage)
            page.load_way(item, None, self.document)
        else:
            page = self._pages[kind]
            page.load(item, self.document)
        self._dress(page, kind, item)
        self.pages.setCurrentWidget(page)

    def _dress(self, page: Page, kind: str, item) -> None:
        """The page's heading, and in story mode what of it is the plot's to
        change here (the rest is read only, and says where it is changed)."""
        what = {
            FRONT: "Title and settings",
            WORLD: "World",
            OPENING: "Opening",
            PLOT: "Its clock",
            CHARACTER: "A character",
            PLACE: "A place",
            LORE: "A lore entry",
            FACT: "A fact",
            EVENT: "An event",
        }.get(kind, "")
        if kind == EVENT and page is self._pages["variant"]:
            what = "A way the event can go"
        half = PLOT_HALF if kind in _PLOT_SECTIONS else STORY_HALF
        for strip in (getattr(page, "pictures", None), getattr(page, "pictures_label", None)):
            if strip is not None:
                # A story's pictures are its own, on the Cast and Lore pages.
                strip.setVisible(self.story is None)
        story = self.story
        if story is None or kind in _PLOT_SECTIONS:
            page.set_context(f"{half} · {what}")
            page.set_locked(False)
            return
        if kind in (FRONT, WORLD, OPENING):
            where = "Story → Setup… and on the Style page" if kind == FRONT else "Story → Setup…"
            page.set_context(
                f"{half} · {what}",
                f"From the story “{story.title}”: changed in {where}, not here.",
            )
            page.set_locked(True)
            return
        if kind == CHARACTER:
            assert isinstance(page, CharacterPage)
            self._dress_character(page, item, story, f"{half} · {what}")
        else:
            assert isinstance(page, EntryPage)
            self._dress_entry(page, item, story, f"{half} · {what}")

    def _dress_character(
        self, page: CharacterPage, item: CharacterDoc, story: EditedStory, scope: str
    ) -> None:
        if not item.story_id:
            page.set_context(
                scope,
                f"New: added to “{story.title}” when you save. Once saved, someone the plot "
                "doesn't keep hidden is the story's, changed on the Cast page.",
            )
            page.set_locked(
                True,
                open_=(
                    page.name,
                    page.role,
                    page.hidden,
                    page.aliases,
                    page.voice,
                    page.canon,
                    page.summary,
                    page.description,
                ),
            )
            return
        if item.hidden:
            page.set_context(
                scope,
                "Kept hidden by the plot until an event brings them in, so theirs to change "
                "here. Their pictures and skills are on the Cast page.",
            )
            page.set_locked(
                True,
                open_=(
                    page.name,
                    page.role,
                    page.hidden,
                    page.aliases,
                    page.voice,
                    page.canon,
                    page.summary,
                    page.description,
                ),
            )
            return
        if story.begun:
            why = "The story has begun, so the plot can't hide them now."
        elif item.story_id in story.starts_with:
            why = "The story begins with them, so the plot can't hide them."
        else:
            why = "Here the plot can keep them hidden until an event brings them in."
        page.set_context(
            scope, f"From the story “{story.title}”: changed on the Cast page, not here. {why}"
        )
        hideable = not story.begun and item.story_id not in story.starts_with
        page.set_locked(True, open_=(page.hidden,) if hideable else ())

    def _dress_entry(self, page: EntryPage, item: EntryDoc, story: EditedStory, scope: str) -> None:
        if not item.story_id or item.hidden:
            page.set_context(
                scope,
                f"New: added to “{story.title}” when you save."
                if not item.story_id
                else "Kept hidden by the plot until an event reveals it, so the plot's to "
                "change here.",
            )
            page.set_locked(False)
            return
        hideable = not story.begun
        page.set_context(
            scope,
            f"From the story “{story.title}”: changed on the Lore page, not here. Here: "
            "whether it is one of the plot's places"
            + (", and whether the plot hides it until an event reveals it." if hideable else "."),
        )
        page.set_locked(True, open_=(page.hidden,) if hideable else ())

    def _story_item(self, ref: tuple | None) -> bool:
        """Whether the row is one of the story's own cards or entries (story mode)."""
        if self.story is None or ref is None or ref[0] not in (CHARACTER, PLACE, LORE):
            return False
        items = self._items(ref[0])
        return 0 <= ref[1] < len(items) and bool(items[ref[1]].story_id)

    def _sync_buttons(self, ref: tuple | None) -> None:
        kind = ref[0] if ref else None
        section = ref[1] if kind == HEADER else kind
        in_list = section in _LISTS or section == EVENT
        self.add_button.setEnabled(in_list)
        self.variant_button.setEnabled(kind == EVENT)
        # The story's own cards are removed and ordered on its pages.
        own = self._story_item(ref)
        is_item = kind != HEADER and kind is not None
        self.remove_button.setEnabled(is_item and not own)
        self.remove_button.setToolTip(
            "The story's own are removed on its Cast and Lore pages" if own else ""
        )
        self.up_button.setEnabled(is_item and not own)
        self.down_button.setEnabled(is_item and not own)

    # --- editing -------------------------------------------------------------------

    def _touch(self) -> None:
        self._set_modified(True)
        self._check_timer.start()

    def _on_page_changed(self) -> None:
        ref = self.current_ref()
        item = self.tree.currentItem()
        if ref is not None and item is not None and ref[0] != HEADER:
            kind, index, variant = ref
            target = self._items(kind)[index]
            if kind == EVENT and variant >= 0:
                target = target.variants[variant]
            label = self._label(kind, target) if variant < 0 else (target.title or "(untitled)")
            if item.text(0) != label:
                item.setText(0, label)
            # Ticking "on the timeline" moves the event between the two headers.
            if kind == EVENT and variant < 0:
                parent = item.parent()
                listed_under = parent.data(0, REF_ROLE)[1] if parent is not None else None
                wanted = TIMELINE if target.timeline else OPTIONAL
                if listed_under != wanted:
                    self._rebuild_tree(select=ref)
            elif self.story is not None and kind in (CHARACTER, PLACE, LORE):
                # Hidden or shown, a story's card is the plot's or the story's:
                # what may change on its page follows.
                page = self.pages.currentWidget()
                if isinstance(page, Page):
                    self._dress(page, kind, target)
        self._touch()

    def _on_renamed(self, old: str, new: str) -> None:
        ref = self.current_ref()
        if ref is None or ref[0] == HEADER:
            return
        kind = ref[0]
        if kind == FACT:
            self.document.rename_fact(old, new)
        elif kind in (PLACE, LORE):
            self.document.rename_place(old, new)
        elif kind == EVENT:
            self.document.rename_event(old, new)
        elif kind == CHARACTER:
            self.document.rename_character(old, new)

    def _on_place_toggled(self, place: bool) -> None:
        """An entry moved between Places and Lore: a story's own entry becomes
        one of the plot's places this way, or stops being one."""
        ref = self.current_ref()
        if ref is None or ref[0] not in (PLACE, LORE) or (ref[0] == PLACE) == place:
            return
        source = self._items(ref[0])
        if not 0 <= ref[1] < len(source):
            return
        entry = source.pop(ref[1])
        target = self.document.places if place else self.document.lore
        target.append(entry)
        self._rebuild_tree(select=(PLACE if place else LORE, len(target) - 1, -1))
        self._touch()

    def _add(self) -> None:
        ref = self.current_ref()
        if ref is None:
            return
        section = ref[1] if ref[0] == HEADER else ref[0]
        if section == EVENT:
            parent = self.tree.currentItem()
            while parent.parent() is not None and parent.data(0, REF_ROLE)[0] != HEADER:
                parent = parent.parent()
            section = parent.data(0, REF_ROLE)[1]
        if section == CHARACTER:
            # Someone a story's plot adds is most often someone it brings in.
            self.document.characters.append(
                CharacterDoc(name="New character", hidden=self.story is not None)
            )
            select: ItemRef = (CHARACTER, len(self.document.characters) - 1, -1)
        elif section == PLACE:
            self.document.places.append(EntryDoc(name="New place"))
            select = (PLACE, len(self.document.places) - 1, -1)
        elif section == LORE:
            self.document.lore.append(EntryDoc(name="New entry"))
            select = (LORE, len(self.document.lore) - 1, -1)
        elif section == FACT:
            self.document.facts.append(FactDoc(name="new_fact", initial="no"))
            select = (FACT, len(self.document.facts) - 1, -1)
        elif section in (TIMELINE, OPTIONAL):
            timeline = section == TIMELINE
            self.document.events.append(
                EventDoc(title="New event", timeline=timeline, when=(1, 1) if timeline else None)
            )
            select = (EVENT, len(self.document.events) - 1, -1)
        else:
            return
        self._rebuild_tree(select=select)
        self._touch()

    def _add_variant(self) -> None:
        ref = self.current_ref()
        if ref is None or ref[0] != EVENT:
            return
        event = self.document.events[ref[1]]
        event.variants.append(VariantDoc(title="New variant"))
        self._rebuild_tree(select=(EVENT, ref[1], len(event.variants) - 1))
        self._touch()

    def _remove(self) -> None:
        ref = self.current_ref()
        if ref is None or ref[0] == HEADER or self._story_item(ref):
            return
        kind, index, variant = ref
        if kind == EVENT and variant >= 0:
            del self.document.events[index].variants[variant]
            select: ItemRef = (EVENT, index, -1)
        else:
            items = self._items(kind)
            del items[index]
            select = (kind, min(index, len(items) - 1), -1) if items else (HEADER, kind, -1)
            if kind == EVENT and not items:
                select = (HEADER, TIMELINE, -1)
        self._rebuild_tree(select=select)
        self._touch()

    def _move(self, step: int) -> None:
        ref = self.current_ref()
        if ref is None or ref[0] == HEADER or self._story_item(ref):
            return
        kind, index, variant = ref
        if kind == EVENT and variant >= 0:
            variants = self.document.events[index].variants
            other = variant + step
            if not 0 <= other < len(variants):
                return
            variants[variant], variants[other] = variants[other], variants[variant]
            self._rebuild_tree(select=(EVENT, index, other))
        else:
            items = self._items(kind)
            other = index + step
            if kind == EVENT:
                # Stay under the same header: the neighbour of the same kind.
                same = [i for i, e in enumerate(items) if e.timeline == items[index].timeline]
                position = same.index(index) + step
                if not 0 <= position < len(same):
                    return
                other = same[position]
            if not 0 <= other < len(items):
                return
            items[index], items[other] = items[other], items[index]
            self._rebuild_tree(select=(kind, other, -1))
        self._touch()

    # --- checking --------------------------------------------------------------------

    def check_now(self) -> None:
        self._check_timer.stop()
        self.rendered, self.problems = check_plot_document(self.document)
        self.problem_list.clear()
        for problem in self.problems:
            where = describe_where(self.document, problem.where)
            kind = "Error" if problem.error else "Note"
            row = QListWidgetItem(f"{kind} — {where}: {problem.message}")
            row.setData(REF_ROLE, problem.where)
            row.setForeground(theme.colour("plot_error" if problem.error else "plot_note"))
            self.problem_list.addItem(row)
        errors = sum(1 for p in self.problems if p.error)
        notes = len(self.problems) - errors
        reads = "The story takes this plot" if self.story else "The importer reads this file"
        if not self.problems:
            self.summary.setText(f"{reads} with nothing to say.")
        elif errors:
            stops = "this plot reaching the story" if self.story else "this file importing"
            self.summary.setText(
                f"{errors} error{'s stop' if errors != 1 else ' stops'} {stops}"
                + (f", and {notes} note{'s' if notes != 1 else ''}." if notes else ".")
            )
        else:
            self.summary.setText(
                f"{'Saves to the story' if self.story else 'Imports'}, with {notes} "
                f"note{'s' if notes != 1 else ''}."
            )
        self._mark_problems()

    def _problems_for(self, ref: tuple) -> list[DocProblem]:
        """The problems a tree row stands for. The story's row takes the file's own."""
        if ref[0] == HEADER:
            section = ref[1]
            if section == FRONT:
                return [p for p in self.problems if p.where is None or p.where[0] in (FRONT, NOTES)]
            if section in (WORLD, OPENING):
                return [p for p in self.problems if p.where == (section, 0, -1)]
            return []
        return [p for p in self.problems if p.where == ref]

    def _mark_problems(self) -> None:
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            stack.extend(item.child(i) for i in range(item.childCount()))
            found = self._problems_for(item.data(0, REF_ROLE))
            if not found:
                item.setData(0, Qt.ForegroundRole, None)
                item.setToolTip(0, _GROUP_ABOUT.get(item.data(0, REF_ROLE), ""))
                continue
            item.setForeground(
                0, theme.colour("plot_error" if any(p.error for p in found) else "plot_note")
            )
            item.setToolTip(0, "\n".join(p.message for p in found))

    def _on_problem_activated(self, row: QListWidgetItem) -> None:
        self._select(row.data(REF_ROLE) or (HEADER, FRONT, -1))

    # --- files ------------------------------------------------------------------------

    def _confirm_discard(self) -> bool:
        if not self.modified:
            return True
        where = f"“{self.story.title}”" if self.story else "this plot file"
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            f"Save the changes to {where} first?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    def new_file(self) -> None:
        if not self._confirm_discard():
            return
        self.load_document(PlotDocument(), path=None)

    def load_document(
        self, document: PlotDocument, *, path: Path | None, story: EditedStory | None = None
    ) -> None:
        self.document = document
        self.path = path
        self.story = story
        self._sync_mode()
        self._rebuild_tree(select=(HEADER, FRONT, -1))
        self.check_now()
        self._set_modified(False)

    def load_story(self, bundle: StoryBundle, *, select: ItemRef | None = None) -> None:
        """Work on a story's plot (story mode): the document filled in from
        the story (engine/story_plot.py)."""
        story = bundle.story
        document = document_from_story(bundle)
        self.load_document(
            document,
            path=None,
            story=EditedStory(
                id=story.id,
                title=story.title,
                begun=bool(bundle.nodes),
                starts_with=frozenset(
                    c.story_id for c in document.characters if c.present or c.choose
                ),
                had_plot=story.plot is not None,
            ),
        )
        if select is not None:
            self._select(select)

    def open_story(self, story_id: str) -> bool:
        """The story's plot, from the main window (Story → Edit the plot in
        the plot editor…, File → Open a story…)."""
        if self.host is None:
            return False
        if self.story is not None and self.story.id == story_id and self.modified:
            return True  # what is being written for it stays
        if not self._confirm_discard():
            return False
        bundle = self.host.plot_editor_bundle(story_id)
        if bundle is None:
            return False
        self.load_story(bundle)
        return True

    def open_story_chosen(self) -> None:
        """File → Open a story…: which, from the author's stories."""
        if self.host is None:
            return
        choices = self.host.plot_editor_stories()
        if not choices:
            QMessageBox.information(
                self, "No stories", "There are no stories to give a plot to yet."
            )
            return
        labels = [label for _, label in choices]
        current = next(
            (
                i
                for i, (story_id, _) in enumerate(choices)
                if self.story and story_id == self.story.id
            ),
            0,
        )
        label, chosen = QInputDialog.getItem(
            self,
            "Open a story",
            "Which story's plot? Its world and cast come filled in:",
            labels,
            current,
            False,
        )
        if chosen:
            self.open_story(choices[labels.index(label)][0])

    def open_file(self) -> None:
        if not self._confirm_discard():
            return
        start = str(self.path.parent if self.path else Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Open plot file", start, f"Plot files (*{PLOT_SUFFIX});;All files (*)"
        )
        if chosen:
            self.open_path(Path(chosen))

    def open_path(self, path: Path) -> bool:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.warning(self, "Could not read plot file", f"{path.name}: {exc}")
            return False
        read = read_plot_document(text)
        if read.problems:
            box = QMessageBox(
                QMessageBox.Warning,
                "Some of this file can't be kept",
                f"{path.name} has {len(read.problems)} thing(s) the editor has no place for. "
                "They will be left out if the file is saved from here. Open it anyway?",
                QMessageBox.Yes | QMessageBox.Cancel,
                self,
            )
            box.setDetailedText("\n".join(problem.describe() for problem in read.problems))
            if box.exec() != QMessageBox.Yes:
                return False
        self.load_document(read.document, path=path)
        return True

    def save(self) -> bool:
        if self.story is not None:
            return self._save_to_story()
        if self.path is None:
            return self.save_as()
        return self._write(self.path)

    def save_as(self) -> bool:
        """A plot file. For a story, a copy of it all, the story's world and
        cast with its plot, and the editor stays on the story."""
        start = str(self.path) if self.path else str(Path.home() / self._suggested_name())
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Save plot file", start, f"Plot files (*{PLOT_SUFFIX});;All files (*)"
        )
        if not chosen:
            return False
        path = Path(chosen)
        if path.suffix.lower() != PLOT_SUFFIX:
            path = path.with_suffix(PLOT_SUFFIX)
        return self._write(path)

    def _suggested_name(self) -> str:
        title = "".join(c for c in self.document.title if c.isalnum() or c in " -_").strip()
        return f"{title or 'plot'}{PLOT_SUFFIX}"

    def _write(self, path: Path) -> bool:
        self.check_now()
        errors = sum(1 for p in self.problems if p.error)
        if errors:
            answer = QMessageBox.question(
                self,
                "Errors in the file",
                f"The importer will refuse this file until {errors} error"
                f"{'s are' if errors != 1 else ' is'} fixed. Save it anyway?",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Save:
                return False
        text = render_plot_markdown(self.document).text
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", f"{path.name}: {exc}")
            return False
        if self.story is not None:
            # A copy: the story is still the one being written for.
            self.statusBar().showMessage(f"A copy saved as {path.name}.", 8000)
            return True
        old = self._folder()
        self.path = path
        self._set_modified(False)
        left: list[str] = []
        if old is not None and old.resolve() != path.parent.resolve():
            # Saved somewhere else: the pictures its lines name come along.
            left += copy_pictures(self._pictures(), old, path.parent)
        if left:
            QMessageBox.warning(
                self,
                "Some pictures stayed behind",
                f"The file was saved, but these couldn't be copied beside it from {old}:\n\n"
                + "\n".join(left),
            )
        self._show(self.current_ref())  # a picture's thumbnail follows the folder
        return True

    def _save_to_story(self) -> bool:
        """The plot given to the story, through the main window. A plot the
        importer refuses can't reach a story, so its errors come first."""
        story = self.story
        if story is None or self.host is None:
            return False
        self.check_now()
        errors = sum(1 for p in self.problems if p.error)
        if errors:
            QMessageBox.warning(
                self,
                "Not saved yet",
                f"{errors} error{'s' if errors != 1 else ''} in the plot must be fixed before "
                f"“{story.title}” can take it. They are listed at the bottom.",
            )
            return False
        if story.had_plot and not self.document.facts and not self.document.events:
            answer = QMessageBox.question(
                self,
                "Take the plot away",
                f"This plot has no facts and no events. Take “{story.title}”'s plot away?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return False
        ref = self.current_ref()
        notes = self.host.plot_editor_save(story.id, self.document)
        if notes is None:
            return False
        bundle = self.host.plot_editor_bundle(story.id)
        if bundle is not None:
            # What is new is the story's now: read back, with the story's ids.
            self.load_story(bundle, select=ref)
        else:
            self._set_modified(False)
        self.statusBar().showMessage(f"Saved to “{story.title}”.", 8000)
        if notes:
            QMessageBox.information(
                self,
                "Saved, with notes",
                f"“{story.title}” has the plot. Some of it couldn't be done:\n\n"
                + "\n".join(f"• {note}" for note in notes),
            )
        return True

    def preview(self) -> None:
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)  # kept with the editor otherwise
        dialog.setWindowTitle("Plot file preview")
        dialog.setMinimumSize(720, 560)
        view = QPlainTextEdit(render_plot_markdown(self.document).text)
        view.setReadOnly(True)
        font = QFont(theme.fonts().mono or "monospace")
        font.setStyleHint(QFont.Monospace)
        view.setFont(font)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(view)
        layout.addWidget(buttons)
        dialog.exec()

    def start_story(self) -> None:
        self.check_now()
        if any(p.error for p in self.problems):
            QMessageBox.warning(
                self,
                "Not ready to import",
                "The file has errors the importer will refuse; fix them first (they are "
                "listed at the bottom).",
            )
            return
        if not self.save():
            return
        self.start_requested.emit(self.path)

    def _revert(self) -> None:
        """Back to the file as it was last saved, or to a blank one.

        The window is kept while the app runs (MainWindow.open_plot_editor),
        so a close that discarded changes left them in the document: opened
        again, the editor showed what the author had just thrown away. A
        story's is read from the story again when it is next opened.
        """
        document = PlotDocument()
        path = self.path if self.story is None else None
        if path is not None:
            try:
                document = read_plot_document(path.read_text(encoding="utf-8")).document
            except (OSError, UnicodeDecodeError):
                path = None  # the file has gone: nothing to go back to
        self.load_document(document, path=path)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._confirm_discard():
            event.ignore()
            return
        if self.modified:
            # Still modified after the question: the answer was Discard.
            self._revert()
        event.accept()
