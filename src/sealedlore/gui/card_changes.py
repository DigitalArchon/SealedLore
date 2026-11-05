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


# A changed stretch longer than this (in words and spaces, both sides
# together) is marked whole rather than word by word: a word diff takes time
# with the square of its length, and a card's 3,000-word description,
# diffed whole, held the window 1-2 seconds a side.
WORD_DIFF_MOST = 1200

Pieces = list[tuple[str, bool]]


def marked(text: str, other: str) -> Pieces:
    """`text` in pieces, each said to be changed or not against `other`, by
    whole words (and the spaces between them): a long description changed in
    one sentence showed whole on both sides with nothing to say where."""
    return marked_both(text, other)[0]


def marked_both(text: str, other: str) -> tuple[Pieces, Pieces]:
    """`marked` for both sides, from one diff."""
    mine, theirs = _tokens(text), _tokens(other)
    ops = _diff(mine, theirs)
    return _pieces(mine, [(kind, i1, i2) for kind, i1, i2, _j1, _j2 in ops]), _pieces(
        theirs, [(kind, j1, j2) for kind, _i1, _i2, j1, j2 in ops]
    )


def _tokens(text: str) -> list[str]:
    return re.findall(r"\S+|\s+", text)


def _sentences(tokens: list[str]) -> list[tuple[int, int]]:
    """The tokens' (start, end) by sentence or line: the space after one ends it."""
    spans: list[tuple[int, int]] = []
    start = 0
    for index, token in enumerate(tokens):
        if token.isspace() and ("\n" in token or (index and tokens[index - 1][-1] in '.!?…"”)')):
            spans.append((start, index + 1))
            start = index + 1
    if start < len(tokens):
        spans.append((start, len(tokens)))
    return spans


def _diff(mine: list[str], theirs: list[str]) -> list[tuple[str, int, int, int, int]]:
    """Opcodes over the tokens: sentences matched first (few of them, and
    seldom alike, so quick), then the words inside each stretch that differs."""
    ours, others = _sentences(mine), _sentences(theirs)

    def bounds(spans: list[tuple[int, int]], lo: int, hi: int, total: int) -> tuple[int, int]:
        start = spans[lo][0] if lo < len(spans) else total
        return start, spans[hi - 1][1] if hi > lo else start

    ops: list[tuple[str, int, int, int, int]] = []
    keys = ["".join(mine[a:b]) for a, b in ours], ["".join(theirs[a:b]) for a, b in others]
    for kind, a1, a2, b1, b2 in difflib.SequenceMatcher(None, *keys, autojunk=False).get_opcodes():
        i1, i2 = bounds(ours, a1, a2, len(mine))
        j1, j2 = bounds(others, b1, b2, len(theirs))
        if kind == "equal":
            ops.append((kind, i1, i2, j1, j2))
        else:
            ops += _words_diff(mine, theirs, i1, i2, j1, j2)
    return ops


def _words_diff(
    mine: list[str], theirs: list[str], i1: int, i2: int, j1: int, j2: int
) -> list[tuple[str, int, int, int, int]]:
    """A stretch that differs, word by word: what both ends share left out
    first (a sentence added to a long paragraph is all that differs in it),
    and what is left marked whole when it is long."""
    head = 0
    while i1 + head < i2 and j1 + head < j2 and mine[i1 + head] == theirs[j1 + head]:
        head += 1
    tail = 0
    while (
        i2 - tail > i1 + head
        and j2 - tail > j1 + head
        and mine[i2 - tail - 1] == theirs[j2 - tail - 1]
    ):
        tail += 1
    ops = [("equal", i1, i1 + head, j1, j1 + head)] if head else []
    a1, a2, b1, b2 = i1 + head, i2 - tail, j1 + head, j2 - tail
    if (a2 - a1) + (b2 - b1) > WORD_DIFF_MOST:
        ops.append(("replace", a1, a2, b1, b2))
    elif a1 < a2 or b1 < b2:
        matcher = difflib.SequenceMatcher(None, mine[a1:a2], theirs[b1:b2], autojunk=False)
        ops += [
            (kind, a1 + c1, a1 + c2, b1 + d1, b1 + d2)
            for kind, c1, c2, d1, d2 in matcher.get_opcodes()
        ]
    if tail:
        ops.append(("equal", i2 - tail, i2, j2 - tail, j2))
    return ops


def _pieces(tokens: list[str], spans: list[tuple[str, int, int]]) -> Pieces:
    pieces: Pieces = []
    for kind, start, end in spans:
        if start == end:
            continue
        piece = "".join(tokens[start:end])
        changed = kind != "equal" and bool(piece.strip())
        if pieces and pieces[-1][1] == changed:
            pieces[-1] = (pieces[-1][0] + piece, changed)
        else:
            pieces.append((piece, changed))
    return pieces


def _mark(box: QPlainTextEdit, pieces: Pieces) -> None:
    """`box`'s text written again a piece at a time, the changed pieces on
    the theme's accent. Written, not marked at offsets into the text: a
    document counts in UTF-16 (an emoji is two) and reads "\\r\\n" as one
    break, so after either every mark landed on the wrong words."""
    shade = QTextCharFormat()
    shade.setBackground(theme.colour("accent", 70))
    plain = QTextCharFormat()
    box.clear()
    cursor = QTextCursor(box.document())
    for piece, changed in pieces:
        cursor.insertText(piece, shade if changed else plain)


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

    def __init__(
        self,
        heading: str,
        change: Change,
        value: Any,
        side: str,
        thumb: Thumbnail,
        pieces: Pieces | None = None,
    ):
        """`pieces`: a field's words, marked where the other side differs."""
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
            text.setPlainText(_words(value))
            if pieces is not None:
                # The words the other side doesn't have, marked over the plain
                # text (as HTML the box measured a line too many).
                _mark(text, pieces)
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
        ours, theirs = _words(change.story), _words(change.card)
        # One diff for both sides (each side diffed it again, the other way).
        story_marks = card_marks = None
        if change.field and NONE not in (ours, theirs):
            story_marks, card_marks = marked_both(ours, theirs)
        story_side = _Side("In this story", change, change.story, STORY, thumb, story_marks)
        card_side = _Side("On their card", change, change.card, CARD, thumb, card_marks)
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
