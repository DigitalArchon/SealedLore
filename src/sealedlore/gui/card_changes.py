"""What changed between a story's character and the card of the author's own
they came from (engine/card_link.py), side by side, and what to do with it:
the story's changes saved to the card, made a new version of it, or kept in
the story only; the card's changes since taken into the story.

The author (Nov 4 2026): "it should show what has changed from default and
allow the player to save those changes (new pictures, changed info) to the
main card, or create a new version of the card with the new changes, or just
keep them only in that story."
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.card_link import BOTH, CARD, PORTRAIT, STORY, Change, picture_words
from sealedlore.gui import theme
from sealedlore.gui.sizing import GrowingText, size_to_screen
from sealedlore.models.collection import PictureMark

SAVE, VERSION, KEEP, TAKE = "save", "version", "keep", "take"
THUMB = 96
NONE = "(none)"

# A picture shown by its mark ("story" or "card" side): the window reads it.
Thumbnail = Callable[[str, str], QPixmap | None]


def marked(text: str, other: str) -> list[tuple[str, bool]]:
    """`text` in pieces, each said to be changed or not against `other`, by
    whole words (and the spaces between them): a long description changed in
    one sentence showed whole on both sides with nothing to say where."""
    mine, theirs = re.findall(r"\S+|\s+", text), re.findall(r"\S+|\s+", other)
    pieces: list[tuple[str, bool]] = []
    matcher = difflib.SequenceMatcher(None, mine, theirs, autojunk=False)
    for kind, start, end, _o_start, _o_end in matcher.get_opcodes():
        if start == end:
            continue
        piece = "".join(mine[start:end])
        changed = kind != "equal" and bool(piece.strip())
        if pieces and pieces[-1][1] == changed:
            pieces[-1] = (pieces[-1][0] + piece, changed)
        else:
            pieces.append((piece, changed))
    return pieces


def _mark(box: QPlainTextEdit, pieces: list[tuple[str, bool]]) -> None:
    """The changed pieces of `box`'s text shown on the theme's accent."""
    shade = QTextCharFormat()
    shade.setBackground(theme.colour("accent", 70))
    cursor = QTextCursor(box.document())
    at = 0
    for piece, changed in pieces:
        if changed:
            cursor.setPosition(at)
            cursor.setPosition(at + len(piece), QTextCursor.KeepAnchor)
            cursor.mergeCharFormat(shade)
        at += len(piece)


def _words(value: Any) -> str:
    """A field's value, as text to read."""
    if isinstance(value, dict):  # competence
        tiers = ", ".join(f"{skill} ({tier})" for skill, tier in (value.get("tiers") or {}).items())
        return "\n".join(part for part in (value.get("notes") or "", tiers) if part) or NONE
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or NONE
    return str(value or "") or NONE


class _Side(QWidget):
    """One side of a change: a heading, then its text or picture."""

    def __init__(self, heading: str, change: Change, value: Any, side: str, thumb: Thumbnail):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        title = QLabel(heading)
        title.setObjectName("hintLabel")
        layout.addWidget(title)
        if change.field:
            text = GrowingText(lines=1, most=6)
            text.setReadOnly(True)
            mine = _words(value)
            other = _words(change.card if side == STORY else change.story)
            text.setPlainText(mine)
            if mine != NONE and other != NONE:
                # The words the other side doesn't have, marked over the plain
                # text (as HTML the box measured a line too many).
                _mark(text, marked(mine, other))
            layout.addWidget(text)
            return
        sha = value if change.key == PORTRAIT else (value.sha256 if value else "")
        if not sha:
            layout.addWidget(QLabel("Not there"))
            return
        picture = QLabel()
        picture.setFixedSize(THUMB, int(THUMB * 1.5))
        picture.setAlignment(Qt.AlignCenter)
        pixmap = thumb(sha, side)
        if pixmap is not None and not pixmap.isNull():
            picture.setPixmap(pixmap)
        layout.addWidget(picture)
        if isinstance(value, PictureMark):
            words = QLabel(picture_words(value))
            words.setWordWrap(True)
            layout.addWidget(words)


