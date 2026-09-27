"""File → Prompts (advanced)…: every text SealedLore sends to a model, editable.

The author asked (Sept 2026) to see and change every prompt the program uses,
for one story or for every story, with a warning and an easy way back. The
texts and their defaults are in engine/prompt_texts.py; which edit is in
force is engine/prompt_edits.py. This dialog edits copies of both layers and
hands them back only on Save.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.prompt_edits import (
    copied,
    default_changed,
    default_sha,
    missing_slots,
    reset,
    set_text,
    texts_in_force,
    unknown_keys,
)
from sealedlore.engine.prompt_texts import GROUPS, TEXTS
from sealedlore.models.config import Config
from sealedlore.models.prompt_edit import PromptEdit
from sealedlore.models.story import Story
from sealedlore.storage.repository import stories_with_prompt_edits

WARNING = (
    "These are the instructions SealedLore sends to its models: the storyteller's rules, "
    "what it is told each turn, and every read, summary and plan made around it. Changing "
    "them can give unpredictable results and can break a story: the storyteller may start "
    "writing your character, and the scene read, the plot or the chapters may stop working "
    "if a reply no longer has the shape the program reads. Reset puts any text back."
)
# Marks in the list, beside a text's title.
EDITED = "●"
INHERITED = "○"
CHANGED = "⚠"
KEY_ROLE = Qt.UserRole


class PromptEditorDialog(QDialog):
    def __init__(
        self,
        config: Config,
        story: Story | None,
        *,
        root: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Prompts (advanced)")
        self.resize(1100, 720)
        self.story = story
        self.root = root
        # The working copies; the config and the story change only on Save.
        self.edits: dict[str, dict[str, PromptEdit]] = {
            "story": {k: e.model_copy() for k, e in (story.prompt_edits if story else {}).items()},
            "app": {k: e.model_copy() for k, e in config.prompt_edits.items()},
        }
        self._key: str | None = None
        self._loading = False

        warning = QLabel(WARNING)
        warning.setObjectName("warningLabel")
        warning.setWordWrap(True)

        self.scope = QComboBox()
        if story is not None:
            self.scope.addItem(f"This story: {story.title}", "story")
        self.scope.addItem("Every story", "app")
        self.scope_note = QLabel()
        self.scope_note.setObjectName("hintLabel")
        self.scope_note.setWordWrap(True)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Edit for"))
        scope_row.addWidget(self.scope)
        scope_row.addWidget(self.scope_note, 1)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a text: its name, what it does, or its words")
        self.search.setClearButtonEnabled(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.tree, 1)
        legend = QLabel(
            f"{EDITED} edited here   {INHERITED} edited for every story   "
            f"{CHANGED} the default changed since"
        )
        legend.setObjectName("hintLabel")
        left_layout.addWidget(legend)

        self.title = QLabel()
        self.title.setStyleSheet("font-weight: bold;")
        self.about = QLabel()
        self.about.setWordWrap(True)
        self.slots = QLabel()
        self.slots.setObjectName("hintLabel")
        self.slots.setWordWrap(True)
        self.slots.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status = QLabel()
        self.status.setObjectName("warningLabel")
        self.status.setWordWrap(True)
        self.editor = QPlainTextEdit()
        self.editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.default_view = QPlainTextEdit()
        self.default_view.setReadOnly(True)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.editor, "Text")
        self.tabs.addTab(self.default_view, "Default")
        self.reset_button = QPushButton("Reset to default")
        self.reset_button.setToolTip("Remove this edit, here only.")
        reset_row = QHBoxLayout()
        reset_row.addStretch(1)
        reset_row.addWidget(self.reset_button)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.title, self.about, self.slots, self.status):
            right_layout.addWidget(widget)
        right_layout.addWidget(self.tabs, 1)
        right_layout.addLayout(reset_row)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 760])

        self.counts = QLabel()
        self.counts.setObjectName("hintLabel")
        self.counts.setWordWrap(True)
        self.copy_button = QPushButton("Copy from another story…")
        self.reset_all_button = QPushButton("Reset all…")
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.copy_button)
        bottom.addWidget(self.reset_all_button)
        bottom.addWidget(self.counts, 1)
        bottom.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(warning)
        layout.addLayout(scope_row)
        layout.addWidget(splitter, 1)
        layout.addLayout(bottom)

        self._build_tree()
        self.scope.currentIndexChanged.connect(lambda _index: self._scope_changed())
        self.search.textChanged.connect(lambda _text: self._filter())
        self.tree.currentItemChanged.connect(lambda item, _previous: self._show(item))
        self.editor.textChanged.connect(self._edited)
        self.reset_button.clicked.connect(self._reset_one)
        self.reset_all_button.clicked.connect(self._reset_all)
        self.copy_button.clicked.connect(self._copy_from_story)
        self._scope_changed()
        first = self.tree.topLevelItem(0)
        if first is not None and first.childCount():
            self.tree.setCurrentItem(first.child(0))

    # --- the layers ------------------------------------------------------------

    @property
    def scope_name(self) -> str:
        return self.scope.currentData()

    @property
    def here(self) -> dict[str, PromptEdit]:
        return self.edits[self.scope_name]

    def _in_force(self, key: str) -> str:
        """The text this scope sends: for one story, its own edit, then the
        edit for every story, then the default."""
        story = self.edits["story"] if self.scope_name == "story" else {}
        return texts_in_force(self.edits["app"], story)[key]

    # --- the list --------------------------------------------------------------

    def _build_tree(self) -> None:
        groups: dict[str, QTreeWidgetItem] = {}
        for group in GROUPS:
            item = QTreeWidgetItem([group])
            item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
            groups[group] = item
            self.tree.addTopLevelItem(item)
        for key, text in TEXTS.items():
            child = QTreeWidgetItem([text.title])
            child.setData(0, KEY_ROLE, key)
            child.setToolTip(0, f"{key}\n\n{text.about}")
            groups[text.group].addChild(child)
        # By title, so the roster's lines, the REMEMBER line's parts and the
        # dice sit together.
        for item in groups.values():
            item.sortChildren(0, Qt.AscendingOrder)
        self.tree.expandAll()

    def _items(self) -> list[QTreeWidgetItem]:
        found = []
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            found += [group.child(i) for i in range(group.childCount())]
        return found

    def _mark(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, KEY_ROLE)
        marks = ""
        edit = self.here.get(key)
        if edit is not None:
            marks = EDITED + (f" {CHANGED}" if default_changed(key, edit) else "")
        elif self.scope_name == "story" and key in self.edits["app"]:
            marks = INHERITED
        item.setText(0, f"{TEXTS[key].title}  {marks}".rstrip())

    def _refresh_marks(self) -> None:
        for item in self._items():
            self._mark(item)
        story_count, app_count = len(self.edits["story"]), len(self.edits["app"])
        parts = []
        if self.story is not None:
            parts.append(f"{story_count} edited for this story")
        parts.append(f"{app_count} for every story")
        unknown = unknown_keys(self.here)
        if unknown:
            parts.append(
                f"{len(unknown)} kept for texts this version doesn't have: {', '.join(unknown)}"
            )
        self.counts.setText("; ".join(parts) + ".")
        self.reset_all_button.setEnabled(bool(self.here))

    def _filter(self) -> None:
        words = self.search.text().strip().lower()
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            shown = 0
            for i in range(group.childCount()):
                child = group.child(i)
                key = child.data(0, KEY_ROLE)
                text = TEXTS[key]
                haystack = " ".join((key, text.title, text.about, self._in_force(key))).lower()
                visible = not words or all(word in haystack for word in words.split())
                child.setHidden(not visible)
                shown += visible
            group.setHidden(shown == 0)

    # --- one text --------------------------------------------------------------

    def _scope_changed(self) -> None:
        story = self.scope_name == "story"
        self.copy_button.setVisible(story)
        self.scope_note.setText(
            "Only this story. Its edits win over those for every story."
            if story
            else "Every story, unless a story has its own edit of the same text."
        )
        self._refresh_marks()
        self._show(self.tree.currentItem())

    def _show(self, item: QTreeWidgetItem | None) -> None:
        key = item.data(0, KEY_ROLE) if item is not None else None
        self._key = key
        self.tabs.setEnabled(key is not None)
        self.reset_button.setEnabled(key is not None and key in self.here)
        if key is None:
            for label in (self.title, self.about, self.slots, self.status):
                label.clear()
            return
        text = TEXTS[key]
        self.title.setText(text.title)
        self.about.setText(text.about)
        self.slots.setText(
            "Filled in by SealedLore: "
            + "; ".join(f"{{{name}}} — {meaning}" for name, meaning in text.placeholders)
            if text.placeholders
            else ""
        )
        self.slots.setVisible(bool(text.placeholders))
        self.default_view.setPlainText(text.default)
        self._loading = True
        self.editor.setPlainText(self._in_force(key))
        self._loading = False
        self._update_status()

    def _update_status(self) -> None:
        key = self._key
        if key is None:
            return
        notes = []
        edit = self.here.get(key)
        if edit is None and self.scope_name == "story" and key in self.edits["app"]:
            notes.append(
                "This is your edit for every story. Changing it here changes it for this "
                "story only."
            )
        if edit is not None and default_changed(key, edit):
            notes.append(
                "SealedLore's default for this text has changed since you edited it: compare them "
                "on the Default tab. Reset takes the new default."
            )
        missing = missing_slots(key, self.editor.toPlainText())
        if missing:
            notes.append(
                "This text leaves out "
                + ", ".join(f"{{{slot}}}" for slot in missing)
                + ": what SealedLore puts there won't reach the model."
            )
        self.status.setText(" ".join(notes))
        self.status.setVisible(bool(notes))
        self.reset_button.setEnabled(key in self.here)

    def _edited(self) -> None:
        if self._loading or self._key is None:
            return
        key, text = self._key, self.editor.toPlainText()
        inherited = self.edits["app"].get(key) if self.scope_name == "story" else None
        if inherited is not None and text == inherited.text:
            # The same as the edit for every story: nothing of its own.
            reset(self.here, [key])
        elif inherited is not None and text == TEXTS[key].default:
            # The default, for this story only: it must stand as an edit, or
            # the edit for every story would come back in its place.
            self.here[key] = PromptEdit(text=text, default_sha=default_sha(key))
        else:
            set_text(self.here, key, text)
        item = self.tree.currentItem()
        if item is not None:
            self._mark(item)
        self._refresh_marks()
        self._update_status()

    def _reset_one(self) -> None:
        if self._key is None:
            return
        reset(self.here, [self._key])
        self._refresh_marks()
        self._show(self.tree.currentItem())

    def _reset_all(self) -> None:
        where = "this story" if self.scope_name == "story" else "every story"
        answer = QMessageBox.question(
            self,
            "Reset all",
            f"Put back the default of all {len(self.here)} texts edited for {where}?",
        )
        if answer != QMessageBox.Yes:
            return
        reset(self.here)
        self._refresh_marks()
        self._show(self.tree.currentItem())

    def _copy_from_story(self) -> None:
        others = stories_with_prompt_edits(
            self.root, excluding=self.story.id if self.story else None
        )
        if not others:
            QMessageBox.information(
                self, "Copy from another story", "No other story has edited prompts."
            )
            return
        labels = [f"{title} ({len(edits)} edited)" for _id, title, edits in others]
        choice, ok = QInputDialog.getItem(
            self, "Copy from another story", "Take the edits of:", labels, 0, False
        )
        if not ok:
            return
        _id, title, edits = others[labels.index(choice)]
        self.edits["story"].update(copied(edits))
        self._refresh_marks()
        self._show(self.tree.currentItem())
        self.scope_note.setText(
            f"Copied {len(edits)} edits from {title}: they replace this story's own edits of "
            "the same texts. Save to keep them."
        )

    # --- the result ------------------------------------------------------------

    def apply_to(self, config: Config, story: Story | None) -> None:
        config.prompt_edits = self.edits["app"]
        if story is not None:
            story.prompt_edits = self.edits["story"]
