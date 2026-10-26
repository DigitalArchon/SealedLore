"""A visual story's rules, kept by the program (the author, Oct 24 2026:
combat, possession of items, broken things, line of sight, hearing "will
all be programmatic, not decided by the AI model"). Pure: nothing here
knows the session or Qt.

- Kinds of thing (`kind_of`): what a thing is from its name, so the rules
  know a rifle from a tin of beans: what can be eaten, what is a weapon and
  how much it adds, what opens a lock.
- Combat (`fight`): the author's ruling for now, one combat ability a side,
  the higher wins; a tie goes to the defender. A loss is a hit taken.
- The dead (`step_creatures`): a state machine with no model behind it:
  wander, drawn to a noise, chase on sight, attack in reach. They move when
  the player's time moves (each action's seconds), at a shamble.
- Doors (`door_key`, `can_open`): locked, opened by a key the player
  carries, forced with a strength check that makes a noise, sealed by the
  plot until it opens them.
- Reports (`sight_report`): who and what the player can newly see or hear.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from sealedlore.engine.area import Area, bare, in_earshot, path, sees, stand_near
from sealedlore.models.area import ThingRecord
from sealedlore.models.visual import Creature, DoorState

# --- kinds -----------------------------------------------------------------------

# Word lists by kind: a whole word of the thing's name (plurals too).
_KIND_WORDS: tuple[tuple[str, str], ...] = (
    (
        "weapon",
        r"rifles?|shotguns?|pistols?|revolvers?|guns?|carbines?|axes?|hatchets?|"
        r"machetes?|knives|knife|daggers?|swords?|clubs?|bats?|crowbars?|spears?|bows?|"
        r"pipes?|hammers?|cleavers?|blades?",
    ),
    ("ammunition", r"ammunition|ammo|cartridges?|shells?|rounds?|bullets?|magazines?"),
    (
        "medicine",
        r"medicines?|first.?aid|bandages?|pills?|antibiotics|painkillers?|"
        r"morphine|salves?|ointments?|tourniquets?|splints?|medical kits?|aid kits?",
    ),
    (
        "drink",
        r"water|bottles? of water|canteens?|flasks?|juice|milk|coffee|tea|beer|ale|"
        r"wine|cider|whisky|whiskey|rum|vodka|brandy",
    ),
    (
        "food",
        r"tins?|cans?|beans|soup|stew|bread|meat|jerky|rations?|biscuits?|crackers?|"
        r"cheese|apples?|fruit|fish|ham|eggs?|potato(?:es)?|rice|pasta|oats|chocolate|"
        r"honey|food|meals?|berries|vegetables?|canned goods|preserves|jam|nuts|sugar|flour",
    ),
    ("key", r"keys?|keycards?|passcards?|keyrings?"),
    (
        "light",
        r"torch(?:es)?|flashlights?|lanterns?|lamps?|candles?|matches|matchbox(?:es)?|"
        r"lighters?|flares?|glow ?sticks?",
    ),
    (
        "fuel",
        r"fuel|petrol|gasoline|gas cans?|jerry ?cans?|diesel|kerosene|propane|"
        r"firewood|logs|coal|charcoal|oil",
    ),
    (
        "clothing",
        r"coats?|jackets?|boots?|gloves?|hats?|scarf|scarves|shirts?|trousers|"
        r"pants|jeans|socks|sweaters?|jumpers?|cloaks?|parkas?|ponchos?|clothes|clothing",
    ),
    (
        "tool",
        r"tools?|rope|wire|tape|screwdrivers?|wrench(?:es)?|spanners?|pliers|saws?|"
        r"shovels?|spades?|picks?|pickaxes?|binoculars|compass(?:es)?|radios?|batteries|"
        r"battery|lockpicks?|cans? opener|can opener|needle|thread|fishing line|hooks?",
    ),
    (
        "paper",
        r"notes?|letters?|maps?|books?|journals?|diar(?:y|ies)|papers?|photographs?|"
        r"photos?|documents?|manuals?|pamphlets?|ledgers?",
    ),
    (
        "container",
        r"sacks?|bags?|backpacks?|rucksacks?|boxes|box|crates?|chests?|trunks?|"
        r"cabinets?|cupboards?|drawers?|lockers?|barrels?|bins?|baskets?|cases?|pouches?|"
        r"satchels?|footlockers?|safes?|wardrobes?|dressers?|shelves|shelf",
    ),
    (
        "vehicle",
        r"trucks?|pickups?|cars?|jeeps?|vans?|motorbikes?|motorcycles?|bikes?|bicycles?|"
        r"snowmobiles?|tractors?|buses|bus|lorr(?:y|ies)|boats?|canoes?|sleds?|sledges?",
    ),
)
_KINDS = [(kind, re.compile(rf"\b(?:{words})\b", re.IGNORECASE)) for kind, words in _KIND_WORDS]

# What a weapon adds to the combat ability, by its words.
_WEAPON_BONUS: tuple[tuple[str, int], ...] = (
    (r"\b(?:rifles?|shotguns?|carbines?)\b", 4),
    (r"\b(?:pistols?|revolvers?|guns?)\b", 3),
    (r"\b(?:axes?|machetes?|swords?|spears?|bows?|cleavers?)\b", 2),
    (r"\b(?:knives|knife|daggers?|hatchets?|crowbars?|clubs?|bats?|pipes?|hammers?|blades?)\b", 1),
)
_WEAPON_BONUSES = [(re.compile(w, re.IGNORECASE), bonus) for w, bonus in _WEAPON_BONUS]
# A gun makes a noise the dead hear this far.
_LOUD = re.compile(r"\b(?:rifles?|shotguns?|carbines?|pistols?|revolvers?|guns?)\b", re.IGNORECASE)
GUN_NOISE_M = 300.0
FORCE_NOISE_M = 60.0

# The dead: how fast they shamble, how far they see and hear, and how far
# a chase goes on once the player is out of sight.
DEAD_M_PER_S = 0.6
DEAD_SIGHT_M = 40.0
DEAD_HEARING_M = 25.0
# How far the player hears one of the dead they can't see.
HEARD_M = 30.0
# How far the player sees a person, in the open (live, a watchman greeted
# them from the far gate, 500 m up the street).
PERSON_SIGHT_M = 60.0
# The walker picture a world ships of its dead (`MapPicture.subject_id`).
DEAD_SPRITE = "the dead"
CHASE_GIVES_UP = 6
# A strength check to force a lock: ability against this.
FORCE_DIFFICULTY = 6
# A vehicle the keeper has made to work (`ThingRecord.uses` has "drive")
# travels at this speed, against walking's 1.4 m/s.
DRIVE_M_PER_S = 12.0
# Across a visual story's overmap: a working vehicle's full tank, and what a
# can of fuel carried puts back in it, in km.
FULL_TANK_KM = 150.0
FUEL_CAN_KM = 50.0
# A person tagged with any of these by the keeper (`AreaState.person_tags`)
# stays where they are and carries out no intent.
OUT_WORDS = (
    r"unconscious|out cold|knocked out|knocked cold|senseless|passed out|asleep|sleeping|"
    r"dead|dying|comatose"
)
HELD_WORDS = re.compile(
    r"\b(?:held|bound|tied|captive|prisoner|restrained|subdued|pinned|locked up|chained|"
    + OUT_WORDS
    + r")\b",
    re.IGNORECASE,
)
OUT = re.compile(rf"\b(?:{OUT_WORDS})\b", re.IGNORECASE)


def is_held(tags: Iterable[str]) -> bool:
    """Whether a person's state words keep them where they are."""
    return any(HELD_WORDS.search(tag) for tag in tags)


