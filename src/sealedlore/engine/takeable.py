"""What can be taken: the words a thing's name is judged by, for the play
view's menu (engine/area.py: Take is offered only for what can be) and the
map read (engine/world.py: a thing that can't be taken stays where it is
unless the author's Direction or Narration, or a turn under Fiat, moves it;
the author, Oct 17 2026). Pure: names in, answers out."""

from __future__ import annotations

import re
from collections.abc import Sequence

# Things a person can carry off (and, in a plan, can't walk through).
SMALL = (
    "chair",
    "stool",
    "lamp",
    "lantern",
    "candle",
    "bucket",
    "basket",
    "bottle",
    "book",
    "mug",
    "cup",
    "plate",
    "box",
    "crate",
    "sack",
    "bag",
    "pot",
    "jar",
    "tool",
    "rifle",
    "gun",
    "knife",
    "sword",
    "bow",
    "axe",
    "key",
    "map",
    "ledger",
    "bread",
    "apple",
    "rope",
    "bell",
    "skull",
    "bones",
    "kettle",
    "radio",
    # Small things the map's reads name (Oct 17 2026: an amulet was "use,
    # destroy" and so, under the map read's rule, fixed). Each is matched at a
    # word's start; a short one carries its own end ("tin" took "tinted").
    "amulet",
    "pendant",
    "locket",
    "necklace",
    r"rings?\b",
    "coin",
    "letter",
    "scroll",
    "gem",
    "jewel",
    "potion",
    "vial",
    "flask",
    "pistol",
    "revolver",
    "dagger",
    "wand",
    "compass",
    "pouch",
    "purse",
    "wallet",
    r"cards?\b",
    "keycard",
    "datapad",
    "phone",
    "medallion",
    "idol",
    "figurine",
    "ammo",
    "bandage",
    "medkit",
    r"tins?\b",
    "journal",
    "diary",
    "notebook",
    "photo",
)
BIG = (
    "chest",
    "crate",
    "trunk",
    "cupboard",
    "table",
    "bed",
    "bunk",
    "stove",
    "counter",
    "shelf",
    "shelves",
    "cabinet",
    "wardrobe",
    "altar",
    "throne",
    "hearth",
    "workbench",
    "bench",
    "desk",
    "piano",
    "truck",
    "cart",
    r"cars?\b",  # a word's start alone took "card" and "carpet"
    "tractor",
    "engine",
    "boiler",
    "furnace",
    "vat",
    "tower",
    "well",
    "statue",
    "fountain",
    "tree",
    "oak",
    "haystack",
    "barricade",
    "console",
    "locker",
    "sofa",
    "couch",
    "cupboard",
    "pantry",
    "dresser",
    "stairs",
    "staircase",
    "lift",
    "loader",
    "stall",
)
FOOD = (
    "bread",
    "apple",
    "stew",
    "soup",
    "meat",
    "cheese",
    "fruit",
    "pie",
    "cake",
    "food",
    "meal",
    "berries",
    "fish",
    "ham",
    "biscuit",
    "ration",
    "canned goods",
    "vegetables",
    "potato",
    "egg",
    "honey",
    "jerky",
    "ale",
    "wine",
    "cider",
    "water bucket",
)

# Things that go when someone drives, sails or rides them: they can't be
# taken, but the map read may move them with whoever is aboard, as a
# vehicle place goes where the passage docks it. Whole words only ("car"
# would take "carpet").
VEHICLES = re.compile(
    r"\b(?:truck|pickup|cart|car|van|jeep|wagon|carriage|tractor|bus|"
    r"bikes?|bicycle|motorbike|motorcycle|sled|sledge|sleigh|boat|ship|skiff|"
    r"canoe|raft|barge|shuttle|horse|mule|pony|camel)s?\b"
)


def _matches(words: Sequence[str], lowered: str) -> bool:
    return any(re.search(rf"\b{w}", lowered) for w in words)


def object_verbs(name: str) -> tuple[str, ...]:
    """What a thing's name suggests: food is eaten, small things taken,
    anything used or destroyed."""
    lowered = name.lower()
    verbs = ["use"]
    small = _matches(SMALL, lowered)
    big = _matches(BIG, lowered)
    if any(word in lowered for word in FOOD):
        verbs = ["eat", "take", "use"] if not big else ["eat", "use"]
    elif small and not big:
        verbs.append("take")
    verbs.append("destroy")
    return tuple(dict.fromkeys(verbs))


def item_verbs(name: str, planned: Sequence[str] = ()) -> tuple[str, ...]:
    """What can be done with a thing: what its plan says, else what its name
    suggests (food is eaten, small things taken, anything used or
    destroyed), in the menu's order."""
    order = ("use", "take", "eat", "destroy")
    chosen = set(planned) | set(object_verbs(name)) if planned else set(object_verbs(name))
    if planned and any(word in name.lower() for word in BIG):
        chosen.discard("take")
    return tuple(v for v in order if v in chosen)


def can_be_taken(name: str) -> bool:
    """Whether the play view would offer Take for a thing of this name."""
    return "take" in item_verbs(name)


def is_vehicle(name: str) -> bool:
    return VEHICLES.search(name.lower()) is not None


def fixed_thing(name: str) -> bool:
    """A thing the map read may not move on an ordinary turn: one that can't
    be taken (the town's stores, a statue, a well) and isn't driven or
    ridden. Judged as the Take menu judges it, so the two never disagree."""
    return not can_be_taken(name) and not is_vehicle(name)


# How the storyteller is shown a thing that can't be taken (or driven) on a
# turn that may not move it: beside it in "Within reach" and "Things here",
# with `map.fixed` once under the positions.
FIXED_MARK = "fixed in place"

# Words that say what a thing is made of or like, not what it is: an author
# writing "the iron bar" isn't naming "a heavy iron anvil".
_NOT_NAMING = frozenset(
    "a an the of with and on in for to by at its his her their old small large big little "
    "some few long short tall low high round square two three set pair heavy light wooden wood "
    "iron steel stone brass copper metal rusted rusty rough worn battered broken new dark "
    "black white red green blue grey gray brown oak pine timber end side top bottom front back "
    "full empty open closed".split()
)


def named_in(name: str, text: str) -> int:
    """How many words of the thing's name that say what it is `text` names
    ("I shove the bench" names "rough wooden bench" once); 0 for none."""
    words = {w for w in re.findall(r"[a-z]+", name.lower()) if w not in _NOT_NAMING and len(w) > 2}
    said = text.lower()
    return sum(1 for w in words if re.search(rf"\b{re.escape(w)}(?:e?s)?\b", said))
