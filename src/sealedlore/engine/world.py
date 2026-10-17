"""The story's map (models/world.py): pure. The session makes the read after
each passage (`engine/session_world.py`); this renders the map for it, holds
the reply to account and folds what each passage changed along a path.

The read names places and people; code resolves the names, and every entry
stands only on a quote found in the author's turn or the passage, as the
plot's facts do. What the replays in SealedLoreDocs design/map.md found the
read getting wrong is checked here rather than asked for:

- one person, one entry: a name form of someone in the story ("Marcus" for
  Marcus Webb) is that person, and a description ("the woman", "the
  player's character") is no one;
- a distance once stated is kept: a passage that says otherwise is a
  conflict, noted, not an overwrite (the stories contradict themselves);
- a direction the link's own quote doesn't carry is a guess, and dropped.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.scene_state import same_person
from sealedlore.engine.takeable import FIXED_MARK, can_be_taken, fixed_thing, is_vehicle, same_thing
from sealedlore.engine.validators import locate_quote
from sealedlore.engine.world_layout import (
    bearing,
    describe_metres,
    geometry_conflicts,
    metres,
    moves,
    place_new,
    position,
    size_of,
)
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.node import Node
from sealedlore.models.world import (
    PlaceKind,
    WorldDelta,
    WorldLink,
    WorldMap,
    WorldPlace,
    WorldThing,
)

PLACE_KINDS: frozenset[str] = frozenset(PlaceKind.__args__)  # type: ignore[attr-defined]

_DIRECTIONS = re.compile(
    r"\b(north|south|east|west|up|down|above|below|upstairs|downstairs|"
    r"left|right|across|next door|behind|beyond|ahead)",
    re.IGNORECASE,
)
# Not a name: a description, a pronoun, a role.
_NOT_A_NAME = re.compile(
    r"^(the|a|an|his|her|their|my|your|our|player|character|someone|somebody|"
    r"man|woman|stranger|guard|figure|people|nobody|everyone)\b",
    re.IGNORECASE,
)


def key(name: str | None) -> str:
    """A name as compared: case, spacing and a leading article ignored."""
    return re.sub(r"^(the|a|an) ", "", " ".join(str(name or "").lower().split()))


def find_place(world: WorldMap, name: str | None, pending: Sequence[WorldPlace] = ()) -> str | None:
    """The id of the place answering to `name` (its name or an alias), on
    the map or among places added in the same reply."""
    wanted = key(name)
    if not wanted:
        return None
    for place in [*world.places.values(), *pending]:
        if wanted in {key(place.name), *(key(a) for a in place.aliases)}:
            return place.id
    return None


def _find_thing(world: WorldMap, name: str) -> WorldThing | None:
    """The map's thing of this name; else the one thing whose name it
    shortens or lengthens (`same_thing`: "heavy iron anvil" for the "heavy
    iron anvil block on a timber stand"), when only one does."""
    wanted = key(name)
    found = next((t for t in world.things.values() if key(t.name) == wanted), None)
    if found is not None or not name:
        return found
    like = [t for t in world.things.values() if same_thing(name, t.name)]
    return like[0] if len(like) == 1 else None


# --- Rendering, for the read -------------------------------------------------


def place_name(world: WorldMap, place_id: str | None) -> str:
    place = world.places.get(place_id or "")
    return place.name if place is not None else "(not on the map)"


def render_map(world: WorldMap) -> str:
    """The map as the read is shown it: places as a tree, then links, who is
    where, and the things."""
    lines = ["PLACES"]
    shown: set[str] = set()

    def under(parent: str | None, depth: int) -> None:
        for place in world.places.values():
            top = place.inside is None or place.inside not in world.places
            if place.id in shown:
                continue
            if (parent is None and top) or (parent is not None and place.inside == parent):
                shown.add(place.id)
                label = f"{'  ' * depth}- {place.name} ({place.kind or 'place'})"
                lines.append(f"{label}: {place.what}" if place.what else label)
                under(place.id, depth + 1)

    under(None, 0)
    for place in world.places.values():  # a loop of "inside" no read should make
        if place.id not in shown:
            lines.append(f"- {place.name} ({place.kind or 'place'})")
    if len(lines) == 1:
        lines.append("(none yet)")
    lines.append("")
    lines.append("LINKS")
    for link in world.links:
        text = f"- {place_name(world, link.a)} -> {place_name(world, link.b)}: "
        text += link.direction or "direction unknown"
        lines.append(text + (f", {link.distance}" if link.distance else ""))
    if not world.links:
        lines.append("(none yet)")
    lines.append("")
    lines.append("WHO IS WHERE")
    for who, place_id in world.where.items():
        outside = " (outside it)" if who in world.outside else ""
        lines.append(f"- {who}: {place_name(world, place_id)}{outside}")
    if not world.where:
        lines.append("(nobody placed yet)")
    lines.append("")
    lines.append("THINGS")
    for thing in world.things.values():
        where = (
            f"carried by {thing.carried_by}" if thing.carried_by else place_name(world, thing.at)
        )
        lines.append(f"- {thing.name}: {where}" + (f", {thing.state}" if thing.state else ""))
    if not world.things:
        lines.append("(none yet)")
    return "\n".join(lines)


def build_world_read_messages(
    *,
    world: WorldMap,
    people: Sequence[str],
    held: str | None,
    author_label: str,
    author_turn: str,
    passage: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    note: str = "",
    last: str = "",
) -> list[PromptMessage]:
    """`note`: a line after the author's character, for a simple chat (who
    "I" and "you" are, `map_read.chat`); nothing for a story. `last`: after
    the passage, where the read reads it last (`map_read.outside`)."""
    user = (
        f"THE MAP BEFORE THE PASSAGE\n\n{render_map(world)}\n\n"
        f"PEOPLE: {', '.join(people) or '(none named yet)'}\n"
        f"The author's character: {held or 'not set'}\n"
        + (f"{note}\n" if note else "")
        + f"\n{author_label.upper()}:\n{author_turn}\n\n"
        f"THE LATEST PASSAGE:\n{passage}" + (f"\n\n{last}" if last else "")
    )
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["map_read.system"]),)),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


# --- The map here, for the storyteller -------------------------------------

