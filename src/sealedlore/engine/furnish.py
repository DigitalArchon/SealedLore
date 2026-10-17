"""Furnishing a building for map-first play: pure. A model plans each room
(its purpose, the furniture it holds, each piece's footprint and where it
stands); the program places the pieces (engine/area.py `furnish`) with
walkways and doorways clear, at the story's density. Measured in
SealedLoreDocs tools/bakeoff/furnish.py: GLM 5.3 Flash planned three of four
buildings, and its JSON broke on the nine-room farmhouse, where Sonnet 4.6
did it.
"""

from __future__ import annotations

from typing import Any

from sealedlore.engine.area import Area
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.area import THING_VERBS, FurnishItem, FurnishPlan, FurnishRoom
from sealedlore.models.world import WorldPlace

FURNISH_MAX_TOKENS = 4000
# Pieces of one kind in a room at normal density, at most.
MAX_COUNT = 8


def _sides(area: Area, room_index: int) -> list[str]:
    room = area.rooms[room_index]
    x0, y0, x1, y1 = room.box
    out = []
    for d in area.doorways:
        touches = any(
            area.room_at(x + dx, y + dy) is room
            for x, y in d.tiles()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        )
        if not touches:
            continue
        mx, my = d.middle
        out.append("top" if my < y0 else "bottom" if my > y1 else "left" if mx < x0 else "right")
    return out


def room_lines(area: Area) -> str:
    lines = []
    for room in area.rooms:
        x0, y0, x1, y1 = room.box
        doors = ", ".join(_sides(area, room.index)) or "none"
        things = ", ".join(room.objects) or "nothing"
        lines.append(
            f"Room {room.index}: the {room.name}, {x1 - x0 + 1} x {y1 - y0 + 1} tiles, "
            f"{room.cells} floor tiles; doorways: {doors}; already in it: {things}."
        )
    return "\n".join(lines)


def furnish_messages(
    area: Area,
    place: WorldPlace,
    setting: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    *,
    level: str = "",
) -> list[PromptMessage]:
    """`level`: the name of the plan's level being furnished ("Upper
    floor"; engine/levels.py), told with the building's name; "" for the
    plan itself."""
    user = texts.fill(
        "map.furnish.user",
        setting=setting or "(not said)",
        name=f"{place.name}, {level.lower()}" if level else place.name,
        what=(" ".join(place.what.split()) or "(not said)")
        + (f" As the story has it now: {place.state}." if place.state else ""),
        rooms=room_lines(area),
    )
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["map.furnish.system"]),)),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


def _int(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(round(float(value)))))
    except (TypeError, ValueError, OverflowError):
        return default


def parse_furnishing(
    text: str, area: Area, *, place_id: str, state: str, model: str, made_at: str
) -> FurnishPlan:
    """The planner's reply, held to the area: rooms it has, sizes 1-4,
    counts up to MAX_COUNT, verbs from the menu's list. ValueError when it
    furnishes nothing."""
    data = extract_json(text, dict, "furnishing")
    rooms_in = data.get("rooms")
    if not isinstance(rooms_in, list):
        raise ValueError("the furnishing has no rooms")
    rooms: list[FurnishRoom] = []
    seen: set[int] = set()
    for entry in rooms_in:
        if not isinstance(entry, dict):
            continue
        index = _int(entry.get("room"), -1, 10_000, -1)
        if not (0 <= index < len(area.rooms)) or index in seen:
            continue
        seen.add(index)
        items = []
        for item in entry.get("items") or []:
            if not isinstance(item, dict):
                continue
            name = " ".join(str(item.get("name") or "").split())[:80]
            if not name:
                continue
            where = str(item.get("where") or "wall")
            verbs = [
                v for v in (item.get("verbs") or []) if isinstance(v, str) and v in THING_VERBS
            ]
            items.append(
                FurnishItem(
                    name=name,
                    w=_int(item.get("w"), 1, 4, 1),
                    h=_int(item.get("h"), 1, 4, 1),
                    where=where if where in ("wall", "middle", "corner") else "wall",
                    count=_int(item.get("count"), 0, MAX_COUNT, 1),
                    verbs=list(dict.fromkeys(verbs)),
                )
            )
        rooms.append(
            FurnishRoom(
                room=index,
                purpose=" ".join(str(entry.get("purpose") or "").split())[:60],
                items=items,
            )
        )
    if not any(r.items for r in rooms):
        raise ValueError("the furnishing furnishes nothing")
    return FurnishPlan(place_id=place_id, state=state, rooms=rooms, model=model, made_at=made_at)
