"""The stories in the data folder, as a list shows them.

Read from each folder's story.json and a count of its messages, never the
whole bundle: the Stories list rebuilds after every job, and loading every
story whole grew with the library (gui/stories.py). Phone access lists the
shared ones from the same scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sealedlore.models.story import maps_offered
from sealedlore.storage.atomic import read_json
from sealedlore.storage.paths import is_safe_story_id, stories_dir


@dataclass(frozen=True)
class StoryListing:
    id: str
    title: str
    messages: int
    updated: str  # story.json's updated_at, "" when it has none
    chat: bool = False
    keeps_map: bool = False
    # A story that can't be read: why. It is still listed, so it can be
    # looked into or deleted.
    error: str | None = None


def story_listings(root: Path | None = None) -> list[StoryListing]:
    """Every story folder, most recently played first; unreadable ones last."""
    directory = stories_dir(root)
    if not directory.exists():
        return []
    out: list[StoryListing] = []
    for entry in directory.iterdir():
        if not entry.is_dir() or not is_safe_story_id(entry.name):
            continue
        try:
            story = read_json(entry / "story.json")
            if not isinstance(story, dict):
                raise ValueError("no story.json")
            nodes = read_json(entry / "nodes.json", [])
            defaults = story.get("defaults")
            out.append(
                StoryListing(
                    id=entry.name,
                    title=str(story.get("title") or entry.name),
                    messages=len(nodes) if isinstance(nodes, list) else 0,
                    updated=str(story.get("updated_at") or ""),
                    chat=story.get("mode") == "chat",
                    keeps_map=maps_offered()
                    and isinstance(defaults, dict)
                    and bool(defaults.get("world_map")),
                )
            )
        except (OSError, ValueError) as exc:
            out.append(StoryListing(entry.name, entry.name, 0, "", error=str(exc)))
    return sorted(out, key=lambda listing: listing.updated, reverse=True)