SECTION_MAP_HEADING = "# THE MAP HERE"
# Enough to stand on without crowding the tail.
MAP_LIST_LIMIT = 8
_COMPASS = (
    "north",
    "north-east",
    "east",
    "south-east",
    "south",
    "south-west",
    "west",
    "north-west",
)
# Past this a distance is given in miles too; past the second, whole miles.
_MILES_FROM = 1600.0
_WHOLE_MILES_FROM = 8000.0
_METRES_A_MILE = 1609.0


def established(world: WorldMap, base: WorldDelta | None) -> WorldMap:
    """The map as the storyteller is told it. A plot file's places keep the
    file's own words (a read's later words for them were the moment's), and
    its ways stand: a way the story gave between two of the file's places is
    left out, since the file already says where they are. Everything the
    story added is as the reads found it. The plot's hidden places are
    already gone (`world_at`)."""
    if base is None:
        return world
    own = {p.id: p for p in base.places}
    places = {
        k: p.model_copy(update={"what": own[k].what}) if k in own else p
        for k, p in world.places.items()
    }
    links = [k for k in base.links if k.a in places and k.b in places] + [
        k for k in world.links if not (k.a in own and k.b in own)
    ]
    return world.model_copy(update={"places": places, "links": links})


def _chain(world: WorldMap, place_id: str | None) -> list[WorldPlace]:
    """The place and every place it is inside, innermost first."""
    out: list[WorldPlace] = []
    while place_id is not None and place_id in world.places:
        place = world.places[place_id]
        if place in out:
            break
        out.append(place)
        place_id = place.inside
    return out


def _how_far(world: WorldMap, here: str | None, there: str, fixed: Collection[str]) -> str:
    """ "8.0 km / 5 miles north", from where a plot file put them. Nothing for
    a place the story added (where the program put it is a guess), nor for a
    road, which is a line and not a spot."""
    if here is None or there not in fixed or world.places[there].kind == "route":
        return ""
    a, b = position(world, here), position(world, there)
    if a is None or b is None:
        return ""
    dx, dy = b[0] - a[0], b[1] - a[1]
    distance = math.hypot(dx, dy)
    if distance < 1:
        return ""
    angle = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    bearing = _COMPASS[round(angle / 45) % 8]
    place = world.places[there]
    if place.kind in ("area", "region") and place.size:
        # Country is reached at its edge, not its middle: a village at the
        # edge of a 12 km forest was told the forest lay 8.2 km north.
        edge = distance - size_of(place) / 2
        if edge < 1:
            return "all around"
        return f"its edge {_far(edge)} {bearing}"
    return f"{_far(distance)} {bearing}"


def _far(distance: float) -> str:
    far = describe_metres(distance)
    if distance >= _MILES_FROM:
        miles = distance / _METRES_A_MILE
        far += f" / {miles:.0f} miles" if distance >= _WHOLE_MILES_FROM else f" / {miles:.1f} miles"
    return far


def _around(world: WorldMap, here: str | None, place: WorldPlace, fixed: Collection[str]) -> str:
    where = _how_far(world, here, place.id, fixed)
    head = f"{place.name} ({place.kind or 'place'}" + (f", {where}" if where else "") + ")"
    what = " ".join(place.what.split())
    if place.state:  # what the story did to it, else the map says a barn stands burnt
        what = (what + " " if what else "") + f"Now: {' '.join(place.state.split())}."
    return f"{head}: {what}" if what else head


def render_map_context(
    world: WorldMap,
    held: str | None,
    texts: PromptTexts = DEFAULT_TEXTS,
    *,
    fixed: Collection[str] = (),
    mark_fixed: bool = False,
) -> str:
    """The local map for the storyteller's tail: where the author's character
    is (the start before any read), what is inside it, what else is around
    it at each level up, the ways between them, and the things here. `world`
    is `established`; `fixed` are the places a plot file put, whose distance
    and bearing are given, from the nearest of them the character is in.
    `mark_fixed`: a thing that can't be taken is marked so, on a turn that
    may not move it. Empty without a place."""
    here_id = world.where.get(held or "") or world.start
    chain = _chain(world, here_id)
    if not chain:
        return ""
    here = chain[0]
    origin = next((p.id for p in chain if p.id in fixed), None)
    lines = [SECTION_MAP_HEADING, texts["map.context"], ""]
    who = held or "The author's character"  # a Question before anyone is held
    verb = "are" if who.lower() == "you" else "is"  # a chat's user, unnamed
    outside = "just outside " if held is not None and held in world.outside else ""
    lines.append(
        f"{who} {verb} at: {outside}" + here.name + "".join(f", in {o.name}" for o in chain[1:])
    )
    if here.what or here.state:
        said = " ".join(here.what.split())
        if here.state:
            said = (said + " " if said else "") + f"Now: {' '.join(here.state.split())}."
        lines.append("  " + said)
    inside = [p for p in world.places.values() if p.inside == here.id][:MAP_LIST_LIMIT]
    if inside:
        lines.append("Inside it: " + "; ".join(_around(world, origin, p, fixed) for p in inside))
    shown = {p.id for p in chain} | {p.id for p in inside}
    for level, outer in enumerate([*chain[1:], None]):
        parent = outer.id if outer is not None else None
        around = [
            p
            for p in world.places.values()
            if p.inside == parent and p.id != chain[level].id and p.id not in shown
        ][:MAP_LIST_LIMIT]
        if around:
            label = f"Also in {outer.name}" if outer is not None else "Elsewhere on the map"
            lines.append(f"{label}: " + "; ".join(_around(world, origin, p, fixed) for p in around))
            shown |= {p.id for p in around}
    ways = [
        f"- from {place_name(world, k.a)} to {place_name(world, k.b)}"
        + (f": {words}" if (words := ", ".join(w for w in (k.direction, k.distance) if w)) else "")
        for k in world.links
        if k.a in shown or k.b in shown
    ]
    if ways:
        lines.append("Ways between them:")
        lines += ways[: MAP_LIST_LIMIT * 2]
    near = {p.id for p in chain} | {p.id for p in inside}
    things = [
        t
        for t in reversed(list(world.things.values()))  # the newest first
        # Carried by the held character, never "carried by nobody": with no
        # one held yet, every thing on the map was "here".
        if (held is not None and t.carried_by == held) or (t.at is not None and t.at in near)
    ]
    carried = [t for t in things if t.carried_by == held]
    things = (carried + [t for t in things if t.carried_by != held])[:MAP_LIST_LIMIT]
    if things:
        lines.append("Things here:")
        for thing in things:
            at = (
                f"carried by {thing.carried_by}"
                if thing.carried_by
                else place_name(world, thing.at)
            )
            stays = mark_fixed and not thing.carried_by and fixed_thing(thing.name)
            lines.append(
                f"- {thing.name}: {at}"
                + (f", {thing.state}" if thing.state else "")
                + (f", {FIXED_MARK}" if stays else "")
            )
    return "\n".join(lines)


