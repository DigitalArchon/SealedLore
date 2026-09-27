"""XDG path resolution and the on-disk story layout.

Note: §1 says config lives "under XDG config dir" but §2.1's concrete layout
places config.json under $XDG_DATA_HOME/sealedlore/ alongside stories/. We
follow §2.1 (the literal, unambiguous tree) here; flagged for the user to
confirm or correct.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

APP_DIR_NAME = "sealedlore"
WINDOWS_APP_DIR_NAME = "SealedLore"

# A story id becomes a folder name, and an imported archive supplies its own:
# "../x" or "/abs/path" would put the story's files outside stories/.
_SAFE_STORY_ID = re.compile(r"[A-Za-z0-9_-]+")


def data_home() -> Path:
    """The app's directory, e.g. ~/.local/share/sealedlore.

    On Windows, %LOCALAPPDATA%\\SealedLore unless XDG_DATA_HOME is set: Local,
    not Roaming, so a domain's roaming profile never copies the stories and
    the API key to a server."""
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if sys.platform == "win32" and not xdg_data_home:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / WINDOWS_APP_DIR_NAME
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / APP_DIR_NAME


def samples_dir() -> Path | None:
    """The sample plot files and their format guide: `$SEALEDLORE_SAMPLES`
    (the AppImage sets it), else the checkout's `SampleStories/`, else None."""
    from_env = os.environ.get("SEALEDLORE_SAMPLES")
    if from_env and Path(from_env).is_dir():
        return Path(from_env)
    checkout = Path(__file__).resolve().parents[3] / "SampleStories"
    return checkout if checkout.is_dir() else None


# The one Markdown file in the samples folder that isn't a story.
PLOT_FORMAT_GUIDE = "PLOT_FORMAT.md"


def sample_stories(folder: Path | None = None) -> list[tuple[str, Path]]:
    """The sample stories offered by File → New story from a sample, as
    (name, path), sorted by name: every plot file (.md) in the samples folder
    but the format guide, named after the file ("Doomsville.md" is
    "Doomsville", "Dust_Road.md" "Dust Road"). A file dropped into
    SampleStories/ is offered with no other change: the AppImage build copies
    every .md there. Read afresh on each call, never cached."""
    folder = samples_dir() if folder is None else folder
    if folder is None:
        return []
    found = [
        (path.stem.replace("_", " "), path)
        for path in folder.glob("*.md")
        if path.is_file() and path.name != PLOT_FORMAT_GUIDE
    ]
    return sorted(found, key=lambda item: item[0].casefold())


DATA_DIR_MODE = 0o700


def ensure_data_home(root: Path | None = None) -> Path:
    """The data folder, created if need be and kept to its owner alone.

    It holds the API key (config.json) and every prompt ever sent (each
    story's api_log.jsonl); the folder's mode covers them all, whatever the
    umask gave each file.
    """
    folder = root or data_home()
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        _restrict_on_windows(folder)
        return folder
    try:
        os.chmod(folder, DATA_DIR_MODE)
    except OSError:
        pass  # not ours to change (a read-only mount); the files still get 0600
    return folder


# Folders given their ACL in this process: it is carried down the whole tree,
# and the data folder is ensured on every save.
_restricted: set[Path] = set()


def _restrict_on_windows(folder: Path) -> None:
    """Windows' 0700 (`storage/winacl.py`), once per folder per run."""
    resolved = folder.resolve()
    if resolved in _restricted:
        return
    from sealedlore.storage import winacl

    try:
        winacl.restrict_to_owner(resolved)
    except OSError:
        return  # a drive with no ACLs (FAT, exFAT), or not ours to change
    _restricted.add(resolved)


def config_file(root: Path | None = None) -> Path:
    return (root or data_home()) / "config.json"


def stories_dir(root: Path | None = None) -> Path:
    return (root or data_home()) / "stories"


def is_safe_story_id(story_id: str) -> bool:
    """One plain path component: nothing that could leave the stories folder."""
    return bool(_SAFE_STORY_ID.fullmatch(story_id))


def story_dir(story_id: str, root: Path | None = None) -> Path:
    if not is_safe_story_id(story_id):
        raise ValueError(f"not a valid story id: {story_id!r}")
    return stories_dir(root) / story_id


def ensure_story_dir(story_id: str, root: Path | None = None) -> Path:
    ensure_data_home(root)
    path = story_dir(story_id, root)
    path.mkdir(parents=True, exist_ok=True)
    return path
