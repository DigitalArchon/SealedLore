"""Scene state and the presence roster.

The scene keeps itself now: after each passage a small model reads what
changed and the card follows (engine/scene_update.py). This panel shows the
card, says what the latest passage changed with an Undo, and lets the author
correct anything — an author's edit always wins, and the next read starts
from it. With the read switched off (Settings), it is the hand-kept roster it
used to be, and says when it has gone stale.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.scene_state import describe_changes, same_person
from sealedlore.gui import theme
from sealedlore.gui.fields import FormScroll, line_edit, set_field_text
from sealedlore.models.character import Character
from sealedlore.models.scene import OffstageCharacter, SceneLogEntry, SceneState

# "Who can know": the scene's privacy, as the storyteller is told it. (The
# Private button is a different thing: a separate model for a span of the story.)
_PRIVACY_CHOICES = (
    ("Not recorded", None),
    ("Everyone, in time", "public"),
    ("Only those present", "private"),
)


def _split_names(text: str) -> list[str]:
    names: list[str] = []
    for part in text.split(","):
        name = " ".join(part.split())
        if name and name.lower() not in {existing.lower() for existing in names}:
            names.append(name)
    return names


class ScenePanel(QWidget):
    changed = Signal()
    update_requested = Signal()
    undo_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._scene: SceneState | None = None
        self._cast: list[Character] = []
        self._loading = False

        self.location = QLineEdit()
        self.time_of_day = QLineEdit()
        self.situation = QPlainTextEdit()
        self.situation.setMaximumHeight(72)
        self.situation.setPlaceholderText("One or two lines of immediate framing.")
        self.privacy = QComboBox()
        for label, value in _PRIVACY_CHOICES:
            self.privacy.addItem(label, value)
        self.privacy.setToolTip(
            "Private: what happens here stays with the people in it until one of them "
            "passes it on. Public: people around see it, and word travels."
        )

        self.location.textChanged.connect(self._apply_edits)
        self.time_of_day.textChanged.connect(self._apply_edits)
        self.situation.textChanged.connect(self._apply_edits)
        self.privacy.currentIndexChanged.connect(self._apply_edits)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Location", self.location)
        form.addRow("Time", self.time_of_day)
        self._form = form
        form.addRow("Situation", self.situation)
        form.addRow("Who can know", self.privacy)

        # What the latest passage changed, and the way to take it back.
        self.changes = QLabel()
        self.changes.setObjectName("hintLabel")
        self.changes.setWordWrap(True)
        self.changes.setTextFormat(Qt.PlainText)
        self.undo_button = QPushButton("Undo")
        self.undo_button.setToolTip("Put the scene back as it was before this passage")
        self.undo_button.clicked.connect(self.undo_requested)
        changes_row = QHBoxLayout()
        changes_row.setContentsMargins(0, 0, 0, 0)
        changes_row.addWidget(self.changes, 1)
        changes_row.addWidget(self.undo_button, 0, Qt.AlignTop)
        self.changes_strip = QWidget()
        self.changes_strip.setLayout(changes_row)
        self.changes_strip.hide()

        # The cast, then the supporting characters. Playtesting: in the long
        # test story the cast is two people and the station's regulars are all
        # supporting cards, so the roster showed two rows and everyone else was
        # a comma list, and a wrong scene went unnoticed.
        self.roster = QTableWidget(0, 3)
        self.roster.setHorizontalHeaderLabels(["Here", "Character", "If elsewhere, where?"])
        self.roster.verticalHeader().setVisible(False)
        self.roster.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.roster.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.roster.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.roster.setSelectionMode(QTableWidget.NoSelection)

        self.others = QLineEdit()
        self.others.setPlaceholderText("Anyone else here, by name: a guard, Gus")
        self.others.setToolTip(
            "People in the scene with no card: the story's own people. Comma-separated. "
            "Cast and supporting characters are ticked in the list above."
        )
        self.others.textEdited.connect(self._others_edited)
        others_form = QFormLayout()
        others_form.setContentsMargins(0, 0, 0, 0)
        others_form.addRow("Also here", self.others)

        caption = QLabel(
            "Ticked characters are here: they may perceive, speak and act. Anyone else "
            "has to arrive, shown coming in. Correct anything that's wrong; the next "
            "read starts from your version."
        )
        caption.setObjectName("hintLabel")
        caption.setWordWrap(True)

        self.stale = QLabel()
        self.stale.setObjectName("driftLabel")
        self.stale.setWordWrap(True)
        self.stale.hide()
        self.update_button = QPushButton("Update from the story")
        self.update_button.setToolTip(
            "Read the recent passages afresh and propose where the story is now: location, "
            "time, what is going on, and who is in the scene. You approve it."
        )
        self.update_button.clicked.connect(self.update_requested)
        self.update_button.hide()

        self.recent = QLabel()
        self.recent.setObjectName("hintLabel")
        self.recent.setWordWrap(True)
        self.recent.setTextFormat(Qt.PlainText)
        self.recent.hide()

        # For a glance: what the last passage changed, then who is here (every
        # row showing; the page scrolls, not the list), then where and when,
        # then the explanations.
        self.roster.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self.changes_strip)
        layout.addWidget(self.roster)
        layout.addLayout(others_form)
        layout.addLayout(form)
        layout.addWidget(caption)
        layout.addWidget(self.stale)
        layout.addWidget(self.update_button)
        layout.addWidget(self.recent)
        layout.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(FormScroll(content))
        self._others_touched = False
        # One per character on the roster: who, whether a supporting card, and
        # the table row (the supporting characters have a heading row above).
        self._rows: list[tuple[Character, bool, int]] = []

    # --- population -------------------------------------------------------

    def set_scene(
        self,
        scene: SceneState,
        cast: list[Character],
        *,
        supporting: Sequence[Character] = (),
        held_id: str | None = None,
        log: Sequence[SceneLogEntry] = (),
        show_time: bool = True,
    ) -> None:
        """`show_time` is whether the Time line reaches the storyteller
        (`prompt.scene_time_shown`); where it doesn't, the field is hidden
        rather than left looking as though it did something."""
        self._scene = scene
        self._form.setRowVisible(self.time_of_day, show_time)
        self._cast = cast
        self._loading = True
        self._others_touched = False
        self._rows = [(c, False, row) for row, c in enumerate(cast)] + [
            (c, True, len(cast) + 1 + row) for row, c in enumerate(supporting)
        ]

        set_field_text(self.location, scene.location or "")
        set_field_text(self.time_of_day, scene.time_of_day or "")
        self.situation.setPlainText(scene.situation or "")
        self.privacy.setCurrentIndex(
            next(i for i, (_, value) in enumerate(_PRIVACY_CHOICES) if value == scene.privacy)
        )
        cards = [c for c, is_supporting, _row in self._rows if is_supporting]
        set_field_text(
            self.others,
            ", ".join(n for n in scene.present_others if not any(same_person(n, c) for c in cards)),
        )

        notes = {entry.character_id: entry.note for entry in scene.offstage_but_nearby}
        present = set(scene.present_character_ids)

        self.roster.clearSpans()
        self.roster.setRowCount(len(cast) + (len(supporting) + 1 if supporting else 0))
        if supporting:
            heading = QTableWidgetItem("Supporting characters")
            heading.setFlags(Qt.NoItemFlags)
            heading.setForeground(theme.colour("text_dim"))
            self.roster.setItem(len(cast), 0, heading)
            self.roster.setSpan(len(cast), 0, 1, 3)
        for character, is_supporting, row in self._rows:
            here = (
                any(same_person(n, character) for n in scene.present_others)
                if is_supporting
                else character.id in present
            )
            box = QCheckBox()
            box.setChecked(here)
            box.toggled.connect(
                self._supporting_toggled if is_supporting else lambda _on: self._apply_edits()
            )
            holder = QWidget()
            holder_layout = QVBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignCenter)
            holder_layout.addWidget(box)
            self.roster.setCellWidget(row, 0, holder)

            you = "  · you" if character.id == held_id else ""
            name = QTableWidgetItem((character.name or "(unnamed)") + you)
            name.setFlags(Qt.ItemIsEnabled)
            name.setData(Qt.UserRole, character.id)
            if is_supporting:
                name.setToolTip("A supporting character: ticked, they are named on the roster")
            self.roster.setItem(row, 1, name)

            note = line_edit("" if is_supporting else notes.get(character.id, ""))
            if is_supporting:
                # Nearby-but-elsewhere is kept for the cast only.
                note.setPlaceholderText("—")
                note.setToolTip("Kept for the cast only")
                note.setEnabled(False)
            else:
                note.setPlaceholderText("e.g. in the corridor, out of earshot")
                note.setEnabled(not here)
            note.textChanged.connect(self._apply_edits)
            self.roster.setCellWidget(row, 2, note)
        self._fit_roster()

        self._show_log(log)
        self._loading = False

    def _show_log(self, log: Sequence[SceneLogEntry]) -> None:
        if not log:
            self.recent.hide()
            return
        lines = ["Recent scenes (the model sees these, to know who can know what):"]
        for entry in log:
            where = entry.location or "Somewhere"
            if entry.privacy:
                where += f" ({entry.privacy})"
            who = ", ".join(entry.present_names) or "nobody recorded"
            lines.append(f"• {where}: {who}" + (f" — {entry.gist}" if entry.gist else ""))
        self.recent.setText("\n".join(lines))
        self.recent.show()

    # --- editing ----------------------------------------------------------

    def _fit_roster(self) -> None:
        """Tall enough for every row, so who is here reads at a glance."""
        self.roster.resizeRowsToContents()
        rows = sum(self.roster.rowHeight(r) for r in range(self.roster.rowCount()))
        header = self.roster.horizontalHeader().sizeHint().height()
        self.roster.setFixedHeight(header + rows + 2 * self.roster.frameWidth())

    def _row_widgets(self, row: int) -> tuple[QCheckBox, QLineEdit]:
        holder = self.roster.cellWidget(row, 0)
        return holder.findChild(QCheckBox), self.roster.cellWidget(row, 2)

    def _others_edited(self, _text: str) -> None:
        # Typing here is keeping track of everyone, not just the cast.
        self._others_touched = True
        self._apply_edits()

    def _supporting_toggled(self, _on: bool) -> None:
        # So is saying who of the supporting characters is here.
        self._others_touched = True
        self._apply_edits()

    def _present_others(self) -> list[str]:
        """Ticked supporting characters, under the names the scene already has
        for them (a read's "Sergeant Hale" stays), then the uncarded names."""
        assert self._scene is not None
        before = self._scene.present_others
        names: list[str] = []
        for character, is_supporting, row in self._rows:
            if not is_supporting or not self._row_widgets(row)[0].isChecked():
                continue
            written = [n for n in before if same_person(n, character)]
            names.extend(written or [character.name])
        for name in _split_names(self.others.text()):
            if name.lower() not in {n.lower() for n in names}:
                names.append(name)
        return names

    def _apply_edits(self) -> None:
        if self._loading or self._scene is None:
            return
        scene = self._scene
        scene.location = self.location.text().strip() or None
        scene.time_of_day = self.time_of_day.text().strip() or None
        scene.situation = self.situation.toPlainText().strip() or None
        scene.privacy = self.privacy.currentData()
        scene.present_others = self._present_others()
        if self._others_touched:
            scene.tracked = True

        present: list[str] = []
        offstage: list[OffstageCharacter] = []
        for character, is_supporting, row in self._rows:
            if is_supporting:
                continue
            box, note = self._row_widgets(row)
            if box.isChecked():
                present.append(character.id)
                note.setEnabled(False)
            else:
                note.setEnabled(True)
                text = note.text().strip()
                if text:
                    offstage.append(OffstageCharacter(character_id=character.id, note=text))

        scene.present_character_ids = present
        scene.offstage_but_nearby = offstage
        self.changed.emit()

    # --- what the story changed -------------------------------------------

    def show_changes(self, scene: SceneState, *, passage: int, can_undo: bool) -> None:
        """ "Changed by the story at passage 213: +Hollis (…), −Ruiz (…)" and Undo.

        Only while the scene at the leaf is the read's own: an edit by the
        author replaces it, and there is nothing of the story's left to undo.
        """
        if not can_undo:
            self.changes_strip.hide()
            return
        words = describe_changes(scene.changes)
        self.changes.setText(
            f"Changed by the story at passage {passage}: {words}"
            if words
            else f"Read at passage {passage}: no change to the scene."
        )
        self.undo_button.setVisible(bool(words))
        self.changes_strip.show()

    def show_age(self, passages: int, *, threshold: int = 20, kept: bool = False) -> None:
        """Say how long ago the scene was set, once that starts to matter.

        With the scene read on (`kept`), the card is read after every passage,
        so its age says nothing; the full re-read stays available all the same.
        """
        self.update_button.setVisible(passages > 0 or kept)
        if kept or passages < threshold:
            self.stale.hide()
            return
        self.stale.setText(
            f"The scene above was set {passages} passages ago. The story has moved on "
            "since; the model is told to go by the recent prose where they differ, but "
            "it reads better when this says where you actually are."
        )
        self.stale.show()
