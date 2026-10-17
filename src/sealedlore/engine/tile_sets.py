"""Tile sets for map-first play (SealedLoreDocs design/map-first.md §6-7):
pure. A world's textures and sprites, drawn once by an image model and laid
by the program (gui/area_render.py), so a room is as full as the law layer
makes it and nothing is drawn that isn't there.

A set is a folder of assets and a manifest (`TileSetInfo`): shipped beside
the samples (storage/tile_sets.py reads them), or drawn for one story and
kept in its pictures (`WorldArt.tile_sets`, several a story, each chosen as
"own:<key>"). Roles are the files' names:
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

from sealedlore.models.world import OWN_SET_PREFIX, own_set_key, own_set_name

__all__ = ["OWN_SET_PREFIX", "own_set_key", "own_set_name"]

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
    # The ground looks its outdoor ground stands for ("snow"; `GROUND_WORDS`):
    # an area whose plan has another gets a ground of its own drawn in the
    # set's style (`ground_role`). Empty: read from the ground's words
    # (`set_grounds`), as a story's own set has none listed.
    grounds: list[str] = Field(default_factory=list)
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


# Words that say little of what a thing is, left out when matching names.
_FILLER = frozenset(
    "a an the of with and on in for to by at its his her their old small large big little "
    "some few long short tall low high round square two three set pair".split()
)
# The words after which a name goes on to say what is with the thing, not
# what it is ("a kitchen table with an oil lamp" is a table).
_AFTER_HEAD = frozenset("with of on in by near under beside behind holding full".split())


def _stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "sses")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _words(name: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z]+", name.lower())]


def _thing(name: str) -> str:
    """A name without its asides: "command console (dark)" is a command
    console, "medical bay - bed" a bed."""
    name = re.sub(r"\([^)]*\)", " ", name)
    return re.split(r"\s[-–—:]\s", name)[-1]


def head_noun(name: str) -> str:
    """What a thing is, from its name: the last word before the name goes on
    to say what is with it ("a kitchen table with oil lamp": table)."""
    words = _words(_thing(name))
    for i, word in enumerate(words):
        if word in _AFTER_HEAD and i > 0:
            words = words[:i]
            break
    content = [w for w in words if w not in _FILLER]
    return content[-1] if content else ""


def closest_item(name: str, slugs: Iterable[str]) -> str | None:
    """The set's thing that best stands in for one it has no picture of: the
    same kind of thing (its words hold the name's head noun) first, then the
    most words shared ("a control console bank" is the station's
    "a-bank-of-control-consoles"). None when it shares no word with any.
    A new world's furnisher names things freely, and nearly every one was a
    plain box on the play view (Oct 17 2026)."""
    head = head_noun(name)
    if not head:
        return None
    mine = set(_words(_thing(name))) - _FILLER
    best: tuple[int, int, int, str] | None = None
    for found in slugs:
        theirs = set(_words(found.replace("-", " "))) - _FILLER
        shared = len(mine & theirs)
        if not shared:
            continue
        # The same kind of thing first; else the most words in common ("a
        # wall bunk bed" sleeps in the set's bunks).
        score = (head in theirs, shared, -len(theirs), found)
        if best is None or score[:3] > best[:3]:
            best = score
    return best[3] if best is not None else None


# A building's roof by what it is made of, from its own words: the set's
# roof otherwise covered a stone watchtower in the farmhouse's shingles.
ROOF_MATERIALS: dict[str, tuple[str, ...]] = {
    "stone": ("stone", "granite", "limestone", "flint", "fieldstone", "masonry"),
    "slate": ("slate",),
    "brick": ("brick", "terracotta", "tiled", "tile"),
    "metal": ("metal", "tin", "iron", "steel", "corrugated", "copper", "brass", "sheet"),
    "thatch": ("thatch", "thatched", "straw", "reed", "turf", "sod"),
    "concrete": ("concrete", "cement", "prefab", "bunker"),
    "canvas": ("canvas", "tent", "tarp", "tarpaulin", "hide", "felt", "yurt"),
    "wood": ("wood", "wooden", "timber", "log", "plank", "shingle", "cabin"),
}
# The colour each roof is tinted towards when the set has no roof of its own
# for that material.
ROOF_TINTS: dict[str, tuple[int, int, int]] = {
    "stone": (150, 148, 140),
    "slate": (78, 86, 100),
    "brick": (168, 82, 60),
    "metal": (128, 138, 148),
    "thatch": (196, 164, 92),
    "concrete": (150, 150, 146),
    "canvas": (190, 176, 140),
    "wood": (124, 88, 58),
}


def roof_material(words: str) -> str:
    """The material a building's words say it is built of (its name, then
    what it is), "" when they say none: the set's own roof then."""
    found = set(re.findall(r"[a-z]+", words.lower()))
    for material, terms in ROOF_MATERIALS.items():
        if found.intersection(terms):
            return material
    return ""


