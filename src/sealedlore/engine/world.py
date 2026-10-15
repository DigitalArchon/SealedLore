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
from collections.abc import Collection, Iterable, Sequence
from typing import Any

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.scene_state import same_person
from sealedlore.engine.validators import locate_quote
from sealedlore.engine.world_layout import (
    describe_metres,
    geometry_conflicts,
    moves,
    place_new,
    position,
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
    wanted = key(name)
    return next((t for t in world.things.values() if key(t.name) == wanted), None)


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
        lines.append(f"- {who}: {place_name(world, place_id)}")
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
) -> list[PromptMessage]:
    user = (
        f"THE MAP BEFORE THE PASSAGE\n\n{render_map(world)}\n\n"
        f"PEOPLE: {', '.join(people) or '(none named yet)'}\n"
        f"The author's character: {held or 'not set'}\n\n"
        f"{author_label.upper()}:\n{author_turn}\n\n"
        f"THE LATEST PASSAGE:\n{passage}"
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
    far = describe_metres(distance)
    if distance >= _MILES_FROM:
        miles = distance / _METRES_A_MILE
        far += f" / {miles:.0f} miles" if distance >= _WHOLE_MILES_FROM else f" / {miles:.1f} miles"
    angle = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return f"{far} {_COMPASS[round(angle / 45) % 8]}"


def _around(world: WorldMap, here: str | None, place: WorldPlace, fixed: Collection[str]) -> str:
    where = _how_far(world, here, place.id, fixed)
    head = f"{place.name} ({place.kind or 'place'}" + (f", {where}" if where else "") + ")"
    what = " ".join(place.what.split())
    return f"{head}: {what}" if what else head


def render_map_context(
    world: WorldMap,
    held: str | None,
    texts: PromptTexts = DEFAULT_TEXTS,
    *,
    fixed: Collection[str] = (),
) -> str:
    """The local map for the storyteller's tail: where the author's character
    is (the start before any read), what is inside it, what else is around
    it at each level up, the ways between them, and the things here. `world`
    is `established`; `fixed` are the places a plot file put, whose distance
    and bearing are given, from the nearest of them the character is in.
    Empty without a place."""
    here_id = world.where.get(held or "") or world.start
    chain = _chain(world, here_id)
    if not chain:
        return ""
    here = chain[0]
    origin = next((p.id for p in chain if p.id in fixed), None)
    lines = [SECTION_MAP_HEADING, texts["map.context"], ""]
    who = held or "The author's character"  # a Question before anyone is held
    lines.append(f"{who} is at: " + here.name + "".join(f", in {o.name}" for o in chain[1:]))
    if here.what:
        lines.append("  " + " ".join(here.what.split()))
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
        for t in world.things.values()
        # Carried by the held character, never "carried by nobody": with no
        # one held yet, every thing on the map was "here".
        if (held is not None and t.carried_by == held) or (t.at is not None and t.at in near)
    ][:MAP_LIST_LIMIT]
    if things:
        lines.append("Things here:")
        for thing in things:
            at = (
                f"carried by {thing.carried_by}"
                if thing.carried_by
                else place_name(world, thing.at)
            )
            lines.append(f"- {thing.name}: {at}" + (f", {thing.state}" if thing.state else ""))
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


# What a read writes when the passage said nothing ("unknown" went into a way,
# live): no words at all, or it reaches the storyteller as a fact.
_NOTHING_SAID = re.compile(
    r"^(unknown|unclear|unspecified|not (stated|given|specified|known)|none|n/?a|-+|\?+)$",
    re.IGNORECASE,
)


def _said(value: Any) -> str | None:
    text = _text(value)
    return None if not text or _NOTHING_SAID.match(text.strip(" .")) else text