def is_out(tags: Iterable[str]) -> bool:
    """Whether a person's state words leave them unable to answer."""
    return any(OUT.search(tag) for tag in tags)


def drivable(record: ThingRecord) -> bool:
    """A thing the keeper has made drivable: a vehicle that works."""
    kind = record.kind or kind_of(record.name)
    uses = {u.lower() for u in record.uses}
    tags = " ".join(record.tags).lower()
    works = re.search(r"\b(?:works|running|repaired|fuelled|fueled)\b", tags) is not None
    return "drive" in uses or (kind == "vehicle" and works)


def kind_of(name: str, hint: str = "") -> str:
    """What kind of thing a name is (`models/visual.Kind`): the hint when
    it names a kind, else the first kind whose words the name has, else
    "thing"."""
    hint = hint.strip().lower()
    if hint in {k for k, _ in _KIND_WORDS} | {"thing"}:
        return hint
    for kind, pattern in _KINDS:
        if pattern.search(name):
            return kind
    return "thing"


def weapon_bonus(name: str) -> int:
    for pattern, bonus in _WEAPON_BONUSES:
        if pattern.search(name):
            return bonus
    return 0


def is_loud(name: str) -> bool:
    return bool(_LOUD.search(name))


# Clothing that keeps out the cold (the overmap's warmth, engine/overmap.py):
# a coat, boots, gloves, a blanket… each counts once, by kind of garment.
_WARM = re.compile(
    r"\b(?:(?:winter |wool |down |fur |sheepskin |thermal )?(?:coats?|jackets?|parkas?|"
    r"anoraks?|greatcoats?)|(?:winter |snow |work )?boots|(?:work |wool |leather )?gloves|"
    r"mittens|(?:wool |knit |fur )?(?:hats?|caps?)|scarf|scarves|(?:wool )?sweaters?|"
    r"jumpers?|blankets?|(?:survival )?blankets?|sleeping bags?|bedrolls?|"
    r"thermal underwear|long johns)\b",
    re.IGNORECASE,
)


