"""Tile sets for map-first play (SealedLoreDocs design/map-first.md §6-7):
pure. A world's textures and sprites, drawn once by an image model and laid
by the program (gui/area_render.py), so a room is as full as the law layer
makes it and nothing is drawn that isn't there.

A set is a folder of assets and a manifest (`TileSetInfo`): shipped beside
the samples (storage/tile_sets.py reads them), or drawn for one story and
kept in its pictures (`WorldArt.tile_assets`). Roles are the files' names:
textures (floor, cap, face, out-ground, out-road, out-water, out-pave,
out-canopy, out-roof, out-wall), sprites (rug, decor0-2, out-tree0-1,
out-bush, out-rock, out-rubble, out-fence, out-door), each kind of thing
under items/<slug>, and a few generic people under people/.

What the measurements left as rules (design/map.md, tiles v2): a texture has
no direction ("packed earth", never "a winding path"), sprites are drawn
front-on ("not isometric"), and every prompt names the world: an "ops table"
came out wooden on a space station.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, Field

# Indoor textures and sprites, then outdoor ones.
INDOOR_TEXTURES = ("floor", "cap", "face")
INDOOR_SPRITES = ("rug", "decor0", "decor1", "decor2")
OUTDOOR_TEXTURES = (
    "out-ground",
    "out-road",
    "out-water",
    "out-pave",
    "out-canopy",
    "out-roof",
    "out-wall",
)
OUTDOOR_SPRITES = (
    "out-tree0",
    "out-tree1",
    "out-bush",
    "out-rock",
    "out-rubble",
    "out-fence",
    "out-door",
)
TEXTURES = (*INDOOR_TEXTURES, *OUTDOOR_TEXTURES)
SPRITES = (*INDOOR_SPRITES, *OUTDOOR_SPRITES)
# Words in a thing's name that make it give light.
LIGHT_WORDS = (
    "stove",
    "hearth",
    "fire",
    "brazier",
    "forge",
    "furnace",
    "lamp",
    "lantern",
    "torch",
    "boiler",
    "reactor",
    "conduit",
    "coals",
    "candle",
    "altar",
    "cauldron",
)


class TileSetInfo(BaseModel):
    """A set's manifest (tileset.json)."""

    name: str
    title: str = ""
    # Light from lamps and windows, and the world's colour grade.
    light: tuple[int, int, int] = (255, 200, 140)
    grade: tuple[float, float, float] = (1.0, 0.96, 0.9)
    # The outdoor water is molten (it glows, and has no foam).
    lava: bool = False
    # Space outside the walls (a deck): the void is drawn as stars.
    deck: bool = False
    # Which decorations give light.
    decor_lights: list[bool] = Field(default_factory=list)
    # What the set fits, for choosing one for a story's setting.
    keywords: list[str] = Field(default_factory=list)
    # The words each asset was drawn from, by role (for redrawing one).
    words: dict[str, str] = Field(default_factory=dict)


def slug(name: str) -> str:
    """A thing's name as a file name: "a heavy pine dining table" is
    "a-heavy-pine-dining-table"."""
    found = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
    if found or not name.strip():
        return found
    # A name in another script has no ASCII to slug: a stable one from its
    # bytes, so it still gets a sprite and a place in the editor.
    return "x" + format(zlib.crc32(name.strip().encode("utf-8")), "08x")


def item_slugs(name: str) -> list[str]:
    """The slugs a thing's sprite may be under, best first: the name as it
    is, then without its article ("a wooden chest" is drawn as
    "wooden-chest" in one set, "a-wooden-chest" in another)."""
    whole = slug(name)
    bare = slug(re.sub(r"^(the|a|an)\s+", "", name.strip(), flags=re.IGNORECASE))
    return list(dict.fromkeys(s for s in (whole, bare) if s))


def choose_set(
    chosen: str,
    setting: str,
    looks: Iterable[str],
    shipped: Mapping[str, TileSetInfo],
) -> str:
    """The set a story's map is drawn in: the author's choice if it is still
    there ("own" stands), else the shipped set whose keywords the setting
    names most, a space station for a map whose ground is space, and the
    first shipped set otherwise. "" when nothing is shipped."""
    if chosen == "own" or chosen in shipped:
        return chosen
    if not shipped:
        return ""
    text = " ".join(setting.lower().split())
    looks = {look for look in looks if look}
    best, score = "", 0
    for name, info in shipped.items():
        # Whole words (and their plurals): "star" took "starts", "inn"
        # "inner" (the second review); each keyword counts once, so one
        # word said often doesn't outweigh the rest.
        hits = sum(
            1
            for k in info.keywords
            if re.search(rf"\b{re.escape(k.lower())}(?:e?s)?\b", text) is not None
        )
        if info.deck and looks & {"space", "metal"}:
            hits += 5
        if hits > score:
            best, score = name, hits
    return best or next(iter(sorted(shipped)))
