"""Map pictures from an image model (`Config.map_pictures`): what the map
still needs drawn, as prompts. Pure; the session sends them
(engine/session_world_art.py) and the view cuts them out
(gui/map_art.py).

The author approved these as a class (Settings → Images → Draw map pictures,
Oct 2026): off until turned on, the model and a daily limit chosen there,
every request logged with its prompt. Story pictures still ask each time.

Measured (SealedLoreDocs design/map.md, "The look"): on a plain white
background Qwen Image and Z Image Turbo give sprites the program can cut out,
and characters read well at map size; whole scenes and repaints don't keep
the story's geography, so the program's tileset stays the ground.

What comes first: the author's character, then whoever is with them, the
rest of the cast the story has placed; then the postcard of where the story
is; then places shown on the map, nearest first; then things; then the other
postcards. A place or thing the story changed is drawn from its
last picture (when the model takes pictures), so it stays the same barn.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.world_art import LAYOUT_HEIGHT, LAYOUT_WIDTH, MAX_FAILURES, SPRITE_KINDS
from sealedlore.engine.world_layout import position
from sealedlore.models.world import MapPicture, WorldArt, WorldMap, WorldPlace
from sealedlore.providers.images import ImageModelInfo

# Where map pictures are kept, inside the story's pictures folder.
MAP_PICTURES_DIR = "images/map"
# What a card says of someone, at most, in a prompt.
LOOKS_CHARS = 420
# The isometric view is laid over its area's plan on the map, so it is painted
# in the shape the model lists nearest the plan's (5:3), at most this wide.
ISOMETRIC_WIDEST = 1536


@dataclass(frozen=True)
class Person:
    """Someone who may have an avatar: their name and what their card says."""

    name: str
    looks: str


@dataclass(frozen=True)
class PictureJob:
    key: str
    subject: Literal["person", "place", "thing", "postcard", "isometric", "walker", "item"]
    subject_id: str
    name: str
    prompt: str
    state: str = ""
    # The picture to draw it from (a change), by its file; None for a first.
    reference: str | None = None
    from_picture: str | None = None
    # An isometric view: the plan it is painted from (its `made_at`).
    plan: str = ""


def looks_of(summary: str | None, description: str | None) -> str:
    """What a card says of someone, shortened at a sentence for a prompt."""
    text = " ".join(" ".join(p.split()) for p in (summary, description) if p)
    if len(text) <= LOOKS_CHARS:
        return text
    cut = text[:LOOKS_CHARS]
    end = cut.rfind(". ")
    return cut[: end + 1] if end > LOOKS_CHARS // 2 else cut.rstrip() + "…"


def picture_size(model: ImageModelInfo | None, *, landscape: bool = False) -> str:
    """For a sprite, the smallest square size the model lists at 512 or more
    (the view shrinks it anyway); for a postcard, the widest landscape size
    up to 1360 across. Else the model's first size; "1024x1024" (a postcard
    "1024x768") when it isn't listed."""
    sizes = list(model.resolutions) if model is not None else []
    shapes = []
    for size in sizes:
        parts = size.replace("*", "x").split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            shapes.append((int(parts[0]), int(parts[1]), size))
    if landscape:
        wide = sorted(s for s in shapes if s[0] > s[1] and s[0] <= 1360)
        if wide:
            return wide[-1][2]
        return sizes[0] if sizes else "1024x768"
    big_enough = sorted(s for s in shapes if s[0] == s[1] and s[0] >= 512)
    if big_enough:
        return big_enough[0][2]
    return sizes[0] if sizes else "1024x1024"


def size_pixels(size: str) -> tuple[int, int]:
    """A listed size as (width, height) in pixels; a size with no numbers
    ("1k", "auto", "16:9") as 1024 by 576, the shape most such models paint."""
    parts = size.replace("*", "x").split("x")
    if len(parts) == 2 and all(p.isdigit() for p in parts) and all(int(p) > 0 for p in parts):
        return int(parts[0]), int(parts[1])
    return 1024, 576


