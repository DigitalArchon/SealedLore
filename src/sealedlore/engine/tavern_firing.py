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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# SillyTavern's own numbers.
MAX_SCAN_DEPTH = 1000
DEFAULT_WEIGHT = 100
DEFAULT_ORDER = 100
# The separator between scanned messages: matched by no key.
MATCHER = "\x01"
JOINER = "\n" + MATCHER
# Secondary keys' logic (`world_info_logic`).
AND_ANY, NOT_ALL, NOT_ANY, AND_ALL = 0, 1, 2, 3
# The kinds of turn an entry's triggers name, as SealedLore makes them.
TRIGGERS = ("normal", "continue", "impersonate", "swipe", "regenerate", "quiet")

_PATTERN = re.compile(r"^/([\s\S]+?)/([gimsuy]*)$")


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


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return int(number) if math.isfinite(number) else default


def _num(value: Any, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
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
    keys = _strings(
        raw.get("key") if numbered and "key" in raw else raw.get("keys", raw.get("key"))
    )
    secondary = _strings(
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
        constant=bool(raw.get("constant")) or always_on,
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


def _pattern(key: str) -> re.Pattern[str] | None:
    """A key written /like this/flags, as SillyTavern's parseRegexFromString
    takes one (JavaScript's flags, as near as Python's are)."""
    match = _PATTERN.match(key)
    if match is None:
        return None
    flags = 0
    for letter in match.group(2):
        flags |= {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}.get(letter, 0)
    try:
        return re.compile(match.group(1), flags)
    except re.error:
        return None


class Buffer:
    """What is scanned: the messages, newest first, and what fired before."""

    def __init__(self, messages: Sequence[str], settings: TavernSettings) -> None:
        self.messages = [message.strip() for message in messages[:MAX_SCAN_DEPTH]]
        self.settings = settings
        self.recurse: list[str] = []
        self.skew = 0

    def depth(self) -> int:
        return self.settings.scan_depth + self.skew

    def text(self, entry: FireEntry, state: str) -> str:
        depth = entry.scan_depth if entry.scan_depth is not None else self.depth()
        if depth <= 0:
            return ""
        depth = min(depth, MAX_SCAN_DEPTH)
        result = MATCHER + JOINER.join(self.messages[:depth])
        if self.recurse and state != "min_activations":
            result += JOINER + JOINER.join(self.recurse)
        return result

    def matches(self, haystack: str, key: str, entry: FireEntry) -> bool:
        pattern = _pattern(key)
        if pattern is not None:
            return pattern.search(haystack) is not None
        case = entry.case_sensitive
        case = self.settings.case_sensitive if case is None else case
        if not case:
            haystack, key = haystack.lower(), key.lower()
        whole = entry.match_whole_words
        whole = self.settings.match_whole_words if whole is None else whole
        if not whole:
            return key in haystack
        if len(key.split()) > 1:
            return key in haystack
        # JavaScript's \W: anything but an ASCII letter, digit or underscore.
        found = re.search(rf"(?:^|\W)({re.escape(key)})(?:$|\W)", haystack, re.ASCII)
        return found is not None

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
) -> Fired:
    """One turn's firing. `messages`: the chat newest first, as scanned;
    `context`: the context size the budget is a share of; `timed`: the state
    replayed along the path (`replay`), `length` the chat's length now;
    `present`: who is in the scene, for character filters; `trigger`: the
    kind of turn, or the kinds it counts as (SealedLore's Regenerate keeps the
    old take, as a SillyTavern swipe does, and is a regenerate too)."""
    rng = rng or random.Random()
    triggers = {trigger} if isinstance(trigger, str) else set(trigger)
    trigger_name = trigger if isinstance(trigger, str) else next(iter(trigger), "normal")
    known = {entry.id: entry for entry in entries}
    sorted_entries = sorted(entries, key=lambda e: -e.order)
    index = {entry.id: i for i, entry in enumerate(sorted_entries)}
    sticky, cooldown, delayed = _active(timed, length, known)
    buffer = Buffer(messages, settings)
    budget = round(settings.budget_percent * context / 100) or 1
    if settings.budget_cap > 0 and budget > settings.budget_cap:
        budget = settings.budget_cap
    why: dict[str, str] = {}
    not_fired: dict[str, str] = {}
    activated: dict[str, FireEntry] = {}
    failed_probability: set[str] = set()
    activated_text = ""
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
                continue
            by = "the text of an entry that fired" if state == "recursion" else "key"
            if not (entry.selective and entry.secondary):
                why[entry.id] = f"{by} “{key}”"
                now.append(entry)
                continue
            if _secondary(entry, buffer, text):
                why[entry.id] = f"{by} “{key}”, and its secondary keys"
                now.append(entry)
            else:
                not_fired[entry.id] = f"key “{key}”, but not its secondary keys"

        if len(now) > 1:
            now.sort(key=lambda e: (0 if e.id in sticky else 1, index[e.id]))
        _groups(now, activated, buffer, state, sticky, cooldown, delayed, settings, rng, not_fired)

        new_text = ""
        ignoring = sum(1 for e in now if e.ignore_budget)
        for entry in now:
            ignoring -= 1 if entry.ignore_budget else 0
            if overflowed and not entry.ignore_budget:
                not_fired[entry.id] = "over the lore budget"
                if ignoring > 0:
                    continue
                for rest in now[now.index(entry) + 1 :]:
                    if not rest.ignore_budget:
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
            new_text += f"{entry.content}\n"
            if not entry.ignore_budget and (
                count_tokens(activated_text) + count_tokens(new_text) >= budget
            ):
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
                activated_text = text + "\n" + activated_text

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
