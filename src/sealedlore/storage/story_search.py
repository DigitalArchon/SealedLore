"""Find words across every story in the data folder (the Stories list's search).

The author (Oct 31 2026): "a full text search of all stories to find what the
person is looking for". Every message on every branch, the chapters, the
Questions, the world, the opening and the cards, read as plain JSON; never the
API log, and never what a plot is keeping back unless the author has asked to
see spoilers: a search must not be the way to find out who is coming.

A story matches when it holds every word of the query, anywhere; the words
around the whole phrase are shown when it holds the phrase, else around the
first word found. Measured on the largest test story (2.3 MB of nodes.json, 122
messages): ~10 ms to parse and search, so a library takes about a second, on a
thread of the window's. No index: nothing to keep up to date or to leave on disk.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sealedlore.storage.atomic import read_json
from sealedlore.storage.paths import is_safe_story_id, stories_dir

# The story's own text; `StoryHit.find` is only ever words found here, which
# Find in story can go to.
IN_THE_STORY = "Story"
TITLE = "Title"
# Words on each side of a match, in characters.
SNIPPET_REACH = 60
# Models write curly quotes and long dashes; nobody types them in a search box.
_TYPED_AS = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-"})
_CARD_FIELDS = ("name", "summary", "full_description", "voice_notes", "canon")


@dataclass(frozen=True)
class StoryHit:
    story_id: str
    # Where the words shown are: TITLE, IN_THE_STORY, "Chapters", "World"…
    where: str
    # The words around the match, cut at word ends; "" for a title.
    snippet: str
    # What Find in story can look for on opening it; "" when no message has it.
    find: str


def _fold(text: str, typed: bool) -> str:
    """Case aside; `typed` when the query holds a quote or a dash (rarely, and
    translating every message is what this would otherwise spend its time on)."""
    return text.lower().translate(_TYPED_AS) if typed else text.lower()


def _snippet(text: str, at: int, length: int) -> str:
    start = max(0, at - SNIPPET_REACH)
    end = min(len(text), at + length + SNIPPET_REACH)
    if start > 0:
        space = text.find(" ", start, at)
        start = space + 1 if space >= 0 else start
    if end < len(text):
        space = text.rfind(" ", at + length, end)
        end = space if space >= 0 else end
    words = " ".join(text[start:end].split())
    return f"{'…' if start > 0 else ''}{words}{'…' if end < len(text) else ''}"


def _strings(value: Any) -> Iterator[str]:
    """Every string in a piece of JSON."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _records(path: Path) -> list[dict]:
    try:
        records = read_json(path, [])
    except (OSError, ValueError):
        return []
    return (
        [record for record in records if isinstance(record, dict)]
        if isinstance(records, list)
        else []
    )


def _cards(records: list[dict], spoilers: bool) -> Iterator[str]:
    for card in records:
        if card.get("plot_hidden") and not spoilers:
            continue
        for name in _CARD_FIELDS:
            if isinstance(card.get(name), str):
                yield card[name]
        yield from _strings(card.get("aliases"))


def _texts(folder: Path, story: dict, spoilers: bool) -> Iterator[tuple[str, str]]:
    """(where, text), the title first, then what the story is made of. A file
    is only read if the search gets that far."""
    yield TITLE, str(story.get("title") or "")
    if story.get("mode") == "visual":
        return  # a Visual Story's, listed but no longer played here
    for node in _records(folder / "nodes.json"):
        for text in (node.get("content"), node.get("ooc")):
            if isinstance(text, str):
                yield IN_THE_STORY, text
    for summary in _records(folder / "summaries.json"):
        if isinstance(summary.get("content"), str):
            yield "Chapters", summary["content"]
    for aside in _records(folder / "asides.json"):
        for text in (aside.get("question"), aside.get("answer")):
            if isinstance(text, str):
                yield "Questions", text
    setup = story.get("setup") if isinstance(story.get("setup"), dict) else {}
    style = story.get("style") if isinstance(story.get("style"), dict) else {}
    for where, text in (
        ("World", story.get("world_bible")),
        ("Description", setup.get("description")),
        ("Opening", setup.get("opening_text")),
        ("System prompt", story.get("chat_prompt")),
        ("Tail", story.get("chat_tail")),
        ("Style", style.get("notes")),
    ):
        if isinstance(text, str):
            yield where, text
    for text in _cards(_records(folder / "cast.json"), spoilers):
        yield "Cast", text
    try:
        supporting = read_json(folder / "supporting.json", {})
    except (OSError, ValueError):
        supporting = {}
    if isinstance(supporting, dict) and isinstance(supporting.get("characters"), list):
        for text in _cards(supporting["characters"], spoilers):
            yield "Cast", text
    for entry in _records(folder / "lore.json"):
        if entry.get("plot_hidden") and not spoilers:
            continue
        for text in (entry.get("title"), entry.get("content"), *entry.get("keywords", [])):
            if isinstance(text, str):
                yield "Lore", text
    if spoilers:
        for text in _strings(story.get("plot")):
            yield "Plot", text


def _search_one(
    folder: Path, story: dict, query: str, spoilers: bool, stop: threading.Event
) -> StoryHit | None:
    phrase = " ".join(query.translate(_TYPED_AS).lower().split())
    words = phrase.split()
    typed = any(mark in phrase for mark in "'\"-")
    missing = set(words)
    # (where, text, offset, length): the first words found, better than a
    # title's if any are found elsewhere, and the whole phrase, which ends it.
    shown: tuple[str, str, int, int] | None = None
    find = ""
    for where, text in _texts(folder, story, spoilers):
        if stop.is_set():
            return None
        folded = _fold(text, typed)
        if phrase in folded:
            shown = (where, text, folded.index(phrase), len(phrase))
            missing.clear()
            find = phrase if where == IN_THE_STORY else find
            break
        for word in words:
            if word not in folded:
                continue
            missing.discard(word)
            if shown is None or shown[0] == TITLE:
                shown = (where, text, folded.index(word), len(word))
            if where == IN_THE_STORY and not find:
                find = word
    if missing or shown is None:
        return None
    where, text, at, length = shown
    return StoryHit(folder.name, where, "" if where == TITLE else _snippet(text, at, length), find)


def search_stories(
    query: str,
    root: Path | None = None,
    *,
    spoilers: bool = False,
    stop: threading.Event | None = None,
) -> Iterator[StoryHit]:
    """Every story holding all of `query`'s words, as each is found. `stop`
    ends it between one text and the next. A story that can't be read is left
    out: the list shows it, greyed, for what it is."""
    directory = stories_dir(root)
    if not query.strip() or not directory.exists():
        return
    stop = stop or threading.Event()
    for entry in directory.iterdir():
        if stop.is_set():
            return
        if not entry.is_dir() or not is_safe_story_id(entry.name):
            continue
        try:
            story = read_json(entry / "story.json")
        except (OSError, ValueError):
            continue
        if not isinstance(story, dict):
            continue
        hit = _search_one(entry, story, query, spoilers, stop)
        if hit is not None:
            yield hit