def isometric_size(model: ImageModelInfo | None) -> str:
    """The isometric view's size: the landscape size the model lists nearest
    the shape of a plan (LAYOUT_WIDTH by LAYOUT_HEIGHT, 5:3), the larger on a
    tie, up to ISOMETRIC_WIDEST across. The map lays the view over the plan
    it was painted from, so the nearer its shape, the less is cut off (a 4:3
    view lost a tenth at the top and the bottom). Else as a postcard."""
    wanted = LAYOUT_WIDTH / LAYOUT_HEIGHT
    shapes: list[tuple[float, int, str]] = []
    for size in model.resolutions if model is not None else ():
        parts = size.replace("*", "x").split("x")
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        width, height = int(parts[0]), int(parts[1])
        if height < width <= ISOMETRIC_WIDEST:
            shapes.append((abs(width / height - wanted), -width * height, size))
    if shapes:
        return min(shapes)[2]
    return picture_size(model, landscape=True)


def plan_pictures(
    world: WorldMap,
    art: WorldArt,
    *,
    people: Sequence[Person],
    held: str | None,
    setting: str,
    edits: bool,
    texts: PromptTexts = DEFAULT_TEXTS,
    limit: int = 6,
) -> list[PictureJob]:
    """What the map still needs from the image model, in the order it is
    wanted. `edits`: the model takes a picture to work from (a change is
    then drawn from the last picture; otherwise drawn afresh). Nothing drawn,
    and nothing that failed too often, is asked again."""
    style = texts["map.picture.style"]
    here = world.where.get(held or "")
    jobs: list[tuple[tuple, PictureJob]] = []

    def failed(key: str) -> bool:
        return art.failures.get(key, 0) >= MAX_FAILURES

    def far(place_id: str | None) -> float:
        a, b = position(world, here), position(world, place_id)
        if a is None or b is None:
            return math.inf
        return math.hypot(a[0] - b[0], a[1] - b[1])

    for person in people:
        key = f"picture:person:{person.name}"
        if art.picture_for("person", person.name) is not None or failed(key):
            continue
        at = world.where.get(person.name)
        rank = 0 if person.name == held else 1 if at is not None and at == here else 2
        if rank == 2 and at is None:
            continue  # the story hasn't placed them: no avatar yet
        prompt = texts.fill(
            "map.picture.person",
            who=person.name,
            looks=person.looks,
            setting=setting,
            style=style,
        )
        jobs.append(
            (
                (0, rank, far(at), person.name),
                PictureJob(key, "person", person.name, person.name, prompt),
            )
        )

    shown = set(world.seen) | {pid for places in world.visited.values() for pid in places}
    if world.start is not None:
        shown.add(world.start)
    subjects: list[tuple[Literal["place", "thing"], str, str, str, str | None, int]] = [
        ("place", p.id, p.name, p.what, p.id, 2)
        for p in world.places.values()
        if p.kind in SPRITE_KINDS and p.id in shown
    ]
    subjects += [
        ("thing", t.id, t.name, "", world.where.get(t.carried_by or "") or t.at, 3)
        for t in world.things.values()
        if t.carried_by or t.at in shown
    ]
    for subject, subject_id, name, what, at, order in subjects:
        state = (
            world.places[subject_id].state if subject == "place" else world.things[subject_id].state
        )
        if art.picture_for(subject, subject_id, state) is not None:
            continue
        key = f"picture:{subject}:{subject_id}" + (f":{state}" if state else "")
        if failed(key):
            continue
        last = art.picture_for(subject, subject_id)
        if last is not None and edits:
            prompt = texts.fill(
                "map.picture.change", what=name, change=state or "as it was before", style=style
            )
            reference, from_picture = last.file, last.id
        else:
            words = name + (f": {what}" if what else "") + (f" ({state})" if state else "")
            prompt = texts.fill("map.picture.place", what=words, setting=setting, style=style)
            reference, from_picture = None, None
        jobs.append(
            (
                (order, 0, far(at), name),
                PictureJob(key, subject, subject_id, name, prompt, state, reference, from_picture),
            )
        )
    # Postcards: each place the author's character has been (not a road or a
    # vehicle), where they are now first.
    been = set(world.visited.get(held or "", []))
    if world.start is not None and not been:
        been.add(world.start)
    for place_id in been:
        place = world.places.get(place_id)
        if place is None or place.kind in ("route", "vehicle"):
            continue
        state = place.state
        if art.picture_for("postcard", place_id, state) is not None:
            continue
        key = f"picture:postcard:{place_id}" + (f":{state}" if state else "")
        if failed(key):
            continue
        last = art.picture_for("postcard", place_id)
        if last is not None and edits:
            prompt = texts.fill(
                "map.picture.postcard.change",
                place=place.name,
                change=state or "as it was before",
            )
            reference, from_picture = last.file, last.id
        else:
            inside = [p.name for p in world.places.values() if p.inside == place_id][:6]
            what = (place.what or place.kind or "").rstrip(".") + "."
            prompt = texts.fill(
                "map.picture.postcard",
                place=place.name,
                view="seen inside" if place.kind == "room" else "seen from outside",
                what=what + (f" ({state})" if state else ""),
                inside=f"In it: {', '.join(inside)}. " if inside else "",
                setting=setting,
            )
            reference, from_picture = None, None
        here_now = place_id == here or place_id == _container(world, here, place_id)
        jobs.append(
            (
                (1 if here_now else 4, 0, far(place_id), place.name),
                PictureJob(
                    key, "postcard", place_id, place.name, prompt, state, reference, from_picture
                ),
            )
        )
    jobs.sort(key=lambda pair: pair[0])
    return [job for _, job in jobs[:limit]]


