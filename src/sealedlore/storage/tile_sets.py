"""The tile sets shipped beside the sample stories (engine/tile_sets.py):
`tilesets/<name>/tileset.json` and the set's pictures, textures already made
to wrap and sprites already cut out of their white ground, so nothing is
processed when a map is drawn. Read through the same folder checks as the
samples' pictures: no path leaves the set's folder."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sealedlore.engine.tile_sets import TileSetInfo
from sealedlore.storage.paths import samples_dir

TILESETS_DIR = "tilesets"
MANIFEST = "tileset.json"
EXTENSIONS = (".png", ".jpg", ".webp")


def sets_folder(folder: Path | None = None) -> Path | None:
    root = samples_dir() if folder is None else folder
    if root is None:
        return None
    found = root / TILESETS_DIR
    return found if found.is_dir() else None


@lru_cache(maxsize=4)
def _shipped(root: str) -> dict[str, tuple[TileSetInfo, str]]:
    out: dict[str, tuple[TileSetInfo, str]] = {}
    for manifest in sorted(Path(root).glob(f"*/{MANIFEST}")):
        try:
            info = TileSetInfo.model_validate_json(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out[info.name] = (info, str(manifest.parent))
    return out


def shipped_sets(folder: Path | None = None) -> dict[str, tuple[TileSetInfo, Path]]:
    """Every shipped set, by name: its manifest and its folder."""
    root = sets_folder(folder)
    if root is None:
        return {}
    return {name: (info, Path(path)) for name, (info, path) in _shipped(str(root)).items()}


def read_asset(folder: Path, role: str) -> bytes | None:
    """A set's asset by role ("floor", "items/a-wooden-chest"), whichever of
    the picture kinds it is kept as; None when there is none. A role that
    would leave the folder is refused."""
    if not role or ".." in role.split("/") or role.startswith("/"):
        return None
    base = folder.resolve()
    for ext in EXTENSIONS:
        path = (folder / f"{role}{ext}").resolve()
        if not path.is_relative_to(base) or path.is_symlink():
            continue
        try:
            return path.read_bytes()
        except OSError:
            continue
    return None


def asset_names(folder: Path, sub: str) -> list[str]:
    """The assets in a set's subfolder ("items", "people"), by role."""
    found = []
    for path in sorted((folder / sub).glob("*")):
        if path.suffix in EXTENSIONS and path.is_file() and not path.is_symlink():
            found.append(f"{sub}/{path.stem}")
    return found
