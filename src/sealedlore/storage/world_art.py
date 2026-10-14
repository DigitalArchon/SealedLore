"""world_art.json: the map's sprites, layouts and terrain (models/world.py).

Like images.json it is never part of `save_story_bundle`, which rewrites the
bundle from memory: art is drawn in the background and saved here as each
piece comes back, by the session's art thread under its lock
(engine/session_world_art.py), the file's only writer.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from sealedlore.models.world import WorldArt
from sealedlore.storage.atomic import atomic_write_json, read_json
from sealedlore.storage.images import WORLD_ART_FILE
from sealedlore.storage.paths import story_dir


def load_world_art(story_id: str, root: Path | None = None) -> WorldArt:
    data = read_json(story_dir(story_id, root) / WORLD_ART_FILE, None)
    if not isinstance(data, dict):
        return WorldArt()
    try:
        return WorldArt.model_validate(data)
    except ValidationError:
        return WorldArt()  # unreadable art is redrawn, never a failure to open


def save_world_art(story_id: str, art: WorldArt, root: Path | None = None) -> None:
    atomic_write_json(story_dir(story_id, root) / WORLD_ART_FILE, art.model_dump())
