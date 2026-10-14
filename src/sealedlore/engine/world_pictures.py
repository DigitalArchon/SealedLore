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
rest of the cast the story has placed, then places shown on the map, nearest
first, then things. A place or thing the story changed is drawn from its
last picture (when the model takes pictures), so it stays the same barn.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.world_art import MAX_FAILURES, SPRITE_KINDS
from sealedlore.engine.world_layout import position
from sealedlore.models.world import WorldArt, WorldMap
from sealedlore.providers.images import ImageModelInfo

# Where map pictures are kept, inside the story's pictures folder.
MAP_PICTURES_DIR = "images/map"
# What a card says of someone, at most, in a prompt.
LOOKS_CHARS = 420


@dataclass(frozen=True)
class Person:
    """Someone who may have an avatar: their name and what their card says."""

    name: str
    looks: str


@dataclass(frozen=True)
class PictureJob:
    key: str
    subject: Literal["person", "place", "thing"]
    subject_id: str
    name: str
    prompt: str
    state: str = ""
    # The picture to draw it from (a change), by its file; None for a first.
    reference: str | None = None
    from_picture: str | None = None


def looks_of(summary: str | None, description: str | None) -> str:
    """What a card says of someone, shortened at a sentence for a prompt."""
    text = " ".join(" ".join(p.split()) for p in (summary, description) if p)
    if len(text) <= LOOKS_CHARS:
        return text
    cut = text[:LOOKS_CHARS]
    end = cut.rfind(". ")
    return cut[: end + 1] if end > LOOKS_CHARS // 2 else cut.rstrip() + "…"


def picture_size(model: ImageModelInfo | None) -> str:
    """The smallest square size the model lists at 512 or more (the view
    shrinks it anyway), else its first; "1024x1024" when it isn't listed."""
    sizes = list(model.resolutions) if model is not None else []
    squares = []
    for size in sizes:
        parts = size.replace("*", "x").split("x")
        if len(parts) == 2 and parts[0] == parts[1] and parts[0].isdigit():
            squares.append((int(parts[0]), size))
    big_enough = sorted(s for s in squares if s[0] >= 512)
    if big_enough:
        return big_enough[0][1]
    return sizes[0] if sizes else "1024x1024"


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
        ("place", p.id, p.name, p.what, p.id, 1)
        for p in world.places.values()
        if p.kind in SPRITE_KINDS and p.id in shown
    ]
    subjects += [
        ("thing", t.id, t.name, "", world.where.get(t.carried_by or "") or t.at, 2)
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
    jobs.sort(key=lambda pair: pair[0])
    return [job for _, job in jobs[:limit]]
