"""A visual story's play (the author, Oct 24 2026: a separate game mode on
the Doomsville map, the visuals centre stage, every rule the program's). A
mixin for `StorySession`, over the map-first area rules
(engine/session_area.py): walking, doors, journeys and the clock, applied at
once with no turn written, since there is no storyteller to tell.

Step 1 of the design (SealedLoreDocs/design/visual-stories.md): moving
about the predesigned world and the clock. No model is called here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sealedlore.engine.area import area_of
from sealedlore.engine.session_area import DEFAULT_START_MINUTE
from sealedlore.models.area import Spot

# The opening's hour by its words, when the plot names no start.
TIME_WORDS = (
    ("dawn", 6 * 60 + 30),
    ("sunrise", 6 * 60 + 30),
    ("early morning", 7 * 60),
    ("morning", 9 * 60),
    ("noon", 12 * 60),
    ("midday", 12 * 60),
    ("late afternoon", 16 * 60),
    ("afternoon", 14 * 60 + 30),
    ("sunset", 18 * 60),
    ("dusk", 18 * 60 + 15),
    ("evening", 19 * 60 + 30),
    ("midnight", 0),
    ("night", 22 * 60),
)


@dataclass(frozen=True)
class VisualMove:
    """A move made on the play view: the tiles walked in the area set out
    from (one tile: no walk), the area arrived in (another through a door),
    and a line for the log ("" for a plain walk)."""

    steps: list[tuple[int, int]]
    from_area: str
    to_area: str
    words: str = ""


class VisualRuntime:
    def visual_start_minute(self) -> int:
        """The minute of the day the story opens at: the plot's `start:`
        (day 1, 15:30), else the opening's words ("late afternoon"), else
        nine in the morning."""
        plot = self.story.plot
        if plot is not None and getattr(plot, "start_minutes", None):
            return int(plot.start_minutes)
        words = (self.story.setup.starting_scene.time_of_day or "").casefold()
        for word, minute in TIME_WORDS:
            if re.search(rf"\b{word}\b", words):  # "noon" is not in "afternoon"
                return minute
        return DEFAULT_START_MINUTE

    def visual_go(self, x: int, y: int, *, beside: bool = False) -> VisualMove | None:
        """Walk to this tile (or beside it), and through the door there if it
        is one: applied now, the clock moved. None when there is no way."""
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        if held is None or me is None:
            return None
        steps = [tuple(s) for s in self.walk_route(x, y, beside=beside)]
        if not steps:
            return None
        from_area = me.area
        if len(steps) > 1:
            state = self.walked(steps)
        words = ""
        to_area = from_area
        if not beside:
            world, art = self.world(), self._art_read()
            end = Spot(area=from_area, x=steps[-1][0], y=steps[-1][1])
            through = self._through(state, held, end, world, art)
            if through is not None and self.go_through() is not None:
                spot, frm, to = through
                to_area = spot.area
                there = self.area(to_area, world, art)
                words = (
                    f"You go into {to}."
                    if there is not None
                    and there.indoor
                    and not (self.area(from_area) or there).indoor
                    else f"You step out into {to}."
                    if there is not None and not there.indoor
                    else f"You go through to {to}."
                )
        return VisualMove(steps=steps, from_area=from_area, to_area=to_area, words=words)

    def visual_travel_choices(self) -> list[tuple[str, str, int]]:
        """Where a journey can go: the known places with an area to arrive
        in (a place with nothing drawn has nowhere to stand)."""
        world, art = self.world(), self._art_read()
        return [
            choice for choice in self.travel_choices() if area_of(world, art, choice[0]) is not None
        ]

    def visual_travel(self, place_id: str) -> tuple[str, int] | None:
        """Set off for a place now: stood at its edge (or its way in), the
        clock moved by the way's length on foot. (The place's name, the
        seconds); None when the place can't be reached."""
        world, art = self.world(), self._art_read()
        place = world.places.get(place_id)
        choices = {pid: seconds for pid, _n, seconds in self.visual_travel_choices()}
        if place is None or place_id not in choices:
            return None
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        target = area_of(world, art, place_id)
        if held is None or me is None or target is None:
            return None
        landing = self._arrival(world, art, target, held)
        spot = (
            Spot(area=target, x=landing[0], y=landing[1])
            if landing is not None
            else self._stand(world, art, target, place_id, held, dict(state.spots), None)
        )
        if spot is None:
            return None
        frm = self.area(me.area, world, art)
        state.spots[held] = spot
        self._add_time(state, choices[place_id], frm.name if frm else "", place.name)
        self._set_live(state)
        return place.name, choices[place_id]

    def visual_cross(self, side: str) -> str | None:
        """On past this edge into the area that adjoins it; the area's
        name, or None when the way is barred."""
        state = self.cross_edge(side)
        if state is None:
            return None
        me = state.spots.get(self.held_name() or "")
        area = self.area(me.area) if me is not None else None
        return area.name if area is not None else None
