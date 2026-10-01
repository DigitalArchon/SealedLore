"""The story-so-far panel: chapter summaries, editable. See §5.2, §5.3, §10.

Summaries stand in for prose the model will never see again, so the author has
to be able to read them, fix them, and see when they no longer match the story.
Nothing here rebuilds or overwrites on its own: the window confirms first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.archival import chapter_number
from sealedlore.engine.recall import RecallReport
from sealedlore.models.summary import Summary


def chapter_title(
    position: int,
    summary: Summary,
    *,
    on_path: bool,
    in_part: bool = False,
    rebuilding: bool = False,
) -> str:
    """`position` is the chapter number (a part's first); `in_part`, a chapter a
    part on the path stands in for."""
    badges = []
    if rebuilding:
        badges.append("rebuilding…")
    elif summary.stale:
        badges.append("stale")
    if summary.hand_edited:
        badges.append("your words")
    if summary.keep_as_written:
        badges.append("kept as written")
    if in_part:
        badges.append("merged into a part")
    elif not on_path:
        badges.append("other branch")
    last = position + summary.chapters - 1
    number = f"Chapters {position}–{last}" if last > position else f"Chapter {position}"
    label = f"{number} · {len(summary.covered_node_ids)} messages"
    return f"{label}  ({', '.join(badges)})" if badges else label


class SummariesPanel(QWidget):
    archive_requested = Signal()
    rebuild_requested = Signal(str)
    keep_toggled = Signal(str, bool)
    edit_committed = Signal(str, str)
    # The first message of a chapter on this path, to scroll the transcript to
    # (double-click). The left dock's own chapter list did only this, twice over.
    chapter_chosen = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._summaries: list[Summary] = []
        self._on_path: set[str] = set()
        self._in_parts: set[str] = set()
        self._rebuilding: set[str] = set()
        self._loading = False

        caption = QLabel(
            "Archived history. These chapters replace the original prose in every later "
            "prompt: what is written here is what the model sees of them (older chapters "
            "may be merged into one part, and the story ledger, if on, rides alongside)."
        )
        caption.setObjectName("hintLabel")
        caption.setWordWrap(True)

        self.chapters = QListWidget()
        self.chapters.setMaximumHeight(140)
        self.chapters.currentRowChanged.connect(self._on_selection_changed)
        self.chapters.itemDoubleClicked.connect(self._go_to_chapter)
        self.chapters.setToolTip("Double-click a chapter to go to it in the story")

        self.detail = QLabel()
        self.detail.setObjectName("hintLabel")
        self.detail.setWordWrap(True)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Nothing archived yet.")
        self.editor.textChanged.connect(self._on_text_changed)

        self.save_button = QPushButton("Save edits")
        self.save_button.setToolTip(
            "Marks the chapter as your words; it won't be rebuilt without asking"
        )
        self.save_button.clicked.connect(self._commit)
        self.revert_button = QPushButton("Revert")
        self.revert_button.clicked.connect(self._reload_editor)
        self.rebuild_button = QPushButton("Rebuild")
        self.rebuild_button.setToolTip(
            "Summarise the same messages again, in the background: you can keep playing"
        )
        self.rebuild_button.clicked.connect(self._rebuild)
        self.keep_box = QCheckBox("Keep as written")
        self.keep_box.setToolTip(
            "Never merge this chapter into a part: it is sent as it is however long the "
            "story grows. Older chapters are merged around it."
        )
        self.keep_box.toggled.connect(self._on_keep_toggled)
        self.archive_button = QPushButton("Archive now")
        self.archive_button.clicked.connect(self.archive_requested)

        # Two rows, editing and then the chapters themselves: in one row the
        # four buttons ("Archive 10 turns" once filled) made this the widest
        # page, and the inspector grew the moment it was shown.
        edit_row = QHBoxLayout()
        edit_row.setContentsMargins(0, 0, 0, 0)
        edit_row.setSpacing(6)
        edit_row.addWidget(self.save_button)
        edit_row.addWidget(self.revert_button)
        edit_row.addStretch(1)
        chapter_row = QHBoxLayout()
        chapter_row.setContentsMargins(0, 0, 0, 0)
        chapter_row.setSpacing(6)
        chapter_row.addWidget(self.rebuild_button)
        chapter_row.addWidget(self.keep_box)
        chapter_row.addStretch(1)
        chapter_row.addWidget(self.archive_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        # What recall brought back for the last turn, with scores and near
        # misses: without them the threshold can't be tuned (as for lore).
        self.recall_note = QLabel()
        self.recall_note.setObjectName("hintLabel")
        self.recall_note.setWordWrap(True)
        self.recall_note.setTextFormat(Qt.PlainText)
        self.recall_note.hide()
        # A simple chat's kept messages, when they take much of the budget.
        self.kept_warning = QLabel()
        self.kept_warning.setObjectName("hintLabel")
        self.kept_warning.setWordWrap(True)
        self.kept_warning.hide()
        layout.addWidget(caption)
        layout.addWidget(self.kept_warning)
        layout.addWidget(self.chapters)
        layout.addWidget(self.detail)
        layout.addWidget(self.editor, 1)
        layout.addLayout(edit_row)
        layout.addLayout(chapter_row)
        layout.addWidget(self.recall_note)

        self._update_buttons()

    # --- population -------------------------------------------------------

    def set_recall(self, report: RecallReport | None) -> None:
        """What the last turn or Question recalled (Settings → Context → Recall
        earlier detail), what a recent Question carried into the turn, and what
        nearly was; hidden after a turn that recalls nothing by setting."""
        if report is None or not (report.items or report.near_misses or report.reason):
            self.recall_note.hide()
            return

        def named(items) -> str:
            return ", ".join(item.label.split(",")[0] for item in items)

        carried = [item for item in report.items if item.carried]
        found = [item for item in report.items if not item.carried]
        bits = []
        if found:
            bits.append(f"Recalled for your last message: {named(found)}.")
        if carried:
            bits.append(f"Kept from a recent Question: {named(carried)}.")
        if not report.items:
            bits.append("Nothing recalled for your last message.")
        if report.near_misses:
            bits.append(f"Next in line: {named(report.near_misses)}.")
        if report.reason:
            bits.append(f"({report.reason[0].upper()}{report.reason[1:]}.)")
        self.recall_note.setText(" ".join(bits))
        self.recall_note.show()

    def set_kept_warning(self, text: str | None) -> None:
        self.kept_warning.setText(text or "")
        self.kept_warning.setVisible(bool(text))

    def set_summaries(
        self,
        summaries: Sequence[Summary],
        *,
        on_path_ids: set[str],
        archivable_turns: int,
        numbers: Mapping[str, int] | None = None,
        rebuilding: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        """Repopulate, keeping the selected chapter where it still exists.

        `numbers` are the chapter numbers along the active path (as the
        navigator and the transcript count them); a chapter off the path
        falls back to its place among all chapters."""
        self._numbers = dict(numbers or {})
        selected = self.selected_id()
        self._summaries = list(summaries)
        self._on_path = set(on_path_ids)
        self._rebuilding = set(rebuilding)

        self._loading = True
        self.chapters.clear()
        in_parts = {
            chapter_id
            for summary in self._summaries
            if summary.id in self._on_path
            for chapter_id in summary.merged_from
        }
        self._in_parts = in_parts
        for summary in self._summaries:
            item = QListWidgetItem(
                chapter_title(
                    self._numbers.get(summary.id) or chapter_number(self._summaries, summary),
                    summary,
                    on_path=summary.id in self._on_path,
                    in_part=summary.id in in_parts,
                    rebuilding=summary.id in self._rebuilding,
                )
            )
            item.setData(Qt.UserRole, summary.id)
            self.chapters.addItem(item)
        row = next(
            (index for index, summary in enumerate(self._summaries) if summary.id == selected),
            0 if self._summaries else -1,
        )
        self.chapters.setCurrentRow(row)
        self._loading = False

        self.archive_button.setEnabled(archivable_turns > 0)
        self.archive_button.setText(
            f"Archive {archivable_turns} turns" if archivable_turns else "Nothing to archive"
        )
        self._reload_editor()

    def _go_to_chapter(self, _item: object) -> None:
        summary = self.selected()
        # A chapter off this branch (or merged into a part) has no place in
        # the transcript as it stands.
        if summary is not None and summary.id in self._on_path and summary.covered_node_ids:
            self.chapter_chosen.emit(summary.covered_node_ids[0])

    def selected_id(self) -> str | None:
        item = self.chapters.currentItem()
        return str(item.data(Qt.UserRole)) if item is not None else None

    def selected(self) -> Summary | None:
        summary_id = self.selected_id()
        return next((s for s in self._summaries if s.id == summary_id), None)

    def select(self, summary_id: str) -> None:
        for row in range(self.chapters.count()):
            if self.chapters.item(row).data(Qt.UserRole) == summary_id:
                self.chapters.setCurrentRow(row)
                return

    # --- editing ----------------------------------------------------------

    def _on_selection_changed(self) -> None:
        if not self._loading:
            self._reload_editor()

    def _reload_editor(self) -> None:
        summary = self.selected()
        self._loading = True
        self.editor.setPlainText(summary.content if summary else "")
        self._loading = False
        self.detail.setText(self._detail_text(summary))
        self._update_buttons()

    def _detail_text(self, summary: Summary | None) -> str:
        if summary is None:
            return "Chapters appear here once the oldest turns are archived."
        bits = [f"Written by {summary.model}" if summary.model else "Writer not recorded"]
        if summary.hand_edited:
            bits.append("edited by you")
        if summary.id in self._rebuilding:
            bits.append("being rebuilt in the background; this text is sent until it's in")
        elif summary.stale:
            bits.append("stale: a message it covers has been edited")
        if summary.id in self._in_parts:
            bits.append(
                "merged into a part: the part is sent instead, and editing or rebuilding "
                "this chapter takes the part apart until the next merge"
            )
        if summary.id not in self._on_path:
            bits.append("not part of the branch you are on")
        return " · ".join(bits)

    def _on_text_changed(self) -> None:
        if not self._loading:
            self._update_buttons()

    def _is_dirty(self) -> bool:
        summary = self.selected()
        return summary is not None and self.editor.toPlainText() != summary.content

    def _update_buttons(self) -> None:
        summary = self.selected()
        rebuilding = summary is not None and summary.id in self._rebuilding
        self.editor.setEnabled(summary is not None)
        self.save_button.setEnabled(self._is_dirty())
        self.revert_button.setEnabled(self._is_dirty())
        self.rebuild_button.setEnabled(summary is not None and not self._rebuilding)
        # A part is merged already; keeping applies to a chapter.
        self.keep_box.setEnabled(summary is not None and not summary.merged_from and not rebuilding)
        self.keep_box.blockSignals(True)
        self.keep_box.setChecked(bool(summary and summary.keep_as_written))
        self.keep_box.blockSignals(False)

    def _on_keep_toggled(self, checked: bool) -> None:
        summary = self.selected()
        if summary is not None and not self._loading:
            self.keep_toggled.emit(summary.id, checked)

    def _commit(self) -> None:
        summary = self.selected()
        if summary is not None:
            self.edit_committed.emit(summary.id, self.editor.toPlainText())

    def _rebuild(self) -> None:
        summary = self.selected()
        if summary is not None:
            self.rebuild_requested.emit(summary.id)