def warm_garment(name: str) -> str:
    """Which warm garment a thing is ("coat", "boots"…), or "" for none:
    two coats keep out no more cold than one."""
    found = _WARM.search(name)
    if found is None:
        return ""
    word = found.group(0).lower().split()[-1]
    return {
        "jackets": "coat",
        "jacket": "coat",
        "parka": "coat",
        "parkas": "coat",
        "anorak": "coat",
        "greatcoat": "coat",
        "coats": "coat",
        "mittens": "gloves",
        "cap": "hat",
        "caps": "hat",
        "hats": "hat",
        "scarves": "scarf",
        "jumper": "sweater",
        "jumpers": "sweater",
        "sweaters": "sweater",
        "blankets": "blanket",
        "bag": "blanket",
        "bags": "blanket",
        "bedroll": "blanket",
        "bedrolls": "blanket",
        "underwear": "underwear",
        "johns": "underwear",
    }.get(word, word)


def warmth(carried: Iterable[ThingRecord]) -> int:
    """How many kinds of warm garment are carried."""
    return len({g for r in carried if (g := warm_garment(r.name))})


def best_weapon(carried: Iterable[ThingRecord]) -> tuple[str, int]:
    """The carried weapon that adds most: (its name, the bonus); ("", 0)."""
    best = ("", 0)
    for record in carried:
        bonus = weapon_bonus(record.name)
        if bonus > best[1]:
            best = (record.name, bonus)
    return best


def item_actions(kind: str) -> tuple[str, ...]:
    """What the inventory offers for a kind: eat, drink, use (light,
    medicine, paper read), drop always."""
    if kind == "food":
        return ("eat", "drop")
    if kind == "drink":
        return ("drink", "drop")
    if kind in ("medicine", "light", "paper", "tool"):
        return ("use", "drop")
    if kind == "vehicle":
        return ()  # driven from its own menu, never carried
    return ("drop",)


# --- combat ---------------------------------------------------------------------


@dataclass(frozen=True)
class Fight:
    won: bool
    mine: int
    theirs: int
    weapon: str
    words: str