# --- The reply, held to account ----------------------------------------------


def parse_world_reply(text: str) -> dict[str, Any]:
    """The read's JSON object; ValueError when there is none."""
    return extract_json(text, dict, "map changes")


def _entries(reply: dict[str, Any], field: str) -> list[dict[str, Any]]:
    value = reply.get(field)
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


# A place's width from the read, in metres: a hut to a city, nothing wider.
MIN_SIZE, MAX_SIZE = 2.0, 50_000.0


def _size(value: Any) -> float | None:
    """A read's "size" as metres, or None: a number (or a string of one,
    "150 m") within MIN_SIZE..MAX_SIZE; anything else is no size."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        value = float(match.group()) if match else None
    if not isinstance(value, (int, float)):
        return None
    return float(value) if MIN_SIZE <= value <= MAX_SIZE else None


# What a read writes when the passage said nothing ("unknown" went into a way,
# live): no words at all, or it reaches the storyteller as a fact.
# A state that says the place is as it was again: back to "" rather than a
# state drawn as a change for good ("rebuilt" stood as burnt and open; the
# second review).
_RESTORED = re.compile(
    r"\b(rebuilt|repaired|restored|mended|patched up|put right|as (good as )?new|"
    r"intact again|as it was|back to normal|whole again)\b",
    re.IGNORECASE,
)

_NOTHING_SAID = re.compile(
    r"^(unknown|unclear|unspecified|not (stated|given|specified|known)|none|n/?a|-+|\?+)$",
    re.IGNORECASE,
)


def _said(value: Any) -> str | None:
    text = _text(value)
    return None if not text or _NOTHING_SAID.match(text.strip(" .")) else text


# Why a change to a place already on the map is refused on an ordinary turn.
_ONLY_AUTHOR = "only the author's Direction or Narration"


# Words that say a person is a woman or a man, and the pronouns for each:
# a naming in a sentence that speaks of the other is someone else's.
_FEMALE = frozenset(
    "woman women girl lady she her hers herself mother wife sister daughter widow matron "
    "aunt grandmother nun queen princess duchess mistress madam".split()
)
_MALE = frozenset(
    "man men boy he him his himself father husband brother son lad gentleman uncle "
    "grandfather monk king prince duke master sir".split()
)


def _sex_of(text: str) -> set[str]:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return ({"f"} if words & _FEMALE else set()) | ({"m"} if words & _MALE else set())


def _sentences_to(source: str, end: int) -> str:
    """The sentence the quote ends in, and the one before it."""
    head = source[:end]
    stops = [m.end() for m in re.finditer(r"[.!?…]['\"”’]?\s", head)]
    start = stops[-2] if len(stops) >= 2 else 0
    rest = source[end:]
    stop = re.search(r"[.!?…]['\"”’]?(\s|$)", rest)
    return source[start : end + (stop.end() if stop else len(rest))]


def _other_sex(described: str, around: str) -> bool:
    """Whether the passage around a naming speaks only of the other sex than
    the unknown's description. Live (Oct 17 2026): the watchwoman at the
    gate, "a lean woman in her forties", was named Marcus Webb from
    '"Marcus Webb," he said'."""
    theirs, here = _sex_of(described), _sex_of(around)
    return len(theirs) == 1 and len(here) == 1 and theirs != here


def _parents_first(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The reply's places, each after the place it is inside when that is in
    the same reply. Live (Oct 17 2026), a new world's opening listed the
    watch room and the lamp room before "The lighthouse" they were inside:
    a parent not found yet was dropped, and every room stood on its own."""
    named = {key(_text(e.get("name"))): i for i, e in enumerate(entries)}
    done: set[int] = set()
    out: list[dict[str, Any]] = []

    def visit(i: int, seen: set[int]) -> None:
        if i in done or i in seen:
            return
        seen.add(i)
        parent = named.get(key(_text(entries[i].get("inside"))))
        if parent is not None:
            visit(parent, seen)
        done.add(i)
        out.append(entries[i])

    for i in range(len(entries)):
        visit(i, set())
    return out


