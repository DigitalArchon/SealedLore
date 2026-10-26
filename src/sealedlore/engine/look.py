"""A visual story's Look (the author, Oct 24 2026): the player highlights a
part of the area, or a thing in it, and looks closely. A model that reads
pictures is shown that part as the play view shows it and told what the
game knows of it; it says what the player sees and what they find, and
what it finds becomes real in the game: the contents of the thing looked
in, or a thing of its own lying where the look was (`apply_look`), so a
sack looked into holds what the look said from then on.

The things in the part are given to the model by handle (T1, T2…), never by
their ids: live (Oct 26 2026), Sonnet 4.6 cut "<plan id>:object:4:22:25" at
its first colon, the find was taken to be lying in the open, and it was
dropped on the scarecrow's own tile, under it, where no click could reach
it. Looked at again, what the game knows of the part is said to be there
now, as it stands: told "FOUND HERE BEFORE … lying here: flannel shirt"
beside the scarecrow seen "dressed in a faded flannel shirt", the model
wrote that the player had pulled the clothes off it.

Pure: the request, the reply's shape, and what the reply does to the area's
records. The session makes the call (engine/session_visual.py) and the
window shows the picture and the words (gui/visual_window.py).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from sealedlore.engine.area import FOUND_PREFIX, Area, Placement, bare, stand_near
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.visual_rules import kind_of
from sealedlore.ids import new_id
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.area import Found, Spot, ThingRecord

Rect = tuple[int, int, int, int]  # x0, y0, x1, y1 in tiles, inclusive

# The world's words the look is given, at most.
WORLD_CHARS = 1200
# A find's count and name are held to sense.
MAX_COUNT = 50
MAX_FOUND = 8
LOOK_MAX_TOKENS = 1200
# The model knows the things in the part by these: T1, T2…
HANDLE = "T"
_HANDLE = re.compile(rf"^{HANDLE}(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True)
class LookFind:
    name: str
    count: int = 1
    kind: str = ""
    note: str = ""
    # The placement it is in or on; "" for lying in the open.
    inside: str = ""


@dataclass(frozen=True)
class LookReply:
    see: str
    found: list[LookFind] = field(default_factory=list)


def things_in(area: Area, rect: Rect) -> list[Placement]:
    """The things whose footprint touches the part, nearest the part's
    middle first."""
    x0, y0, x1, y1 = rect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    found = [
        p
        for p in area.placements
        if p.x <= x1 and p.x + p.w - 1 >= x0 and p.y <= y1 and p.y + p.h - 1 >= y0
    ]
    found.sort(key=lambda p: abs(p.centre[0] - cx) + abs(p.centre[1] - cy))
    return found


def part_words(area: Area, rect: Rect) -> str:
    """Where in the area the part is, in words: the room, and the part's
    size in metres."""
    x0, y0, x1, y1 = rect
    room = area.room_at((x0 + x1) // 2, (y0 + y1) // 2)
    where = room.name if room is not None and room.name else area.name
    width = round((x1 - x0 + 1) * area.tile_m, 1)
    height = round((y1 - y0 + 1) * area.tile_m, 1)
    return f"in {where}, a patch about {width} by {height} metres"


def handle(index: int) -> str:
    """The model's name for the `index`th thing in the part (from 0)."""
    return f"{HANDLE}{index + 1}"


def _thing_lines(things: Sequence[Placement], records: Mapping[str, ThingRecord]) -> str:
    """What is in the part now, by handle: each thing, whether it is broken
    or lies where someone put it, and what it holds."""
    lines = []
    for index, p in enumerate(things):
        record = records.get(p.id)
        state = ""
        if (record is not None and record.state == "broken") or p.broken:
            state = " (broken)"
        elif record is not None and record.spot is not None:
            state = " (lying loose)"
        contents = ""
        if record is not None and record.contents:
            contents = " — in or on it: " + ", ".join(_count(f) for f in record.contents)
            if record.stocked:
                contents += ", and nothing else"
        elif record is not None and record.stocked:
            contents = " — holds nothing"
        if record is not None and record.taken_out:
            contents += " — taken from it since: " + ", ".join(record.taken_out)
        lines.append(f"- {handle(index)}: {p.name}{state}{contents}")
    return "\n".join(lines) or "- nothing the game knows of; bare ground or floor"


def _same_find(a: str, b: str) -> bool:
    """Two finds of one thing ("a hunting rifle", "hunting rifle"): live, the
    model listed the design's own contents again as new finds."""
    return bare(a).casefold() == bare(b).casefold()


def _count(f: Found) -> str:
    return f"{f.name} ×{f.count}" if f.count > 1 else f.name


def _before(things: Sequence[Placement], records: Mapping[str, ThingRecord]) -> str:
    """The words each thing in the part was seen in, at earlier looks."""
    seen = [
        f'- {p.name}: "{records[p.id].seen}"'
        for p in things
        if p.id in records and records[p.id].seen
    ]
    return "\n".join(seen) or "- nothing: this part hasn't been looked at closely"


