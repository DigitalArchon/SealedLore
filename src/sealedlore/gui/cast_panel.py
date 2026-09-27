"""Cast management.

Deliberately lightweight: the author never enters numeric stats. Competence is
freeform prose, plus optional per-skill tiers (§4.1) — the author's ruling on
a character's ceiling, which dice mode turns into odds. Nothing here is
required; with no tiers the prose carries it.

Two tiers share one list. The cast is the author's: on the roster, holdable,
in the cached system block. Supporting characters are people the story
introduced, carded for continuity and shown to the model only when mentioned.
The model suggests them; nothing joins either tier without the author's say.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.dice import TIER_ORDER
from sealedlore.gui import theme
from sealedlore.gui.fields import FormScroll, set_field_text
from sealedlore.gui.ref_images import RefImageStrip, StoryFolder
from sealedlore.models.character import Character
from sealedlore.models.supporting import CharacterSuggestion

HEADER_ROLE = Qt.UserRole + 1
MODEL_MARK = "  ✦"
# Past this many skills the table scrolls rather than pushing the form down.
SKILL_ROWS_VISIBLE = 8


class CastPanel(QWidget):
    changed = Signal()
    # (character id, the name before): the rosters hold supporting cards by name.
    renamed = Signal(str, str)
    promote_requested = Signal(str)
    demote_requested = Signal(str)
    accept_requested = Signal(str, bool)
    dismiss_requested = Signal(str)
    scan_requested = Signal()
    infer_requested = Signal(str)

    def __init__(self, folder: StoryFolder | None = None) -> None:
        super().__init__()
        self._folder: StoryFolder = folder or (lambda: None)
        self._cast: list[Character] = []
        self._hidden: set[str] = set()
        self._supporting: list[Character] = []
        self._suggestions: list[CharacterSuggestion] = []
        self._current: Character | None = None
        self._loading = False

        self.list = QListWidget()
        self.list.setObjectName("castList")
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.currentItemChanged.connect(self._on_selection_changed)

        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add_character)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove_character)
        self.move_button = QPushButton("Move to supporting")
        self.move_button.clicked.connect(self._move_character)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(add_button)
        buttons.addWidget(self.remove_button)
        buttons.addWidget(self.move_button)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)
        top_layout.addWidget(self.list, 1)
        top_layout.addLayout(buttons)
        top_layout.addWidget(self._build_suggestions())

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self._build_editor())
        # The list is a handful of names; the sheet under it is the long part.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 560])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter)

    def _build_editor(self) -> QWidget:
        self.name = QLineEdit()
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText("comma-separated")
        self.summary = QPlainTextEdit()
        self.summary.setMaximumHeight(60)
        self.summary.setPlaceholderText("One or two lines. Always included in the prompt.")
        self.full_description = QPlainTextEdit()
        self.full_description.setMaximumHeight(110)
        self.full_description.setPlaceholderText("Longer sheet. Included when the budget allows.")
        self.voice_notes = QPlainTextEdit()
        self.voice_notes.setMaximumHeight(60)
        self.voice_notes.setPlaceholderText("Speech patterns, verbal tics, register.")
        self.competence = QPlainTextEdit()
        self.competence.setMaximumHeight(60)
        self.competence.setPlaceholderText(
            "Freeform: 'a veteran duellist, illiterate, hopeless at deception'"
        )
        self.player_available = QCheckBox("Available to play")
        self.player_available.setToolTip(
            "On: you can hold this character, and the storyteller voices them on the turns "
            "you don't.\nOff: they are the storyteller's — it writes their speech, thoughts "
            "and choices in full, and they no longer appear in the Speaking as list."
        )
        self.author_only = QCheckBox("Never voiced by the storyteller")
        self.author_only.setToolTip(
            "Yours on every turn, not only while you hold them. The storyteller is told "
            "never to write their speech, thoughts or decisions."
        )

        self.skills = QTableWidget(0, 2)
        self.skills.setHorizontalHeaderLabels(["Skill", "Tier"])
        self.skills.verticalHeader().setVisible(False)
        self.skills.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.skills.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        # Rows as tall as the tier combos in them, and a table as tall as its
        # rows (see _fit_skills_table): left to the form, it was squeezed to
        # one row and every skill after the first needed the scroll wheel.
        self.skills.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.skills.itemChanged.connect(self._apply_edits)
        add_skill = QPushButton("Add")
        add_skill.clicked.connect(self._add_skill)
        self.remove_skill = QPushButton("Remove")
        self.remove_skill.clicked.connect(self._remove_skill)
        self.suggest_skills = QPushButton("Suggest skills")
        self.suggest_skills.setToolTip(
            "Ask the model to draft skills and tiers from this sheet; you accept or discard them"
        )
        self.suggest_skills.clicked.connect(
            lambda: self._current and self.infer_requested.emit(self._current.id)
        )
        skill_buttons = QHBoxLayout()
        skill_buttons.setContentsMargins(0, 0, 0, 0)
        skill_buttons.addWidget(add_skill)
        skill_buttons.addWidget(self.remove_skill)
        skill_buttons.addStretch(1)
        skills_box = QWidget()
        skills_layout = QVBoxLayout(skills_box)
        skills_layout.setContentsMargins(0, 0, 0, 0)
        skills_layout.addWidget(self.skills)
        skills_layout.addLayout(skill_buttons)
        # Its own line: three buttons abreast made this the form's widest row,
        # and the inspector had to widen (squeezing the Stories dock) to fit it.
        skills_layout.addWidget(self.suggest_skills, 0, Qt.AlignLeft)
        self.canon = QLineEdit()
        self.canon.setPlaceholderText("e.g. 'Night of the Living Dead (1968)' — blank if original")
        self.pictures = RefImageStrip(self._folder, "them")
        self.pictures.changed.connect(self._on_pictures_changed)
        self.origin_note = QLabel("Card drafted by the model from the story. Check it.")
        self.origin_note.setObjectName("hintLabel")
        self.origin_note.setWordWrap(True)

        for widget in (
            self.summary,
            self.full_description,
            self.voice_notes,
            self.competence,
        ):
            widget.textChanged.connect(self._apply_edits)
        self.name.textChanged.connect(self._apply_edits)
        self.aliases.textChanged.connect(self._apply_edits)
        self.canon.textChanged.connect(self._apply_edits)
        self.player_available.toggled.connect(self._apply_edits)
        self.player_available.toggled.connect(self._sync_voicing)
        self.author_only.toggled.connect(self._apply_edits)

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Name", self.name)
        form.addRow("Aliases", self.aliases)
        form.addRow("From", self.canon)
        form.addRow(self.origin_note)
        form.addRow("Summary", self.summary)
        form.addRow("Description", self.full_description)
        form.addRow("Voice", self.voice_notes)
        form.addRow("Competence", self.competence)
        form.addRow("Skills", skills_box)
        # Full width, under its label: beside it, the strip's three buttons
        # plus the label column were the form's widest row.
        form.addRow(QLabel("Pictures"))
        form.addRow(self.pictures)
        form.addRow(self.player_available)
        form.addRow(self.author_only)

        self.empty_hint = QLabel("Add a character, or select one to edit.")
        self.empty_hint.setObjectName("hintLabel")
        self.empty_hint.setWordWrap(True)
        form.addRow(self.empty_hint)

        scroll = FormScroll(form_host)
        self._form_host = form_host
        return scroll

    def _build_suggestions(self) -> QWidget:
        self.suggestion_list = QListWidget()
        self.suggestion_list.setObjectName("suggestionList")
        self.suggestion_list.setMaximumHeight(110)
        self.suggestion_list.currentItemChanged.connect(self._sync_suggestion_buttons)

        self.accept_button = QPushButton("Add")
        self.accept_button.setToolTip(
            "Keep as a supporting character: shown to the model when mentioned"
        )
        self.accept_button.clicked.connect(lambda: self._accept(False))
        self.accept_cast_button = QPushButton("Add to cast")
        self.accept_cast_button.setToolTip(
            "Put on the roster and in every prompt; costs one cache miss"
        )
        self.accept_cast_button.clicked.connect(lambda: self._accept(True))
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.setToolTip("Don't suggest this name again")
        self.dismiss_button.clicked.connect(self._dismiss)
        self.scan_button = QPushButton("Scan recent story")
        self.scan_button.setToolTip("Ask the model who in the recent prose isn't on file yet")
        self.scan_button.clicked.connect(self.scan_requested)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.accept_button)
        row.addWidget(self.accept_cast_button)
        row.addWidget(self.dismiss_button)

        self.suggestion_hint = QLabel(
            "New characters are suggested when turns are archived, or scan now."
        )
        self.suggestion_hint.setObjectName("hintLabel")
        self.suggestion_hint.setWordWrap(True)

        caption = QLabel("SUGGESTED FROM THE STORY")
        caption.setObjectName("composerCaption")
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(caption)
        header.addStretch(1)
        header.addWidget(self.scan_button)

        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.suggestion_list)
        layout.addWidget(self.suggestion_hint)
        layout.addLayout(row)
        return box

    # --- population -------------------------------------------------------

    def set_cast(
        self,
        cast: list[Character],
        supporting: list[Character] | None = None,
        suggestions: list[CharacterSuggestion] | None = None,
        *,
        hidden: set[str] | None = None,
    ) -> None:
        """`hidden`: ids the plot hasn't brought in yet, left off the list unless
        the author has chosen to see the plot (they're still edited in place)."""
        self._cast = cast
        if hidden is not None:
            self._hidden = hidden
        if supporting is not None:
            self._supporting = supporting
        if suggestions is not None:
            self._suggestions = suggestions
        selected = self._current.id if self._current else None
        self._loading = True
        self.list.clear()
        sections = [("CAST", self._cast)]
        if self._supporting:
            sections.append(("SUPPORTING — FROM THE STORY", self._supporting))
        for title, characters in sections:
            self._add_header(title)
            for character in characters:
                if character.id in self._hidden:
                    continue
                label = character.name or "(unnamed)"
                if character.origin == "model":
                    label += MODEL_MARK
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, character.id)
                if character.canon:
                    item.setToolTip(character.canon)
                self.list.addItem(item)
                if character.id == selected:
                    self.list.setCurrentItem(item)
        self._loading = False
        self._show_suggestions()
        if self.list.currentItem() is None and self._cast:
            self.list.setCurrentRow(1)
        else:
            self._on_selection_changed(self.list.currentItem(), None)

    def _add_header(self, title: str) -> None:
        item = QListWidgetItem(title)
        item.setData(HEADER_ROLE, True)
        item.setFlags(Qt.NoItemFlags)
        # QSS can't select list items by role, so the header is styled here.
        font = QFont(self.list.font())
        # The stylesheet sets sizes in pixels, which leaves pointSizeF() at -1.
        if font.pixelSize() > 0:
            font.setPixelSize(max(9, round(font.pixelSize() * 0.8)))
        elif font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * 0.8)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 1)
        item.setFont(font)
        item.setForeground(theme.colour("text_placeholder"))
        self.list.addItem(item)

    def _show_suggestions(self) -> None:
        self.suggestion_list.clear()
        for suggestion in self._suggestions:
            label = suggestion.name + (f" — {suggestion.canon}" if suggestion.canon else "")
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, suggestion.id)
            item.setToolTip(suggestion.description or "No description.")
            self.suggestion_list.addItem(item)
        has_any = bool(self._suggestions)
        self.suggestion_list.setVisible(has_any)
        self.suggestion_hint.setVisible(not has_any)
        if has_any:
            self.suggestion_list.setCurrentRow(0)
        self._sync_suggestion_buttons()

    def _sync_suggestion_buttons(self, *_args) -> None:
        chosen = self.suggestion_list.currentItem() is not None and bool(self._suggestions)
        for button in (self.accept_button, self.accept_cast_button, self.dismiss_button):
            button.setVisible(bool(self._suggestions))
            button.setEnabled(chosen)

    def _chosen_suggestion(self) -> str | None:
        item = self.suggestion_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _accept(self, to_cast: bool) -> None:
        suggestion_id = self._chosen_suggestion()
        if suggestion_id:
            self.accept_requested.emit(suggestion_id, to_cast)

    def _dismiss(self) -> None:
        suggestion_id = self._chosen_suggestion()
        if suggestion_id:
            self.dismiss_requested.emit(suggestion_id)

    def _sync_voicing(self) -> None:
        """ "Never voiced" only means something for a character you can play.

        With "Available to play" off the character is the storyteller's, which
        is the opposite setting; leaving the box live would offer a state where
        nobody writes them.
        """
        playable = self.player_available.isChecked()
        self.author_only.setEnabled(playable)
        if not playable and self.author_only.isChecked():
            self.author_only.setChecked(False)

    def _is_supporting(self, character: Character | None) -> bool:
        return character is not None and any(c is character for c in self._supporting)

    def _on_selection_changed(self, current: QListWidgetItem | None, _previous=None) -> None:
        if self._loading:
            return
        character_id = current.data(Qt.UserRole) if current else None
        self._current = next(
            (c for c in (*self._cast, *self._supporting) if c.id == character_id), None
        )
        self._load_editor()

    def _load_editor(self) -> None:
        character = self._current
        enabled = character is not None
        self._form_host.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)
        self.move_button.setEnabled(enabled)
        self.move_button.setText(
            "Move to cast" if self._is_supporting(character) else "Move to supporting"
        )
        self.empty_hint.setVisible(not enabled)
        self.origin_note.setVisible(bool(character and character.origin == "model"))
        self.pictures.set_refs(character.reference_images if character is not None else None)
        if character is None:
            return

        self._loading = True
        set_field_text(self.name, character.name)
        set_field_text(self.aliases, ", ".join(character.aliases))
        self.summary.setPlainText(character.summary or "")
        self.full_description.setPlainText(character.full_description or "")
        self.voice_notes.setPlainText(character.voice_notes or "")
        self.competence.setPlainText(character.competence.notes)
        self.player_available.setChecked(character.is_player_available)
        self.author_only.setChecked(character.author_only)
        self._sync_voicing()
        set_field_text(self.canon, character.canon or "")
        self._show_skills(character)
        self._loading = False

    # --- editing ----------------------------------------------------------

    def _apply_edits(self) -> None:
        if self._loading or self._current is None:
            return
        character = self._current
        old_name = character.name
        character.name = self.name.text().strip()
        if character.name and character.name != old_name:
            self.renamed.emit(character.id, old_name)
        character.aliases = [
            alias.strip() for alias in self.aliases.text().split(",") if alias.strip()
        ]
        character.summary = self.summary.toPlainText().strip() or None
        character.full_description = self.full_description.toPlainText().strip() or None
        character.voice_notes = self.voice_notes.toPlainText().strip() or None
        character.competence.notes = self.competence.toPlainText().strip()
        character.is_player_available = self.player_available.isChecked()
        character.author_only = self.author_only.isChecked()
        character.canon = self.canon.text().strip() or None
        character.competence.tiers = self._skills_from_table()

        item = self.list.currentItem()
        if item is not None:
            mark = MODEL_MARK if character.origin == "model" else ""
            item.setText((character.name or "(unnamed)") + mark)
        self.changed.emit()

    def _on_pictures_changed(self) -> None:
        if self._current is not None:
            self.changed.emit()

    def _add_character(self) -> None:
        character = Character(name="New character")
        self._cast.append(character)
        self._current = character
        self.set_cast(self._cast)
        self._select(character.id)
        self.name.setFocus()
        self.name.selectAll()
        self.changed.emit()

    def _remove_character(self) -> None:
        if self._current is None:
            return
        tier = self._supporting if self._is_supporting(self._current) else self._cast
        tier.remove(self._current)
        self._current = None
        self.set_cast(self._cast)
        self.changed.emit()

    # --- skills (§4.1) ----------------------------------------------------

    def _tier_combo(self, tier: str) -> QComboBox:
        combo = QComboBox()
        for value in TIER_ORDER:
            combo.addItem(value, value)
        combo.setCurrentIndex(max(combo.findData(tier), 0))
        combo.currentIndexChanged.connect(self._apply_edits)
        return combo

    def _show_skills(self, character: Character) -> None:
        self.skills.blockSignals(True)
        self.skills.setRowCount(0)
        for domain, tier in character.competence.tiers.items():
            self._append_skill_row(domain, tier)
        self.skills.blockSignals(False)
        self._fit_skills_table()

    def _append_skill_row(self, domain: str, tier: str) -> None:
        row = self.skills.rowCount()
        self.skills.insertRow(row)
        self.skills.setItem(row, 0, QTableWidgetItem(domain))
        self.skills.setCellWidget(row, 1, self._tier_combo(tier))
        self._fit_skills_table()

    def _fit_skills_table(self) -> None:
        """Show every skill without scrolling, up to SKILL_ROWS_VISIBLE of them."""
        table = self.skills
        table.resizeRowsToContents()
        rows = [table.rowHeight(row) for row in range(table.rowCount())]
        empty_row = table.verticalHeader().defaultSectionSize()
        body = sum(rows[:SKILL_ROWS_VISIBLE]) if rows else empty_row
        header = table.horizontalHeader().sizeHint().height()
        table.setFixedHeight(header + body + 2 * table.frameWidth())

    def _skills_from_table(self) -> dict:
        tiers = {}
        for row in range(self.skills.rowCount()):
            item = self.skills.item(row, 0)
            combo = self.skills.cellWidget(row, 1)
            domain = item.text().strip().lower() if item else ""
            if domain and isinstance(combo, QComboBox):
                tiers[domain] = combo.currentData()
        return tiers

    def _add_skill(self) -> None:
        if self._current is None:
            return
        self.skills.blockSignals(True)
        self._append_skill_row("", "competent")
        self.skills.blockSignals(False)
        row = self.skills.rowCount() - 1
        self.skills.setCurrentCell(row, 0)
        self.skills.editItem(self.skills.item(row, 0))

    def _remove_skill(self) -> None:
        row = self.skills.currentRow()
        if row < 0:
            return
        self.skills.removeRow(row)
        self._fit_skills_table()
        self._apply_edits()

    def show_suggested_skills(self, character_id: str, tiers: dict) -> None:
        """Put accepted suggestions in the table, where they can still be edited."""
        if self._current is None or self._current.id != character_id:
            self._select(character_id)
        if self._current is None:
            return
        self._current.competence.tiers = dict(tiers)
        self._loading = True
        self._show_skills(self._current)
        self._loading = False
        self.changed.emit()

    def _move_character(self) -> None:
        if self._current is None:
            return
        if self._is_supporting(self._current):
            self.promote_requested.emit(self._current.id)
        else:
            self.demote_requested.emit(self._current.id)

    def _select(self, character_id: str) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(Qt.UserRole) == character_id:
                self.list.setCurrentItem(item)
                return

    def select_character(self, character_id: str) -> None:
        self._select(character_id)
