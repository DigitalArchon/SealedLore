"""Pictures of the places a story goes to (the author, Oct 24 2026): a
picture of the place itself, with nobody in it, kept on the lore entry for
that place and shown behind the story's text while the scene is there.

Pure: which lore entry the scene's location names, the title a new entry
takes from a location, and the picture that stands for a place now.
`StorySession` (engine/session_images.py) and the window do the rest.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from sealedlore.models.image import ImageRef
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.images import ImageModelInfo

# A matched name shorter than this says nothing ("inn" in "beginning" is
# kept off by the word boundary; "it" as a keyword would still match).
MIN_NAME = 3
# A new entry's title is at most this long.
TITLE_MAX = 60
# The largest square a place's picture is drawn at when the model draws no
# landscape (FLUX.3 Klein lists squares alone): the background covers the
# story's column, so a square loses only its top and bottom.
PLACE_SIDE = 1536
# What a place's picture is captioned on its entry.
PLACE_CAPTION = "the place, as the story first found it"
PLACE_CAPTION_AGAIN = "the place, as it is now"

_THE = re.compile(r"^the\s+", re.IGNORECASE)
_CUT = re.compile(r"\s+[—–-]\s+|;|,\s")


def place_for(location: str | None, entries: Sequence[LoreEntry]) -> LoreEntry | None:
    """The lore entry the scene's location names: its title or a keyword as
    a whole word in the location, a leading "the" aside. The match that
    comes first in the location wins (a location names the nearer place
    first: "the kitchen of the farmhouse", "Main street, Hellsville"), then
    the longest, then a title over a keyword. None when nothing matches."""
    if not location or not location.strip():
        return None
    text = location.casefold()
    best: tuple[tuple[int, int, int], LoreEntry] | None = None
    for entry in entries:
        for name in (entry.title, *entry.keywords):
            bare = _THE.sub("", name.strip())
            if len(bare) < MIN_NAME:
                continue
            found = re.search(rf"\b{re.escape(bare.casefold())}\b", text)
            if found is not None:
                score = (-found.start(), len(bare), 1 if name == entry.title else 0)
                if best is None or score > best[0]:
                    best = (score, entry)
    return best[1] if best is not None else None


def place_title(location: str) -> str:
    """A title for a new entry from the scene's location: up to the first
    dash, semicolon or comma, the first letter a capital, at most TITLE_MAX
    characters."""
    head = _CUT.split(location.strip(), maxsplit=1)[0].strip() or location.strip()
    head = head[:TITLE_MAX].rstrip(" .")
    return head[:1].upper() + head[1:]


def newest_picture(entry: LoreEntry | None) -> ImageRef | None:
    """The picture that stands for the place now: the entry's last. A new
    picture of the place goes after the others; a shipped sample's one
    picture is both first and last."""
    if entry is None or not entry.reference_images:
        return None
    return entry.reference_images[-1]


def place_size(model: ImageModelInfo | None) -> str:
    """A place's picture is wide where the model draws one (the widest
    landscape listed up to 1360 across), else the largest square up to
    PLACE_SIDE, else the model's first size, or 1024x768."""
    sizes = list(model.resolutions) if model is not None else []
    shapes = []
    for size in sizes:
        parts = size.replace("*", "x").split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            shapes.append((int(parts[0]), int(parts[1]), size))
    wide = sorted(s for s in shapes if s[0] > s[1] and s[0] <= 1360)
    if wide:
        return wide[-1][2]
    square = sorted(s for s in shapes if s[0] == s[1] and s[0] <= PLACE_SIDE)
    if square:
        return square[-1][2]
    return sizes[0] if sizes else "1024x768"
