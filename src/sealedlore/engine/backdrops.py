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

from collections.abc import Mapping

from sealedlore.engine.area import Area
from sealedlore.engine.takeable import thing_class
from sealedlore.engine.world_pictures import area_plan, isometric_state
from sealedlore.models.area import ThingRecord
from sealedlore.models.world import MapPicture, WorldArt, WorldMap

BACKDROP = "backdrop"
# The square the picture is painted at, indoors (a 1600-wide render) and out
# (2880): one shape in and out (grey bands above and below). The measured
# paintings were 1536; outdoors 2048 since Oct 23 2026: a 120x72 area gets
# 17 px a tile instead of 13, and Tinker's Row came back crisper (chimneys,
# smoke, roof tiles) at 1.4¢ against 1.1¢. The render goes as JPEG: a 2048
# PNG was refused as too large (HTTP 413).
INDOOR_SIDE = 1536
OUTDOOR_SIDE = 2048
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


def _side(area: Area, x: int, y: int) -> str:
    w, h = area.width, area.height
    distances = {"north": y, "south": h - 1 - y, "west": x, "east": w - 1 - x}
    return min(distances, key=distances.get)
