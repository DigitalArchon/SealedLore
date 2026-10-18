"""What can be taken, moved or neither: the words a thing's name is judged
by, for the play view's menu (engine/area.py: Take is offered only for what
can be carried) and the map read (engine/world.py). Pure: names in, answers
out.

Four kinds (`thing_class`; the author, Oct 17 2026: "the moveable item
class (not just furniture, could be large boulders for example with
sufficient strength)", and furniture the program lays "no different to any
other kind, it should be movable"):

- carried: small enough to pick up and take (a rifle, a lantern, a key);
- movable: too heavy to carry off, but it can be shoved, dragged or rolled
  with enough strength (a table, a chest, an anvil, a boulder): the story
  decides whether an attempt succeeds, and the map follows it;
- fixed: part of the building or the ground (a hearth, a well, a counter, a
  tree, anything bolted down): it stays where it is unless the author's
  Direction or Narration, or a turn under Fiat, moves it;
- vehicle: driven, sailed or ridden, moved with whoever is aboard."""

from __future__ import annotations

import re
from collections.abc import Sequence

from sealedlore.engine.tile_sets import head_noun

# Things a person can carry off (and, in a plan, can't walk through).
SMALL = (
    "chair",
    "stool",
    r"lamps?\b",
    "lantern",
    "candle",
    "bucket",
    "basket",
    "bottle",
    r"books?\b",
    r"mugs?\b",
    r"cups?\b",
    r"plates?\b",
    r"box(?:es)?\b",
    "crate",
    r"sacks?\b",
    r"bags?\b",
    r"pots?\b",
    r"jars?\b",
    r"tools?\b",
    "rifle",
    r"guns?\b",
    "knife",
    "sword",
    r"bows?\b",
    r"axe?s?\b",
    r"keys?\b",
    r"maps?\b",
    "ledger",
    "bread",
    "apple",
    r"ropes?\b",
    r"bells?\b",
    "skull",
    "bones",
    "kettle",
    "radio",
    "toolbox",
    r"bowls?\b",
    r"buckets?\b",
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

# Things that are part of the building or the ground: built in, rooted,
# plumbed or bolted down. Whole words, plurals too ("well" never takes
# "wellington"). Everything else too heavy to carry is movable.
BUILT_IN = re.compile(
    r"\b(?:hearths?|fireplaces?|chimneys?|chimney ?breasts?|stoves?|ovens?|kilns?|forges?|"
    r"furnaces?|boilers?|wells?|wellheads?|fountains?|towers?|trees?|"
    r"stumps?|stairs|staircases?|stairways?|lifts?|elevators?|counters?|bar counters?|"
    r"pantry|pantries|altars?|vats?|engines?|consoles?|reactors?|statues?|monuments?|"
    r"obelisks?|pillars?|columns?|sinks?|baths?|bathtubs?|toilets?|pumps?|shrines?|"
    r"menhirs?|standing stones?|cisterns?|silos?|windmills?|waterwheels?|water wheels?|"
    r"masts?|turrets?|beacons?|hatch(?:es)?|airlocks?|bulkheads?|terminals?|lens(?:es)?|"
    r"doors?|gates?|windows?|walls?|fences?|lamp ?posts?|sign ?posts?|hitching posts?|"
    r"built-in|built in|bolted|mounted|"
    r"set into|carved into|embedded)\b"
)

# A tree by its kind is rooted, but only as what the thing is: "the old
# oak" is a tree, "a heavy oak table" a table.
TREES = frozenset(
    "oak elm pine ash birch willow yew maple beech cedar fir spruce larch rowan hawthorn "
    "chestnut sycamore poplar cypress palm baobab".split()
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


_OWNED = re.compile(r"^(?:\S+['’]s?|my|his|her|its|our|your|their)\s", re.IGNORECASE)


def the(name: str) -> str:
    """A thing's name as the storyteller and the Map page say it: "the" before
    it, but not before an owner's ("Aldous's coffee cup", not "the Aldous's
    coffee cup"), nor twice."""
    bare = re.sub(r"^(?:the|a|an)\s+", "", " ".join(name.split()), flags=re.IGNORECASE)
    return bare if _OWNED.match(bare) else f"the {bare}"


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


def thing_class(name: str) -> str:
    """ "vehicle", "carried", "fixed" or "movable" (the module's docstring):
    whether it can be taken is judged as the Take menu judges it, so the two
    never disagree."""
    if is_vehicle(name):
        return "vehicle"
    if can_be_taken(name):
        return "carried"
    if BUILT_IN.search(name.lower()) is not None:
        return "fixed"
    if head_noun(name) in TREES:
        return "fixed"
    return "movable"


def fixed_thing(name: str) -> bool:
    """A thing the map read may not move on an ordinary turn: part of the
    building or the ground (a hearth, a well, a counter, a tree, anything
    bolted down). A heavy thing that isn't (a table, an anvil, a boulder)
    is movable: the story decides whether it shifts, and the map follows."""
    return thing_class(name) == "fixed"


def movable_thing(name: str) -> bool:
    """Too heavy to carry off but not fixed: shoved, dragged or rolled."""
    return thing_class(name) == "movable"


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


def _content(name: str) -> set[str]:
    words = re.findall(r"[a-z]+", name.lower())
    return (
        {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words}
        - _NOT_NAMING
        - {w for w in words if len(w) <= 2}
    )


def same_thing(said: str, known: str) -> bool:
    """Whether a name a read gave is a shortening of a known thing's: every
    word of it that says what the thing is is in the known name ("heavy iron
    anvil" is the "heavy iron anvil block on a timber stand"; "Second
    workbench" the "Second workbench strewn with tiny brass cogs"). Never
    the other way: the "Second workbench strewn with tiny brass cogs" is not
    the map's "Workbench" (the replay, Oct 17 2026). Live, the read's short
    name made a second anvil."""
    mine, theirs = _content(said), _content(known)
    return bool(mine) and mine <= theirs
