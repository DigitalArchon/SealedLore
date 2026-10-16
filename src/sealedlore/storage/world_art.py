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

from sealedlore.models.world import WorldArt, own_set_key
from sealedlore.storage.atomic import atomic_write_json, read_json
from sealedlore.storage.image_meta import MetadataError, strip_metadata
from sealedlore.storage.images import IMAGES_DIR, WORLD_ART_FILE, is_safe_relative, read_story_file
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
    # The story's own tile sets and the author's changes to single assets,
    # each asset only with its file; a set left with none is left out.
    for roles in (*(own.assets for own in carried.tile_sets.values()), carried.tile_overrides):
        for role, file in list(roles.items()):
            if not file:
                continue
            data = read(file)
            if data is None:
                roles.pop(role)
            else:
                files[file] = data
    carried.tile_sets = {key: own for key, own in carried.tile_sets.items() if own.assets}
    return carried, files


def art_with_files(art: WorldArt, has_file: Callable[[str], bool]) -> WorldArt:
    """`art` for a story being made from a scenario or a sidecar: each map
    picture, own tile set asset and changed asset only with its file (a kept
    record of nothing would stop it being drawn again), and the counts afresh."""
    art = art.model_copy(deep=True)
    art.pictures = [p for p in art.pictures if has_file(p.file)]
    for own in art.tile_sets.values():
        own.assets = {role: file for role, file in own.assets.items() if has_file(file)}
    art.tile_sets = {key: own for key, own in art.tile_sets.items() if own.assets}
    art.tile_overrides = {r: f for r, f in art.tile_overrides.items() if not f or has_file(f)}
    art.pictures_by_day, art.failures = {}, {}
    return art


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
# Its map pictures (a sample's isometric views) are files beside the plot
# file, each record's `file` written from the plot file's folder as a
# `- picture:` line is ("pictures/map/farm-isometric.jpg").
SIDECAR_SUFFIX = ".world_art.json"
# Where a sidecar's map pictures go in the new story: beside those the story
# draws (engine/world_pictures.MAP_PICTURES_DIR).
SIDECAR_PICTURES_DIR = f"{IMAGES_DIR}/map"


def sidecar_art_path(plot_path: Path) -> Path:
    name = plot_path.name
    if name.endswith(".md"):
        name = name[: -len(".md")]
    return plot_path.with_name(name + SIDECAR_SUFFIX)


def load_sidecar_art(plot_path: Path) -> WorldArt | None:
    """The art shipped beside a plot file, without its map pictures, or
    None (missing or unreadable: the map is drawn as it goes, as for any
    story)."""
    art, _files = load_sidecar(plot_path)
    if art is not None:
        art.pictures = []
    return art


def load_sidecar(plot_path: Path) -> tuple[WorldArt | None, dict[str, bytes]]:
    """The art shipped beside a plot file and its map pictures' files, as
    the new story will hold them (`StoryBundle.world_art` and
    `pending_files`). A picture comes only with its file: one whose file is
    missing, outside the plot file's folder (a plot file may be someone
    else's) or can't have its hidden data stripped is left out, or the story
    would hold a record of nothing and never draw it again. The counts are
    the story's that drew it, and start afresh."""
    path = sidecar_art_path(plot_path)
    try:
        data = read_json(path, None)
    except (OSError, ValueError):
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    try:
        art = WorldArt.model_validate(data)
    except ValidationError:
        return None, {}
    art.pictures_by_day, art.failures = {}, {}
    files: dict[str, bytes] = {}
    kept = []
    for picture in art.pictures:
        source = _inside(plot_path.parent, picture.file)
        name = Path(picture.file).name
        relative = f"{SIDECAR_PICTURES_DIR}/{name}"
        if relative in files:
            # Two files of one name from different folders: the second was
            # dropped with its record. Named by its own record instead.
            relative = f"{SIDECAR_PICTURES_DIR}/{picture.id}{Path(name).suffix}"
        if source is None or not is_safe_relative(relative) or relative in files:
            continue
        try:
            files[relative] = strip_metadata(source.read_bytes())
        except (OSError, MetadataError):
            continue
        picture.file = relative
        kept.append(picture)
    art.pictures = kept
    # A story's own sets and the author's changes name files the new story
    # won't have: dropped (a kept record stopped the figure being drawn). A
    # sample's set is shipped beside it (storage/tile_sets.py).
    art.tile_sets = {}
    art.tile_overrides = {role: file for role, file in art.tile_overrides.items() if file == ""}
    if own_set_key(art.tile_set) is not None:
        art.tile_set = ""
    return art, files


def _inside(folder: Path, relative: str) -> Path | None:
    """`relative` under `folder` as an existing file, or None if it isn't
    one or leads out of it (by `..`, an absolute path, or a link)."""
    try:
        base = folder.resolve()
        target = (base / relative).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not target.is_relative_to(base) or target == base or not target.is_file():
        return None
    return target
