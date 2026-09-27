"""The branch bar, above the transcript: which branch you are reading.

A branch is a line of the story split off by rewriting an earlier message
(⋯ → New branch from here…); a take is only another version of one passage and
never shows here (engine/branches.py). The bar always names the branch being
read, and its menu switches, renames and deletes. Playtesting: the left dock's
list of forks meant nothing to the author, most of them being takes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QMenu, QToolButton, QWidget

from sealedlore.engine.branches import Branch
from sealedlore.gui.wrap_row import WrapRow


def branch_line(branch: Branch) -> str:
    """ "Branch 2 · from message 44 · 60 messages", as the menu and map say it."""
    where = "the start" if branch.start_at <= 1 else f"message {branch.start_at}"
    count = f"{branch.length} message{'s' if branch.length != 1 else ''}"
    return f"{branch.name}  ·  from {where}  ·  {count}"


def opening_words(text: str, limit: int = 300) -> str:
    """A message's first words, whitespace folded, cut with "…"."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class BranchBar(QWidget):
    # A branch's first message id, or None for the first line.
    switch_requested = Signal(object)
    rename_requested = Signal(object)
    delete_requested = Signal(object)
    map_toggled = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("branchBar")
        caption = QLabel("Branch")
        caption.setObjectName("composerCaption")
        self.button = QToolButton()
        self.button.setObjectName("branchButton")
        self.button.setPopupMode(QToolButton.InstantPopup)
        self.menu = QMenu(self.button)
        self.button.setMenu(self.menu)
        self.hint = QLabel()
        self.hint.setObjectName("hintLabel")
        self.map_button = QToolButton()
        self.map_button.setObjectName("viewToggleButton")
        self.map_button.setText("Map")
        self.map_button.setCheckable(True)
        self.map_button.setToolTip("Every branch of the story as a map (Esc to go back)")
        self.map_button.toggled.connect(self.map_toggled)
        # Wraps under a large text size rather than widening the window.
        layout = WrapRow(self)
        layout.add_group([caption, self.button])
        layout.add_group([self.hint])
        layout.add_group([self.map_button])
        self._branches: list[Branch] = []
        self.set_branches([])

    def set_branches(
        self,
        branches: Sequence[Branch],
        openings: Mapping[str | None, str] | None = None,
        *,
        pending: str | None = None,
    ) -> None:
        """`openings`: how each branch begins, by its start id (None: the
        first line), for the menu's tooltips: the message that was rewritten
        is what tells one branch from another.
        `pending`: a branch named before its first turn (the review's)."""
        self._branches = list(branches)
        current = next((b for b in branches if b.is_current), None)
        self.button.setText((current.name if current else "—") + "  ▾")
        self.button.setEnabled(bool(branches))
        self.map_button.setEnabled(bool(branches))
        if pending:
            self.hint.setText(f"“{pending}” starts with your next turn")
        elif len(branches) == 1:
            self.hint.setText("the only one")
        else:
            self.hint.setText(f"{len(branches)} branches" if branches else "")
        self.button.setToolTip(
            "The line of the story you are reading. ⋯ → New branch from here… on any "
            "message starts another; the story as it was is kept under its own name."
        )
        self.menu.clear()
        for branch in branches:
            action = self.menu.addAction(("   ↳ " * min(branch.depth, 1)) + branch_line(branch))
            action.setCheckable(True)
            action.setChecked(branch.is_current)
            action.setToolTip((openings or {}).get(branch.start_id, ""))
            action.triggered.connect(
                lambda _on=False, start=branch.start_id: self.switch_requested.emit(start)
            )
        self.menu.setToolTipsVisible(True)
        self.menu.addSeparator()
        rename = self.menu.addAction("Rename this branch…")
        rename.setEnabled(current is not None)
        rename.triggered.connect(
            lambda: current is not None and self.rename_requested.emit(current.start_id)
        )
        delete = self.menu.addAction("Delete this branch…")
        delete.setEnabled(current is not None and not current.is_main)
        delete.setToolTip("The first line of the story can't be deleted")
        delete.triggered.connect(
            lambda: current is not None and self.delete_requested.emit(current.start_id)
        )

    def branches(self) -> list[Branch]:
        return list(self._branches)