def resolve_reply(
    world: WorldMap,
    reply: dict[str, Any],
    *,
    source: str,
    people: Sequence[Character],
    author_says: bool = False,
    marked: Mapping[str, tuple[float, float]] | None = None,
    moves_fixed: bool | None = None,
) -> tuple[WorldDelta, list[str]]:
    """What the reply changes, as a delta on `world`, and what was refused
    and why (for the log and the panel). `source` is the author's turn and
    the passage: every entry's quote must be found in it.

    `author_says`: the turn is the author's Direction or Narration, which
    state what is so (the author, Oct 16 2026: "The Director must be able to
    make as broad and complete changes as possible"). Then, and only then, a
    place already on the map may be taken off it ("gone"), renamed ("was"
    beside its new "name", or "renamed"), resized ("size"), put inside
    another ("inside") or moved: to a spot the author marked (`marked`: the
    spots the reply's place entries name, by the entry's name as compared
    with `key`, resolved by the session from the turn's marks), or by a link
    with a direction and a distance from a place that has its spot. On any
    other turn those are refused, said in `refused`, and the map stands:
    places are placed once (engine/world_layout.py).

    `moves_fixed`: the turn may move a thing that can't be taken
    (engine/takeable.py: the town's stores, a statue, a well): the author's
    Direction or Narration, or a turn under Fiat (the author, Oct 17 2026).
    On any other turn such a thing stays where it is and only its state may
    change: live, the read moved "The town's stores" from Hellsville into
    the farmhouse on a passage that only named them. None: as `author_says`."""
    # `added` is always a list from here: a delta that changes known places
    # and makes none must still skip their copies on a fold without them.
    delta = WorldDelta(added=[])
    refused: list[str] = []
    marked = marked or {}
    if moves_fixed is None:
        moves_fixed = author_says

    def quoted(entry: dict[str, Any]) -> str | None:
        quote = _text(entry.get("quote"))
        span = locate_quote(source, quote) if quote else None
        return source[span[0] : span[1]] if span else None

    def unplace(place: WorldPlace) -> None:
        place.x, place.y = None, None
        if place.id not in delta.moved:
            delta.moved.append(place.id)

    # Places first, so links and people can name places added in this reply.
    # A "renamed" entry is a place entry with "was" beside its new name.
    entries = _parents_first(
        _entries(reply, "places")
        + [
            {"name": e.get("now"), "was": e.get("was"), "quote": e.get("quote")}
            for e in _entries(reply, "renamed")
        ]
    )
    for entry in entries:
        name = _text(entry.get("name"))
        if not name or quoted(entry) is None:
            refused.append(f"place {name or '(unnamed)'}: no quote found")
            continue
        kind = _text(entry.get("kind")).lower() or None
        if kind not in PLACE_KINDS:
            kind = None
        existing = find_place(world, name, delta.places)
        inside_name = _text(entry.get("inside"))
        inside = find_place(world, inside_name, delta.places) if inside_name else None
        was = _text(entry.get("was"))
        rename: str | None = None
        if was:
            old_id = find_place(world, was, delta.places)
            if old_id is None:
                refused.append(f"rename {was} -> {name}: {was} is not on the map")
            elif existing is not None and existing != old_id:
                refused.append(f"rename {was} -> {name}: {name} is another place's name")
                existing = old_id
            elif not author_says:
                refused.append(f"rename {was} -> {name}: {_ONLY_AUTHOR} renames a place")
                existing = old_id  # the entry is about the old place, under its name
            else:
                existing = old_id
                rename = name
        if existing is not None:
            pending = next((p for p in delta.places if p.id == existing), None)
            old = pending or world.places[existing]
            changed = old.model_copy(deep=True)
            # What a place is, its kind and where it is stand once known (an
            # imported map's are the author's): a later passage's words for it
            # were the moment's ("where four people have set an ambush", "at the
            # far end of the street, south of where Jane stood"). What a passage
            # does to it is its state.
            changed.what = changed.what or _text(entry.get("what"))
            state = _text(entry.get("state"))
            if state and changed.state and _RESTORED.search(state):
                state = ""  # rebuilt, repaired: as it was, not a state of its own
                changed.state = ""
            changed.state = state or changed.state
            changed.kind = changed.kind or kind  # type: ignore[assignment]
            known_places = {**world.places, **{p.id: p for p in delta.places}}
            if rename is not None and key(rename) != key(changed.name):
                # The old name stays an alias: a later read (or the author)
                # naming it so finds this place rather than making a second.
                if changed.name not in changed.aliases:
                    changed.aliases.append(changed.name)
                changed.name = rename
                delta.renamed[existing] = rename
            renest = (
                inside is not None
                and inside != changed.inside
                and not _would_loop(world, delta, changed.id, inside)
            )
            if (
                # A vehicle (or a room in one) has no spot of its own and
                # goes where the passage docks it; once in a bay, it stayed
                # there for good (the review, Oct 15 2026).
                (changed.inside is None or moves(known_places, changed))
                and changed.x is None  # a placed place has its spot: never moved
                and renest
            ):
                changed.inside = inside
            elif renest and not author_says:
                refused.append(f"place {name}: {_ONLY_AUTHOR} puts a place inside another")
            elif renest:  # the author's say: inside it, placed there anew
                changed.inside = inside
                if not moves(known_places, changed):
                    unplace(changed)
            size = _size(entry.get("size"))
            if size is not None and size != size_of(changed):
                if author_says:
                    changed.size = size
                else:
                    refused.append(f"place {name}: {_ONLY_AUTHOR} resizes a place")
            spot = marked.get(key(name)) or (marked.get(key(was)) if was else None)
            if spot is not None and not moves(known_places, changed):
                if author_says:
                    changed.x, changed.y = spot  # the session fits it in what is there
                    if changed.id not in delta.moved:
                        delta.moved.append(changed.id)
                else:
                    refused.append(f"place {name}: {_ONLY_AUTHOR} moves a place")
            delta.places = [p for p in delta.places if p.id != existing] + [changed]
        else:
            delta.places.append(
                WorldPlace(
                    name=name,
                    kind=kind,  # type: ignore[arg-type]
                    inside=inside,
                    what=_text(entry.get("what")),
                    state=_text(entry.get("state")),
                    # The read's sense of scale, when it has one: without it
                    # a vast castle got a building's 40 m and the four-tile
                    # box that goes with it (the author's castle, Oct 15 2026).
                    # The layout clamps it to its parent (world_layout.place_one).
                    size=_size(entry.get("size")),
                )
            )
            delta.added.append(delta.places[-1].id)
    for entry in _entries(reply, "gone"):
        name = _text(entry.get("name"))
        if not name or quoted(entry) is None:
            refused.append(f"gone {name or '(unnamed)'}: no quote found")
            continue
        gone = find_place(world, name, delta.places)
        if gone is None:
            refused.append(f"gone {name}: not on the map")
            continue
        if not author_says:
            refused.append(f"gone {name}: {_ONLY_AUTHOR} removes a place")
            continue
        delta.places = [p for p in delta.places if p.id != gone]
        if delta.added is not None and gone in delta.added:
            delta.added.remove(gone)  # made and unmade in one reply: nothing
            continue
        delta.renamed.pop(gone, None)
        if gone in delta.moved:
            delta.moved.remove(gone)
        if gone not in delta.removed:
            delta.removed.append(gone)
    for entry in _entries(reply, "links"):
        label = f"{_text(entry.get('from'))} -> {_text(entry.get('to'))}"
        quote = quoted(entry)
        if quote is None:
            refused.append(f"link {label}: no quote found")
            continue
        a = find_place(world, entry.get("from"), delta.places)
        b = find_place(world, entry.get("to"), delta.places)
        if a is None or b is None or a == b:
            refused.append(f"link {label}: a place not on the map")
            continue
        if a in delta.removed or b in delta.removed:
            refused.append(f"link {label}: taken off the map")
            continue
        if _is_vehicle(world, delta, a) or _is_vehicle(world, delta, b):
            refused.append(f"link {label}: a vehicle moves, so it has no fixed link")
            continue
        direction = _said(entry.get("direction"))
        words = _DIRECTIONS.findall(direction or "")
        if direction and words and not any(w.lower() in quote.lower() for w in words):
            direction = None  # a direction its own quote doesn't carry is a guess
        distance = _said(entry.get("distance"))
        mover = _link_mover(world, delta, a, b, direction, distance) if author_says else None
        old = next(
            (link for link in [*world.links, *delta.links] if {link.a, link.b} == {a, b}),
            None,
        )
        if old is not None and mover is None:
            if old.distance and distance and key(distance) != key(old.distance):
                delta.conflicts.append(
                    f"{place_name_any(world, delta, a)} – {place_name_any(world, delta, b)}: "
                    f"the map keeps {old.distance}; the passage says {distance}"
                )
                distance = old.distance
            distance = distance or old.distance
            if direction is None and old.direction and old.a == a:
                direction = old.direction
        if mover is not None:
            # The author's say: the place is where the link puts it, placed
            # again from the other end (`place_new` reads this link first).
            unplace(mover)
            delta.places = [p for p in delta.places if p.id != mover.id] + [mover]
        delta.links = [k for k in delta.links if {k.a, k.b} != {a, b}]
        delta.links.append(WorldLink(a=a, b=b, direction=direction, distance=distance))
    named_people = [n for n in world.where if not is_unknown(n)]
    for entry in _entries(reply, "where"):
        who = _text(entry.get("who"))
        # A name starts with a capital; a description ("the woman") doesn't.
        # `islower`, not `not isupper`: a script with no case (CJK) is a name too.
        # Someone in the story is a name whatever it looks like: "The Hollow
        # King" was refused as a description (the review, Oct 15 2026).
        known = any(same_person(who, c) for c in people) if who else False
        label = None if known or not who else unknown_for(who, world, delta)
        described = (
            bool(who)
            and not known
            and label is None
            and (_NOT_A_NAME.match(who) is not None or who[0].islower() or is_unknown(who))
        )
        if not who or (described and (is_unknown(who) or not _describes_one(who))):
            refused.append(f"where {who or '(nobody)'}: not a name")
            continue
        if quoted(entry) is None:
            refused.append(f"where {who}: no quote found")
            continue
        place = find_place(world, entry.get("place"), delta.places)
        if place is None:
            refused.append(f"where {who} @ {_text(entry.get('place'))}: not on the map")
            continue
        if place in delta.removed:
            refused.append(f"where {who} @ {_text(entry.get('place'))}: taken off the map")
            continue
        if described:
            # Placed but not named: shown as Unknown 1, 2… until a passage
            # names them (the author, Oct 17 2026), the description kept so
            # the next read knows them.
            label = next_unknown(world, delta)
            delta.unknowns[label] = who[0].lower() + who[1:] if _ARTICLE.match(who) else who
        outside = entry.get("outside") is True
        if label is not None:
            delta.where[label] = place
            delta.outside[label] = outside
            continue
        canonical = person_name(
            who, people, [*named_people, *(n for n in delta.where if not is_unknown(n))]
        )
        delta.where[canonical] = place
        delta.outside[canonical] = outside
    for entry in _entries(reply, "named"):
        label = unknown_for(_text(entry.get("who")), world, delta)
        name = _text(entry.get("name"))
        if label is None or not name or _NOT_A_NAME.match(name) or name[0].islower():
            refused.append(
                f"named {_text(entry.get('who')) or '(nobody)'}: no such unknown or name"
            )
            continue
        quote = _text(entry.get("quote"))
        span = locate_quote(source, quote) if quote else None
        if span is None:
            refused.append(f"named {label} {name}: no quote found")
            continue
        said = {**world.unknowns, **delta.unknowns}.get(label, "")
        if _other_sex(said, _sentences_to(source, span[1])):
            refused.append(f"named {label} {name}: the passage names someone else")
            continue
        canonical = person_name(name, people, named_people)
        delta.named[label] = canonical
        delta.unknowns.pop(label, None)
        if label in delta.where:
            delta.where.setdefault(canonical, delta.where.pop(label))
        if label in delta.outside:
            delta.outside.setdefault(canonical, delta.outside.pop(label))
    for entry in _entries(reply, "things"):
        name = _text(entry.get("name"))
        if not name or quoted(entry) is None:
            refused.append(f"thing {name or '(unnamed)'}: no quote found")
            continue
        at = _text(entry.get("at"))
        existing = _find_thing(world, name)
        thing = existing.model_copy(deep=True) if existing is not None else WorldThing(name=name)
        if at.lower().startswith("carried by"):
            holder = at[len("carried by") :].strip()
            holder = person_name(holder, people, [*world.where, *delta.where])
            if can_be_taken(thing.name) or is_vehicle(thing.name):
                thing.carried_by = holder
                thing.at = None
            else:
                # Too heavy to carry off (an anvil, a boulder): dragged along,
                # it is where they are (the replay, Oct 17 2026: an anvil
                # "carried by" its dragger was taken into her hands).
                thing.at = delta.where.get(holder) or world.where.get(holder) or thing.at
                thing.carried_by = None
        elif at:
            found = find_place(world, at, delta.places)
            if found is not None or existing is None:
                thing.at = found
                thing.carried_by = None
            # A place the map hasn't ("the anvil on its stump"): a known thing
            # stays where it was (it was taken off the map, the replay).
        if thing.at in delta.removed:  # as the fold leaves it: where the place was
            thing.at = world.places[thing.at].inside
        held = (
            existing is not None
            and not moves_fixed
            and existing.carried_by is None
            and (thing.at, thing.carried_by) != (existing.at, existing.carried_by)
            and fixed_thing(existing.name)
        )
        if held:
            refused.append(
                f"thing {existing.name}: can't be taken, so it moves only on the author's "
                "Direction or Narration, or a turn under Fiat"
            )
            thing.at, thing.carried_by = existing.at, existing.carried_by
        thing.state = _text(entry.get("state")) or thing.state
        if held and thing == existing:
            continue  # its move refused, and nothing else about it changed
        delta.things = [t for t in delta.things if t.id != thing.id] + [thing]
    weather = _weather_entry(reply)
    if weather is not None:
        words = _text(weather.get("now"))
        if not words:
            pass  # null, or nothing said: the weather is as it was
        elif quoted(weather) is None:
            refused.append(f"weather {words}: no quote found")
        else:
            delta.weather = words
    return delta, refused


