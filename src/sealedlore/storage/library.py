"""The stories in the data folder, as a list shows them.

Read from each folder's story.json and its messages as plain JSON, never the
whole bundle: the Stories list rebuilds after every job, and loading every
story whole grew with the library (gui/stories.py). Phone access lists the
shared ones from the same scan.

A folder's listing is read again only when its story.json or nodes.json
stands otherwise than when it was last read (`atomic.standing_of`: which
file, its size, when it changed): every save swaps a new file in. Reading
every story's messages to count them was 33-72 ms on 42 stories, on the GUI
thread, after every turn.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sealedlore.models.story import STORY_MODEL_FIELDS
from sealedlore.storage.atomic import read_json, standing_of
from sealedlore.storage.formats import STORY_FORMAT, format_of, newer_story_words
from sealedlore.storage.paths import is_safe_story_id, stories_dir
from sealedlore.storage.repository import VISUAL_ELSEWHERE


@dataclass(frozen=True)
class StoryListing:
    id: str
    title: str
    messages: int
    updated: str  # story.json's updated_at, "" when it has none
    chat: bool = False
    # A story that can't be read: why. It is still listed, so it can be
    # looked into or deleted.
    error: str | None = None
    plot: bool = False
    made: str = ""  # story.json's created_at
    # When its first and latest messages were written; "" before it begins.
    # `updated` moves for a change of settings too, which isn't playing.
    started: str = ""
    played: str = ""
    # This story's own models, by role: blank ones go with the settings.
    models: dict[str, str] = field(default_factory=dict)
    # Saved by a newer SealedLore (storage/formats.py): `error` says so, and
    # opening it asks first.
    newer: bool = False

    @property
    def kind(self) -> str:
        # What it is in words (Nov 22 2026): "Simple", "Standard" and "With
        # Plot" didn't say a chat from a story.
        return "Simple chat" if self.chat else "Story with a plot" if self.plot else "Story"

    @property
    def last_active(self) -> str:
        return self.played or self.updated


def _own_models(story: dict) -> dict[str, str]:
    defaults = story.get("defaults")
    if not isinstance(defaults, dict):
        return {}
    found = {role: defaults.get(name) for role, name in STORY_MODEL_FIELDS.items()}
    return {role: str(model) for role, model in found.items() if model}


def _written(node: object) -> str:
    return str(node.get("created_at") or "") if isinstance(node, dict) else ""


def _read(path: Path, default: Any = None) -> Any:
    """A file's JSON, not remembered for a save: none comes of a listing."""
    return read_json(path, default, remember=False)


# Each folder's listing as last read, by the folder's path, with how its
# story.json and nodes.json stood just before they were read.
_LISTED: dict[str, tuple[tuple[int, ...] | None, tuple[int, ...] | None, StoryListing]] = {}
_LISTED_LOCK = threading.Lock()


def story_listings(root: Path | None = None) -> list[StoryListing]:
    """Every story folder, played last first (one not begun by when it last
    changed); unreadable ones last."""
    directory = stories_dir(root)
    if not directory.exists():
        return []
    out: list[StoryListing] = []
    seen: set[str] = set()
    for entry in directory.iterdir():
        if not entry.is_dir() or not is_safe_story_id(entry.name):
            continue
        key = str(entry)
        seen.add(key)
        # Before anything is read: a file swapped in meanwhile stands
        # otherwise next time, and is read again then.
        stood = (standing_of(entry / "story.json"), standing_of(entry / "nodes.json"))
        with _LISTED_LOCK:
            known = _LISTED.get(key)
        if known is not None and stood[0] is not None and known[:2] == stood:
            out.append(known[2])
            continue
        try:
            listing = _listing(entry)
        except (OSError, ValueError) as exc:
            # Read again each time: what kept it from being read may pass.
            listing = StoryListing(entry.name, entry.name, 0, "", error=str(exc))
        else:
            with _LISTED_LOCK:
                _LISTED[key] = (*stood, listing)
        out.append(listing)
    with _LISTED_LOCK:
        for key in [k for k in _LISTED if Path(k).parent == directory and k not in seen]:
            del _LISTED[key]  # a folder gone
    return sorted(out, key=lambda listing: listing.last_active, reverse=True)


def _listing(entry: Path) -> StoryListing:
    """A story folder's listing, read from its files. Raises OSError, and
    ValueError for one that can't be read."""
    story = _read(entry / "story.json")
    if not isinstance(story, dict):
        raise ValueError("no story.json")
    if story.get("mode") == "visual":
        # A Visual Story, played here before it became a game of its
        # own: listed so it can be found and deleted, never opened.
        return StoryListing(
            entry.name,
            str(story.get("title") or entry.name),
            0,
            str(story.get("updated_at") or ""),
            error=VISUAL_ELSEWHERE,
        )
    if (found := format_of(story)) > STORY_FORMAT:
        return StoryListing(
            entry.name,
            str(story.get("title") or entry.name),
            0,
            str(story.get("updated_at") or ""),
            error=newer_story_words(found),
            newer=True,
        )
    nodes = _read(entry / "nodes.json", [])
    written = sorted(filter(None, map(_written, nodes))) if isinstance(nodes, list) else []
    return StoryListing(
        id=entry.name,
        title=str(story.get("title") or entry.name),
        messages=len(nodes) if isinstance(nodes, list) else 0,
        updated=str(story.get("updated_at") or ""),
        chat=story.get("mode") == "chat",
        plot=story.get("plot") is not None,
        made=str(story.get("created_at") or ""),
        started=written[0] if written else "",
        played=written[-1] if written else "",
        models=_own_models(story),
    )
