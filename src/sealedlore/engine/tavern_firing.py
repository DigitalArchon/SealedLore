"""Lore fired exactly as SillyTavern fires it (the author, Nov 20 2026: "make
it possible to support using a SillyTavern lorebook exactly as presented, but
only optional"): a story's choice (Lore page, Fire entries), its entries
chosen per turn by SillyTavern's own rules, from the settings an entry
brought with it (`LoreEntry.tavern`), and placed SealedLore's way.

What SillyTavern 1.19.1 does (public/scripts/world-info.js, `checkWorldInfo`
and the classes it uses), behaviour for behaviour:

- The scan reads the last `scan_depth` messages, newest first, each as
  "name: text" when `include_names` is on, joined with a separator no key
  can match across; an entry's own scan depth wins. Keys are matched
  without case (unless the entry or the settings say otherwise), as whole
  words when asked (a key of several words then as a substring), or as a
  pattern when written /like this/flags.
- Entries are taken in `order`, highest first. One fires when it is
  constant, still sticky, marked @@activate, or a primary key matches and
  its secondary keys pass its logic (and any, not all, not any, and all).
  Disabled, triggers that don't include this kind of turn, a character
  filter, a delay not yet reached, a cooldown while not sticky, and the
  recursion flags keep one from firing.
- Fired in one pass, sticky ones first, then by order: inclusion groups
  leave one winner each (a sticky member; else by key score when group
  scoring is on; else the one marked to win, highest order; else a roll by
  weight), a probability is rolled (not for a sticky one), and the budget
  (`budget_percent` of the context, at most `budget_cap`) counts the text
  of what fired before and in this pass, an entry that would overflow it
  left out unless it ignores the budget.
- What fired is read again (recursion), unless an entry prevents it, until
  nothing new fires, the steps run out (`max_recursion_steps`), or the
  budget overflowed; then entries delayed until a recursion level, one
  level at a time; and while fewer than `min_activations` fired, the scan
  reaches back one more message, up to `min_activations_depth_max`.
- Sticky keeps an entry firing for its number of messages; cooldown keeps it
  from firing again for its number; a sticky one ending starts its cooldown;
  delay keeps it from firing before the chat has that many messages. Counted
  in messages, as SillyTavern counts them.

SealedLore's ways, where a story differs from a chat:

- Placement is SealedLore's, so caching holds (the author's choice): a
  constant entry nothing can stop (no probability, timing, group, trigger or
  character filter) stands in the cached system block with the always-on
  entries; every other entry fired goes with the turn, in SillyTavern's
  order (lowest `order` first, as it builds the prompt). Positions, depths
  and outlets are not used: SealedLore has one place for lore.
- Timed effects are kept on the path, not in a chat's metadata: each author
  turn records what fired and the chat's length then (`NodeMeta`), and the
  state is replayed along the path, so every branch, a take and a turn
  taken back have their own.
- An entry written in SealedLore fires by its keywords at order 100, always
  on as constant: SillyTavern's defaults for a new entry.
- The character filter is read against the people in the scene (SillyTavern
  reads it against the one character a chat is with).
- One quirk is not kept: SillyTavern removes a group's loser with
  `splice(indexOf(...))`, and a loser already removed by another group of
  the same entry took the pass's last entry with it.

Pure: entries, messages, settings and a random source in; what fired, and
why each did or didn't, out.
"""

from __future__ import annotations

import math
import random
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

from sealedlore.storage.tavern_lore import is_pattern

# SillyTavern's own numbers.
MAX_SCAN_DEPTH = 1000
DEFAULT_WEIGHT = 100
DEFAULT_ORDER = 100
# The separator between scanned messages: matched by no key.
MATCHER = "\x01"
JOINER = "\n" + MATCHER
# Secondary keys' logic (`world_info_logic`).
AND_ANY, NOT_ALL, NOT_ANY, AND_ALL = 0, 1, 2, 3

_PATTERN = re.compile(r"^/([\s\S]+?)/([gimsuy]*)$")

# A pattern is a stranger's: lorebooks, cards and scenario files come from
# anyone, and Python's `re` has no time limit and holds the interpreter while
# it matches (Nov 2026: /(\w+\s?)+$/ took 4.5 s on eight words and eight
# times that for each word more, the window frozen, and again every time the
# story was opened). So each pattern is read before it is run (`_screened`):
# one that can take exponential time is never run, and every other reads
# only as much of what is scanned (the newest of it) as one search can get
# through in about a second on text made to be slow for it (`_search_cost`,
# SEARCH_BUDGET). A screen, not a proof: the time all of a turn's patterns
# take is measured too (PATTERN_SECONDS), and past it no more are run that
# turn.
PATTERN_MAX_CHARS = 1000
# A repeat of more than this many times is open-ended.
OPEN_REPEAT = 16
# The most repeats of something that itself varies (a repeat inside, or
# alternatives): more is exponential on the wrong text.
VARIED_REPEAT = 4
# The most characters a pattern reads, by its open-ended repeats that have
# something after them (a repeat that ends the pattern never backtracks):
# each makes a search one power slower in the text's length. What follows
# the repeats is tried at every place they could stop, so the more there is
# of it the less is read (SEARCH_BUDGET): these are for a word or so.
# Measured at them on text made to be slow, at the slowest: one repeat
# 0.7 s, two 0.8 s, three 0.5 s (two read 1,000 until Nov 22 2026: 2.2 s
# when both were lazy and a class followed).
SCAN_CAPS = {1: 8000, 2: 750, 3: 150}
# The most ways a pattern's optional parts and alternatives can be taken.
WAYS_MOST = 4096
# What one search may cost at the most, as `_search_cost` reckons it for the
# text slowest for the pattern: billionths of a second on the computer the
# costs under it were measured on, so about a second. A pattern reads as
# many characters as keep it under this, at most SCAN_CAPS' or SCAN_MOST.
# Nov 22 2026: of 8,800 patterns made at random from every piece the screen
# knows, the 6,200 it lets run were each run at their cap on a dozen texts
# made to be slow for them and on the samples' prose. The slowest search
# took 0.8 s (made text) and 0.8 s (prose), and none more than nine tenths
# of its reckoning; half took under a hundredth of a second. Before this,
# an open-ended repeat read its 8,000 characters whatever came after it
# (/[^§]*/ before 400 counted characters: 12 s on 8,000 characters of a
# sample's own prose), and counts inside counts were taken for open-ended
# repeats (/(?:\w{1,10} ){0,2}farmhouse/ read 118 characters).
SEARCH_BUDGET = 1_000_000_000
# The most characters any pattern reads, whatever the screen makes of it:
# more than a story scans (its newest messages and the lore that fired), and
# a few thousandths of a second for a pattern that reads straight through.
SCAN_MOST = 500_000
# A pattern that could read fewer than this is never run: it would say
# nothing of a story, and the reckoning is at its roughest on short text.
SCAN_LEAST = 100
PATTERN_SECONDS = 2.0
OUT_OF_TIME = "this turn's patterns had taken too long by then"