def _weather_entry(reply: dict[str, Any]) -> dict[str, Any] | None:
    """The reply's weather as {"now", "quote"}: the object asked for, or a
    string with "weather_quote" beside it (as the minutes are given), or
    None when there is none. The author's Direction states it ("It begins
    to snow") and the read's quote is found in the turn as in the passage."""
    value = reply.get("weather")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        return {"now": value, "quote": reply.get("weather_quote")}
    return None


def _link_mover(
    world: WorldMap,
    delta: WorldDelta,
    a: str,
    b: str,
    direction: str | None,
    distance: str | None,
) -> WorldPlace | None:
    """On the author's turn, the place a link moves: the end the read gave
    again in this reply (the "to" end first) that is on the map with a spot,
    measured from an end that has its spot, by a compass direction and a
    distance in metres the layout can read. None when the link moves nothing
    (a way between two standing places, noted as a conflict if they
    disagree). The mover leaves the place it was in when the other end lies
    outside it, or is it: "two miles east of Hellsville" is not inside
    Hellsville, nor inside the farm it stood in."""
    stated = metres(distance)
    if bearing(direction) is None or stated is None:
        return None
    copies = {p.id: p for p in delta.places}
    for mover_id, anchor_id in ((b, a), (a, b)):
        mover = copies.get(mover_id)
        before = world.places.get(mover_id)
        # Moved already in this reply (a mark, which is exact): that stands.
        if mover is None or before is None or before.x is None or mover_id in delta.moved:
            continue
        anchor = copies.get(anchor_id) or world.places.get(anchor_id)
        if anchor is None or position_any(world, delta, anchor_id) is None:
            continue
        known = {**world.places, **copies}
        if moves(known, mover) or mover_id in _ancestors(known, anchor_id):
            continue  # a vehicle, or its own container measured from it
        if anchor_id in _ancestors(known, mover_id):
            # Measured from what it is in: outside it only when the distance
            # says so, else it stays where it is (the author said no more).
            if stated <= size_of(anchor) / 2:
                continue
            mover.inside = anchor.inside
        elif mover.inside is not None and mover.inside not in {
            anchor_id,
            *_ancestors(known, anchor_id),
        }:
            mover.inside = anchor.inside  # beside the other end, at its level
        return mover
    return None


