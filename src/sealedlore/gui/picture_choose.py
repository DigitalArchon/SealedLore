"""Whom a picture goes to, for Copy to / Move to another character… on a
cast board's or Your characters' sheet (engine/picture_move.py)."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import QInputDialog, QMessageBox, QWidget

from sealedlore.models.character import Character


def label_of(character: Character, kept: Sequence[Character] = ()) -> str:
    """A character as the list names them: a version of one of Your
    characters with its main card's name and its own."""
    info = character.collection
    if info is None or not info.version:
        return character.name
    main = next((c for c in kept if c.id == info.family), None)
    return f"{main.name if main is not None else character.name} — {info.version}"


def choose_character(
    parent: QWidget,
    source: Character,
    others: Sequence[Character],
    *,
    move: bool,
    kept: Sequence[Character] = (),
) -> Character | None:
    """The character chosen, or None (Cancel, or nobody to give it to)."""
    title = "Move the picture" if move else "Copy the picture"
    if not others:
        QMessageBox.information(parent, title, "There is no other character to give it to.")
        return None
    labels = [label_of(c, kept) for c in others]
    # Two of one name are told apart by where they are in the list.
    seen: dict[str, int] = {}
    for i, label in enumerate(labels):
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            labels[i] = f"{label} ({seen[label]})"
    where = (
        f"Onto whose card does this picture of {source.name} go? It comes off {source.name}'s."
        if move
        else f"Onto whose card does this picture of {source.name} go too? {source.name} keeps it."
    )
    chosen, ok = QInputDialog.getItem(parent, title, where, labels, 0, False)
    if not ok or chosen not in labels:
        return None
    return others[labels.index(chosen)]


def given_words(source: Character, target: Character, move: bool) -> str:
    return (
        f"The picture moved from {source.name} to {target.name}."
        if move
        else f"{target.name} has the picture too."
    )
