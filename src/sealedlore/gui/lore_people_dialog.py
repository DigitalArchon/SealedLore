"""What Find the people… found in a lorebook, for the author to say where
each goes (engine/lore_people.py).

In a story (the Lore page, a story just made from a lorebook): each person a
cast card, with their whole profile sent every turn, a supporting card, sent
when the story names them, or left in the lore as they are; and kept in Your
characters too, if ticked. The entries of instructions for the storyteller
are listed, each to move into the story's style notes if ticked (the
author's choice, Nov 20 2026: pointed out, never moved unasked). For a kept
lorebook (Lorebooks), only who is kept in Your characters: the lorebook
itself stays as it is.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.lore_people import FoundPerson, FoundRules, LorePeople, Placement
from sealedlore.gui.sizing import enter_only_on_buttons, size_to_screen

# What each choice says, by placement, in the order offered.
PLACES: tuple[tuple[Placement, str], ...] = (
    ("cast", "Cast: whole card, every turn"),
    ("supporting", "Supporting: sent when named"),
    ("lore", "Leave in the lore"),
)
DROP = ("drop", "Has a card: take the entry out")
NAME, ENTRY, WORDS, GOES, KEEP = range(5)
RULES_LEAD = (
    "These entries are instructions for the storyteller (how to write, rules for the game) "
    "rather than lore. As lore they are sent like any other entry; ticked, they move into "
    "the story's style notes, sent every turn with SealedLore's own rules (those stand "
    "either way)."
)
RULES_LEAD_KEPT = (
    "These entries are instructions for the storyteller rather than lore. In a story made "
    "from this lorebook, Find the people… on its Lore page can move them into the story's "
    "style notes."
)


def _words(count: int) -> str:
    return f"{count:,}"


class LorePeopleDialog(QDialog):
    """`story`: a story's lore (placements and rules); else a kept lorebook's
    (Your characters only). `carded`: the casefolded names of the story's
    cards, whose entries are offered to take out with no card made."""

    def __init__(
        self,
        found: LorePeople,
        *,
        source: str,
        story: bool,
        carded: Sequence[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._found = found
        self._story = story
        self.setWindowTitle(f"The people in {source}")
        size_to_screen(self, 0.6, 0.75, at_least=QSize(860, 600))
        carded_names = {name.casefold() for name in carded}

        people = len(found.people)
        if story:
            lead = (
                f"Found {people} {'person' if people == 1 else 'people'} in {source}. Choose "
                "who joins the cast (their whole profile goes to the storyteller every turn) "
                "and who is a supporting character (sent when the story names them); their "
                "entries come out of the lore, as the card has their words now. Tick "
                "“Your characters” to keep them there too, for other stories."
            )
        else:
            lead = (
                f"Found {people} {'person' if people == 1 else 'people'} in {source}. Tick "
                "those to keep in Your characters, each with their profile from the lorebook "
                "as their description. The lorebook stays as it is."
            )
        heading = QLabel(lead)
        heading.setWordWrap(True)

        self.table = QTableWidget(people, 5 if story else 4)
        labels = ["Name", "From the entry", "Words"]
        labels += ["Goes to", "Your characters"] if story else ["Keep in Your characters"]
        self.table.setHorizontalHeaderLabels(labels)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._places: list[QComboBox] = []
        self._keeps: list[QCheckBox] = []
        for row, person in enumerate(found.people):
            name = QTableWidgetItem(person.name)
            name.setToolTip(person.summary or person.name)
            self.table.setItem(row, NAME, name)
            self.table.setItem(row, ENTRY, QTableWidgetItem(person.entry_title))
            words = QTableWidgetItem(_words(len(person.text.split())))
            words.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, WORDS, words)
            keep = QCheckBox()
            keep.setToolTip("Also keep them in Your characters, for any story")
            if story:
                place = QComboBox()
                for value, words_for in PLACES:
                    place.addItem(words_for, value)
                if person.name.casefold() in carded_names:
                    place.addItem(DROP[1], DROP[0])
                    place.setCurrentIndex(place.count() - 1)
                else:
                    place.setCurrentIndex(1)
                self._places.append(place)
                self.table.setCellWidget(row, GOES, place)
                self.table.setCellWidget(row, KEEP, _centred(keep))
            else:
                keep.setChecked(True)
                self.table.setCellWidget(row, GOES, _centred(keep))
            self._keeps.append(keep)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(lambda row, *_rest: self._preview(row))

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlaceholderText("Choose someone to read their part of the lorebook.")

        bulk = QHBoxLayout()
        bulk.setContentsMargins(0, 0, 0, 0)
        if story:
            for value, words_for in (("supporting", "All supporting"), ("lore", "None")):
                button = QPushButton(words_for)
                button.setToolTip(
                    "Every one a supporting character"
                    if value == "supporting"
                    else "Leave everyone in the lore"
                )
                button.clicked.connect(lambda _on=False, value=value: self._place_all(value))
                bulk.addWidget(button)
        for on, words_for in ((True, "Keep all"), (False, "Keep none")):
            button = QPushButton(words_for)
            button.setToolTip("Tick everyone for Your characters" if on else "Untick everyone")
            button.clicked.connect(lambda _on=False, on=on: self._keep_all(on))
            bulk.addWidget(button)
        bulk.addStretch(1)

        upper = QWidget()
        upper_layout = QVBoxLayout(upper)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.addWidget(self.table, 1)
        upper_layout.addLayout(bulk)
        split = QSplitter(Qt.Vertical)
        split.addWidget(upper)
        split.addWidget(self.text)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(split, 1)

        self._rules: list[QCheckBox] = []
        if found.instructions:
            rules_head = QLabel(RULES_LEAD if story else RULES_LEAD_KEPT)
            rules_head.setWordWrap(True)
            layout.addWidget(rules_head)
            for rule in found.instructions:
                words = f"{rule.title} ({_words(rule.words)} words"
                words += ", always sent)" if rule.always_on else ")"
                if story:
                    box = QCheckBox(f"Move into the style notes: {words}")
                    self._rules.append(box)
                    layout.addWidget(box)
                else:
                    line = QLabel(f"• {words}")
                    layout.addWidget(line)
        if found.warnings:
            notes = QLabel("Not found: " + "; ".join(found.warnings) + ".")
            notes.setObjectName("hintLabel")
            notes.setWordWrap(True)
            layout.addWidget(notes)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Make the cards" if story else "Keep them")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if people:
            self.table.setCurrentCell(0, NAME)
            self._preview(0)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)

    def reject(self) -> None:
        """Cancel asks first: finding them was paid for."""
        if self._found.people or self._found.instructions:
            answer = QMessageBox.question(
                self,
                "Discard what was found?",
                "Close without making any cards? Finding them again means asking the model again.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        super().reject()

    def _preview(self, row: int) -> None:
        if not 0 <= row < len(self._found.people):
            self.text.clear()
            return
        person = self._found.people[row]
        head = f"{person.name}" + (f" ({', '.join(person.aliases)})" if person.aliases else "")
        summary = f"\n{person.summary}" if person.summary else ""
        self.text.setPlainText(f"{head}{summary}\n\n{person.text}")

    def _place_all(self, value: str) -> None:
        for place in self._places:
            index = place.findData(value)
            # Someone with a card here keeps "take the entry out" unless "None".
            if place.findData("drop") >= 0 and value != "lore":
                continue
            place.setCurrentIndex(index)

    def _keep_all(self, on: bool) -> None:
        for keep in self._keeps:
            keep.setChecked(on)

    # --- what was chosen --------------------------------------------------------------------

    def placements(self) -> list[tuple[FoundPerson, Placement]]:
        if not self._story:
            return []
        return [
            (person, place.currentData())
            for person, place in zip(self._found.people, self._places, strict=True)
        ]

    def kept(self) -> list[FoundPerson]:
        """Those ticked for Your characters."""
        return [
            person
            for person, keep in zip(self._found.people, self._keeps, strict=True)
            if keep.isChecked()
        ]

    def rules(self) -> list[FoundRules]:
        return [
            rule
            for rule, box in zip(self._found.instructions, self._rules, strict=True)
            if box.isChecked()
        ]


def _centred(widget: QWidget) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setAlignment(Qt.AlignCenter)
    layout.addWidget(widget)
    return holder