def position_any(world: WorldMap, delta: WorldDelta, place_id: str) -> tuple[float, float] | None:
    """Where a place is, the delta's copy first (`position` for the rest)."""
    seen: set[str] = set()
    copies = {p.id: p for p in delta.places}
    current: str | None = place_id
    while current is not None and current not in seen:
        seen.add(current)
        place = copies.get(current) or world.places.get(current)
        if place is None:
            return None
        if place.x is not None and place.y is not None:
            return place.x, place.y
        current = place.inside
    return None


def _ancestors(places: Mapping[str, WorldPlace], place_id: str) -> set[str]:
    out: set[str] = set()
    current = places.get(place_id)
    current = places.get(current.inside or "") if current is not None else None
    while current is not None and current.id not in out:
        out.add(current.id)
        current = places.get(current.inside or "")
    return out


def within_ids(world: WorldMap, place_id: str) -> set[str]:
    """Every place inside `place_id`, at any depth."""
    found = {place_id}
    grew = True
    while grew:
        more = {p.id for p in world.places.values() if p.inside in found and p.id not in found}
        found |= more
        grew = bool(more)
    found.discard(place_id)
    return found


def _is_vehicle(world: WorldMap, delta: WorldDelta, place_id: str) -> bool:
    place = next((p for p in delta.places if p.id == place_id), None) or world.places.get(place_id)
    return place is not None and place.kind == "vehicle"


def _would_loop(world: WorldMap, delta: WorldDelta, place_id: str, inside: str) -> bool:
    """Whether putting `place_id` inside `inside` would make it inside itself."""
    parents = {p.id: p.inside for p in world.places.values()}
    parents.update({p.id: p.inside for p in delta.places})
    seen: set[str] = set()
    current: str | None = inside
    while current is not None and current not in seen:
        if current == place_id:
            return True
        seen.add(current)
        current = parents.get(current)
    return False


def place_name_any(world: WorldMap, delta: WorldDelta, place_id: str) -> str:
    for place in delta.places:
        if place.id == place_id:
            return place.name
    return place_name(world, place_id)


# Someone placed but not yet named (the author, Oct 17 2026): "Unknown 1".
UNKNOWN = "Unknown"
_UNKNOWN_LABEL = re.compile(r"^unknown\s*(\d+)$", re.IGNORECASE)
# Descriptions that are no one person: never an unknown.
_NOT_ONE = re.compile(
    r"^(everyone|everybody|nobody|no one|people|the crowd|a crowd|the others|others|they|them|"
    r"the player|player|the character|character|the author|someone|somebody)\b",
    re.IGNORECASE,
)
_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def is_unknown(name: str) -> bool:
    return _UNKNOWN_LABEL.match(name.strip()) is not None


def _describes_one(text: str) -> bool:
    return _NOT_ONE.match(text.strip()) is None


def _plain(text: str) -> str:
    return _ARTICLE.sub("", " ".join(text.lower().split()))


def unknown_for(who: str, world: WorldMap, delta: WorldDelta | None = None) -> str | None:
    """The unknown `who` is: their label ("Unknown 2", any spacing or case)
    or their description as the map has it ("the girl" for "a girl")."""
    known = {**world.unknowns, **(delta.unknowns if delta is not None else {})}
    found = _UNKNOWN_LABEL.match(who.strip())
    if found is not None:
        label = f"{UNKNOWN} {int(found.group(1))}"
        return label if label in known else None
    wanted = _plain(who)
    return next((label for label, said in known.items() if _plain(said) == wanted), None)