def look_messages(
    *,
    world: str,
    place: str,
    area: Area,
    rect: Rect,
    records: Mapping[str, ThingRecord],
    player: str,
    picture: str = "",
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The Look's request: the rules, then the world, the place, the part,
    what stands there and what was found before, with the picture of the
    part (a data URL) when the model reads pictures."""
    things = things_in(area, rect)
    request = texts.fill(
        "look.request",
        world=" ".join(world.split())[:WORLD_CHARS] or "(nothing said of it)",
        place=" ".join(place.split()) or area.name,
        part=part_words(area, rect),
        things=_thing_lines(things, records),
        before=_before(things, records),
        player=player,
    )
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["look.system"]),)),
        PromptMessage(role="user", parts=(ContentPart(text=request, image=picture),)),
    ]


def holder_of(value: object, things: Sequence[Placement]) -> str:
    """The id of the thing a find's `in` names: its handle ("T2", "t2",
    "T2 (the sack)"), its id, or its name when one thing in the part has it;
    "" for none (lying in the open)."""
    want = " ".join(str(value or "").split())
    if not want or want.casefold() in ("null", "none"):
        return ""
    numbered = _HANDLE.match(want)
    if numbered is not None:
        index = int(numbered.group(1)) - 1
        return things[index].id if 0 <= index < len(things) else ""
    if any(p.id == want for p in things):
        return want
    named = [p.id for p in things if bare(p.name).casefold() == bare(want).casefold()]
    return named[0] if len(named) == 1 else ""


def parse_look(text: str, things: Sequence[Placement]) -> LookReply:
    """The reply held to its shape: `see` as words, each find with a name,
    a sane count, and an `in` that names a thing in the part (`holder_of`;
    else lying in the open). Raises ValueError on a reply with no JSON
    object."""
    reply = extract_json(text, dict, "what the player sees")
    see = " ".join(str(reply.get("see") or "").split())
    found: list[LookFind] = []
    for item in reply.get("found") or []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())
        if not name:
            continue
        try:
            count = max(1, min(MAX_COUNT, int(item.get("count") or 1)))
        except (TypeError, ValueError):
            count = 1
        found.append(
            LookFind(
                name=name[:80],
                count=count,
                kind=" ".join(str(item.get("kind") or "").split())[:30],
                note=" ".join(str(item.get("note") or "").split())[:160],
                inside=holder_of(item.get("in"), things),
            )
        )
        if len(found) >= MAX_FOUND:
            break
    return LookReply(see=see, found=found)


def apply_look(
    records: dict[str, ThingRecord],
    area: Area,
    rect: Rect,
    reply: LookReply,
    occupied: Collection[tuple[int, int]] = (),
) -> list[str]:
    """What the look found, made real (what the game already has in the
    part is never made again): a find in or on a thing goes into
    that thing's record (`contents`, the record made "seen" if it has none);
    one lying in the open becomes a record of its own on a free tile nearest
    the part's middle (never under a thing, nor where someone stands:
    `occupied`), so the area draws it and it can be taken. The thing looked
    at keeps the words it was seen in. The lines for the log."""
    x0, y0, x1, y1 = rect
    things = things_in(area, rect)
    taken = set(occupied)
    lines: list[str] = []
    for p in things[:1]:
        record = records.get(p.id)
        if record is None:
            record = records[p.id] = ThingRecord(state="seen", name=p.name)
        record.seen = reply.see
        record.searched = True
    for find in reply.found:
        found = Found(name=find.name, count=find.count, kind=find.kind, note=find.note)
        if any(_same_find(p.name, found.name) for p in things):
            # Already lying in the part (live, the tin of beans the design put
            # on the bedroom floor was found again, and there were two).
            continue
        if find.inside:
            holder = next((p for p in things if p.id == find.inside), None)
            record = records.get(find.inside)
            if record is None and holder is not None:
                record = records[find.inside] = ThingRecord(state="seen", name=holder.name)
            if record is not None:
                if any(_same_find(f.name, found.name) for f in record.contents):
                    continue  # known to be in it already (the design's, or found before)
                if record.stocked:
                    continue  # the kit's tables decided what it holds
                record.searched = True
                record.contents.append(found)
                lines.append(f"You find {_count(found)} in {holder.name if holder else 'it'}.")
                continue
        x, y = stand_near(area, ((x0 + x1) // 2, (y0 + y1) // 2), taken)
        taken.add((x, y))
        records[FOUND_PREFIX + new_id()] = ThingRecord(
            state="dropped",
            name=find.name,
            spot=Spot(area=area.id, x=x, y=y),
            contents=[],
            seen=find.note,
            count=find.count,
            kind=kind_of(find.name, find.kind),
            found_at=things[0].id if things else "",
        )
        lines.append(f"You find {_count(found)} here.")
    return lines