def fight(ability: int, weapon: tuple[str, int], creature: Creature, *, attacking: bool) -> Fight:
    """The author's simple combat: one number a side, the higher wins; a
    tie goes to whoever was attacked."""
    mine = ability + weapon[1]
    theirs = creature.ability
    won = mine > theirs if attacking else mine >= theirs
    with_what = f" with the {bare(weapon[0])}" if weapon[0] else " bare-handed"
    if won:
        words = f"You put {creature.name} down{with_what} ({mine} against {theirs})."
    else:
        words = (
            f"{creature.name[0].upper()}{creature.name[1:]} gets the better of you"
            f"{with_what} ({mine} against {theirs}): you are hurt."
        )
    return Fight(won=won, mine=mine, theirs=theirs, weapon=weapon[0], words=words)


# --- the dead -------------------------------------------------------------------


def _metres(area: Area, a: tuple[int, int], b: tuple[int, int]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1]) * area.tile_m


def adjacent(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1])) <= 1


def step_creatures(
    area: Area,
    creatures: Mapping[str, Creature],
    player: tuple[int, int],
    seconds: int,
    *,
    rng: random.Random | None = None,
    noise: tuple[int, int] | None = None,
) -> list[str]:
    """Move the dead in this area through `seconds` of the player's time,
    in place: one not down wanders, is drawn to a noise, or chases the
    player on sight; it stops a tile short, where it attacks (the session
    resolves the fight). The ids of those now beside the player, nearest
    first."""
    rng = rng or random.Random(seconds)
    tiles = max(0, int(seconds * DEAD_M_PER_S / max(area.tile_m, 0.1)))
    taken = {(c.x, c.y) for c in creatures.values() if c.area == area.id and c.state != "down"}
    beside: list[tuple[float, str]] = []
    for creature in creatures.values():
        if creature.area != area.id or creature.state == "down":
            continue
        here = (creature.x, creature.y)
        if noise is not None and _metres(area, here, noise) <= DEAD_HEARING_M:
            creature.state, creature.goal_x, creature.goal_y = "drawn", noise[0], noise[1]
        if _metres(area, here, player) <= DEAD_SIGHT_M and sees(area, here, player):
            creature.state, creature.goal_x, creature.goal_y = "chase", player[0], player[1]
        for _ in range(tiles):
            here = (creature.x, creature.y)
            if creature.state == "chase" and adjacent(here, player):
                break
            goal = (
                (creature.goal_x, creature.goal_y)
                if creature.goal_x is not None and creature.goal_y is not None
                else None
            )
            if goal is not None and goal != here:
                route = path(area, here, goal, blocked=taken - {here})
                step = route[1] if len(route) > 1 else None
            else:
                step = None
                if creature.state in ("drawn", "chase"):
                    creature.state = "wander"  # got there, or lost it
                    creature.goal_x = creature.goal_y = None
            if step is None:
                # A shuffle to a tile beside, now and then.
                if rng.random() < 0.5:
                    options = [
                        (here[0] + dx, here[1] + dy)
                        for dx in (-1, 0, 1)
                        for dy in (-1, 0, 1)
                        if (dx or dy)
                        and area.walkable(here[0] + dx, here[1] + dy)
                        and (here[0] + dx, here[1] + dy) not in taken
                        and (here[0] + dx, here[1] + dy) != player
                    ]
                    step = rng.choice(options) if options else None
            if step is None or step == player:
                break
            taken.discard(here)
            creature.x, creature.y = step
            taken.add(step)
            if creature.state == "chase" and not sees(area, step, player):
                creature.goal_x, creature.goal_y = player
        if creature.state != "down" and adjacent((creature.x, creature.y), player):
            beside.append((_metres(area, (creature.x, creature.y), player), creature.id))
    return [cid for _d, cid in sorted(beside)]


def spawn_creatures(
    area: Area, kind: str, count: int, ability: int, taken: set[tuple[int, int]], seed: str
) -> list[Creature]:
    """`count` of the dead stood apart on the area's walkable tiles, away
    from the edges, never on anyone."""
    rng = random.Random(seed)
    free = [
        (x, y)
        for y in range(2, area.height - 2)
        for x in range(2, area.width - 2)
        if area.walkable(x, y) and (x, y) not in taken
    ]
    out: list[Creature] = []
    for _ in range(count):
        if not free:
            break
        x, y = rng.choice(free)
        x, y = stand_near(area, (x, y), taken)
        taken.add((x, y))
        free = [t for t in free if max(abs(t[0] - x), abs(t[1] - y)) > 3]
        out.append(
            Creature(
                kind=kind,
                name=f"one of {kind}" if kind.startswith("the") else kind,
                area=area.id,
                x=x,
                y=y,
                ability=ability,
            )
        )
    return out