def next_unknown(world: WorldMap, delta: WorldDelta | None = None) -> str:
    """The next label: one past every label the story has given, named or not."""
    used = [world.unknowns_made, *(delta.unknowns if delta is not None else {})]
    numbers = [u if isinstance(u, int) else int(u.split()[-1]) for u in used]
    numbers += [int(label.split()[-1]) for label in world.unknowns]
    return f"{UNKNOWN} {max(numbers, default=0) + 1}"


def unknown_words(text: str, world: WorldMap) -> str:
    """`text` with each unknown's label as their description, for the
    storyteller and the turns a menu writes ("the teenage girl, not named
    yet" rather than "Unknown 1")."""
    for label in sorted(world.unknowns, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(label)}\b", f"{world.unknowns[label]} (not named yet)", text)
    return text


def person_name(name: str, people: Sequence[Character], placed: Iterable[str]) -> str:
    """One person, one name: someone in the story it is a form of, else
    someone already on the map, else as written."""
    for character in people:
        if same_person(name, character):
            return character.name
    for other in placed:
        if same_person(name, Character(id="", name=other)):
            return other
    return name


def describe_delta(world: WorldMap, delta: WorldDelta) -> str:
    """One line for the status bar: what the passage changed on the map."""
    parts: list[str] = []
    new = [p.name for p in delta.places if p.id not in world.places]
    if new:
        parts.append("new " + ", ".join(new))
    changed_state = [
        f"{p.name} now {p.state or 'as it was'}"
        for p in delta.places
        if p.id in world.places and p.state != world.places[p.id].state
    ]
    parts.extend(changed_state)
    # The author's Direction: taken off, renamed, moved, resized, put inside.
    gone = [world.places[pid].name for pid in delta.removed if pid in world.places]
    if gone:
        parts.append("gone: " + ", ".join(gone))
    for pid, new_name in delta.renamed.items():
        if pid in world.places:
            parts.append(f"{world.places[pid].name} now {new_name}")
    for pid in delta.moved:
        if pid in world.places:
            parts.append(f"{place_name_any(world, delta, pid)} moved")
    after = applied(world, delta)
    for p in delta.places:
        old = world.places.get(p.id)
        if old is None:
            continue
        if p.size is not None and p.size != size_of(old):
            parts.append(f"{p.name} now {describe_metres(p.size)}")
        if p.inside != old.inside and p.inside is not None:
            parts.append(f"{p.name} now in {place_name_any(world, delta, p.inside)}")
    for who, place_id in delta.where.items():
        if world.where.get(who) != place_id:
            parts.append(f"{who} at {place_name(after, place_id)}")
    changed = [t.name for t in delta.things]
    if changed:
        parts.append("things: " + ", ".join(changed))
    if delta.weather is not None and delta.weather != world.weather:
        parts.append(f"weather now {delta.weather}")
    if delta.conflicts:
        parts.append(f"{len(delta.conflicts)} distance kept against the passage")
    return "; ".join(parts) if parts else "no change"


# --- Folding ------------------------------------------------------------------


def applied(
    world: WorldMap, delta: WorldDelta, *, base: bool = False, in_place: bool = False
) -> WorldMap:
    """`world` with `delta` applied (a new map; `world` is left alone, unless
    `in_place`: `world_at` folds a long path onto one copy, since a deep copy
    of a 500-place map for each of 500 messages took ~1s a call on the GUI
    thread). Places come placed (`place_delta`); one that somehow isn't is
    placed here by the same rule, so the map is the same however often it is
    folded.

    A passage's places, and the places it puts people or ways between, are
    seen; an imported map (`base`) shows only the places it says are known.

    What the author's Direction did to known places (`removed`, `renamed`,
    `moved`) is applied here too, so every fold of the path agrees: a place
    moved takes what is inside it along and loses its old ways; a renamed
    one keeps its old name as an alias; a removed one leaves what was inside
    it, and anyone or anything there, to its parent."""
    out = world if in_place else world.model_copy(deep=True)
    # Where each moved place was, before its copy lands (`in_place` writes
    # over `world`): what is inside it moves by the same step.
    moved_from = {
        pid: (p.x, p.y)
        for pid in delta.moved
        if (p := world.places.get(pid)) is not None and p.x is not None and p.y is not None
    }
    # A known place the delta changed is carried whole; one not on this
    # fold's map and not made here was another take's (`WorldDelta.added`).
    places = [
        p
        for p in delta.places
        if p.id in world.places or delta.added is None or p.id in delta.added
    ]
    known = {**world.places, **{p.id: p for p in places}}
    unplaced = [
        p.model_copy(deep=True)
        for p in places
        if (p.x is None or p.y is None) and not moves(known, p)
    ]
    if unplaced:
        # Placed against the map with the delta's placed places on it: an
        # imported map's place with `in:` and no `at:` was placed with its
        # parent unseen, at the origin (the second review).
        seeded = world.model_copy()
        seeded.places = {
            **world.places,
            **{p.id: p for p in places if p.x is not None and p.y is not None},
        }
        place_new(seeded, unplaced, delta.links, None)
        fixed = {p.id: p for p in unplaced}
    else:
        fixed = {}
    for place in places:
        out.places[place.id] = fixed.get(place.id) or place.model_copy(deep=True)
    for pid, (from_x, from_y) in moved_from.items():
        moved = out.places.get(pid)
        if moved is None or moved.x is None or moved.y is None:
            continue
        dx, dy = moved.x - from_x, moved.y - from_y
        for child_id in within_ids(out, pid):
            child = out.places[child_id]
            # Not one placed in this fold (it was placed within the moved
            # place where it is now), nor one the author moved on its own.
            if child_id in fixed or child_id in delta.moved or child.x is None:
                continue
            child.x, child.y = child.x + dx, child.y + dy  # type: ignore[operator]
        # Its ways measured from where it was; the delta's are added below.
        out.links = [k for k in out.links if pid not in (k.a, k.b)]
    for pid, new_name in delta.renamed.items():
        renamed = out.places.get(pid)
        if renamed is None:
            continue
        if key(renamed.name) != key(new_name) and renamed.name not in renamed.aliases:
            renamed.aliases.append(renamed.name)
        renamed.name = new_name
    for link in delta.links:
        out.links = [k for k in out.links if {k.a, k.b} != {link.a, link.b}]
        out.links.append(link.model_copy())
    for label, description in delta.unknowns.items():
        out.unknowns[label] = description
        out.unknowns_made = max(out.unknowns_made, int(label.split()[-1]))
    for label, name in delta.named.items():
        # Named: the label's place and travels are the name's from now on.
        out.unknowns_made = max(out.unknowns_made, int(label.split()[-1]))
        out.unknowns.pop(label, None)
        if label in out.where:
            out.where.setdefault(name, out.where.pop(label))
        if label in out.outside:
            out.outside = [name if n == label else n for n in out.outside]
        been = out.visited.pop(label, [])
        mine = out.visited.setdefault(name, [])
        mine.extend(p for p in been if p not in mine)
    for who, place_id in delta.where.items():
        out.where[who] = place_id
        seen = out.visited.setdefault(who, [])
        if place_id not in seen:
            seen.append(place_id)
    # Placed anew without the word (an older delta has none), someone is in
    # their place.
    for who in [*delta.where, *delta.outside]:
        if delta.outside.get(who) and who not in out.outside:
            out.outside.append(who)
        elif who in out.outside and not delta.outside.get(who):
            out.outside.remove(who)
    for thing in delta.things:
        out.things[thing.id] = thing.model_copy(deep=True)
    out.conflicts.extend(delta.conflicts)
    if delta.weather is not None:
        out.weather = delta.weather
    for pid in delta.removed:
        _remove(out, pid)
    seen = list(delta.seen)
    if not base:
        seen += [p.id for p in delta.places]
        seen += list(delta.where.values())
        seen += [end for link in delta.links for end in (link.a, link.b)]
    for place_id in seen:
        if place_id not in out.seen:
            out.seen.append(place_id)
    _prune(out)
    return out


