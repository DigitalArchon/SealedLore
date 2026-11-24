"""The author's own Lorebooks: lorebooks kept outside any story, each a file
of its own in the data folder's `lorebooks` folder (models/lorebook.py).

The author (Nov 20 2026): a lorebook on its own, not a character's, should be
kept, changed, imported and exported, and a story started from it, bringing
in anyone from Your characters. A book is kept as it came (SillyTavern's own
shape or a card's), so it goes out again with every setting other apps read;
a story takes its entries as the Lore page's import does
(storage/tavern_lore.py `lore_from_book`).

Only the window writes here (gui/window_lorebooks.py), from the GUI thread.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sealedlore.ids import utc_now_iso
from sealedlore.models.character import Character
from sealedlore.models.lorebook import StoredLorebook
from sealedlore.models.scenario import Scenario
from sealedlore.models.scene import SceneState
from sealedlore.models.story import StyleDirectives
from sealedlore.storage.atomic import atomic_write_json, read_json
from sealedlore.storage.formats import FORMAT_KEY, LOREBOOK_FORMAT, format_of, with_format
from sealedlore.storage.paths import data_home, ensure_data_home, is_plain_id
from sealedlore.storage.tavern_lore import (
    LoreImport,
    ReadLorebook,
    entries_of,
    entry_enabled,
    is_numbered,
    lore_from_book,
    write_world,
)

# What a kept lorebook's file says it is.
FILE_KIND = "sealedlore-lorebook"
SUFFIX = ".json"
FOLDER = "lorebooks"


def lorebooks_dir(root: Path | None = None) -> Path:
    return (root or data_home()) / FOLDER


def lorebook_path(book_id: str, root: Path | None = None) -> Path:
    if not is_plain_id(book_id):
        raise ValueError(f"not a valid lorebook id: {book_id!r}")
    return lorebooks_dir(root) / f"{book_id}{SUFFIX}"


@dataclass
class Shelf:
    """Every lorebook kept, and the files that couldn't be read, each with why
    (they are left where they are, never written over)."""

    books: list[StoredLorebook] = field(default_factory=list)
    unreadable: list[tuple[Path, str]] = field(default_factory=list)


def load_lorebooks(root: Path | None = None) -> Shelf:
    shelf = Shelf()
    folder = lorebooks_dir(root)
    if not folder.is_dir():
        return shelf
    for path in sorted(folder.glob(f"*{SUFFIX}")):
        if not is_plain_id(path.stem):
            continue
        try:
            data = read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            shelf.unreadable.append((path, f"it can't be read ({exc.__class__.__name__})"))
            continue
        if not isinstance(data, dict) or data.get("format") != FILE_KIND:
            shelf.unreadable.append((path, "it isn't a kept lorebook"))
            continue
        if format_of(data) > LOREBOOK_FORMAT:
            shelf.unreadable.append(
                (path, "it was saved by a newer SealedLore: open it in that version")
            )
            continue
        try:
            book = StoredLorebook.model_validate(
                {k: v for k, v in data.items() if k not in ("format", FORMAT_KEY)}
            )
        except ValidationError:
            shelf.unreadable.append((path, "it isn't a kept lorebook this version can read"))
            continue
        # Its file's name is its id, whatever the file says (one copied by
        # hand would otherwise be saved over the other).
        book.id = path.stem
        shelf.books.append(book)
    return shelf


def save_lorebook(book: StoredLorebook, root: Path | None = None) -> None:
    ensure_data_home(root)
    folder = lorebooks_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    data = {"format": FILE_KIND, **book.model_dump(mode="json")}
    atomic_write_json(lorebook_path(book.id, root), with_format(data, LOREBOOK_FORMAT), mode=0o600)


def touch(book: StoredLorebook) -> None:
    book.changed_at = utc_now_iso()


# --- what a book holds ------------------------------------------------------------------


def name_of(book: Mapping[str, Any], fallback: str) -> str:
    """A lorebook's own name: its `name`, else the name SillyTavern kept with
    a card's book beside its entries (`originalData`), else `fallback` (its
    file's name, less the ending)."""
    for holder in (book, book.get("originalData")):
        if isinstance(holder, Mapping):
            name = str(holder.get("name") or "").strip()
            if name:
                return name
    return fallback.strip() or "Lorebook"


@dataclass(frozen=True)
class BookStats:
    entries: int
    words: int
    always: int
    off: int