class CardChangesDialog(QDialog):
    """`choice` is SAVE, VERSION, KEEP or TAKE once a button is pressed;
    `chosen()` the changes ticked for it."""

    def __init__(
        self,
        name: str,
        card_words: str,
        found: Sequence[Change],
        thumb: Thumbnail,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{name}: this story and their card")
        size_to_screen(self, 0.5, 0.8, at_least=QSize(760, 640))
        self.choice = ""
        self._found = list(found)
        self._ticks: dict[str, list[tuple[QCheckBox, Change]]] = {STORY: [], CARD: []}
        lead = QLabel(
            f"{name} came into this story from {card_words} in Your characters. A change can go "
            "either way: ticked ones go when you press a button below it."
        )
        lead.setWordWrap(True)
        mine = [c for c in self._found if c.side in (STORY, BOTH)]
        theirs = [c for c in self._found if c.side in (CARD, BOTH)]
        content = QWidget()
        rows = QVBoxLayout(content)
        rows.setContentsMargins(0, 0, 8, 0)
        rows.setSpacing(10)
        if mine:
            rows.addWidget(self._heading("Changed in this story"))
            for change in mine:
                rows.addWidget(self._row(change, STORY, thumb))
            self.save_button = QPushButton("Save the ticked to the card")
            self.save_button.setObjectName("sendButton")
            self.save_button.setToolTip(
                "Onto their card in Your characters: every story they join from now on gets them"
            )
            self.save_button.clicked.connect(lambda: self._done(SAVE))
            self.version_button = QPushButton("Save as a new version…")
            self.version_button.setToolTip(
                "The character as this story has them now becomes a new version of their card, "
                "kept beside it; this story's character is that version from now on"
            )
            self.version_button.clicked.connect(lambda: self._done(VERSION))
            self.keep_button = QPushButton("Keep them in this story only")
            self.keep_button.setToolTip(
                "Their card stays as it is. These changes are said quietly from now on, until "
                "there are others"
            )
            self.keep_button.clicked.connect(lambda: self._done(KEEP))
            rows.addLayout(self._buttons(self.save_button, self.version_button, self.keep_button))
        if theirs:
            rows.addWidget(self._heading("Changed on their card since they joined"))
            for change in theirs:
                rows.addWidget(self._row(change, CARD, thumb))
            self.take_button = QPushButton("Take the ticked into this story")
            self.take_button.setToolTip(
                "The card's changes into this story's character (a change to someone in the "
                "cast is one cache miss next turn)"
            )
            self.take_button.clicked.connect(lambda: self._done(TAKE))
            rows.addLayout(self._buttons(self.take_button))
        if not self._found:
            rows.addWidget(QLabel("Nothing has changed on either side."))
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(lead)
        layout.addWidget(scroll, 1)
        layout.addLayout(bottom)
        self._sync()

    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("peopleSection")
        return label

    @staticmethod
    def _buttons(*buttons: QPushButton) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        for button in buttons:
            row.addWidget(button)
        row.addStretch(1)
        return row

    def _row(self, change: Change, section: str, thumb: Thumbnail) -> QWidget:
        box = QFrame()
        box.setObjectName("changeRow")
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        tick = QCheckBox(change.label)
        # Both sides changed: the story's goes to the card ticked, the card's
        # comes in only when asked.
        tick.setChecked(section == STORY or change.side == CARD)
        if change.side == BOTH:
            tick.setToolTip("Changed on both sides: this side's replaces the other's")
            tick.setText(f"{change.label} (changed on both sides)")
        tick.toggled.connect(lambda _on: self._sync())
        self._ticks[section].append((tick, change))
        grid.addWidget(tick, 0, 0, 1, 2)
        story_side = _Side("In this story", change, change.story, STORY, thumb)
        card_side = _Side("On their card", change, change.card, CARD, thumb)
        grid.addWidget(story_side, 1, 0, Qt.AlignTop)
        grid.addWidget(card_side, 1, 1, Qt.AlignTop)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        return box

    def _sync(self) -> None:
        for section, button_name in ((STORY, "save_button"), (CARD, "take_button")):
            button = getattr(self, button_name, None)
            if button is not None:
                button.setEnabled(any(tick.isChecked() for tick, _c in self._ticks[section]))

    def _done(self, choice: str) -> None:
        self.choice = choice
        self.accept()

    def chosen(self) -> list[Change]:
        section = CARD if self.choice == TAKE else STORY
        return [change for tick, change in self._ticks[section] if tick.isChecked()]