def _remove(world: WorldMap, place_id: str) -> None:
    """A place taken off the map by the author's Direction: what was inside
    it is inside its parent now (where it stands), as are anyone and
    anything that were there (nowhere, with no parent); its ways go. The
    rest (seen, visited) `_prune` cleans."""
    gone = world.places.pop(place_id, None)
    if gone is None:
        return
    parent = gone.inside if gone.inside in world.places else None
    for place in world.places.values():
        if place.inside == place_id:
            place.inside = parent
    world.links = [k for k in world.links if place_id not in (k.a, k.b)]
    where: dict[str, str] = {}
    for who, at in world.where.items():
        if at != place_id:
            where[who] = at
        elif parent is not None:
            where[who] = parent
            been = world.visited.setdefault(who, [])
            if parent not in been:
                been.append(parent)
    world.where = where
    for thing in world.things.values():
        if thing.at == place_id:
            thing.at = parent
    if world.start == place_id:
        world.start = parent


def _prune(world: WorldMap) -> None:
    """Whatever names a place that isn't on the map goes: a take switched
    moves what followed under the other take (`tree.move_continuation`), and
    the passages after it were read against the map the old take made, so
    their deltas name its places. Live (Oct 15 2026) the view indexed one
    of them and the transcript's whole reload died with it."""
    places = world.places
    for place in places.values():
        if place.inside is not None and place.inside not in places:
            place.inside = None
    world.links = [k for k in world.links if k.a in places and k.b in places]
    world.where = {who: pid for who, pid in world.where.items() if pid in places}
    for thing in world.things.values():
        if thing.at is not None and thing.at not in places:
            thing.at = None
    world.seen = [pid for pid in world.seen if pid in places]
    world.visited = {who: [p for p in ps if p in places] for who, ps in world.visited.items()}


def place_delta(
    world: WorldMap,
    delta: WorldDelta,
    held: str | None,
    near_point: tuple[float, float] | None = None,
) -> None:
    """Give the delta's new places their positions, for good, and note any
    stated distance the map's established places disagree with. `held`: the
    author's character, beside whom a place with nothing else to go on sits
    (`near_point`: exactly where they stand, when the program knows)."""
    near = world.where.get(held or "")
    place_new(world, delta.places, delta.links, near, near_point)
    # A way that moved a place (the author's Direction) is its new spot, not
    # a disagreement with where the map had it.
    standing = [k for k in delta.links if k.a not in delta.moved and k.b not in delta.moved]
    for note in geometry_conflicts(world, standing):
        if note not in delta.conflicts:
            delta.conflicts.append(note)


def world_at(
    path: Sequence[Node],
    base: WorldDelta | None = None,
    *,
    start: str | None = None,
    hidden: Collection[str] = (),
) -> WorldMap:
    """The map at the end of `path`: an imported map (`base`) if the story
    has one, then every passage's delta, in order. A place linked to a plot
    entry in `hidden` (`WorldPlace.lore_id`) is left out, with its ways, the
    things in it and anyone placed there: the plot keeps it from every model
    until it is revealed, and from the author's map too."""
    world = WorldMap()
    if base is not None:
        world = applied(world, base, base=True)
    world.start = start if start in world.places else None
    if world.start is not None and world.start not in world.seen:
        world.seen.append(world.start)  # where the story begins is never under fog
    for node in path:
        if node.meta.world is not None:
            world = applied(world, node.meta.world, in_place=True)  # ours alone: fresh above
    gone = {p.id for p in world.places.values() if p.lore_id is not None and p.lore_id in hidden}
    if not gone:
        return world
    inside = True
    while inside:  # anything inside a hidden place is hidden with it
        more = {p.id for p in world.places.values() if p.inside in gone and p.id not in gone}
        gone |= more
        inside = bool(more)
    world.places = {k: v for k, v in world.places.items() if k not in gone}
    world.links = [k for k in world.links if k.a not in gone and k.b not in gone]
    world.where = {who: pid for who, pid in world.where.items() if pid not in gone}
    world.things = {k: t for k, t in world.things.items() if t.at not in gone}
    world.seen = [pid for pid in world.seen if pid not in gone]
    world.visited = {who: [p for p in ps if p not in gone] for who, ps in world.visited.items()}
    if world.start in gone:
        world.start = None
    return world
