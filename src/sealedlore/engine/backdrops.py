"""Painted backdrops for the play view (the author, Oct 23 2026: with moves
as turns, "accuracy of placement is suddenly less important and quality of
visuals can become a greater focus"; FLUX.3 Klein and the storybook look,
measured in SealedLoreDocs tools/testbed/data/painted_backdrops).

The program's own render of an area (its ground and the things fixed in it)
is repainted by the image model keeping the layout, and the painting is the
play view's ground; people, movable and carried things and the map's place
marks stay sprites over it at their spots, so the grid underneath remains
the truth and a drifted bench costs nothing. One painting per area and
state, kept as a map picture (subject "backdrop"); a changed state or a
redrawn plan makes it out of date and it is painted again.

Pure: what is wanted, the request the expander turns into Klein's prompt,
and which picture is current. The session paints
(engine/session_world_art.py); the window renders and trims
(gui/backdrop_render.py)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sealedlore.engine.area import Area
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.takeable import thing_class
from sealedlore.engine.world_pictures import PictureJob, area_plan, isometric_state
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.area import ThingRecord
from sealedlore.models.world import MapPicture, WorldArt, WorldMap

BACKDROP = "backdrop"
# The square the picture is painted at, indoors (a 1600-wide render) and out
# (2880): one shape in and out (grey bands above and below). 2048 outdoors
# was tried and the endpoint refused the request as too large (HTTP 413,
# Oct 23 2026) with the render as PNG; the measured paintings were 1536.
INDOOR_SIDE = 1536
OUTDOOR_SIDE = 1536
# A job that failed this often is left (as the map's other pictures).
GIVE_UP_AFTER = 2
# How much of the expander's reply is kept as the prompt, in characters.
PROMPT_MOST = 1800


def loose_things(area: Area, records: Mapping[str, ThingRecord] | None = None) -> list:
    """The placements drawn as sprites over a backdrop: whatever can be
    carried or driven, anything broken, and anything the story has moved,
    taken or put down (`records`, the area state's things by placement id).
    The furniture, fixed or merely heavy, is in the painting: the Gilded
    Gear painted with its workbench and shelves left out was a bare floor
    with pixel sprites on it (Oct 23 2026)."""
    moved = {
        pid
        for pid, r in (records or {}).items()
        if r.state in ("dropped", "broken", "taken", "gone")
    }
    return [
        f
        for f in area.placements
        if f.broken or f.id in moved or thing_class(f.name) in ("carried", "vehicle")
    ]


def painted_things(area: Area) -> list:
    """The placements the render sent to the painter carries: everything
    that stays put in the plan (fixed, and heavy furniture), whole."""
    return [
        f
        for f in area.placements
        if not f.broken and thing_class(f.name) not in ("carried", "vehicle")
    ]


def backdrop_state(world: WorldMap, area: Area) -> str:
    """What a backdrop of the area shows, as the map has it: the place's
    state and its changed children (`isometric_state`); "" for a window."""
    return isometric_state(world, area.place_id) if not area.window else ""


def backdrops_of(art: WorldArt, area_id: str) -> list[MapPicture]:
    return [p for p in art.pictures if p.subject == BACKDROP and p.subject_id == area_id]


def current_backdrop(world: WorldMap, art: WorldArt, area: Area) -> MapPicture | None:
    """The latest backdrop painted of the area as it is now: under its
    state and from the plan it has (a redrawn plan makes it out of date),
    else None."""
    state = backdrop_state(world, area)
    plan = area_plan(world, art, area.place_id)
    made = plan.made_at if plan is not None else ""
    for picture in reversed(backdrops_of(art, area.id)):
        if picture.state != state:
            continue
        if made and picture.plan and picture.plan != made:
            continue
        return picture
    return None


def backdrop_side(area: Area) -> int:
    return INDOOR_SIDE if area.indoor else OUTDOOR_SIDE


def backdrop_size(area: Area) -> str:
    side = backdrop_side(area)
    return f"{side}x{side}"


def job_key(area: Area, state: str) -> str:
    return f"{BACKDROP}:{area.id}:{state}" if state else f"{BACKDROP}:{area.id}"


def failed_enough(art: WorldArt, area: Area, world: WorldMap) -> bool:
    return art.failures.get(job_key(area, backdrop_state(world, area)), 0) >= GIVE_UP_AFTER


def door_words(area: Area) -> str:
    """Where the area's doors are, in words the painter can hold to: a
    door is the one place the play view puts a figure on purpose, and the
    paintings measured turned a house to face the other way."""
    parts: list[str] = []
    w, h = max(area.width, 1), max(area.height, 1)

    def along(x: int, y: int, side: str) -> str:
        share = x / w if side in ("north", "south") else y / h
        where = (
            "near the left end"
            if share < 0.34
            else "about the middle"
            if share < 0.67
            else "near the right end"
        )
        if side in ("west", "east"):
            where = where.replace("left end", "top").replace("right end", "bottom")
        return where

    if area.indoor:
        for x, y in area.exits:
            side = _side(area, x, y)
            parts.append(f"a door out in the {side} wall, {along(x, y, side)}")
        for e in area.entrances:
            if e.stairs:
                parts.append(f"stairs {e.side} at column {e.x}, row {e.y}")
    else:
        for name, (x0, y0, x1, y1) in area.buildings.items():
            doors = [e for e in area.entrances if e.place_id == name]
            title = area.names.get(name, "a building")
            for e in doors:
                side = e.side or _side_of_box(e.x, e.y, (x0, y0, x1, y1))
                parts.append(f"the door of {title} in its {side} wall")
    return "; ".join(parts)


def _side(area: Area, x: int, y: int) -> str:
    w, h = area.width, area.height
    distances = {"north": y, "south": h - 1 - y, "west": x, "east": w - 1 - x}
    return min(distances, key=distances.get)


def _side_of_box(x: int, y: int, box: tuple[int, int, int, int]) -> str:
    x0, y0, x1, y1 = box
    if y == y0:
        return "north"
    if y == y1:
        return "south"
    return "west" if x == x0 else "east"


def backdrop_request(
    area: Area,
    *,
    words: str,
    style: str,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """The short request the expander turns into the painter's instruction
    (and the prompt itself when no expander can run): the look, the place
    in words, what the picture shows (walls, floors, doorways, the grey
    padding) and the one rule, every wall, doorway, building and path
    exactly where it is, seen straight down."""
    doors = door_words(area)
    facts = texts.fill(
        "map.backdrop.facts.indoor" if area.indoor else "map.backdrop.facts.outdoor",
        doors=f" Doors: {doors}." if doors else "",
    )
    return texts.fill(
        "map.backdrop.request", style=style, words=" ".join(words.split()), facts=facts
    )


def backdrop_job(
    world: WorldMap,
    art: WorldArt,
    area: Area,
    *,
    words: str,
    style: str,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> PictureJob:
    state = backdrop_state(world, area)
    plan = area_plan(world, art, area.place_id)
    return PictureJob(
        key=job_key(area, state),
        subject=BACKDROP,
        subject_id=area.id,
        name=area.title,
        prompt=backdrop_request(area, words=words, style=style, texts=texts),
        state=state,
        plan=plan.made_at if plan is not None else "",
    )


def expander_messages(
    request: str, picture: str | None, texts: PromptTexts = DEFAULT_TEXTS
) -> list[PromptMessage]:
    """The expander's call: BFL's own upsampler instruction as the system
    message (`map.backdrop.expander`), the request with the render as the
    first image (none for a model that reads no pictures)."""
    part = ContentPart(text=request, image=picture or "")
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["map.backdrop.expander"]),)),
        PromptMessage(role="user", parts=(part,)),
    ]


def expanded_prompt(reply: str) -> str:
    """The expander's reply as the painter's prompt: one prose block, its
    thinking and any fence stripped, cut at a sentence past `PROMPT_MOST`;
    "" when it gave nothing usable."""
    text = reply
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    text = text.replace("```", " ")
    text = " ".join(text.split())
    if len(text) < 40:
        return ""
    if len(text) > PROMPT_MOST:
        cut = text[:PROMPT_MOST]
        end = cut.rfind(". ")
        text = cut[: end + 1] if end > PROMPT_MOST // 2 else cut
    return text


def people_sprites(names: Sequence[str]) -> list[str]:
    """Kept for symmetry with the painter's scene: a backdrop shows nobody."""
    return []