def stats(book: Mapping[str, Any]) -> BookStats:
    listed = entries_of(book)
    numbered = is_numbered(book)
    return BookStats(
        entries=len(listed),
        words=sum(len(str(raw.get("content") or "").split()) for raw in listed),
        always=sum(1 for raw in listed if raw.get("constant")),
        off=sum(1 for raw in listed if not entry_enabled(raw, numbered=numbered)),
    )


def stats_words(book: Mapping[str, Any]) -> str:
    return said(stats(book))


def said(found: BookStats) -> str:
    """A book's `stats` in words: "6 entries · 1,204 words · 2 always sent"."""
    parts = [f"{found.entries} {'entry' if found.entries == 1 else 'entries'}"]
    parts.append(f"{found.words:,} words")
    if found.always:
        parts.append(f"{found.always} always sent")
    if found.off:
        parts.append(f"{found.off} switched off")
    return " · ".join(parts)


def kept_from(read: ReadLorebook, name: str, came_from: str) -> StoredLorebook:
    """A lorebook read from a file (or a card, a link) as one to keep: a copy
    of its book, so nothing done to it reaches what it was read from."""
    book = json.loads(json.dumps(read.book))
    return StoredLorebook(
        name=name_of(book, name), came_from=came_from, converted=read.notes(), book=book
    )


def copied(book: StoredLorebook, name: str) -> StoredLorebook:
    data = book.model_dump(mode="json")
    data.pop("id", None)
    data.update(name=name, added_at=utc_now_iso(), changed_at=utc_now_iso())
    return StoredLorebook.model_validate(data)


def unique_name(name: str, books: list[StoredLorebook]) -> str:
    taken = {book.name.casefold() for book in books}
    if name.casefold() not in taken:
        return name
    number = 2
    while f"{name} ({number})".casefold() in taken:
        number += 1
    return f"{name} ({number})"


def lore_of(book: StoredLorebook, fill: Callable[[str], str] = lambda text: text) -> LoreImport:
    """The book's entries as a story's lore, each saying it came from here."""
    return lore_from_book(book.book, fill, f"the lorebook {book.name}", book.condensed)


def converted_sentence(note: str) -> str:
    """What converting a lorebook lost, as a sentence: kept as a phrase
    ("NovelAI's Always On…") before Nov 21 2026, as a sentence ("Left out
    NovelAI's…") since."""
    if note.startswith("Left out "):
        return f"l{note[1:]}"
    return f"left out {note}."


def scenario_from(
    book: StoredLorebook,
    *,
    title: str,
    player: str = "",
    about_player: str = "",
    lore_firing: str = "sillytavern",
) -> tuple[Scenario, list[str]]:
    """A new story set in a kept lorebook's world: its entries the story's
    lore ({{user}} and {{char}} as written, filled as each request goes out,
    {{user}} as `player`), its description the story's, and `player` the author's
    character, in the starting scene. What the lorebook had that a story has
    no place for is said in the notes, as an import says it, in the words
    of `lore_firing`, the rules its lore fires by: SillyTavern's by default,
    the lorebook having been written for them (the author, Nov 21 2026)."""
    # {{user}} and {{char}} stay as written: filled as each request goes out.
    imported = lore_of(book)
    notes = imported.said("Lorebook", lore_firing)
    notes += [f"Converting it: {converted_sentence(note)}" for note in book.converted]
    description = str(book.book.get("description") or "").strip()
    scenario = Scenario(
        title=title.strip() or book.name,
        description=description or None,
        lore=imported.entries,
        style=StyleDirectives(),
        lore_firing="sillytavern" if lore_firing == "sillytavern" else "sealedlore",
    )
    if player.strip():
        you = Character(name=player.strip(), summary=about_player.strip() or None)
        scenario.cast = [you]
        scenario.placeholders.user_id = you.id
        scenario.starting_scene = SceneState(present_character_ids=[you.id])
        scenario.suggested_character_id = you.id
    return scenario, notes


def export(book: StoredLorebook, path: Path) -> None:
    """The book as SillyTavern's own lorebook file, every setting kept, under
    its name (SillyTavern names a lorebook by its file, and keeps a card's
    book's name beside it)."""
    write_world(path, book.book)


def searchable(book: StoredLorebook) -> str:
    words = [book.name, book.came_from, str(book.book.get("description") or "")]
    for raw in entries_of(book.book):
        words.append(str(raw.get("comment") or raw.get("name") or ""))
    return " ".join(words).casefold()
