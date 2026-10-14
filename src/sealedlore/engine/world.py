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

import re
from collections.abc import Iterable, Sequence
from typing import Any

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.scene_state import same_person
from sealedlore.engine.validators import locate_quote
from sealedlore.engine.world_layout import geometry_conflicts, moves, place_new
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


# --- The reply, held to account ----------------------------------------------


def parse_world_reply(text: str) -> dict[str, Any]:
    """The read's JSON object; ValueError when there is none."""
    return extract_json(text, dict, "map changes")


def _entries(reply: dict[str, Any], field: str) -> list[dict[str, Any]]:
    value = reply.get(field)
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


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
            changed.what = _text(entry.get("what")) or changed.what
            changed.state = _text(entry.get("state")) or changed.state
            changed.kind = kind or changed.kind  # type: ignore[assignment]
            if inside is not None and not _would_loop(world, delta, changed.id, inside):
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
        direction = _text(entry.get("direction")) or None
        words = _DIRECTIONS.findall(direction or "")
        if direction and words and not any(w.lower() in quote.lower() for w in words):
            direction = None  # a direction its own quote doesn't carry is a guess
        distance = _text(entry.get("distance")) or None
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
        if not who or _NOT_A_NAME.match(who) or not who[0].isupper():
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


def applied(world: WorldMap, delta: WorldDelta) -> WorldMap:
    """`world` with `delta` applied (a new map; `world` is left alone).
    Places come placed (`place_delta`); one that somehow isn't is placed here
    by the same rule, so the map is the same however often it is folded."""
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


def world_at(path: Sequence[Node]) -> WorldMap:
    """The map at the end of `path`: every passage's delta, in order."""
    world = WorldMap()
    for node in path:
        if node.meta.world is not None:
            world = applied(world, node.meta.world)
    return world
