"""Map pictures from an image model (`Config.map_pictures_mode`): what the map
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

import re
from dataclasses import dataclass
from typing import Literal

from sealedlore.models.world import WorldArt, WorldMap, WorldPlace
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
    subject: Literal[
        "person", "place", "thing", "postcard", "isometric", "walker", "item", "texture", "backdrop"
    ]
    subject_id: str
    name: str
    prompt: str
    state: str = ""
    # The picture to draw it from (a change), by its file; None for a first.
    reference: str | None = None
    from_picture: str | None = None
    # A painting of a place: the plan it is painted from (its `made_at`),
    # and its moment (the time of day, the weather, who was there).
    plan: str = ""
    scene: str = ""


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


# How many people's looks and things' names a painting's prompt carries.
PAINTING_PEOPLE = 6
PAINTING_THINGS = 8
