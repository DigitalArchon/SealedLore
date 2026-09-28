"""The lorebook, and what it injected last turn.

Retrieval is the one part of the prompt the author doesn't write directly, so
the panel's second job is transparency: which entries went in on the last turn,
why, and — when embeddings weren't used — that it was keywords alone.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.retrieval import RetrievalReport
from sealedlore.gui.fields import FormScroll, set_field_text
from sealedlore.gui.ref_images import RefImageStrip, StoryPictures
from sealedlore.models.lore import LoreEntry


class LorePanel(QWidget):
    changed = Signal()
    reembed_requested = Signal()

    def __init__(self, pictures: StoryPictures | None = None) -> None:
        super().__init__()
        self._pictures: StoryPictures = pictures or (lambda: None)
        self._entries: list[LoreEntry] = []
        self._hidden: set[str] = set()
        self._current: LoreEntry | None = None
        self._loading = False

        self.list = QListWidget()
        self.list.setObjectName("loreList")
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.currentItemChanged.connect(self._on_selection_changed)

        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add_entry)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove_entry)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(add_button)
        buttons.addWidget(self.remove_button)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)
        top_layout.addWidget(self.list, 1)
        top_layout.addLayout(buttons)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self._build_editor())
        splitter.addWidget(self._build_injection_view())
        splitter.setSizes([180, 320, 200])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter)

    def _build_editor(self) -> QWidget:
        self.title = QLineEdit()
        self.content = QPlainTextEdit()
        self.content.setPlaceholderText("The text injected into the prompt, verbatim.")
        self.keywords = QLineEdit()
        self.keywords.setPlaceholderText("comma-separated; matched as whole words")
        self.always_on = QCheckBox("Always include")
        self.enabled = QCheckBox("Enabled")
        self.priority = QSpinBox()
        self.priority.setRange(-100, 100)
        self.priority.setToolTip("Higher priority survives the lore token cap first")
        self.pictures = RefImageStrip(self._pictures, "it")
        self.pictures.changed.connect(lambda: self._current is not None and self.changed.emit())

        self.title.textChanged.connect(self._apply_edits)
        self.content.textChanged.connect(self._apply_edits)
        self.keywords.textChanged.connect(self._apply_edits)
        self.always_on.toggled.connect(self._apply_edits)
        self.enabled.toggled.connect(self._apply_edits)
        self.priority.valueChanged.connect(self._apply_edits)

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Title", self.title)
        form.addRow("Content", self.content)
        form.addRow("Keywords", self.keywords)
        form.addRow("Priority", self.priority)
        form.addRow(self.always_on)
        form.addRow(self.enabled)
        # Full width, under its label, as on the Cast page.
        form.addRow(QLabel("Pictures"))
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

        caption = QLabel("INJECTED LAST TURN")
        caption.setObjectName("composerCaption")

        self.status = QLabel("Nothing retrieved yet.")
        self.status.setObjectName("hintLabel")
        self.status.setWordWrap(True)

        self.injected = QListWidget()
        self.injected.setObjectName("injectionList")
        self.injected.setSelectionMode(QAbstractItemView.NoSelection)

        self.reembed_button = QPushButton("Re-embed lorebook")
        self.reembed_button.setToolTip(
            "Rebuild every vector. Vectors are keyed by the entry's text and the model, so an "
            "edited entry or a changed model re-embeds by itself; this is for an endpoint that "
            "changed what it serves under the same model id"
        )
        self.reembed_button.clicked.connect(self.reembed_requested)

        layout.addWidget(caption)
        layout.addWidget(self.status)
        layout.addWidget(self.injected, 1)
        layout.addWidget(self.reembed_button)
        return host

    # --- population -------------------------------------------------------

    def set_entries(self, entries: list[LoreEntry], *, hidden: set[str] | None = None) -> None:
        """`hidden`: ids the plot hasn't revealed, left off unless the author asked."""
        self._entries = entries
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
            self.list.addItem(item)
            if entry.id == selected:
                self.list.setCurrentItem(item)
        self._loading = False
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._on_selection_changed(self.list.currentItem(), None)

    def _label(self, entry: LoreEntry) -> str:
        marks = []
        if entry.always_on:
            marks.append("always on")
        if not entry.enabled:
            marks.append("disabled")
        suffix = f"  ({', '.join(marks)})" if marks else ""
        return f"{entry.title or '(untitled)'}{suffix}"

    def show_retrieval(self, report: RetrievalReport | None) -> None:
        """§7: which entries were injected on the last turn, and why."""
        self.injected.clear()
        if report is None:
            self.status.setText("Nothing retrieved yet.")
            return
        if report.mode == "whole":
            for entry in report.standing:
                self.injected.addItem(f"{entry.title} — sent every turn")
            self.status.setText(
                f"All {len(report.standing)} entries are sent every turn with the world, "
                "cached. A lorebook this size costs less whole than selected from."
            )
            return

        for entry in report.standing:
            self.injected.addItem(f"{entry.title} — always on, sent with the world")
        for injection in report.injected:
            self.injected.addItem(f"{injection.entry.title} — {injection.describe()}")
        for injection in report.dropped:
            self.injected.addItem(f"{injection.entry.title} — dropped, over the lore token cap")
        # Similarity scores cluster, so the threshold can only be tuned by
        # seeing what sat just under it.
        for entry, score in report.near_misses:
            item = QListWidgetItem(f"{entry.title} — below threshold ({score:.2f})")
            item.setForeground(Qt.gray)
            self.injected.addItem(item)

        count = f"{len(report.injected)} entries, ~{report.tokens:,} tokens"
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
        self.reembed_button.setEnabled(available)

    # --- editing ----------------------------------------------------------

    def _on_selection_changed(self, current: QListWidgetItem | None, _previous=None) -> None:
        if self._loading:
            return
        entry_id = current.data(Qt.UserRole) if current else None
        self._current = next((e for e in self._entries if e.id == entry_id), None)
        self._load_editor()

    def _load_editor(self) -> None:
        entry = self._current
        enabled = entry is not None
        self._form_host.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)
        self.empty_hint.setVisible(not enabled)
        self.pictures.set_refs(entry.reference_images if entry is not None else None)
        if entry is None:
            return

        self._loading = True
        set_field_text(self.title, entry.title)
        self.content.setPlainText(entry.content)
        set_field_text(self.keywords, ", ".join(entry.keywords))
        self.priority.setValue(entry.priority)
        self.always_on.setChecked(entry.always_on)
        self.enabled.setChecked(entry.enabled)
        self._loading = False

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
        self._entries.remove(self._current)
        self._current = None
        self.set_entries(self._entries)
        self.changed.emit()

    def _select(self, entry_id: str) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(Qt.UserRole) == entry_id:
                self.list.setCurrentItem(item)
                return