def _container(world: WorldMap, inner: str | None, outer: str) -> str | None:
    """`outer` when `inner` is somewhere inside it, else None."""
    seen: set[str] = set()
    while inner is not None and inner in world.places and inner not in seen:
        if inner == outer:
            return outer
        seen.add(inner)
        inner = world.places[inner].inside
    return None


# --- The isometric view --------------------------------------------------------------


def area_plan(world: WorldMap, art: WorldArt, area_id: str):
    """The plan an area's view is painted from: its plan in its state, else
    its latest (as gui/map_render.plan_image draws it)."""
    area = world.places.get(area_id)
    if area is None:
        return None
    return art.layout_shown(area_id, area.state)


def views_of(art: WorldArt, area_id: str) -> list[MapPicture]:
    """Every isometric view an area has, oldest first: each Paint it (again)
    and each picture of the author's own adds one, and none is thrown away."""
    return [p for p in art.pictures if p.subject == "isometric" and p.subject_id == area_id]


def chosen_view(world: WorldMap, art: WorldArt, area_id: str) -> MapPicture | None:
    """The view the author chose for the area, for the map as it is there
    now (`WorldArt.chosen_views`), or None: none chosen under this state, or
    the one chosen is gone."""
    chosen = art.chosen_views.get(area_id, {}).get(isometric_state(world, area_id))
    return next((p for p in views_of(art, area_id) if p.id == chosen), None) if chosen else None


def shown_view(world: WorldMap, art: WorldArt, area_id: str) -> MapPicture | None:
    """The isometric view to lay over an area: the one the author chose
    for the map as it is there now (`chosen_view`; painting one chooses
    it), else the latest that is of the map as it is now (`view_current`),
    else the latest painted, out of date and said so. The latest alone laid
    a burning farm over the branch where it stands, and offered to paint it
    again (the review, Oct 15 2026)."""
    chosen = chosen_view(world, art, area_id)
    if chosen is not None:
        return chosen
    views = views_of(art, area_id)
    for picture in reversed(views):
        if view_current(world, art, area_id, picture):
            return picture
    return views[-1] if views else None


def view_current(world: WorldMap, art: WorldArt, area_id: str, picture: MapPicture) -> bool:
    """Whether an isometric view is of the map there as it is now: painted
    under the area's state (`isometric_state`) and from the plan it has now.
    A plan drawn again (Redraw plan…) makes it out of date. A view painted
    before views kept their plan counts as current when it is newer than the
    plan."""
    if picture.state != isometric_state(world, area_id):
        return False
    plan = area_plan(world, art, area_id)
    if plan is None:
        return True
    if picture.plan:
        return picture.plan == plan.made_at
    return not (picture.made_at and plan.made_at) or picture.made_at >= plan.made_at


