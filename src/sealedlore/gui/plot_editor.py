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
Open in the plot editor…), filled in with everything a plot file can hold of
it: its settings, world, opening, cast, places, lore, pictures and plot
(engine/story_plot.py). Anything is editable, and Save never writes over the
story it was made from (the author, Nov 6 2026): it makes a new story, the
same setup unplayed with the editor's changes, and the editor goes on with
that one, saving to it until it begins. The main window (`PlotEditorHost`)
reads and makes the stories, so a story open there is written by its session,
its one writer. The story's pictures are shown from a copy of them
(`mirror_story_pictures`), so one added here reaches a story only when saved.
"""

from __future__ import annotations

from collections.abc import Callable
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
from sealedlore.engine.story_plot import PictureReader, document_from_story
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
from sealedlore.gui.plot_pictures import copy_pictures, forget_folder, mirror_story_pictures
from sealedlore.gui.ref_images import inside
from sealedlore.gui.sizing import size_to_screen
from sealedlore.models.plot_file import (
    CharacterDoc,
    EntryDoc,
    EventDoc,
    FactDoc,
    PictureDoc,
    PlotDocument,
    ReferenceKind,
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
# What other items name each kind of item by, and the field of its name.
_NAMED: dict[str, tuple[ReferenceKind, str]] = {
    CHARACTER: ("character", "name"),
    PLACE: ("place", "name"),
    LORE: ("place", "name"),
    FACT: ("fact", "name"),
    EVENT: ("event", "title"),
}
CHECK_DELAY_MS = 250


class PlotEditorHost(Protocol):
    """What the editor needs of the main window to work on a story
    (gui/window_plot.py). Each says why itself when it can't."""

    def plot_editor_stories(self) -> list[tuple[str, str]]:
        """(story id, label) for every story that can have a plot."""
        ...

    def plot_editor_bundle(self, story_id: str) -> StoryBundle | None:
        """The story, as the main window has it (open there or on disk), or None."""
        ...

    def plot_editor_folder(self, story_id: str) -> Path | None:
        """The story's folder, its pictures in it."""
        ...

    def plot_editor_new_story(
        self,
        source_id: str,
        document: PlotDocument,
        before: PlotDocument,
        read: PictureReader,
    ) -> tuple[str, list[str]] | None:
        """A new story: `source_id`'s setup, unplayed, made what the document
        holds. Its id and what couldn't be done, or None."""
        ...

    def plot_editor_update(
        self, story_id: str, document: PlotDocument, before: PlotDocument, read: PictureReader
    ) -> list[str] | None:
        """A story the editor made, not begun, made what the document holds:
        what couldn't be done, or None."""
        ...

    def plot_editor_start(self, story_id: str) -> None:
        """Open the story in the main window and start it."""
        ...


