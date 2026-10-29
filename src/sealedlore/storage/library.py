"""The stories in the data folder, as a list shows them.

Read from each folder's story.json and a count of its messages, never the
whole bundle: the Stories list rebuilds after every job, and loading every
story whole grew with the library (gui/stories.py). Phone access lists the
shared ones from the same scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sealedlore.storage.atomic import read_json
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
            if story.get("mode") == "visual":
                # A Visual Story, played here before it became a game of its
                # own: listed so it can be found and deleted, never opened.
                out.append(
                    StoryListing(
                        entry.name,
                        str(story.get("title") or entry.name),
                        0,
                        str(story.get("updated_at") or ""),
                        error=VISUAL_ELSEWHERE,
                    )
                )
                continue
            nodes = read_json(entry / "nodes.json", [])
            out.append(
                StoryListing(
                    id=entry.name,
                    title=str(story.get("title") or entry.name),
                    messages=len(nodes) if isinstance(nodes, list) else 0,
                    updated=str(story.get("updated_at") or ""),
                    chat=story.get("mode") == "chat",
                )
            )
        except (OSError, ValueError) as exc:
            out.append(StoryListing(entry.name, entry.name, 0, "", error=str(exc)))
    return sorted(out, key=lambda listing: listing.updated, reverse=True)