def isometric_state(world: WorldMap, area_id: str) -> str:
    """What the isometric view of an area shows, as the map has it now: the
    area's own state and each changed place in it. A view painted under
    another is out of date (the map says so and offers to paint it again)."""
    area = world.places.get(area_id)
    if area is None:
        return ""
    parts = [area.state] if area.state else []
    for child in sorted(world.places.values(), key=lambda p: p.name):
        if child.inside == area_id and child.state:
            parts.append(f"{child.name}: {child.state}")
    return "; ".join(parts)


# The plan's look (world_art.plan_ground) in words, for the isometric view:
# the world bible is lore, not how the place looks (and Doomsville's virus
# and dead had two models refuse the picture as sensitive).
LOOK_WORDS = {
    "snow": "snow-covered country in winter",
    "grass": "green countryside",
    "dirt": "bare earth",
    "sand": "sand",
    "rock": "bare rock",
    "floor": "indoors",
    # A deck in space is "space" (the session says which); "metal" is
    # plating on the ground too (a domed town's).
    "metal": "metal decks and plating",
    "space": "a station's metal decks, in space",
    "hell": "cracked black volcanic rock glowing in its seams",
    "cobble": "cobbled streets",
}


def seen_change(art: WorldArt, place: WorldPlace) -> str:
    """How a changed place looks, from its plan in that state against the
    one before it: fire, ruins, walls down. Plain words a painter can draw;
    the story's own ("past saving, a furnace now") never said fire. Empty
    when there is no plan of the change to read."""
    after = art.layout_for(place.id, place.state) if place.state else None
    if after is None or not after.rows:
        return ""
    before = art.layout_for(place.id, "")
    cells = "".join(after.rows)
    seen = []
    if "*" in cells:
        seen.append("on fire")
    if cells.count(":") > len(cells) // 4:
        seen.append("in ruins")
    if before is not None and before.rows:
        walls_before = "".join(before.rows).count("#")
        if walls_before and cells.count("#") < walls_before * 0.6:
            seen.append("most of its walls down")
    return ", ".join(seen)


def _changed(world: WorldMap, area_id: str) -> list[WorldPlace]:
    area = world.places[area_id]
    inside = [p for p in world.places.values() if p.inside == area_id and p.state]
    return inside + ([area] if area.state else [])


def one_line(place: WorldPlace) -> str:
    """The area in one sentence: the first of its description, else its
    name. Measured (SealedLoreDocs design/map.md, Oct 15 2026): with the
    program's render, one line beat the writer's paragraph on layout (Qwen
    3.5 against 2.5, klein 3.1 against 2.75); the paragraph's inventions
    (a steeple, a water tower, "enclosed by a fence") were what got painted."""
    text = " ".join((place.what or "").split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    first = (match.group(1) if match else text).rstrip(".")
    return (first or place.name) + "."


def isometric_job(
    world: WorldMap,
    art: WorldArt,
    area_id: str,
    *,
    look: str = "",
    texts: PromptTexts = DEFAULT_TEXTS,
) -> PictureJob:
    """The isometric view of an area as a picture job: the program's own
    isometric render of its plan goes with the prompt (gui/map_iso.py, made
    on the painting's thread), the prompt says what the render is and asks
    for it repainted, any change first in words a painter can draw, the area
    in one line (`one_line`) and the ground's look (`world_art.plan_ground`).
    No prompt writer: measured, its paragraph cost layout."""
    area = world.places[area_id]
    now = ""
    for place in _changed(world, area_id):
        seen = seen_change(art, place)
        change = f"{seen} (the story: {place.state})" if seen else place.state
        now += texts.fill("map.picture.isometric.now", place=place.name, state=change)
    prompt = texts.fill(
        "map.picture.isometric",
        now=now,
        places=one_line(area),
        look=LOOK_WORDS.get(look, "as the map shows it"),
    )
    state = isometric_state(world, area_id)
    plan = area_plan(world, art, area_id)
    return PictureJob(
        key=f"isometric:{area_id}:{state}" if state else f"isometric:{area_id}",
        subject="isometric",
        subject_id=area_id,
        name=area.name,
        prompt=prompt,
        state=state,
        plan=plan.made_at if plan is not None else "",
    )
