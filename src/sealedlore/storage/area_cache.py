"""What a story's map worked out once and keeps: the ways cut to the edges
of each window of open country (engine/area.py `edges_open_memo`), in
<story>/cache/edges.json, so opening the story settles everyone without
searching 24 windows again (Oct 19 2026). Nothing here is story state: the
file can go, and the map is the same, slower once."""

from __future__ import annotations

from pathlib import Path

from sealedlore.storage.atomic import atomic_write_json, read_json
from sealedlore.storage.paths import story_dir

EDGE_CACHE_FILE = "edges.json"
EDGE_CACHE_MOST = 240  # windows kept: a long walk crosses a few dozen


def edge_cache_path(story_id: str, root: Path | None = None) -> Path:
    return story_dir(story_id, root) / "cache" / EDGE_CACHE_FILE


def load_edge_cache(
    story_id: str, root: Path | None = None
) -> dict[str, tuple[list[str], list[str]]]:
    """The kept edge paths, by the rows they were cut for; {} with none."""
    data = read_json(edge_cache_path(story_id, root), {})
    out: dict[str, tuple[list[str], list[str]]] = {}
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        if (
            isinstance(key, str)
            and isinstance(value, list)
            and len(value) == 2
            and all(isinstance(r, str) for r in value[0])
            and all(isinstance(r, str) for r in value[1])
        ):
            out[key] = (list(value[0]), list(value[1]))
    return out


def save_edge_cache(
    story_id: str, memo: dict[str, tuple[list[str], list[str]]], root: Path | None = None
) -> None:
    """Write the memo, newest windows kept when it is over EDGE_CACHE_MOST."""
    items = list(memo.items())[-EDGE_CACHE_MOST:]
    atomic_write_json(edge_cache_path(story_id, root), {k: [v[0], v[1]] for k, v in items})
