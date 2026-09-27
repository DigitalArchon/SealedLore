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

It is its own window and needs no story open. Saving writes the `.md`;
"Start a story from this file" hands the saved file to the main window's
import, which is the same path a hand-written file takes.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
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
from sealedlore.gui import theme
from sealedlore.gui.plot_editor_pages import (
    CharacterPage,
    EntryPage,
    FactPage,
    OpeningPage,
    Page,
    StoryPage,
    WayPage,
    WorldPage,
)
from sealedlore.models.plot_file import (
    CharacterDoc,
    EntryDoc,
    EventDoc,
    FactDoc,
    PlotDocument,
    VariantDoc,
)

REF_ROLE = Qt.UserRole + 1
# Tree headers: what each holds, and what "Add" makes under it. Timeline and
# optional events share one list in the document; the header says which.
HEADER = "header"
TIMELINE = "timeline"
OPTIONAL = "events"
_HEADERS: list[tuple[str, str]] = [
    (FRONT, "Story"),
    (WORLD, "World"),
    (OPENING, "Opening"),
    (CHARACTER, "Characters"),
    (PLACE, "Places"),
    (LORE, "Lore"),
    (FACT, "Facts"),
    (TIMELINE, "Timeline"),
    (OPTIONAL, "Optional events"),
]
_LISTS = (CHARACTER, PLACE, LORE, FACT, TIMELINE, OPTIONAL)
CHECK_DELAY_MS = 250


class PlotEditorWindow(QMainWindow):
    # The saved file the author wants a story started from.
    start_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.Window)
        self.setMinimumSize(960, 640)
        self.document = PlotDocument()
        self.path: Path | None = None
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
            CHARACTER: CharacterPage(),
            PLACE: EntryPage(is_place=True),
            LORE: EntryPage(is_place=False),
            FACT: FactPage(),
            EVENT: WayPage(is_event=True),
            "variant": WayPage(is_event=False),
        }
        self.empty_page = QLabel("Choose something on the left, or add one.")
        self.empty_page.setAlignment(Qt.AlignCenter)
        self.pages.addWidget(self.empty_page)
        for page in self._pages.values():
            page.changed.connect(self._on_page_changed)
            page.renamed.connect(self._on_renamed)
            self.pages.addWidget(page)

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
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter)
        self.setCentralWidget(central)

        self._build_actions()
        self._rebuild_tree(select=(FRONT, 0, -1))
        self.check_now()
        self._refresh_title()

    # --- actions ---------------------------------------------------------------

    def _build_actions(self) -> None:
        menu = self.menuBar().addMenu("&File")
        for label, shortcut, handler in (
            ("&New", QKeySequence.New, self.new_file),
            ("&Open…", QKeySequence.Open, self.open_file),
            ("&Save", QKeySequence.Save, self.save),
            ("Save &as…", QKeySequence.SaveAs, self.save_as),
        ):
            action = QAction(label, self)
            action.setShortcut(shortcut)
            action.triggered.connect(handler)
            menu.addAction(action)
        menu.addSeparator()
        preview = QAction("&Preview file…", self)
        preview.setStatusTip("The Markdown this will be saved as")
        preview.triggered.connect(self.preview)
        menu.addAction(preview)
        start = QAction("Start a s&tory from this file", self)
        start.setStatusTip("Save, then import it as a new story")
        start.triggered.connect(self.start_story)
        menu.addAction(start)
        menu.addSeparator()
        close = QAction("&Close", self)
        close.setShortcut(QKeySequence.Close)
        close.triggered.connect(self.close)
        menu.addAction(close)

    def _refresh_title(self) -> None:
        name = self.path.name if self.path else "Untitled"
        self.setWindowTitle(f"{name}{'*' if self.modified else ''} — Plot file editor")

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
        return item.name or "(unnamed)"

    def _rebuild_tree(self, *, select: ItemRef | None) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        headers: dict[str, QTreeWidgetItem] = {}
        for section, label in _HEADERS:
            header = QTreeWidgetItem([label])
            header.setData(0, REF_ROLE, (HEADER, section, -1))
            font = QFont(header.font(0))
            font.setBold(True)
            header.setFont(0, font)
            self.tree.addTopLevelItem(header)
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
            if index in (FRONT, WORLD, OPENING):
                page = self._pages[index]
                page.load(self.document, self.document)
                self.pages.setCurrentWidget(page)
            else:
                self.pages.setCurrentWidget(self.empty_page)
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
        self.pages.setCurrentWidget(page)

    def _sync_buttons(self, ref: tuple | None) -> None:
        kind = ref[0] if ref else None
        section = ref[1] if kind == HEADER else kind
        in_list = section in _LISTS or section == EVENT
        self.add_button.setEnabled(in_list)
        self.variant_button.setEnabled(kind == EVENT)
        self.remove_button.setEnabled(kind != HEADER and kind is not None)
        self.up_button.setEnabled(kind != HEADER and kind is not None)
        self.down_button.setEnabled(kind != HEADER and kind is not None)

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

    def _add(self) -> None:
        ref = self.current_ref()
        if ref is None:
            return
        section = ref[1] if ref[0] == HEADER else ref[0]
        if section == EVENT:
            parent = self.tree.currentItem()
            while parent.parent() is not None:
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
        if not self.problems:
            self.summary.setText("The importer reads this file with nothing to say.")
        elif errors:
            self.summary.setText(
                f"{errors} error{'s stop' if errors != 1 else ' stops'} this file importing"
                + (f", and {notes} note{'s' if notes != 1 else ''}." if notes else ".")
            )
        else:
            self.summary.setText(f"Imports, with {notes} note{'s' if notes != 1 else ''}.")
        self._mark_problems()

    def _problems_for(self, ref: tuple) -> list[DocProblem]:
        """The problems a tree row stands for. The Story row takes the file's own."""
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
                item.setToolTip(0, "")
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
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "Save the changes to this plot file first?",
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

    def load_document(self, document: PlotDocument, *, path: Path | None) -> None:
        self.document = document
        self.path = path
        self._rebuild_tree(select=(HEADER, FRONT, -1))
        self.check_now()
        self._set_modified(False)

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
        if self.path is None:
            return self.save_as()
        return self._write(self.path)

    def save_as(self) -> bool:
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
        self.path = path
        self._set_modified(False)
        return True

    def preview(self) -> None:
        dialog = QDialog(self)
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

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()