@dataclass(frozen=True)
class TavernSettings:
    """SillyTavern's World Info settings (its defaults: default/content/settings.json)."""

    scan_depth: int = 2
    budget_percent: int = 25
    budget_cap: int = 0
    min_activations: int = 0
    min_activations_depth_max: int = 0
    max_recursion_steps: int = 0
    recursive: bool = True
    case_sensitive: bool = False
    match_whole_words: bool = True
    use_group_scoring: bool = False
    include_names: bool = True


@dataclass(frozen=True)
class FireEntry:
    """One entry as SillyTavern reads it."""

    id: str
    title: str
    content: str
    keys: tuple[str, ...] = ()
    secondary: tuple[str, ...] = ()
    selective: bool = True
    logic: int = AND_ANY
    constant: bool = False
    disabled: bool = False
    order: int = DEFAULT_ORDER
    probability: float = 100
    use_probability: bool = True
    groups: tuple[str, ...] = ()
    group_text: str = ""
    group_override: bool = False
    group_weight: float = DEFAULT_WEIGHT
    use_group_scoring: bool | None = None
    scan_depth: int | None = None
    case_sensitive: bool | None = None
    match_whole_words: bool | None = None
    exclude_recursion: bool = False
    prevent_recursion: bool = False
    delay_until_recursion: int = 0
    sticky: int = 0
    cooldown: int = 0
    delay: int = 0
    triggers: tuple[str, ...] = ()
    ignore_budget: bool = False
    decorators: tuple[str, ...] = ()
    filter_names: tuple[str, ...] = ()
    filter_exclude: bool = False

    @property
    def standing(self) -> bool:
        """Fires every turn whatever happens: for the cached system block."""
        return (
            not self.disabled
            and (self.constant or "@@activate" in self.decorators)
            and "@@dont_activate" not in self.decorators
            and (not self.use_probability or self.probability >= 100)
            and not (self.sticky or self.cooldown or self.delay)
            and not self.groups
            and not self.triggers
            and not self.filter_names
            and not self.delay_until_recursion
        )


@dataclass
class Timed:
    """Sticky and cooldown, as SillyTavern keeps them in a chat's metadata."""

    sticky: dict[str, tuple[int, int, bool]] = field(default_factory=dict)
    cooldown: dict[str, tuple[int, int, bool]] = field(default_factory=dict)


@dataclass(frozen=True)
class Fired:
    """A turn's firing: what went in, SillyTavern's order, and why."""

    fired: tuple[FireEntry, ...] = ()
    why: Mapping[str, str] = field(default_factory=dict)
    not_fired: Mapping[str, str] = field(default_factory=dict)
    budget: int = 0
    used: int = 0

    @property
    def ids(self) -> list[str]:
        return [entry.id for entry in self.fired]


# --- reading an entry ---------------------------------------------------------


def _get(raw: Mapping[str, Any], numbered: bool, world: str, card: str, default: Any) -> Any:
    """A field under SillyTavern's own name (`world`) in its world files, or
    the card's (`card`, in the entry's `extensions`), as SillyTavern's
    convertCharacterBook reads a card's book."""
    if numbered and world in raw:
        return raw[world]
    extensions = raw.get("extensions") if isinstance(raw.get("extensions"), Mapping) else {}
    if card in extensions:
        return extensions[card]
    if world in raw:
        return raw[world]
    return default


# An entry's own SillyTavern settings a review reads and may change
# (engine/authoring.py), by SealedLore's names: its name in a world file, in a
# card's book, whether a card keeps it at the entry's top level (else in its
# extensions, where SillyTavern reads a card's), and its default.
ENTRY_SETTINGS: dict[str, tuple[str, str, bool, Any]] = {
    "secondary_keywords": ("keysecondary", "secondary_keys", True, []),
    "secondary_logic": ("selectiveLogic", "selectiveLogic", False, "and any"),
    "order": ("order", "insertion_order", True, DEFAULT_ORDER),
    "probability": ("probability", "probability", False, 100),
    "sticky": ("sticky", "sticky", False, 0),
    "cooldown": ("cooldown", "cooldown", False, 0),
    "delay": ("delay", "delay", False, 0),
    "group": ("group", "group", False, ""),
}
# `selectiveLogic` by number, as SillyTavern stores it.
LOGIC_NAMES = ("and any", "not all", "not any", "and all")


def entry_settings(raw: Mapping[str, Any], numbered: bool) -> dict[str, Any]:
    """An entry's `ENTRY_SETTINGS` as `fire_entry` reads them."""
    read = fire_entry(
        id="",
        title="",
        content="",
        keywords=(),
        always_on=False,
        enabled=True,
        raw={**raw, "key": [], "keys": []},
        numbered=numbered,
    )
    logic = read.logic if 0 <= read.logic < len(LOGIC_NAMES) else AND_ANY
    return {
        "secondary_keywords": list(read.secondary),
        "secondary_logic": LOGIC_NAMES[logic],
        "order": read.order,
        "probability": read.probability if read.use_probability else 100,
        "sticky": read.sticky,
        "cooldown": read.cooldown,
        "delay": read.delay,
        "group": read.group_text,
    }


def set_entry_setting(raw: dict[str, Any], numbered: bool, name: str, value: Any) -> None:
    """One of `ENTRY_SETTINGS`, already checked, written where SillyTavern
    (and `fire_entry`) reads it."""
    world, card, top, _default = ENTRY_SETTINGS[name]
    if name == "secondary_logic":
        value = LOGIC_NAMES.index(value)
    if numbered or top:
        if not numbered and world in raw and card not in raw and name != "order":
            # A card entry written with the world file's name. Not its order:
            # a card's is read from `insertion_order` alone (as SillyTavern
            # reads a card's book), so written to `order` it changed nothing.
            field_name = world
        else:
            field_name = world if numbered else card
        raw[field_name] = value
    else:
        extensions = raw.get("extensions")
        if not isinstance(extensions, dict):
            extensions = raw["extensions"] = {}
        extensions[card] = value
        if world in raw and world != card:
            raw[world] = value
    if name == "secondary_keywords" and value:
        raw["selective"] = True
    if name == "probability":
        if numbered:
            raw["useProbability"] = True
        else:
            raw.setdefault("extensions", {})["useProbability"] = True


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):  # an integer of 400 digits
        return default
    return int(number) if math.isfinite(number) else default


