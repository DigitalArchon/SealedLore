"""Which version of their layout a story folder and config.json are saved in.

Every file SealedLore saves as it runs is read through pydantic models that
drop what they don't know. A story saved by a newer SealedLore and opened in
an older one would lose, silently and for good, whatever the newer one kept
that the older one doesn't know of, the first time the older one saved it.
So story.json and config.json say which layout they are in
(`FORMAT_KEY`), and a version that finds a newer one says so instead:

- a story folder isn't opened unless the author asks, knowing what is lost
  (`NewerStory`); the story backups that carry one are refused;
- config.json is read as far as this version can, and isn't saved over
  until the author says to (`storage.repository.config_newer`).

A format goes up whenever the shape of what is saved changes: a field added,
removed, or holding something else. `tests/test_file_formats.py` holds each
format to a fingerprint of its models' shapes, so a change made without
raising it fails there. A file with no format is from before this, format 0,
which every version reads.
"""

from __future__ import annotations

from typing import Any

# The key in story.json and config.json. (A backup's and a scenario's own
# "format" names what kind of file it is.)
FORMAT_KEY = "format_version"

# The layout of a story folder: story.json, nodes.json, cast.json and every
# other record in it (tests/test_file_formats.py lists the models).
STORY_FORMAT = 4
# The layout of config.json.
CONFIG_FORMAT = 3
# The layout of a lorebook kept in Lorebooks (storage/lorebooks.py).
LOREBOOK_FORMAT = 2


def format_of(data: Any) -> int:
    """The format a file's data says it is in: 0 for none (older than
    formats), and for anything that isn't a whole number."""
    if not isinstance(data, dict):
        return 0
    value = data.get(FORMAT_KEY)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def with_format(data: dict[str, Any], version: int) -> dict[str, Any]:
    """The data as it is written: its format first, for a reader by hand."""
    return {FORMAT_KEY: version, **{k: v for k, v in data.items() if k != FORMAT_KEY}}


class NewerStory(ValueError):
    """A story folder saved by a newer SealedLore than this one."""

    def __init__(self, found: int) -> None:
        self.found = found
        super().__init__(newer_story_words(found))


def newer_story_words(found: int) -> str:
    return (
        f"Saved by a newer SealedLore (story format {found}; this version reads up to "
        f"{STORY_FORMAT}). Open it in that version: opened here, whatever this one doesn't "
        "know of would be lost the first time it saved."
    )


def newer_config_words(found: int) -> str:
    return (
        f"Settings were saved by a newer SealedLore (format {found}; this version reads up to "
        f"{CONFIG_FORMAT}). They are used as far as this version understands them, and nothing "
        "is saved over them, so the newer version finds them as it left them: a change made "
        "here lasts until you quit, unless you save Settings and say to."
    )