def roof_role(material: str) -> str:
    """The texture role of a roof of `material` ("out-roof-stone")."""
    return f"out-roof-{material}" if material else "out-roof"


# The ground looks a plan or a stretch of land is drawn in (`map.layout`,
# `map.terrain`), each as a texture's words. A story keeps the tile set it
# began with (the author, Oct 17 2026: "tilesets shouldn't swap mid game";
# new pictures are drawn when a different type of area is met): ground the
# set has none of is drawn for the story in the set's style.
GROUND_WORDS: dict[str, str] = {
    "snow": "deep snow with soft drifts",
    "grass": "short grass with a few small wildflowers",
    "dirt": "bare packed earth with scattered pebbles",
    "sand": "dry sand with faint ripples",
    "rock": "bare grey rock and loose stones",
    "cobble": "old uneven cobblestones",
    "hell": "cracked black volcanic ground with thin glowing cracks",
}
# What a ground's words say it is, for a set that lists no `grounds` (a
# story's own): the first look any of its words names.
_GROUND_TERMS: dict[str, tuple[str, ...]] = {
    "snow": ("snow", "snowy", "ice", "frost", "frozen"),
    "grass": ("grass", "grassy", "meadow", "lawn", "turf", "moss", "clover"),
    "sand": ("sand", "sandy", "dune", "dunes", "beach"),
    "cobble": ("cobble", "cobbles", "cobblestone", "cobblestones", "flagstones", "paving"),
    "hell": ("lava", "volcanic", "ember", "embers", "brimstone"),
    "rock": ("rock", "rocky", "stone", "stony", "gravel", "scree", "basalt", "granite"),
    "dirt": ("dirt", "earth", "mud", "muddy", "soil", "ash", "dust", "dusty", "clay"),
}
# The colour a ground the set has no picture of yet is tinted towards: the
# set's own ground in that colour stands in until it is drawn.
GROUND_TINTS: dict[str, tuple[int, int, int]] = {
    "snow": (232, 236, 242),
    "grass": (104, 142, 74),
    "dirt": (126, 100, 74),
    "sand": (214, 192, 142),
    "rock": (132, 130, 126),
    "cobble": (120, 116, 110),
    "hell": (70, 46, 40),
}
# A deck's floor, for a set that has no deck of its own (a farm story's
# starship): drawn on the fly as the grounds are.
DECK_FLOOR_ROLE = "floor-deck"
DECK_FLOOR_WORDS = "grey metal deck plating in square panels with rivets and thin seams"


def ground_role(look: str) -> str:
    """The texture role of the outdoor ground in `look` ("out-ground-sand")."""
    return f"out-ground-{look}"


def set_grounds(info: TileSetInfo) -> set[str]:
    """The ground looks the set's own outdoor ground stands for: its listed
    `grounds`, else the first look its ground's words name; empty when they
    name none (then it stands for every look, as before)."""
    if info.grounds:
        return set(info.grounds)
    words = set(re.findall(r"[a-z]+", info.words.get("out-ground", "").lower()))
    for look, terms in _GROUND_TERMS.items():
        if words.intersection(terms):
            return {look}
    return set()


def ground_wanted(info: TileSetInfo, look: str) -> bool:
    """Whether an outdoor area in `look` wants a ground the set hasn't got:
    a ground look the program knows that the set's own doesn't stand for."""
    covered = set_grounds(info)
    return bool(covered) and look in GROUND_WORDS and look not in covered


def deck_floor_wanted(info: TileSetInfo, look: str) -> bool:
    """Whether an indoor area in `look` is a deck (engine/area.py: a plan
    standing in space) in a set with no deck. A plan's own "metal" is a
    floor the set has (the Ashen Reach's steel plates)."""
    return not info.deck and look == "space"


def own_key(title: str, taken: Iterable[str]) -> str:
    """The key a new own set is kept under: its title as a slug ("Rain-slick
    alleys" is "rain-slick-alleys"), numbered when a set has it already."""
    base = slug(title) or "own"
    taken = set(taken)
    key, count = base, 1
    while key in taken:
        count += 1
        key = f"{base}-{count}"
    return key


def choose_set(
    chosen: str,
    setting: str,
    looks: Iterable[str],
    shipped: Mapping[str, TileSetInfo],
    own: Iterable[str] = (),
) -> str:
    """The set a story's map is drawn in: the author's choice if it is still
    there (a shipped set, or one of the story's `own` sets by key), else the
    shipped set whose keywords the setting names most, a space station for a
    map whose ground is space, and the first shipped set otherwise. "" when
    nothing is shipped."""
    key = own_set_key(chosen)
    if key is not None:
        if key in set(own):
            return chosen
    elif chosen in shipped:
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
