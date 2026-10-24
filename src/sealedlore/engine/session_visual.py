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
from collections.abc import Callable
from dataclasses import dataclass

from sealedlore.engine.area import Area, area_of, walk_metres, walk_seconds
from sealedlore.engine.look import (
    LOOK_MAX_TOKENS,
    LookReply,
    Rect,
    apply_look,
    look_messages,
    parse_look,
    things_in,
)
from sealedlore.engine.places import place_for
from sealedlore.engine.session_area import DEFAULT_START_MINUTE, _close_quietly
from sealedlore.engine.world_art import is_window
from sealedlore.models.area import Spot
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError
from sealedlore.threads import BackgroundThread

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
        """Where a journey can go: the known places with a plan to arrive
        in. Never the open country round a place (the author, Oct 24 2026:
        a window of it is "a strange looking space", and the ruling of Oct
        23 stands: players are always somewhere designed)."""
        world, art = self.world(), self._art_read()
        out = []
        for choice in self.travel_choices():
            target = area_of(world, art, choice[0])
            if target is not None and not is_window(target):
                out.append(choice)
        return out

    def visual_edge_choices(self, side: str) -> list[tuple[str, str, int]]:
        """Past an edge: the designed area straight on, if one adjoins, then
        the places that lie that way; never open country."""
        world, art = self.world(), self._art_read()
        out = []
        for choice in self.edge_choices(side):
            if choice[0] == "area":
                beyond = self._beyond(side)
                if beyond is None or is_window(beyond[0].area):
                    continue
            elif is_window(area_of(world, art, choice[0]) or ""):
                continue
            out.append(choice)
        return out

    def visual_stand(self, area_id: str, x: int, y: int, refund: int = 0) -> None:
        """The player is here now, the walk they were shown making cut short
        (a click elsewhere mid-walk): stood where the figure stands, the
        unwalked part's seconds given back."""
        state = self.area_state()
        held = self.held_name()
        if held is None:
            return
        state.spots[held] = Spot(area=area_id, x=x, y=y)
        state.seconds = max(0, state.seconds - max(0, refund))
        self._set_live(state)

    def unwalked_seconds(self, area: Area | None, steps: list[tuple[int, int]]) -> int:
        """How long the tiles not walked would have taken."""
        if area is None or len(steps) < 2:
            return 0
        return walk_seconds(walk_metres(area, steps))

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
        """On past this edge into the designed area that adjoins it; the
        area's name, or None when the way is barred (or only open country
        lies that way)."""
        beyond = self._beyond(side)
        if beyond is None or is_window(beyond[0].area):
            return None
        state = self.cross_edge(side)
        if state is None:
            return None
        me = state.spots.get(self.held_name() or "")
        area = self.area(me.area) if me is not None else None
        return area.name if area is not None else None

    # --- Look (engine/look.py) ------------------------------------------------------

    def look_rect(self, area_id: str, rect: Rect) -> Rect:
        """The part held to the area."""
        area = self.area(area_id)
        if area is None:
            return rect
        x0, y0, x1, y1 = rect
        x0, x1 = sorted((max(0, min(area.width - 1, x0)), max(0, min(area.width - 1, x1))))
        y0, y1 = sorted((max(0, min(area.height - 1, y0)), max(0, min(area.height - 1, y1))))
        return x0, y0, x1, y1

    def look_model_reads_pictures(self) -> bool:
        """Whether the Look's model is known to read pictures (the listing
        already fetched; a model it doesn't know is taken to)."""
        return self.reads_pictures(self.role_model("look")) is not False

    def start_look(
        self,
        area_id: str,
        rect: Rect,
        picture: str,
        done: Callable[[LookReply | None, str], None],
    ) -> bool:
        """Look closely at a part of the area, on a thread: the model is
        shown the part (`picture`, a data URL; "" sends the words alone) and
        what the game knows of it, and `done` gets its reply, or "" and why
        not, on the thread. The reply is made real by `apply_look_reply` on
        the GUI thread. False when nothing could be asked."""
        area = self.area(area_id)
        held = self.held_name()
        if area is None or held is None or self.in_private:
            return False
        world = self.world()
        place = world.places.get(area.place_id)
        entry = place_for(area.name, self.visible_lore())
        words = [place.what if place is not None else "", entry.content if entry else ""]
        model = self.role_model("look")
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("look", model),
            messages=look_messages(
                world=self.story.world_bible,
                place=f"{area.title}. " + " ".join(w for w in words if w),
                area=area,
                rect=rect,
                records=self.area_state().things,
                player=held,
                picture=picture if self.look_model_reads_pictures() else "",
                texts=self.texts,
            ),
            params=self.side_params(model, max_tokens=LOOK_MAX_TOKENS),
        )
        detached = getattr(self.provider, "detached", None)
        text = detached() if callable(detached) else self.provider
        things = things_in(area, rect)
        thread = BackgroundThread(
            target=self._look_run,
            args=(text, request, things, done, text is not self.provider),
            name="visual-look",
            daemon=True,
        )
        self._look_thread = thread
        thread.start()
        return True

    def _look_run(self, text: ChatProvider, request: ChatRequest, things, done, own: bool) -> None:
        reply: LookReply | None = None
        problem = ""
        try:
            log_ref = self._log("look_request", {"payload": self.provider.build_payload(request)})
            try:
                answer, completed = text.complete(request)
                self._log(
                    "look_response",
                    {"request_log_ref": log_ref, "text": answer, "usage": completed.raw_usage},
                )
                self.unreported_usage.append(self.priced(completed.usage, request.model))
                reply = parse_look(answer, things)
            except (ProviderError, ValueError) as exc:
                problem = str(exc)[:300]
                self._log("look_error", {"request_log_ref": log_ref, "error": problem})
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            problem = f"{type(exc).__name__}: {exc}"[:300]
            self._log("look_error", {"error": problem})
        finally:
            if own:
                _close_quietly(text)
        done(reply, problem)

    def apply_look_reply(self, area_id: str, rect: Rect, reply: LookReply) -> list[str]:
        """What the look found, made real on the live state (GUI thread);
        the lines for the log."""
        area = self.area(area_id)
        if area is None:
            return []
        state = self.area_state()
        lines = apply_look(state.things, area, rect, reply)
        self._set_live(state)
        return lines