# --- doors ----------------------------------------------------------------------


def door_id(area_id: str, x: int, y: int) -> str:
    return f"{area_id}:{x},{y}"


def opens_with(key: str, carried: Iterable[ThingRecord]) -> ThingRecord | None:
    """The carried thing that opens a lock wanting `key`: one whose name
    has the key's words (a "barn key" for "barn key"; any key for "key")."""
    want = {w for w in re.findall(r"[a-z]+", key.lower()) if w not in ("the", "a", "an", "of")}
    for record in carried:
        words = set(re.findall(r"[a-z]+", record.name.lower()))
        if want and want <= words:
            return record
    return None


def can_open(door: DoorState | None, carried: Iterable[ThingRecord]) -> tuple[bool, str]:
    """Whether a door opens: (yes, why not)."""
    if door is None or not door.locked or door.forced:
        return True, ""
    if door.sealed:
        return False, "It won't open: something holds it fast."
    if door.key and opens_with(door.key, carried) is not None:
        return True, ""
    return False, "Locked." + (f" It wants a {door.key}." if door.key else "")


# --- reports --------------------------------------------------------------------


def sight_report(
    area: Area,
    me: tuple[int, int],
    people: Mapping[str, tuple[int, int]],
    creatures: Sequence[Creature],
    seen_before: Sequence[str],
) -> tuple[list[str], list[str]]:
    """What the player newly sees or hears: lines for the log, and the
    names now in sight or earshot (so the next report is of the change)."""
    now: list[str] = []
    lines: list[str] = []
    for name, spot in people.items():
        if spot == me:
            continue
        if _metres(area, me, spot) <= PERSON_SIGHT_M and sees(area, me, spot):
            now.append(name)
            if name not in seen_before:
                lines.append(f"{name} is here, {_whereabouts(area, me, spot)}.")
        elif in_earshot(area, me, spot):
            now.append(name)
            if name not in seen_before:
                lines.append(f"You can hear {name}, {_whereabouts(area, me, spot)}, out of sight.")
    for creature in creatures:
        if creature.state == "down" or creature.area != area.id:
            continue
        spot = (creature.x, creature.y)
        if _metres(area, me, spot) <= DEAD_SIGHT_M and sees(area, me, spot):
            now.append(creature.id)
            if creature.id not in seen_before:
                lines.append(
                    f"{creature.name[0].upper()}{creature.name[1:]}, "
                    f"{_whereabouts(area, me, spot)}. It has seen you."
                    if creature.state == "chase"
                    else f"{creature.name[0].upper()}{creature.name[1:]}, "
                    f"{_whereabouts(area, me, spot)}, shambling."
                )
        elif _metres(area, me, spot) <= HEARD_M:
            # Out of sight but near: a sound cue, once (step 5 of the design:
            # what the player can hear is reported as sound).
            heard = f"heard:{creature.id}"
            now.append(heard)
            if heard not in seen_before and creature.id not in seen_before:
                over = "over the floor" if area.indoor else "through the snow"
                lines.append(
                    f"You hear something dragging itself {over}, "
                    f"{_whereabouts(area, me, spot)}, out of sight."
                )
    return lines, now


def _whereabouts(area: Area, me: tuple[int, int], spot: tuple[int, int]) -> str:
    metres = _metres(area, me, spot)
    dx, dy = spot[0] - me[0], spot[1] - me[1]
    if abs(dx) >= abs(dy):
        way = "east" if dx > 0 else "west"
    else:
        way = "south" if dy > 0 else "north"
    if metres < 3:
        return "right beside you"
    return f"about {round(metres)} m {way}"