def _num(value: Any, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _trimmed(value: Any) -> tuple[str, ...]:
    return tuple(text.strip() for text in _strings(value))


def fire_entry(
    *,
    id: str,  # noqa: A002 - the lore entry's
    title: str,
    content: str,
    keywords: Sequence[str],
    always_on: bool,
    enabled: bool,
    raw: Mapping[str, Any] | None,
    numbered: bool = False,
    decorators: Sequence[str] = (),
) -> FireEntry:
    """An entry as SillyTavern would read it: from the fields it brought
    (`raw`, its world file's shape when `numbered`, else a card's), or, for
    one written in SealedLore, from its keywords, at SillyTavern's defaults."""
    if not always_on:
        # Always on as SealedLore has it, unticked (the Lore page, Always-on
        # entries…): an entry its text's @@activate made always on stops
        # being so, as a constant one does. It stayed standing and was sent
        # every turn, under a dialog that called it optional.
        decorators = [name for name in decorators if name != "@@activate"]
    if raw is None:
        return FireEntry(
            id=id,
            title=title,
            content=content,
            keys=tuple(keywords),
            constant=always_on,
            disabled=not enabled,
            decorators=tuple(decorators),
        )
    # Trimmed, as SillyTavern trims a key before it reads it: " /wol(f|ves)/i"
    # was no pattern with its space, nor one of the entry's keywords, and so
    # no key at all.
    keys = _trimmed(
        raw.get("key") if numbered and "key" in raw else raw.get("keys", raw.get("key"))
    )
    # The entry's keywords as SealedLore has them win over those it brought
    # (its patterns kept): edited on the Lore page or by a review, they had
    # changed nothing a story firing as SillyTavern does read.
    plain = [key for key in keys if not is_pattern(key)]
    if list(dict.fromkeys(plain)) != list(keywords):
        keys = (*keywords, *(key for key in keys if is_pattern(key)))
    secondary = _trimmed(
        raw.get("keysecondary")
        if numbered and "keysecondary" in raw
        else raw.get("secondary_keys", raw.get("keysecondary"))
    )
    order = raw.get("order") if numbered and "order" in raw else raw.get("insertion_order")
    group_text = str(_get(raw, numbered, "group", "group", "") or "")
    delayed = _get(raw, numbered, "delayUntilRecursion", "delay_until_recursion", False)
    character_filter = raw.get("characterFilter")
    names = character_filter.get("names") if isinstance(character_filter, Mapping) else None
    return FireEntry(
        id=id,
        title=title,
        content=content,
        keys=keys,
        secondary=secondary,
        selective=bool(raw.get("selective", True)) if numbered else bool(raw.get("selective")),
        logic=_int(_get(raw, numbered, "selectiveLogic", "selectiveLogic", AND_ANY), AND_ANY),
        # Always on as SealedLore has it: unticked, a constant entry stops
        # being one (and @@activate is set aside, above). One made always on
        # by @@activate alone isn't constant.
        constant=always_on and (bool(raw.get("constant")) or "@@activate" not in decorators),
        disabled=not enabled,
        order=_int(order, DEFAULT_ORDER),
        probability=_num(_get(raw, numbered, "probability", "probability", 100), 100),
        use_probability=bool(_get(raw, numbered, "useProbability", "useProbability", True)),
        groups=tuple(part for part in re.split(r",\s*", group_text) if part),
        group_text=group_text,
        group_override=bool(_get(raw, numbered, "groupOverride", "group_override", False)),
        group_weight=_num(
            _get(raw, numbered, "groupWeight", "group_weight", DEFAULT_WEIGHT), DEFAULT_WEIGHT
        ),
        use_group_scoring=_optional_bool(
            _get(raw, numbered, "useGroupScoring", "use_group_scoring", None)
        ),
        scan_depth=(
            None
            if _get(raw, numbered, "scanDepth", "scan_depth", None) is None
            else _int(_get(raw, numbered, "scanDepth", "scan_depth", None))
        ),
        case_sensitive=_optional_bool(_get(raw, numbered, "caseSensitive", "case_sensitive", None)),
        match_whole_words=_optional_bool(
            _get(raw, numbered, "matchWholeWords", "match_whole_words", None)
        ),
        exclude_recursion=bool(_get(raw, numbered, "excludeRecursion", "exclude_recursion", False)),
        prevent_recursion=bool(_get(raw, numbered, "preventRecursion", "prevent_recursion", False)),
        delay_until_recursion=1 if delayed is True else max(0, _int(delayed)),
        sticky=max(0, _int(_get(raw, numbered, "sticky", "sticky", 0))),
        cooldown=max(0, _int(_get(raw, numbered, "cooldown", "cooldown", 0))),
        delay=max(0, _int(_get(raw, numbered, "delay", "delay", 0))),
        triggers=_strings(_get(raw, numbered, "triggers", "triggers", ())),
        ignore_budget=bool(_get(raw, numbered, "ignoreBudget", "ignore_budget", False)),
        decorators=tuple(decorators),
        filter_names=_strings(names),
        filter_exclude=bool(character_filter.get("isExclude"))
        if isinstance(character_filter, Mapping)
        else False,
    )


# --- the scan ------------------------------------------------------------------


@dataclass(frozen=True)
class KeyPattern:
    """A key written /like this/, as it is run: `compiled` None when it never
    is (`refused` says why), `cap` the most characters it reads."""

    compiled: re.Pattern[str] | None = None
    cap: int = 0
    refused: str = ""


class _Refused(Exception):
    """A pattern that could take too long on the wrong text."""


_REPEATS = ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")
_ASSERTS = ("ASSERT", "ASSERT_NOT")
# What reads one character.
_ONE_CHAR = ("LITERAL", "NOT_LITERAL", "ANY", "IN")
# JavaScript's named group, (?<name>…), is Python's (?P<name>…).
_JS_NAMED_GROUP = re.compile(r"(?<!\\)\(\?<(?![=!])")


def _varies(items: Any) -> bool:
    """Whether what a repeat repeats can match more than one way: a repeat
    inside it that isn't of a fixed count, alternatives, or a back-reference."""
    for op, av in items:
        name = op.name
        if name in _REPEATS:
            low, high, body = av
            if low != high or _varies(body):
                return True
        elif name in ("BRANCH", "GROUPREF", "GROUPREF_EXISTS"):
            return True
        elif (name == "SUBPATTERN" and _varies(av[3])) or (name == "ATOMIC_GROUP" and _varies(av)):
            return True
    return False


def _degree(items: Any, follow: bool) -> int:
    """How many open-ended repeats a failed match can go back into: each
    makes a search one power slower in the text's length. `follow`: something
    comes after `items` that can still fail. A count inside a count is no
    open-ended repeat, however many they come to: `_search_cost` multiplies
    them."""
    total = 0
    items = list(items)
    for at, (op, av) in enumerate(items):
        after = follow or at < len(items) - 1
        name = op.name
        if name in _REPEATS:
            low, high, body = av
            if _varies(body):
                if high > VARIED_REPEAT:
                    raise _Refused("it repeats something that itself repeats or has alternatives")
                total += high * _degree(body, True)
            else:
                total += _degree(body, after or high > 1)
                # One that must repeat many times can fail part-way through
                # them, whatever comes after it.
                if high > OPEN_REPEAT and (after or low > OPEN_REPEAT):
                    total += 1
        elif name == "BRANCH":
            total += max((_degree(alt, after) for alt in av[1]), default=0)
        elif name == "SUBPATTERN":
            total += _degree(av[3], after)
        elif name == "ATOMIC_GROUP":
            total += _degree(av, after)
        elif name == "ASSERT":
            # A look-ahead that holds isn't the end of the match unless it
            # ends the pattern: what follows it can still fail, at every
            # place in the text.
            total += _degree(av[1], after)
        elif name == "ASSERT_NOT":
            # What a negative one reads through is the match failing.
            total += _degree(av[1], True)
        elif name == "GROUPREF_EXISTS":
            total += max(_degree(alt, after) for alt in av[1:] if alt is not None)
        elif name == "GROUPREF":
            total += 1
    return total


def _ways(items: Any) -> int:
    """How many ways a pattern's optional parts and alternatives can be
    taken: forty optional letters are 2^40."""
    total = 1
    for op, av in items:
        name = op.name
        if name in _REPEATS:
            low, high, body = av
            inner = _ways(body)
            if high <= OPEN_REPEAT:
                total *= (high - low + 1) * inner ** max(1, high)
            else:
                total *= inner
        elif name == "BRANCH":
            total *= max(1, sum(_ways(alt) for alt in av[1]))
        elif name == "SUBPATTERN":
            total *= _ways(av[3])
        elif name == "ATOMIC_GROUP":
            total *= _ways(av)
        elif name in _ASSERTS:
            # What a look-ahead reads is tried every way it can be taken too.
            total *= _ways(av[1])
        elif name == "GROUPREF_EXISTS":
            total *= max(1, sum(_ways(alt) for alt in av[1:] if alt is not None))
        if total > WAYS_MOST:
            raise _Refused("its optional parts and alternatives can be taken too many ways")
    return total


# --- what a search can cost ------------------------------------------------------
#
# `re` tries a pattern from every place in the text, and at each goes back
# into every repeat and alternative before it gives the place up. What that
# can cost is reckoned from the pattern alone, in billionths of a second:
# each part's own cost, times the ways there are to have come to it
# (`_cost`), a try from every place (`_search_cost`). The costs are the
# slowest measured for each kind of step (Nov 22 2026, Python 3.12, texts
# made to be slow: one letter repeated, a letter no class takes quickly, a
# near-match over and over), so the reckoning is over what a search takes:
# by a tenth at the least, of all that were run to see.
#
# A cost is a sum of terms in L, the characters left to read: term k counts
# k places chosen in order along them, L(L-1)…/k!. One open-ended repeat can
# stop at L places and two, one after the other, at L²/2 pairs of places,
# not L²: multiplied as sums of such terms, they come out so.
_Terms = tuple[float, ...]
_NONE: _Terms = (0.0,)
_ONCE: _Terms = (1.0,)
# More terms than any pattern worth running comes to.
_TERMS_MOST = 40

# A letter against the text, and without case; any character (`.`) is a
# letter's cost with or without it.
_LETTER, _LETTER_NOCASE = 2.6, 4.5
# A class: its ranges and letters, its first \w, \s or \d, each further one
# (every one of them is asked about a character none takes), and without
# case; and what a class costs more as a step of its own than inside the
# loop that counts a repeat of it.
_CLASS, _CLASS_CATEGORY, _CLASS_FURTHER, _CLASS_NOCASE, _CLASS_ALONE = 2.5, 6.0, 3.5, 8.0, 3.0
# A letter or any character inside that loop.
_READ, _READ_NOCASE = 0.5, 2.0
# Each place a search tries from.
_START = 8.0
# A repeat of one character: to begin it, and each place it gives back (a
# lazy one: each it takes) before what follows is tried again.
_REPEAT, _STEP, _STEP_LAZY = 9.0, 6.0, 9.0
# One turn of a repeat of anything longer.
_TURN = 45.0
# An alternative, a group that keeps what it matched, a look-ahead or
# look-behind, a boundary or an anchor, a back-reference before its letters.
_ALTERNATIVE, _GROUP, _LOOK, _AT, _REFERENCE = 10.0, 5.5, 8.0, 10.0, 6.0
# Each group the pattern keeps, for each turn, alternative and look-ahead:
# `re` saves what the groups hold before every one (300 groups before
# /(?:aa)*b/: eight times slower a turn). Inside such a repeat it puts them
# back at each place a repeat of one character gives back, too: so much,
# and so much a group.
_KEPT_GROUP = 1.2
_PUT_BACK, _PUT_BACK_GROUP = 5.0, 0.25


class _OverTerms(Exception):
    """A cost of more terms than `_TERMS_MOST`: past any budget."""


@dataclass(frozen=True)
class _Reckoning:
    """What the costs of one pattern are reckoned for: a text of at most
    `length` characters, with or without case, the most each group can hold
    (`re`'s own count), what the groups cost to save and to put back, and
    whether this is inside a repeat of more than one character."""

    length: int
    nocase: bool
    widths: Sequence[Any]
    saving: float
    putting_back: float
    repeated: bool = False


def _plus(a: _Terms, b: _Terms) -> _Terms:
    if len(a) < len(b):
        a, b = b, a
    return tuple(x + (b[at] if at < len(b) else 0.0) for at, x in enumerate(a))


def _times(a: _Terms, b: _Terms) -> _Terms:
    if len(a) == 1:
        return tuple(a[0] * y for y in b)
    if len(b) == 1:
        return tuple(x * b[0] for x in a)
    if len(a) + len(b) > _TERMS_MOST:
        raise _OverTerms
    out = [0.0] * (len(a) + len(b) - 1)
    for i, x in enumerate(a):
        if x:
            for j, y in enumerate(b):
                out[i + j] += x * y
    return tuple(out)


def _at_length(terms: _Terms, length: int) -> float:
    """`terms` for a text of `length` characters."""
    total, places = 0.0, 1.0
    for k, x in enumerate(terms):
        total += x * places
        places = places * (length + k + 1) / (k + 1)
    return total


def _char_cost(op: Any, av: Any, nocase: bool, counted: bool) -> float:
    """One character against a letter or a class: `counted` inside the loop
    that counts a repeat of it, else as a step of its own."""
    if op.name == "IN":
        categories = sum(1 for kind, _ in av if kind.name == "CATEGORY")
        plain = any(kind.name in ("LITERAL", "RANGE") for kind, _ in av)
        cost = _CLASS + (_CLASS_CATEGORY + _CLASS_FURTHER * (categories - 1) if categories else 0)
        cost += _CLASS_NOCASE if nocase and plain else 0
        return cost if counted else cost + _CLASS_ALONE
    nocase = nocase and op.name != "ANY"
    if counted:
        return _READ_NOCASE if nocase else _READ
    return _LETTER_NOCASE if nocase else _LETTER


def _of_one_char(av: Any) -> bool:
    """Whether a repeat is of one character: `re` counts those in a loop of
    its own, and gives them back a place at a time."""
    body = list(av[2])
    return len(body) == 1 and body[0][0].name in _ONE_CHAR


def _first_letter(items: Any, nocase: bool) -> str | None:
    """The letter `items` can only begin with, if there is one."""
    items = list(items)
    if not items or items[0][0].name != "LITERAL":
        return None
    letter = chr(items[0][1])
    if not nocase:
        return letter
    return letter.lower() if letter.isascii() else None


def _letters_from(items: Sequence[Any], at: int, nocase: bool) -> str:
    """The letters that stand one after another from `items[at]`."""
    run: list[str] = []
    for op, av in items[at:]:
        if op.name != "LITERAL" or (nocase and not chr(av).isascii()):
            break
        run.append(chr(av).lower() if nocase else chr(av))
    return "".join(run)


def _letters_compared(run: str) -> tuple[float, int]:
    """The most letters of `run` compared at a place, over places one after
    another, and how many places apart it can hold whole: `n` of them can
    hold only as often as its first `n` repeat themselves ("aaab" three deep
    at every place, "fire" once in four), so one, and for each beginning of
    it one in its period."""
    back = [0] * len(run)
    total = 1.0
    for at in range(len(run)):
        if at:
            k = back[at - 1]
            while k and run[at] != run[k]:
                k = back[k - 1]
            back[at] = k + 1 if run[at] == run[k] else 0
        total += 1.0 / (at + 1 - back[at])
    return total, len(run) - back[-1]


def _repeat_cost(av: Any, after: bool, lazy: bool, about: _Reckoning) -> tuple[_Terms, _Terms]:
    low, high, body = av
    turn = _TURN + about.saving
    if _varies(body):
        # A few turns at most (VARIED_REPEAT, `_degree`), each begun every
        # way the one before could end.
        inside, inside_ways = _cost(body, True, replace(about, repeated=True))
        inside = _plus(inside, (turn,))
        cost, ways, begun = (turn,), _NONE, _ONCE
        for done in range(high + 1):
            if done >= low:
                ways = _plus(ways, begun)
            if done < high:
                cost = _plus(cost, _times(begun, inside))
                begun = _times(begun, inside_ways)
        return cost, ways
    one_char = _of_one_char(av)
    if one_char:
        op, inner = body[0]
        each: _Terms = (_char_cost(op, inner, about.nocase, counted=True),)
    else:
        inside, _ = _cost(body, after or high > 1, replace(about, repeated=True))
        each = _plus(inside, (turn,))
    width = body.getwidth()[0]
    # With nothing after it that can fail, it fails within `low` turns or
    # holds; and what reads nothing isn't repeated for nothing.
    most = high if after else min(high, low + 1)
    if width == 0:
        most = min(most, low + 1)
    if width == 0 or most * width <= about.length:
        turns: _Terms = (float(most),)
        ends: _Terms = (float(most - low + 1),)
    elif width >= about.length:
        turns = ends = (2.0,)
    else:
        # It reaches past the text: as many turns as there is text for.
        turns = ends = (1.0, 1.0 / width)
    if not after or low == high:
        ends = _ONCE
    put_back = about.putting_back if one_char and about.repeated else 0.0
    cost = _plus((_REPEAT + put_back,), _times(turns, each))
    if after and one_char:
        cost = _plus(cost, _times(ends, ((_STEP_LAZY if lazy else _STEP) + put_back,)))
    return cost, ends


def _item_cost(op: Any, av: Any, after: bool, about: _Reckoning) -> tuple[_Terms, _Terms]:
    name = op.name
    if name in _REPEATS:
        return _repeat_cost(av, after, name == "MIN_REPEAT", about)
    if name == "BRANCH":
        # Alternatives that begin with different letters can't both hold at
        # one place: the ways are those of the letter with the most.
        cost, anywhere = _NONE, _NONE
        by_letter: dict[str, _Terms] = {}
        for alt in av[1]:
            inside, inside_ways = _cost(alt, after, about)
            cost = _plus(cost, _plus(inside, (_ALTERNATIVE + about.saving,)))
            letter = _first_letter(alt, about.nocase)
            if letter is None:
                anywhere = _plus(anywhere, inside_ways)
            else:
                by_letter[letter] = _plus(by_letter.get(letter, _NONE), inside_ways)
        most = max(by_letter.values(), key=lambda t: _at_length(t, about.length), default=_NONE)
        return cost, _plus(anywhere, most)
    if name == "SUBPATTERN":
        group, add, remove, inside_items = av
        nocase = (about.nocase or bool(add & re.IGNORECASE)) and not remove & re.IGNORECASE
        cost, ways = _cost(inside_items, after, replace(about, nocase=nocase))
        return (cost if group is None else _plus(cost, (_GROUP,))), ways
    if name == "ATOMIC_GROUP":
        return _cost(av, after, about)
    if name in _ASSERTS:
        # Whichever way it holds, it isn't gone back into.
        cost, _ = _cost(av[1], after or name == "ASSERT_NOT", about)
        return _plus(cost, (_LOOK + about.saving,)), _ONCE
    if name == "GROUPREF_EXISTS":
        cost, ways = (_LOOK,), _NONE
        for alt in av[1:]:
            if alt is not None:
                inside, inside_ways = _cost(alt, after, about)
                cost, ways = _plus(cost, inside), _plus(ways, inside_ways)
        return cost, ways
    if name == "GROUPREF":
        letter = _LETTER_NOCASE if about.nocase else _LETTER
        try:
            most = about.widths[av][1]
        except (IndexError, TypeError):
            most = about.length + 1
        if most <= about.length:
            return (_REFERENCE + letter * most,), _ONCE
        return (_REFERENCE, letter), _ONCE
    if name == "AT":
        return (_AT,), _ONCE
    if name in _ONE_CHAR:
        return (_char_cost(op, av, about.nocase, counted=False),), _ONCE
    return (_LETTER,), _ONCE


def _cost(items: Any, follow: bool, about: _Reckoning) -> tuple[_Terms, _Terms]:
    """What trying `items` at one place can cost before it fails, every way
    they can be taken, and how many ways they can hold: what follows them
    is tried again for each. `follow` as `_degree` has it."""
    cost, ways = _NONE, _ONCE
    items = list(items)
    at = 0
    while at < len(items):
        op, av = items[at]
        after = follow or at < len(items) - 1
        own, own_ways = _item_cost(op, av, after, about)
        cost = _plus(cost, _times(ways, own))
        at += 1
        if op.name in _REPEATS and _of_one_char(av):
            # Letters straight after a repeat of one character are tried at
            # one place after another, and can't all hold at each: what
            # follows them is tried only as often as they can.
            run = _letters_from(items, at, about.nocase)
            if len(run) > 1:
                letter = _LETTER_NOCASE if about.nocase else _LETTER
                deep, apart = _letters_compared(run)
                compared = _times(own_ways, (deep * letter,))
                cost = _plus(cost, _times(ways, _plus(compared, (len(run) * letter,))))
                own_ways = _plus(_times(own_ways, (1.0 / apart,)), _ONCE)
                at += len(run)
        ways = _times(ways, own_ways)
    return cost, ways


def _search_cost(tree: Any, length: int) -> float:
    """The most one search of `length` characters can cost: a failed try
    from every place in them."""
    state = tree.state
    groups = max(0, state.groups - 1)
    about = _Reckoning(
        length=length,
        nocase=bool(state.flags & re.IGNORECASE),
        widths=getattr(state, "groupwidths", ()),
        saving=_KEPT_GROUP * groups,
        putting_back=_PUT_BACK + _PUT_BACK_GROUP * groups if groups else 0.0,
    )
    try:
        cost, _ = _cost(tree, False, about)
    except _OverTerms:
        return math.inf
    # A try from each place: one more place chosen, for every term.
    return _at_length((0.0, *_plus(cost, (_START,))), length)


def _tidy(cap: int) -> int:
    """`cap` to two figures, down: 1,678 is 1,600."""
    unit = 10 ** max(0, len(str(cap)) - 2)
    return cap - cap % unit


def _screened(source: str, flags: int) -> int:
    """The most characters a pattern may read, or `_Refused` for one that is
    never run."""
    try:
        from re import _parser  # the parser `re.compile` itself uses
    except ImportError:  # pragma: no cover - a Python without it
        return SCAN_LEAST
    tree = _parser.parse(source, flags)
    ways = _ways(tree)
    degree = _degree(tree, False)
    if degree and degree not in SCAN_CAPS:
        raise _Refused("it has too many open-ended repeats")
    most = SCAN_CAPS.get(degree, SCAN_MOST)
    if _search_cost(tree, most) <= SEARCH_BUDGET:
        return most
    if _search_cost(tree, SCAN_LEAST) > SEARCH_BUDGET:
        if degree > 1 or ways == 1:
            raise _Refused("it has too many open-ended repeats")
        raise _Refused("its optional parts and alternatives can be taken too many ways")
    # The cost only grows with the length, but for a count that turns from
    # "as far as the text goes" into its own number: any length under the
    # budget is safe, and this finds the longest or near it.
    low, high = SCAN_LEAST, most
    while high - low > 1:
        middle = (low + high) // 2
        if _search_cost(tree, middle) <= SEARCH_BUDGET:
            low = middle
        else:
            high = middle
    return max(SCAN_LEAST, _tidy(low))


@lru_cache(maxsize=4096)
def _pattern(key: str) -> KeyPattern | None:
    """A key written /like this/flags, as SillyTavern's parseRegexFromString
    takes one (JavaScript's flags, as near as Python's are; every slash
    inside escaped, each flag once: `is_pattern`, as the importer reads it).
    None for any other key, which is matched as written."""
    if not is_pattern(key):
        return None
    match = _PATTERN.match(key)
    if match is None:
        return None
    flags = 0
    for letter in match.group(2):
        flags |= {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}.get(letter, 0)
    source = _JS_NAMED_GROUP.sub("(?P<", match.group(1))
    if len(source) > PATTERN_MAX_CHARS:
        return KeyPattern(refused="it is far longer than any key")
    try:
        cap = _screened(source, flags)
        return KeyPattern(compiled=re.compile(source, flags), cap=cap)
    except _Refused as exc:
        return KeyPattern(refused=f"{exc}, which can take minutes on the wrong text")
    except (re.error, OverflowError, RecursionError, ValueError):
        # Not a pattern Python can run (JavaScript's \p{…}, a look-behind of
        # no fixed width, a repeat count past any number, groups nested
        # thousands deep): it matched nothing as plain text either.
        return KeyPattern(refused="it isn't a pattern this program can run")


def _is_word(char: str) -> bool:
    """JavaScript's \\w: an ASCII letter, digit or underscore."""
    return char.isascii() and (char.isalnum() or char == "_")


def _whole_word(haystack: str, key: str) -> bool:
    """`key` in `haystack` with no word character either side of it:
    SillyTavern's /(?:^|\\W)(key)(?:$|\\W)/, found without a regular expression
    (the scan reads every key against the same text, pass after pass)."""
    at = haystack.find(key)
    while at != -1:
        end = at + len(key)
        if (at == 0 or not _is_word(haystack[at - 1])) and (
            end == len(haystack) or not _is_word(haystack[end])
        ):
            return True
        at = haystack.find(key, at + 1)
    return False


class Buffer:
    """What is scanned: the messages, newest first, and what fired before."""

    def __init__(self, messages: Sequence[str], settings: TavernSettings) -> None:
        self.messages = [message.strip() for message in messages[:MAX_SCAN_DEPTH]]
        self.settings = settings
        self.recurse: list[str] = []
        self.skew = 0
        # The joined text by how much of it is read, and each without case:
        # every entry and key reads the same few, pass after pass.
        self._texts: dict[tuple[int, int], str] = {}
        self._lowered: dict[int, str] = {}
        # Which of them a text is, by identity, and for each plain key how far
        # it has been looked for and where it was first found: among the
        # messages and among what fired. The scan reaches back a message a
        # pass (min activations) and reads again what fired (recursion); read
        # whole each time, 200 messages and 100 entries took 38 s.
        self._parts: dict[int, tuple[int, int]] = {}
        self._found: dict[tuple[str, bool, bool], list[int]] = {}
        self._plain: dict[tuple[bool, bool, int], str] = {}
        # Seconds this turn's patterns have taken, and what each couldn't read.
        self.spent = 0.0
        self.unread: dict[str, str] = {}

    def depth(self) -> int:
        return self.settings.scan_depth + self.skew

    def text(self, entry: FireEntry, state: str) -> str:
        depth = entry.scan_depth if entry.scan_depth is not None else self.depth()
        if depth <= 0:
            return ""
        depth = min(depth, MAX_SCAN_DEPTH)
        recursed = len(self.recurse) if state != "min_activations" else 0
        found = self._texts.get((depth, recursed))
        if found is None:
            found = MATCHER + JOINER.join(self.messages[:depth])
            if recursed:
                found += JOINER + JOINER.join(self.recurse)
            self._texts[(depth, recursed)] = found
            self._parts[id(found)] = (depth, recursed)
        return found

    def _part(self, fired: bool, at: int, case: bool) -> str:
        """One message, or one pass's fired text, as a plain key reads it."""
        found = self._plain.get((fired, case, at))
        if found is None:
            found = self.recurse[at] if fired else self.messages[at]
            found = found if case else found.lower()
            self._plain[(fired, case, at)] = found
        return found

    def _plain_matches(self, part: tuple[int, int], key: str, case: bool, whole: bool) -> bool:
        """A plain key against the text `part` names: found in one of its
        messages or of what fired (no key matches across their separator)."""
        depth, recursed = part
        test = _whole_word if whole and len(key.split()) <= 1 else str.__contains__
        seen = self._found.setdefault((key, case, whole), [0, -1, 0, -1])
        for fired, most in ((False, min(depth, len(self.messages))), (True, recursed)):
            done, first = (2, 3) if fired else (0, 1)
            while seen[first] == -1 and seen[done] < most:
                if test(self._part(fired, seen[done], case), key):
                    seen[first] = seen[done]
                seen[done] += 1
            if seen[first] != -1 and seen[first] < most:
                return True
        return False

    def _without_case(self, haystack: str) -> str:
        # By identity: `text` hands out the same strings, kept in `_texts`.
        found = self._lowered.get(id(haystack))
        if found is None:
            found = haystack.lower()
            if any(haystack is kept for kept in self._texts.values()):
                self._lowered[id(haystack)] = found
        return found

    def _pattern_matches(self, haystack: str, key: str, pattern: KeyPattern) -> bool:
        if pattern.compiled is None:
            self.unread.setdefault(key, f"its pattern {key} is never run: {pattern.refused}")
            return False
        if self.spent > PATTERN_SECONDS:
            self.unread.setdefault(key, f"its pattern {key} wasn't run: {OUT_OF_TIME}")
            return False
        if len(haystack) > pattern.cap:
            self.unread.setdefault(
                key,
                f"its pattern {key} read only the newest {pattern.cap:,} characters "
                "(it could take too long on more)",
            )
            haystack = haystack[: pattern.cap]
        began = time.perf_counter()
        try:
            return pattern.compiled.search(haystack) is not None
        except RecursionError:
            return False
        finally:
            self.spent += time.perf_counter() - began

    def matches(self, haystack: str, key: str, entry: FireEntry) -> bool:
        pattern = _pattern(key)
        if pattern is not None:
            return self._pattern_matches(haystack, key, pattern)
        case = entry.case_sensitive
        case = self.settings.case_sensitive if case is None else case
        whole = entry.match_whole_words
        whole = self.settings.match_whole_words if whole is None else whole
        part = self._parts.get(id(haystack))
        if part is not None and key and haystack is self._texts.get(part):
            return self._plain_matches(part, key if case else key.lower(), bool(case), bool(whole))
        if not case:
            haystack, key = self._without_case(haystack), key.lower()
        if not whole:
            return key in haystack
        if len(key.split()) > 1:
            return key in haystack
        return _whole_word(haystack, key)

    def said_unread(self, entry: FireEntry) -> str:
        """Why a key of `entry` didn't read what it was given, if one didn't."""
        return next(
            (self.unread[key.strip()] for key in entry.keys if key.strip() in self.unread), ""
        )

    def score(self, entry: FireEntry, state: str) -> int:
        text = self.text(entry, state)
        primary = sum(1 for key in entry.keys if self.matches(text, key, entry))
        secondary = sum(1 for key in entry.secondary if self.matches(text, key, entry))
        if not entry.keys:
            return 0
        if entry.secondary:
            if entry.logic == AND_ANY:
                return primary + secondary
            if entry.logic == AND_ALL:
                return primary + secondary if secondary == len(entry.secondary) else primary
        return primary


# --- timed effects -------------------------------------------------------------


def replay(turns: Sequence[tuple[int, Sequence[str]]], entries: Mapping[str, FireEntry]) -> Timed:
    """The sticky and cooldown state after the turns on a path: each turn the
    chat's length then and what fired, oldest first."""
    timed = Timed()
    for length, fired in turns:
        _expire(timed, length, entries)
        for entry_id in fired:
            entry = entries.get(entry_id)
            if entry is None:
                continue
            if entry.sticky and entry_id not in timed.sticky:
                timed.sticky[entry_id] = (length, length + entry.sticky, False)
            if entry.cooldown and entry_id not in timed.cooldown:
                timed.cooldown[entry_id] = (length, length + entry.cooldown, False)
    return timed


def _expire(timed: Timed, length: int, entries: Mapping[str, FireEntry]) -> None:
    """SillyTavern's checkTimedEffects: what has ended goes, a sticky ending
    starts its entry's cooldown."""
    for kind in ("sticky", "cooldown"):
        held = getattr(timed, kind)
        for entry_id, (start, end, protected) in list(held.items()):
            entry = entries.get(entry_id)
            if length <= start and not protected:
                del held[entry_id]
                continue
            if entry is None:
                if length >= end:
                    del held[entry_id]
                continue
            if not getattr(entry, kind):
                del held[entry_id]
                continue
            if length >= end:
                del held[entry_id]
                if kind == "sticky" and entry.cooldown:
                    timed.cooldown[entry_id] = (length, length + entry.cooldown, True)


def _active(timed: Timed, length: int, entries: Mapping[str, FireEntry]) -> tuple[set, set, set]:
    """Which entries are sticky, on cooldown and delayed now (after the
    turn's own expiry, as SillyTavern checks before it scans)."""
    _expire(timed, length, entries)
    sticky = set(timed.sticky)
    cooldown = set(timed.cooldown)
    delayed = {i for i, e in entries.items() if e.delay and length < e.delay}
    return sticky, cooldown, delayed


# --- firing --------------------------------------------------------------------


def budget_of(settings: TavernSettings, context: int) -> int:
    """SillyTavern's lore budget: its share of the context, at most its cap."""
    budget = round(settings.budget_percent * context / 100) or 1
    if settings.budget_cap > 0 and budget > settings.budget_cap:
        budget = settings.budget_cap
    return budget


def fire(
    entries: Sequence[FireEntry],
    messages: Sequence[str],
    *,
    settings: TavernSettings,
    context: int,
    count_tokens: Callable[[str], int],
    timed: Timed,
    length: int,
    trigger: str | Sequence[str] = "normal",
    present: Sequence[str] = (),
    rng: random.Random | None = None,
    free: Collection[str] = (),
) -> Fired:
    """One turn's firing. `messages`: the chat newest first, as scanned;
    `context`: the context size the budget is a share of; `timed`: the state
    replayed along the path (`replay`), `length` the chat's length now;
    `present`: who is in the scene, for character filters; `trigger`: the
    kind of turn, or the kinds it counts as (SealedLore's Regenerate keeps the
    old take, as a SillyTavern swipe does, and is a regenerate too). `free`:
    entries outside the budget, neither refused by it nor counted against it
    (those standing in the cached block: SealedLore sends them every turn
    whatever the budget, so counted, they took room they never use and left
    the keyed entries over it; the author's ruling, Nov 21 2026)."""
    rng = rng or random.Random()
    free = set(free)
    triggers = {trigger} if isinstance(trigger, str) else set(trigger)
    trigger_name = trigger if isinstance(trigger, str) else next(iter(trigger), "normal")
    known = {entry.id: entry for entry in entries}
    sorted_entries = sorted(entries, key=lambda e: -e.order)
    index = {entry.id: i for i, entry in enumerate(sorted_entries)}
    sticky, cooldown, delayed = _active(timed, length, known)
    buffer = Buffer(messages, settings)
    budget = budget_of(settings, context)
    why: dict[str, str] = {}
    not_fired: dict[str, str] = {}
    activated: dict[str, FireEntry] = {}
    failed_probability: set[str] = set()
    # What fired before this pass and may be read again, in tokens. Each
    # entry's are counted once: counted as SillyTavern does, the whole of what
    # fired again for every entry, a book of long constants took seconds.
    activated_tokens = 0
    counted: dict[str, int] = {}

    def exempt(entry: FireEntry) -> bool:
        return entry.ignore_budget or entry.id in free

    def tokens_of(entry: FireEntry) -> int:
        if entry.id in free:
            return 0
        if entry.id not in counted:
            counted[entry.id] = count_tokens(f"{entry.content}\n")
        return counted[entry.id]

    overflowed = False
    levels = sorted({e.delay_until_recursion for e in sorted_entries if e.delay_until_recursion})
    level = levels.pop(0) if levels else 0
    state = "initial"
    steps = 0
    present_names = {name.casefold() for name in present}

    while state:
        if settings.max_recursion_steps and settings.max_recursion_steps <= steps:
            break
        steps += 1
        next_state = ""
        now: list[FireEntry] = []
        for entry in sorted_entries:
            if entry.id in failed_probability or entry.id in activated:
                continue
            if entry.disabled:
                not_fired.setdefault(entry.id, "switched off")
                continue
            if entry.triggers and not triggers & set(entry.triggers):
                not_fired.setdefault(entry.id, f"not for a {trigger_name} turn")
                continue
            if entry.filter_names:
                included = any(name.casefold() in present_names for name in entry.filter_names)
                if included if entry.filter_exclude else not included:
                    not_fired.setdefault(entry.id, "its character filter")
                    continue
            is_sticky = entry.id in sticky
            if entry.id in delayed:
                not_fired[entry.id] = f"delayed until the chat has {entry.delay} messages"
                continue
            if entry.id in cooldown and not is_sticky:
                not_fired[entry.id] = "on cooldown"
                continue
            if state != "recursion" and entry.delay_until_recursion and not is_sticky:
                not_fired.setdefault(entry.id, "waits for recursion")
                continue
            if state == "recursion" and entry.delay_until_recursion > level and not is_sticky:
                continue
            if (
                state == "recursion"
                and settings.recursive
                and entry.exclude_recursion
                and not is_sticky
            ):
                continue
            if "@@activate" in entry.decorators:
                why[entry.id] = "@@activate"
                now.append(entry)
                continue
            if "@@dont_activate" in entry.decorators:
                not_fired.setdefault(entry.id, "@@dont_activate")
                continue
            if entry.constant:
                why[entry.id] = "constant"
                now.append(entry)
                continue
            if is_sticky:
                end = timed.sticky[entry.id][1]
                why[entry.id] = (
                    f"sticky ({end - length} more message{'s' if end - length != 1 else ''})"
                )
                now.append(entry)
                continue
            if not entry.keys:
                continue
            text = buffer.text(entry, state)
            key = next(
                (k for k in entry.keys if k.strip() and buffer.matches(text, k.strip(), entry)),
                None,
            )
            if key is None:
                if said := buffer.said_unread(entry):
                    not_fired.setdefault(entry.id, said)
                continue
            # In a later pass the key was found in what an earlier entry says.
            where = ", in another entry's text" if state == "recursion" else ""
            if not (entry.selective and entry.secondary):
                why[entry.id] = f"key “{key}”{where}"
                now.append(entry)
                continue
            if _secondary(entry, buffer, text):
                why[entry.id] = f"key “{key}” and its secondary keys{where}"
                now.append(entry)
            else:
                not_fired[entry.id] = f"key “{key}”, but not its secondary keys"

        if len(now) > 1:
            now.sort(key=lambda e: (0 if e.id in sticky else 1, index[e.id]))
        _groups(now, activated, buffer, state, sticky, cooldown, delayed, settings, rng, not_fired)

        new_tokens = 0
        ignoring = sum(1 for e in now if exempt(e))
        for entry in now:
            ignoring -= 1 if exempt(entry) else 0
            if overflowed and not exempt(entry):
                not_fired[entry.id] = "over the lore budget"
                if ignoring > 0:
                    continue
                for rest in now[now.index(entry) + 1 :]:
                    if not exempt(rest):
                        not_fired[rest.id] = "over the lore budget"
                break
            if (
                entry.use_probability
                and entry.probability != 100
                and entry.id not in sticky
                and rng.random() * 100 > entry.probability
            ):
                failed_probability.add(entry.id)
                not_fired[entry.id] = f"its {entry.probability:g}% chance didn't come up"
                continue
            new_tokens += tokens_of(entry)
            if not exempt(entry) and activated_tokens + new_tokens >= budget:
                overflowed = True
                not_fired[entry.id] = "over the lore budget"
                continue
            activated[entry.id] = entry
            not_fired.pop(entry.id, None)

        successful = [e for e in now if e.id not in failed_probability]
        for_recursion = [e for e in successful if not e.prevent_recursion]
        if settings.recursive and not overflowed and for_recursion:
            next_state = "recursion"
        if settings.recursive and not overflowed and state == "min_activations" and buffer.recurse:
            next_state = "recursion"
        short = settings.min_activations > 0 and len(activated) < settings.min_activations
        if not next_state and not overflowed and short:
            over_max = (
                settings.min_activations_depth_max > 0
                and buffer.depth() > settings.min_activations_depth_max
            ) or buffer.depth() > length
            if not over_max:
                next_state = "min_activations"
                buffer.skew += 1
        if not next_state and levels:
            next_state = "recursion"
            level = levels.pop(0)
        state = next_state
        if state:
            text = "\n".join(e.content for e in for_recursion)
            if text:
                buffer.recurse.append(text)
                activated_tokens += sum(tokens_of(e) for e in for_recursion)

    for entry_id in activated:
        not_fired.pop(entry_id, None)
    ordered = sorted(activated.values(), key=lambda e: -e.order)
    ordered.reverse()  # SillyTavern adds each at the top: lowest order first
    return Fired(
        fired=tuple(ordered),
        why={i: why.get(i, "") for i in activated},
        not_fired={i: w for i, w in not_fired.items() if i not in activated},
        budget=budget,
        used=sum(count_tokens(e.content) for e in ordered),
    )


def _secondary(entry: FireEntry, buffer: Buffer, text: str) -> bool:
    any_match, all_match = False, True
    for key in entry.secondary:
        matched = bool(key.strip()) and buffer.matches(text, key.strip(), entry)
        any_match = any_match or matched
        all_match = all_match and matched
        if entry.logic == AND_ANY and matched:
            return True
        if entry.logic == NOT_ALL and not matched:
            return True
    if entry.logic == NOT_ANY and not any_match:
        return True
    return entry.logic == AND_ALL and all_match


def _groups(
    now: list[FireEntry],
    activated: Mapping[str, FireEntry],
    buffer: Buffer,
    state: str,
    sticky: set[str],
    cooldown: set[str],
    delayed: set[str],
    settings: TavernSettings,
    rng: random.Random,
    not_fired: dict[str, str],
) -> None:
    """SillyTavern's filterByInclusionGroups: one winner a group."""
    grouped: dict[str, list[FireEntry]] = {}
    for entry in now:
        for name in entry.groups:
            grouped.setdefault(name, []).append(entry)
    if not grouped:
        return

    def remove(entry: FireEntry, said: str) -> None:
        if entry in now:
            now.remove(entry)
            # The first reason: a loser met again in a later pass is the same loser.
            not_fired.setdefault(entry.id, said)

    has_sticky: dict[str, bool] = {}
    for name, group in grouped.items():
        stuck = [e for e in group if e.id in sticky]
        has_sticky[name] = bool(stuck)
        for entry in group:
            if stuck and entry not in stuck:
                remove(entry, f"its group “{name}” has a sticky entry")
        for entry in group:
            if entry.id in cooldown or entry.id in delayed:
                remove(entry, f"its group “{name}”: on cooldown or delayed")
    for name, group in grouped.items():
        if not settings.use_group_scoring and not any(e.use_group_scoring for e in group):
            continue
        if has_sticky[name]:
            continue
        scores = [buffer.score(e, state) for e in group]
        best = max(scores) if scores else 0
        for entry, score in list(zip(group, scores, strict=True)):
            scored = entry.use_group_scoring
            scored = settings.use_group_scoring if scored is None else scored
            if scored and score < best:
                remove(entry, f"its group “{name}”: a better match won")
                group.remove(entry)
    for name, group in grouped.items():
        if has_sticky[name]:
            continue
        if any(e.group_text == name for e in activated.values()):
            for entry in group:
                remove(entry, f"its group “{name}” already has an entry in")
            continue
        if len(group) <= 1:
            continue
        chosen = sorted((e for e in group if e.group_override), key=lambda e: -e.order)
        if chosen:
            winner = chosen[0]
        else:
            total = sum(e.group_weight for e in group)
            roll = rng.random() * total
            winner, running = None, 0.0
            for entry in group:
                running += entry.group_weight
                if roll <= running:
                    winner = entry
                    break
            if winner is None:
                continue
        for entry in group:
            if entry is not winner:
                remove(entry, f"its group “{name}”: {winner.title} won")
