"""A world kit beside its tile set (models/kit.py, engine/kits.py):
`tilesets/<set>/kit.json`, the item tables, building templates, chunks and
yard pieces of the setting the set draws. Read once a process, by the set's
name; a set with no kit, or one that doesn't parse, has none (the place is
built as it was, its containers hold what the story put in them)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sealedlore.models.kit import Kit
from sealedlore.storage.tile_sets import shipped_sets

KIT_FILE = "kit.json"


@lru_cache(maxsize=8)
def _read(path: str, stamp: float) -> Kit | None:
    del stamp  # in the key only: an edited kit is read again
    try:
        return Kit.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def kit_path(set_name: str, folder: Path | None = None) -> Path | None:
    """Where a shipped set's kit is, or None when it has none."""
    found = shipped_sets(folder).get(set_name)
    if found is None:
        return None
    path = found[1] / KIT_FILE
    return path if path.is_file() and not path.is_symlink() else None


def load_kit(set_name: str, folder: Path | None = None) -> Kit | None:
    """The kit of a shipped tile set, by the set's name."""
    path = kit_path(set_name, folder)
    if path is None:
        return None
    kit = _read(str(path), path.stat().st_mtime)
    if kit is not None and not kit.name:
        kit = kit.model_copy(update={"name": set_name})
    return kit