@dataclass
class EditedStory:
    """The story the editor works on (story mode)."""

    id: str
    title: str
    # Made by this editor's Save and not begun when last read: saving goes to
    # it. Any other story is never written: saving makes a new one.
    own: bool = False


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
        # The story being worked on, in place of a file (story mode); the
        # document as it was read from it, so a save writes only what changed
        # here; and the copy of its pictures the pages show and add to.
        self.story: EditedStory | None = None
        self._loaded: PlotDocument | None = None
        self._story_pictures: Path | None = None
        self.modified = False
        self.rendered: RenderedPlot | None = None
        self.problems: list[DocProblem] = []
        # The item being renamed, the name it had when its rename began, and
        # what named it then (`_on_renamed`); forgotten when another is shown.
        self._renaming: tuple[object, str, list[Callable[[str], None]]] | None = None
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
            FRONT: StoryPage(self._folder, self.save),
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
            tip="A story of yours, everything filled in: change anything, its plot above all, "
            "and save it as a new story",
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
        self.story_label.setVisible(story is not None)
        story_page = self._pages[FRONT]
        assert isinstance(story_page, StoryPage)
        story_page.set_story_mode(story is not None)
        for page in (self._pages[CHARACTER], self._pages[PLACE], self._pages[LORE]):
            page.pictures.set_story(story is not None)
        if story is None:
            self.save_action.setText("&Save")
            self.save_action.setStatusTip("Save the plot file")
            self.save_as_action.setText("Save &as…")
            self.start_action.setText("Start a s&tory from this file")
            self.start_action.setStatusTip("Save, then import it as a new story")
            return
        self.save_as_action.setText("Save a copy as a plot &file…")
        if story.own:
            self.save_action.setText("&Save")
            self.save_action.setStatusTip(f"Save to “{story.title}”, the story made here")
            self.start_action.setText("S&tart the story")
            self.start_action.setStatusTip(f"Save, then start “{story.title}”")
            self.story_label.setText(
                f"“{story.title}”, made here and not begun yet: saving changes it. Once it "
                "has begun, saving makes a new story instead."
            )
            return
        self.save_action.setText("&Save as a new story…")
        self.save_action.setStatusTip(
            f"A new story with your changes; “{story.title}” stays as it is"
        )
        self.start_action.setText("S&tart a new story from this")
        self.start_action.setStatusTip("Save as a new story, then start it")
        self.story_label.setText(
            f"Made from your story “{story.title}”: change anything. Saving makes a new "
            f"story with your changes, unplayed, and leaves “{story.title}” as it is."
        )

    def _folder(self) -> Path | None:
        """Where the pictures are kept from: beside the file, None until it
        is saved; for a story, the copy of its pictures."""
        if self.story is not None:
            return self._story_pictures
        return self.path.parent if self.path is not None else None

    def _read_picture(self, file: str) -> bytes | None:
        """A picture the document names, from the copy of the story's."""
        folder = self._story_pictures
        path = inside(folder, file) if folder is not None else None
        try:
            return path.read_bytes() if path is not None and path.is_file() else None
        except OSError:
            return None

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
            self.setWindowTitle(f"{self.story.title}{mark} — Plot file editor")
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
            # Capitals, spaced: the two halves stand apart from the bold
            # sections under them.
            font = QFont(half.font(0))
            font.setBold(True)
            font.setCapitalization(QFont.AllUppercase)
            font.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
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
        self._renaming = None
        if ref is None:
            self.pages.setCurrentWidget(self.empty_page)
            return
        kind, index, variant = ref
        if kind == HEADER:
            if index in (FRONT, WORLD, OPENING, PLOT):
                page = self._pages[index]
                page.load(self.document, self.document)
                self._dress(page, index)
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
        self._dress(page, kind)
        self.pages.setCurrentWidget(page)

    def _dress(self, page: Page, kind: str) -> None:
        """The page's heading: which half it is in, and what it is."""
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
        page.set_context(f"{half} · {what}")

    def _sync_buttons(self, ref: tuple | None) -> None:
        kind = ref[0] if ref else None
        section = ref[1] if kind == HEADER else kind
        in_list = section in _LISTS or section == EVENT
        self.add_button.setEnabled(in_list)
        self.variant_button.setEnabled(kind == EVENT)
        is_item = kind != HEADER and kind is not None
        self.remove_button.setEnabled(is_item)
        self.up_button.setEnabled(is_item)
        self.down_button.setEnabled(is_item)

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
        self._touch()

    def _on_renamed(self, old: str, new: str) -> None:
        """The item shown has another name (a key at a time): what named it
        follows. What named it is found once, from the name it had as its
        rename began, and only if that name was its alone; while its name is
        blank or another item's, they keep the name it had (the check says
        what is wrong). Each key used to rename every item and reference with
        the name of the key before: typing "John Junior" for a new character
        renamed John, and everything that named him, on the way."""
        ref = self.current_ref()
        if ref is None or ref[0] not in _NAMED or (ref[0] == EVENT and ref[2] >= 0):
            return
        kind, attribute = _NAMED[ref[0]]
        items = self._items(ref[0])
        if not 0 <= ref[1] < len(items):
            return
        item = items[ref[1]]
        namesakes = (*self.document.places, *self.document.lore) if kind == "place" else items
        others = {getattr(other, attribute) for other in namesakes if other is not item}
        if self._renaming is None or self._renaming[0] is not item:
            alone = bool(old.strip()) and old not in others
            self._renaming = (item, old, self.document.references(kind, old) if alone else [])
        _item, start, follow = self._renaming
        name = new if new.strip() and new not in others else start
        for write in follow:
            write(name)

    def _on_place_toggled(self, place: bool) -> None:
        """An entry moved between Places and Lore: a story's lore entry
        becomes one of the plot's places this way, or stops being one."""
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
            self.document.characters.append(CharacterDoc(name="New character"))
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
        if ref is None or ref[0] == HEADER:
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
        if ref is None or ref[0] == HEADER:
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
        reads = "This saves as a story" if self.story else "The importer reads this file"
        if not self.problems:
            self.summary.setText(f"{reads} with nothing to say.")
        elif errors:
            stops = "this saving as a story" if self.story else "this file importing"
            self.summary.setText(
                f"{errors} error{'s stop' if errors != 1 else ' stops'} {stops}"
                + (f", and {notes} note{'s' if notes != 1 else ''}." if notes else ".")
            )
        else:
            self.summary.setText(
                f"{'Saves' if self.story else 'Imports'}, with {notes} "
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
        story = self.story
        if story is None:
            question = "Save the changes to this plot file first?"
        elif story.own:
            question = f"Save the changes to “{story.title}” first?"
        else:
            question = (
                f"Save your changes as a new story first? “{story.title}” itself stays as it is."
            )
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            question,
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
        self,
        document: PlotDocument,
        *,
        path: Path | None,
        story: EditedStory | None = None,
        pictures: Path | None = None,
    ) -> None:
        """`pictures`: for a story, the copy of its pictures (the editor
        removes it when it leaves the story)."""
        if self._story_pictures is not None and self._story_pictures != pictures:
            forget_folder(self._story_pictures)
        self._story_pictures = pictures
        self.document = document
        self.path = path
        self.story = story
        if story is None:
            self._loaded = None
        self._sync_mode()
        self._rebuild_tree(select=(HEADER, FRONT, -1))
        self.check_now()
        self._set_modified(False)

    def load_story(
        self, bundle: StoryBundle, *, own: bool = False, select: ItemRef | None = None
    ) -> None:
        """Work on a story (story mode): the document filled in from all of
        it a plot file can hold (engine/story_plot.py). `own`: a story this
        editor made, or one being set up that has never begun, which Save
        goes on writing to; any other is saved as a new story."""
        story = bundle.story
        document = document_from_story(bundle, whole=True)
        folder = self.host.plot_editor_folder(story.id) if self.host is not None else None
        pictures = mirror_story_pictures(bundle, folder)
        self.load_document(
            document,
            path=None,
            story=EditedStory(id=story.id, title=story.title, own=own and not bundle.nodes),
            pictures=pictures,
        )
        self._loaded = document.model_copy(deep=True)
        if select is not None:
            self._select(select)

    def open_story(self, story_id: str, *, own: bool = False) -> bool:
        """A story, from the main window (Story → Open in the plot editor…,
        File → Open a story…, or a plot the model has just written)."""
        if self.host is None:
            return False
        if self.story is not None and self.story.id == story_id and self.modified:
            return True  # what is being written for it stays
        if not self._confirm_discard():
            return False
        bundle = self.host.plot_editor_bundle(story_id)
        if bundle is None:
            return False
        self.load_story(bundle, own=own)
        return True

    def open_story_chosen(self) -> None:
        """File → Open a story…: which, from the author's stories."""
        if self.host is None:
            return
        choices = self.host.plot_editor_stories()
        if not choices:
            QMessageBox.information(self, "No stories", "There are no stories to open yet.")
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
            "Which story? Everything comes filled in, and saving makes a new story:",
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
            return self._save_own() if self.story.own else self._save_new()
        if self.path is None:
            return self.save_as()
        return self._write(self.path)

    def save_as(self) -> bool:
        """A plot file. For a story, a copy of it all as a file, its pictures
        beside it, and the editor stays on the story."""
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
            left = copy_pictures(self._pictures(), self._story_pictures, path.parent)
            self.statusBar().showMessage(f"A copy saved as {path.name}.", 8000)
            if left:
                QMessageBox.warning(
                    self,
                    "Some pictures stayed behind",
                    "The file was saved, but these pictures couldn't be copied beside it:\n\n"
                    + "\n".join(left),
                )
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

    def _ready_for_a_story(self) -> bool:
        """A document the importer refuses can't be a story: its errors first."""
        self.check_now()
        errors = sum(1 for p in self.problems if p.error)
        if errors:
            QMessageBox.warning(
                self,
                "Not saved yet",
                f"{errors} error{'s' if errors != 1 else ''} must be fixed before this can be "
                "saved as a story. They are listed at the bottom.",
            )
        return not errors

    def _save_new(self) -> bool:
        """A new story: the one the editor was opened on, its setup unplayed,
        with the changes made here (the author, Nov 6 2026: never written
        over the story it is based on). The editor goes on with the new one."""
        story, before = self.story, self._loaded
        if story is None or before is None or self.host is None:
            return False
        if not self._ready_for_a_story():
            return False
        suggested = self.document.title.strip() or story.title
        if suggested == before.title.strip():
            suggested = f"{suggested} (copy)"
        title, chosen = QInputDialog.getText(
            self,
            "Save as a new story",
            f"The new story's title (“{story.title}” stays as it is):",
            text=suggested,
        )
        if not chosen or not title.strip():
            return False
        document = self.document.model_copy(update={"title": title.strip()}, deep=True)
        made = self.host.plot_editor_new_story(story.id, document, before, self._read_picture)
        if made is None:
            return False
        story_id, notes = made
        self._read_back(story_id)
        self.statusBar().showMessage(
            f"Saved as a new story, “{title.strip()}”. “{story.title}” is as it was.", 10000
        )
        self._tell_notes(title.strip(), notes)
        return True

    def _save_own(self) -> bool:
        """Again to the story the editor made, while it hasn't begun; once it
        has, a new story is made from it instead."""
        story, before = self.story, self._loaded
        if story is None or before is None or self.host is None:
            return False
        bundle = self.host.plot_editor_bundle(story.id)
        if bundle is None:
            return False
        if bundle.nodes:
            story.own = False
            self._sync_mode()
            QMessageBox.information(
                self,
                "Saved as a new story",
                f"“{story.title}” has begun since, so it stays as it is: your changes go into "
                "a new story.",
            )
            return self._save_new()
        if not self._ready_for_a_story():
            return False
        notes = self.host.plot_editor_update(story.id, self.document, before, self._read_picture)
        if notes is None:
            return False
        self._read_back(story.id)
        self.statusBar().showMessage(f"Saved to “{self.story.title}”.", 8000)
        self._tell_notes(self.story.title, notes)
        return True

    def _read_back(self, story_id: str) -> None:
        """The story as saved, read again, with the story's own ids for what
        was new, the row being worked on still chosen."""
        ref = self.current_ref()
        bundle = self.host.plot_editor_bundle(story_id) if self.host is not None else None
        if bundle is not None:
            self.load_story(bundle, own=True, select=ref)
        else:
            self._set_modified(False)

    def _tell_notes(self, title: str, notes: list[str]) -> None:
        if notes:
            QMessageBox.information(
                self,
                "Saved, with notes",
                f"“{title}” is saved. Some of it couldn't be done:\n\n"
                + "\n".join(f"• {note}" for note in notes),
            )

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
        story = self.story
        if story is not None:
            # A story: saved first (as a new one, unless it is the editor's
            # own and nothing changed), then started in the main window.
            if (self.modified or not story.own) and not self.save():
                return
            if self.host is not None and self.story is not None:
                self.host.plot_editor_start(self.story.id)
            return
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
        story is read from the story again when it is next opened, and the
        copy of its pictures goes.
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
