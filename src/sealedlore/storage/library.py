"""The stories in the data folder, as a list shows them.

Read from each folder's story.json and its messages as plain JSON, never the
whole bundle: the Stories list rebuilds after every job, and loading every
story whole grew with the library (gui/stories.py). Phone access lists the
shared ones from the same scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sealedlore.models.story import STORY_MODEL_FIELDS
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
    plot: bool = False
    made: str = ""  # story.json's created_at
    # When its first and latest messages were written; "" before it begins.
    # `updated` moves for a change of settings too, which isn't playing.
    started: str = ""
    played: str = ""
    # This story's own models, by role: blank ones go with the settings.
    models: dict[str, str] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return "Simple" if self.chat else "With Plot" if self.plot else "Standard"

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


def story_listings(root: Path | None = None) -> list[StoryListing]:
    """Every story folder, played last first (one not begun by when it last
    changed); unreadable ones last."""
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
            written = sorted(filter(None, map(_written, nodes))) if isinstance(nodes, list) else []
            out.append(
                StoryListing(
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
            )
        except (OSError, ValueError) as exc:
            out.append(StoryListing(entry.name, entry.name, 0, "", error=str(exc)))
    return sorted(out, key=lambda listing: listing.last_active, reverse=True)
