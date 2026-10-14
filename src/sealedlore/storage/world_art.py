"""world_art.json: the map's sprites, layouts and terrain (models/world.py).

Like images.json it is not part of the bundle `save_story_bundle` rewrites
from memory: art is drawn in the background and saved here as each piece
comes back, by the session's art thread under its lock
(engine/session_world_art.py), the file's only writer once the story is
open. The one exception is the art a new story starts with
(`StoryBundle.world_art`: a restart's, a scenario's, a sample's), written
with the story when it is made.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from sealedlore.models.world import WorldArt
from sealedlore.storage.atomic import atomic_write_json, read_json
from sealedlore.storage.images import WORLD_ART_FILE, read_story_file
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


def carried_art(
    art: WorldArt, read: Callable[[str], bytes | None]
) -> tuple[WorldArt, dict[str, bytes]]:
    """The map's art to take into a new playthrough or a scenario file, and
    its map pictures' files (`read` gives one by its path in the story), to
    go with the reference pictures: what was drawn for the story stays
    drawn. A picture whose file can't be read is left out, or the new story
    would hold a record of nothing and never draw it again. The day's count
    against the daily limit and the failures are the old story's own: the
    new one starts them afresh."""
    carried = art.model_copy(deep=True)
    carried.pictures_by_day = {}
    carried.failures = {}
    files: dict[str, bytes] = {}
    kept = []
    for picture in carried.pictures:
        data = read(picture.file)
        if data is None:
            continue
        files[picture.file] = data
        kept.append(picture)
    carried.pictures = kept
    return carried, files


def carried_story_art(story_id: str, root: Path | None = None) -> tuple[WorldArt, dict[str, bytes]]:
    """`carried_art` for a story on disk (one not open: Duplicate settings,
    the CLI)."""
    return carried_art(
        load_world_art(story_id, root), lambda relative: read_story_file(story_id, relative, root)
    )


# A plot file may come with its map's art drawn beforehand (a sample's: the
# author, Oct 2026: a shipped story has its maps and art ready, drawing anew
# is for a scenario the player makes), as "<name>.world_art.json" beside
# "<name>.md". Its ids are the file's own (ids.stable_id), so it fits every
# import. Made by playing the map once and copying the story's world_art.json.
SIDECAR_SUFFIX = ".world_art.json"


def sidecar_art_path(plot_path: Path) -> Path:
    name = plot_path.name
    if name.endswith(".md"):
        name = name[: -len(".md")]
    return plot_path.with_name(name + SIDECAR_SUFFIX)


def load_sidecar_art(plot_path: Path) -> WorldArt | None:
    """The art shipped beside a plot file, or None (missing or unreadable:
    the map is drawn as it goes, as for any story)."""
    path = sidecar_art_path(plot_path)
    try:
        data = read_json(path, None)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        art = WorldArt.model_validate(data)
    except ValidationError:
        return None
    # Only drawn art travels this way: a map picture's file can't come with
    # it, and the counts are the story's that drew it.
    art.pictures, art.pictures_by_day, art.failures = [], {}, {}
    return art