def resolve_reply(
    world: WorldMap,
    reply: dict[str, Any],
    *,
    source: str,
    people: Sequence[Character],
) -> tuple[WorldDelta, list[str]]:
    """What the reply changes, as a delta on `world`, and what was refused
    and why (for the log and the panel). `source` is the author's turn and
    the passage: every entry's quote must be found in it."""
    delta = WorldDelta()
    refused: list[str] = []

    def quoted(entry: dict[str, Any]) -> str | None:
        quote = _text(entry.get("quote"))
        span = locate_quote(source, quote) if quote else None
        return source[span[0] : span[1]] if span else None

    # Places first, so links and people can name places added in this reply.
    for entry in _entries(reply, "places"):
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
            changed.state = _text(entry.get("state")) or changed.state
            changed.kind = changed.kind or kind  # type: ignore[assignment]
            if (
                changed.inside is None
                and changed.x is None  # a placed place has its spot: never moved
                and inside is not None
                and not _would_loop(world, delta, changed.id, inside)
            ):
                changed.inside = inside
            delta.places = [p for p in delta.places if p.id != existing] + [changed]
        else:
            delta.places.append(
                WorldPlace(
                    name=name,
                    kind=kind,  # type: ignore[arg-type]
                    inside=inside,
                    what=_text(entry.get("what")),
                    state=_text(entry.get("state")),
                )
            )
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
        if _is_vehicle(world, delta, a) or _is_vehicle(world, delta, b):
            refused.append(f"link {label}: a vehicle moves, so it has no fixed link")
            continue
        direction = _said(entry.get("direction"))
        words = _DIRECTIONS.findall(direction or "")
        if direction and words and not any(w.lower() in quote.lower() for w in words):
            direction = None  # a direction its own quote doesn't carry is a guess
        distance = _said(entry.get("distance"))
        old = next(
            (link for link in [*world.links, *delta.links] if {link.a, link.b} == {a, b}),
            None,
        )
        if old is not None:
            if old.distance and distance and key(distance) != key(old.distance):
                delta.conflicts.append(
                    f"{place_name_any(world, delta, a)} – {place_name_any(world, delta, b)}: "
                    f"the map keeps {old.distance}; the passage says {distance}"
                )
                distance = old.distance
            distance = distance or old.distance
            if direction is None and old.direction and old.a == a:
                direction = old.direction
        delta.links = [k for k in delta.links if {k.a, k.b} != {a, b}]
        delta.links.append(WorldLink(a=a, b=b, direction=direction, distance=distance))
    for entry in _entries(reply, "where"):
        who = _text(entry.get("who"))
        # A name starts with a capital; a description ("the woman") doesn't.
        # `islower`, not `not isupper`: a script with no case (CJK) is a name too.
        if not who or _NOT_A_NAME.match(who) or who[0].islower():
            refused.append(f"where {who or '(nobody)'}: not a name")
            continue
        if quoted(entry) is None:
            refused.append(f"where {who}: no quote found")
            continue
        place = find_place(world, entry.get("place"), delta.places)
        if place is None:
            refused.append(f"where {who} @ {_text(entry.get('place'))}: not on the map")
            continue
        canonical = person_name(who, people, [*world.where, *delta.where])
        delta.where[canonical] = place
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
            thing.carried_by = person_name(holder, people, [*world.where, *delta.where])
            thing.at = None
        elif at:
            thing.at = find_place(world, at, delta.places)
            thing.carried_by = None
        thing.state = _text(entry.get("state")) or thing.state
        delta.things = [t for t in delta.things if t.id != thing.id] + [thing]
    return delta, refused


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
    after = applied(world, delta)
    for who, place_id in delta.where.items():
        if world.where.get(who) != place_id:
            parts.append(f"{who} at {place_name(after, place_id)}")
    changed = [t.name for t in delta.things]
    if changed:
        parts.append("things: " + ", ".join(changed))
    if delta.conflicts:
        parts.append(f"{len(delta.conflicts)} distance kept against the passage")
    return "; ".join(parts) if parts else "no change"


# --- Folding ------------------------------------------------------------------


def applied(world: WorldMap, delta: WorldDelta, *, base: bool = False) -> WorldMap:
    """`world` with `delta` applied (a new map; `world` is left alone).
    Places come placed (`place_delta`); one that somehow isn't is placed here
    by the same rule, so the map is the same however often it is folded.

    A passage's places, and the places it puts people or ways between, are
    seen; an imported map (`base`) shows only the places it says are known."""
    out = world.model_copy(deep=True)
    known = {**world.places, **{p.id: p for p in delta.places}}
    unplaced = [
        p.model_copy(deep=True)
        for p in delta.places
        if (p.x is None or p.y is None) and not moves(known, p)
    ]
    if unplaced:
        place_new(world, unplaced, delta.links, None)
        fixed = {p.id: p for p in unplaced}
    else:
        fixed = {}
    for place in delta.places:
        out.places[place.id] = fixed.get(place.id) or place.model_copy(deep=True)
    for link in delta.links:
        out.links = [k for k in out.links if {k.a, k.b} != {link.a, link.b}]
        out.links.append(link.model_copy())
    for who, place_id in delta.where.items():
        out.where[who] = place_id
        seen = out.visited.setdefault(who, [])
        if place_id not in seen:
            seen.append(place_id)
    for thing in delta.things:
        out.things[thing.id] = thing.model_copy(deep=True)
    out.conflicts.extend(delta.conflicts)
    seen = list(delta.seen)
    if not base:
        seen += [p.id for p in delta.places]
        seen += list(delta.where.values())
        seen += [end for link in delta.links for end in (link.a, link.b)]
    for place_id in seen:
        if place_id not in out.seen:
            out.seen.append(place_id)
    return out


def place_delta(world: WorldMap, delta: WorldDelta, held: str | None) -> None:
    """Give the delta's new places their positions, for good, and note any
    stated distance the map's established places disagree with. `held`: the
    author's character, beside whom a place with nothing else to go on sits."""
    near = world.where.get(held or "")
    place_new(world, delta.places, delta.links, near)
    for note in geometry_conflicts(world, delta.links):
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
            world = applied(world, node.meta.world)
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
