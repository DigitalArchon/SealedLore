"""A visual story's play (the author, Oct 24 2026: a separate game mode on
the Doomsville map, the visuals centre stage, every rule the program's). A
mixin for `StorySession`, over the map-first area rules
(engine/session_area.py): walking, doors, journeys and the clock, applied at
once with no turn written, since there is no storyteller to tell.

Step 1 of the design (SealedLoreDocs/design/visual-stories.md): moving
about the predesigned world and the clock. No model is called here.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from sealedlore.engine.area import (
    WALK_M_PER_S,
    Area,
    anchor_of,
    area_of,
    bare,
    can_take_record,
    in_earshot,
    path,
    sees,
    stand_near,
    walk_metres,
    walk_seconds,
)
from sealedlore.engine.cells import is_cell
from sealedlore.engine.chronicle import (
    armed,
    day_of,
    eligible,
    happen,
    initial_chronicle,
    resolve_due,
    set_fact,
    set_place,
)
from sealedlore.engine.dice import explain, roll_for
from sealedlore.engine.health import is_first_aid
from sealedlore.engine.keeper import (
    KEEPER_MAX_TOKENS,
    KeeperReply,
    Op,
    Ruling,
    action_messages,
    aftermaths,
    armed_text,
    direction_messages,
    facts_text,
    journey_messages,
    moment_messages,
    parse_keeper,
    parse_ruling,
    variant_for,
)
from sealedlore.engine.kits import container_rule, has_words, kit_layout, rng_for, stock
from sealedlore.engine.levels import level_area_id, place_of_area
from sealedlore.engine.look import (
    LOOK_MAX_TOKENS,
    LookReply,
    Rect,
    apply_look,
    look_messages,
    parse_look,
    things_in,
)
from sealedlore.engine.npc import (
    NPC_MAX_TOKENS,
    NpcReply,
    intent_words,
    npc_messages,
    parse_npc,
    player_description,
    sheet_of,
)
from sealedlore.engine.places import place_for
from sealedlore.engine.session_area import (
    DEFAULT_START_MINUTE,
    DOOR_SECONDS,
    GIVE_REACH_M,
    WINDING,
    _close_quietly,
)
from sealedlore.engine.takeable import can_be_taken
from sealedlore.engine.tile_sets import head_noun
from sealedlore.engine.visual_rules import (
    DRIVE_M_PER_S,
    FORCE_DIFFICULTY,
    FORCE_NOISE_M,
    GUN_NOISE_M,
    HEARD_M,
    PERSON_SIGHT_M,
    best_weapon,
    can_open,
    door_id,
    drivable,
    is_held,
    is_loud,
    is_out,
    item_actions,
    kind_of,
    sight_report,
    spawn_creatures,
    step_creatures,
)
from sealedlore.engine.world_art import is_window
from sealedlore.engine.world_layout import position, size_of
from sealedlore.ids import new_id
from sealedlore.models.area import HEARD_KEPT, Found, Spot, ThingRecord
from sealedlore.models.character import Character
from sealedlore.models.kit import Kit
from sealedlore.models.plot import Chronicle
from sealedlore.models.visual import DoorState, NpcGoal, PersonSheet, VisualDesign
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError
from sealedlore.storage.kits import load_kit
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


def _place_named(world, words: str):
    """The map's place a keeper's words name: by name, else the name that
    holds the words, else the one sharing most words with them, the last
    word (the head noun) counting double (live, "The Farm - Barn" for the
    barn). None for nothing like it."""
    want = [w for w in re.findall(r"[a-z]+", words.lower()) if w not in STOP_WORDS]
    if not want:
        return None
    places = list(world.places.values())
    folded = words.strip().casefold()
    exact = next((p for p in places if p.name.casefold() == folded), None)
    if exact is not None:
        return exact
    holding = next((p for p in places if folded in p.name.casefold()), None)
    if holding is not None:
        return holding
    best, best_score = None, 0
    for place in places:
        own = [w for w in re.findall(r"[a-z]+", place.name.lower()) if w not in STOP_WORDS]
        if not own:
            continue
        score = sum(1 for w in own if w in want) + (2 if own[-1] == want[-1] else 0)
        if score > best_score:
            best, best_score = place, score
    return best


def _made_flavour(place) -> str:
    """A made building's line the first time it is entered: its state and
    the first sentence of what it is."""
    if place is None:
        return ""
    words = re.split(r"(?<=[.!?])\s", place.what.strip(), maxsplit=1)[0] if place.what else ""
    if place.state:
        return f"{place.name} is {place.state}." + (f" {words}" if words else "")
    return words


def _is_the_thing(holder: str, taken: list[str]) -> bool:
    """Whether a holder's name names a thing taken out of it: "rifle
    leaning by the door" is the rifle it held, and nothing once that is
    taken; the cots aren't the blanket."""
    words = set(re.findall(r"[a-z]+", holder.lower()))
    return any((head := head_noun(name)) and head in words for name in taken)


def _reaches(area: Area, spot: tuple[int, int]) -> bool:
    """Whether a tile is joined to the area's walkable mass: a walk from it
    to the walkable tile nearest the middle exists."""
    if not area.walkable(*spot):
        return False
    blocked = area.blocked()
    middle = stand_near(area, (area.width // 2, area.height // 2), blocked)
    if middle == spot:
        return True
    return len(path(area, spot, middle, blocked=blocked - {spot})) > 1


def _minute_of(words: str) -> int | None:
    found = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*$", words or "")
    if found is None:
        return None
    return int(found.group(1)) * 60 + int(found.group(2) or 0)


def _in_shift(shift, minute: int) -> bool:
    """Whether the day's `minute` falls in the shift (which may cross midnight)."""
    start, end = _minute_of(shift.start), _minute_of(shift.end)
    if start is None or end is None:
        return False
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


# An item taken out of something, or laid by the design: a record of its own.
ITEM_PREFIX = "item:"
# A search of a container takes this long.
SEARCH_SECONDS = 30
# Waiting and sleeping, in minutes.
WAIT_MINUTES = 60
SLEEP_UNTIL = 7 * 60
# Words that name no thing (live, take_away "set of spark plugs" took the
# tin of beans by "of").
STOP_WORDS = frozenset({"the", "a", "an", "of", "my", "your", "some", "this", "that", "it"})
# The keeper is told this many of the things in the player's area.
STATE_THINGS = 30
# A conversation is a moment of the plot every this many of the player's
# lines, when an armed event's trigger speaks of talk (`talk_moment`).
TALK_MOMENT_LINES = 3
TALK_TRIGGER = re.compile(
    r"\b(?:talk\w*|ask\w*|question\w*|told|tell\w*|speak\w*|press\w*|convers\w*|learn\w*)\b",
    re.IGNORECASE,
)
# The noise of forcing a door or a gunshot reaches the dead in the area.
# What is no person's name (a field's name written for one).
NOT_NAMES = frozenset(
    {"who", "name", "stranger", "someone", "somebody", "person", "unknown", "a stranger"}
)
# Where the keeper stands someone a little way off from the player.
OFF_WORDS = ("nearby", "approaching", "in the distance", "a way off", "close by")


@dataclass(frozen=True)
class VisualMove:
    """A move made on the play view: the tiles walked in the area set out
    from (one tile: no walk), the area arrived in (another through a door),
    a line for the log ("" for a plain walk), what the rules reported as the
    time passed (the dead moving, attacks), and a door that stopped the
    move, locked (its tile)."""

    steps: list[tuple[int, int]]
    from_area: str
    to_area: str
    words: str = ""
    lines: list[str] = field(default_factory=list)
    locked: tuple[int, int] | None = None


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
        area = self.area(from_area)
        seconds = walk_seconds(walk_metres(area, steps)) if area is not None else 0
        seconds = int(seconds / max(0.2, self.visual_walk_factor(state)))
        if len(steps) > 1:
            state = self.walked(steps)
        words = ""
        to_area = from_area
        locked = None
        if not beside:
            world, art = self.world(), self._art_read()
            end = Spot(area=from_area, x=steps[-1][0], y=steps[-1][1])
            through = self._through(state, held, end, world, art)
            if through is not None:
                there = self.area(through[0].area, world, art)
                entering = (
                    there is not None and there.indoor and area is not None and not area.indoor
                )
                opens, why = (True, "")
                if entering:
                    opens, why = can_open(
                        state.doors.get(door_id(from_area, end.x, end.y)),
                        [r for _id, r in self.visual_inventory()],
                    )
                if not opens:
                    words, locked = why, (end.x, end.y)
                elif self.go_through() is not None:
                    spot, frm, to = through
                    to_area = spot.area
                    seconds += DOOR_SECONDS
                    self._companions_to(spot)
                    words = (
                        f"You go into {to}."
                        if there is not None and there.indoor and not area.indoor
                        else f"You step out into {to}."
                        if there is not None and not there.indoor
                        else f"You go through to {to}."
                    )
        lines = self.visual_tick(seconds)
        return VisualMove(
            steps=steps,
            from_area=from_area,
            to_area=to_area,
            words=words,
            lines=lines,
            locked=locked,
        )

    def visual_travel_choices(self) -> list[tuple[str, str, int]]:
        """Where a journey can go: every designed place with a plan to
        arrive in (not a room, a road or a region, not the place stood in or
        what holds it; a plot-hidden place isn't on the map at all), nearest
        first, with the way's length on foot. A visual story's places need
        no finding: the board is the game's map. Never the open country
        round a place (the author, Oct 24 2026: a window of it is "a
        strange looking space", and the ruling of Oct 23 stands: players
        are always somewhere designed)."""
        world, art = self.world(), self._art_read()
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        here = me.area if me is not None else world.where.get(self.held_name() or "")
        if here is None:
            return []
        here = place_of_area(here) or here
        start = self.held_point(state, world, art) or position(world, here)
        # Not where they are already: this area, what is in it, and (inside
        # a building) the area outside it.
        outer = world.places[here].inside if here in world.places else None
        local = area_of(world, art, outer) if outer else None
        roots = {here} | ({local} if local else set())
        current = {
            p.id
            for p in world.places.values()
            if p.id in roots or roots & {c.id for c in self._containers_of(world, p.id)}
        }
        out = []
        for place in world.places.values():
            if place.id in current or place.kind in ("room", "route", "region"):
                continue
            target = area_of(world, art, place.id)
            if target is None or is_window(target) or target in current:
                continue
            if place_of_area(target) in current:
                continue
            spot = position(world, place.id)
            if start is None or spot is None:
                continue
            metres = math.hypot(spot[0] - start[0], spot[1] - start[1]) * WINDING
            if metres < 50:
                continue
            out.append((place.id, place.name, int(metres / WALK_M_PER_S)))
        out.sort(key=lambda item: item[2])
        return out

    def visual_edge_choices(self, side: str) -> list[tuple[str, str, int]]:
        """Past an edge: the designed area straight on, if one adjoins, then
        the places that lie that way; never open country. With an overmap,
        the next cell that way (engine/session_overmap.py), whatever it is."""
        if self.overmap() is not None:
            beyond = self.overmap_edge(side)
            return [("area", beyond[0], beyond[1])] if beyond is not None else []
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

    def visual_drive_choices(self) -> list[tuple[str, str, list[tuple[str, str, int]]]]:
        """The vehicles the player can drive from here (a thing the keeper
        made to work, `drivable`, within reach in their area): (its record
        id, its name, the journeys by it with their seconds)."""
        state = self.area_state()
        area, me = self._visual_me(state)
        if area is None or me is None:
            return []
        out = []
        for placement in area.placements:
            record = state.things.get(placement.id)
            if record is None or record.state in ("gone", "taken") or not drivable(record):
                continue
            # The walk over to it first (the author needn't stand beside it).
            cx, cy = placement.centre
            route = self.walk_route(int(cx), int(cy), beside=True)
            up = walk_seconds(walk_metres(area, [tuple(t) for t in route])) if route else 0
            journeys = [
                (pid, name, up + max(60, int(seconds * WALK_M_PER_S / DRIVE_M_PER_S)))
                for pid, name, seconds in self.visual_travel_choices()
            ]
            out.append((placement.id, bare(record.name), journeys))
        return out

    def visual_travel(self, place_id: str, vehicle: str | None = None) -> tuple[str, int] | None:
        """Set off for a place now: stood at its edge (or its way in), the
        clock moved by the way's length on foot, or by `vehicle` (a drivable
        thing's record id: it comes too, and stands beside the player where
        they arrive). (The place's name, the seconds); None when the place
        can't be reached."""
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
        seconds = choices[place_id]
        ride = None
        if vehicle is not None:
            ride, journeys = None, []
            for vid, _name, found in self.visual_drive_choices():
                record = state.things.get(vid)
                if vid == vehicle and record is not None:
                    ride, journeys = (vid, record), found
            if ride is None:
                return None
            seconds = next(
                (s for pid, _n, s in journeys if pid == place_id),
                max(60, int(seconds * WALK_M_PER_S / DRIVE_M_PER_S)),
            )
        landing = self._arrival(world, art, target, held)
        there = self.area(target, world, art, things=state.things)
        if landing is not None and there is not None and not _reaches(there, landing):
            landing = None  # a corner cut off by trees or a wall (live, the gas station)
        spot = (
            Spot(area=target, x=landing[0], y=landing[1])
            if landing is not None
            else self._stand(world, art, target, place_id, held, dict(state.spots), None)
        )
        if spot is None:
            return None
        frm = self.area(me.area, world, art)
        state.spots[held] = spot
        self._add_time(state, seconds, frm.name if frm else "", place.name)
        if ride is not None:
            vid, record = ride
            there = self.area(target, world, art, things=state.things)
            placement = next((p for p in (frm.placements if frm else []) if p.id == vid), None)
            if there is not None:
                taken = there.blocked() | {
                    (s.x, s.y) for s in state.spots.values() if s.area == there.id
                }
                x, y = stand_near(there, (spot.x, spot.y), taken | {(spot.x, spot.y)})
                record.state, record.by = "dropped", ""
                record.spot = Spot(area=there.id, x=x, y=y)
                if placement is not None:
                    record.w, record.h = placement.w, placement.h
        self._set_live(state)
        self._companions_to(spot)
        return place.name, seconds

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
        standing = {(sp.x, sp.y) for sp in state.spots.values() if sp.area == area.id}
        standing |= {
            (c.x, c.y) for c in state.creatures.values() if c.area == area.id and c.state != "down"
        }
        lines = apply_look(state.things, area, rect, reply, occupied=standing)
        self._set_live(state)
        return lines

    # --- the rules (engine/visual_rules.py) ----------------------------------------

    def visual_design(self) -> VisualDesign:
        return self.story.visual_design or VisualDesign()

    def _visual_area_named(self, name: str) -> Area | None:
        """The area of the place of this name (the design names places)."""
        world, art = self.world(), self._art_read()
        want = name.strip().casefold()
        place = next((p for p in world.places.values() if p.name.casefold() == want), None)
        if place is None:
            return None
        area_id = area_of(world, art, place.id)
        return self.area(area_id, world, art) if area_id and not is_window(area_id) else None

    # --- The world kit (engine/kits.py, models/kit.py) ---------------------------

    def visual_kit(self) -> Kit | None:
        """The world kit beside the story's tile set: its item tables,
        building templates and yard pieces; None when the set has none."""
        return load_kit(self.tile_set_name(self._art_read()))

    def visual_seed(self) -> str:
        """What every roll of the story is fixed by: a new playthrough rolls
        afresh, the same one never does."""
        return self.story.id

    def visual_kit_plans(self) -> list[str]:
        """An inside made from the kit for each building or room that has no
        plan (a place the kit has a kind of building for), its main door on
        the side its door is in the ground round it; kept with the story's
        map art, so it is made once. The names of the places given one."""
        kit = self.visual_kit()
        if kit is None or not kit.buildings:
            return []
        world, art = self.world(), self._art_read()
        made: list[tuple[str, object]] = []
        for place in world.places.values():
            if place.kind not in ("building", "room") or art.layout_shown(place.id, place.state):
                continue
            door = self._door_side(place.id)
            layout = kit_layout(
                kit,
                place.id,
                place.name,
                place.what,
                size_m=size_of(place),
                door=door,
                seed=self.visual_seed(),
                state=place.state,
            )
            if layout is not None:
                made.append((place.name, layout))
        if not made:
            return []
        art = self.world_art  # takes the lock itself: read before it is held
        with self._lock():
            for _name, layout in made:
                art.layouts.setdefault(layout.place_id, {})[""] = layout
        self._save_art(art)
        return [name for name, _layout in made]

    def _door_side(self, place_id: str) -> str | None:
        """The side of a building its door out is on, from the ground round
        it (the parent's plan); None when nothing outside says."""
        world, art = self.world(), self._art_read()
        place = world.places.get(place_id)
        if place is None or place.inside not in world.places:
            return None
        outside = self.area(area_of(world, art, place.inside), world, art)
        if outside is None:
            return None
        return next((e.side for e in outside.entrances if e.place_id == place_id), None)

    def _stock(self, state, area: Area) -> None:
        """The kit's item tables rolled for an area the first time it is
        entered (CDDA's item groups): each container its template or the
        kit's container rules give a table holds what the roll says, and the
        floor's loot lies where the template put it. Rolled by the story's
        seed, so the same playthrough holds the same things."""
        if area.id in state.stocked:
            return
        state.stocked.append(area.id)
        kit = self.visual_kit()
        if kit is None:
            return
        seed = self.visual_seed()
        for p in area.placements:
            table, chance = p.stock, p.stock_chance
            if not table:
                rule = container_rule(kit, p.name)
                if rule is None:
                    continue
                table, chance = rule.table, rule.chance
            found = stock(kit, table, chance, rng_for(seed, area.id, p.id))
            record = state.things.get(p.id)
            if record is None:
                record = state.things[p.id] = ThingRecord(state="seen", name=p.name)
            if record.stocked:
                continue
            record.stocked = True
            for f in found:
                if not any(
                    bare(c.name).casefold() == bare(f.name).casefold() for c in record.contents
                ):
                    record.contents.append(f.model_copy(update={"kind": kind_of(f.name, f.kind)}))
        taken = area.blocked() | {(sp.x, sp.y) for sp in state.spots.values() if sp.area == area.id}
        taken |= {
            (r.spot.x, r.spot.y)
            for r in state.things.values()
            if r.spot is not None and r.spot.area == area.id
        }
        for x, y, table, chance in area.loot:
            for f in stock(kit, table, chance, rng_for(seed, area.id, "loot", x, y)):
                fx, fy = stand_near(area, (x, y), taken)
                taken.add((fx, fy))
                state.things[ITEM_PREFIX + new_id()] = ThingRecord(
                    state="dropped",
                    name=f.name,
                    spot=Spot(area=area.id, x=fx, y=fy),
                    count=f.count,
                    kind=kind_of(f.name, f.kind),
                    seen=f.note,
                )

    def visual_search(self, placement_id: str) -> str:
        """Search a thing: what it holds is seen, and listed in its menu, from
        now on (no model is asked; a look tells more). Half a minute passes."""
        state = self.area_state()
        area, me = self._visual_me(state)
        if area is None or me is None:
            return ""
        thing = next((p for p in area.placements if p.id == placement_id), None)
        if thing is None:
            return "It isn't here."
        if not self._in_reach(area, me, thing):
            return "You aren't close enough."
        record = state.things.get(placement_id)
        if record is None:
            record = state.things[placement_id] = ThingRecord(state="seen", name=thing.name)
        record.searched = True
        state.seconds += SEARCH_SECONDS
        self._set_live(state)
        name = bare(thing.name)
        if not record.contents:
            return f"You search the {name}: nothing of use."
        what = ", ".join(f"{f.name} ×{f.count}" if f.count > 1 else f.name for f in record.contents)
        return f"You search the {name} and find {what}."

    def visual_lay_out(self) -> None:
        """The design's dead, things and locks laid on the world, each
        place's once it is on the map (a place the overmap makes is laid
        when it is first made); its people stood where it puts them, each
        once their place is on the map (a plot-hidden place has no one in it
        until it is revealed)."""
        self.visual_kit_plans()
        state = self.area_state()
        began = state.overmap.begun
        self.overmap_begin(state)
        design = self.visual_design()
        world, art = self.world(), self._art_read()
        taken_by_area: dict[str, set[tuple[int, int]]] = {}

        def taken_in(area: Area) -> set[tuple[int, int]]:
            return taken_by_area.setdefault(
                area.id,
                area.blocked()
                | {(sp.x, sp.y) for sp in state.spots.values() if sp.area == area.id},
            )

        changed = began != state.overmap.begun
        for name, sheet in design.people.items():
            if not sheet.place or name in state.placed or not self.has_card(name):
                continue
            area = self._visual_area_named(sheet.place)
            if area is None:
                continue
            changed = True
            state.placed.append(name)
            taken = taken_in(area)
            # At the place itself when it is a part of the area (the south
            # gate on Hellsville's plan), else near the area's middle.
            want = sheet.place.strip().casefold()
            place = next((p for p in world.places.values() if p.name.casefold() == want), None)
            at = anchor_of(area, world, place.id) if place is not None else None
            x, y = stand_near(area, at or (area.width // 2, area.height // 2), taken, seed=name)
            taken.add((x, y))
            state.spots[name] = Spot(area=area.id, x=x, y=y)
            for thing in sheet.carries:
                state.things[ITEM_PREFIX + new_id()] = ThingRecord(
                    state="taken", name=thing, by=name, kind=kind_of(thing)
                )
        places = {
            p
            for p in (
                *(c.place for c in design.creatures),
                *(n.place for n in design.things),
                *(i.place for i in design.items),
                *(lock.place for lock in design.locked),
            )
        }
        laid = set(state.laid_places)
        if state.laid_out and not state.laid_places:
            # A story from before places were laid one at a time: whatever
            # was on its map then was laid.
            laid = {p for p in places if self._visual_area_named(p) is not None}
        due = {p for p in places - laid if self._visual_area_named(p) is not None}
        if not due:
            if changed or laid != set(state.laid_places) or not state.laid_out:
                state.laid_places, state.laid_out = sorted(laid), True
                self._set_live(state)
            return
        for spawn in design.creatures:
            if spawn.place not in due:
                continue
            area = self._visual_area_named(spawn.place)
            for creature in spawn_creatures(
                area,
                spawn.kind,
                spawn.count,
                spawn.ability,
                taken_in(area),
                f"{area.id}:{spawn.kind}",
            ):
                state.creatures[creature.id] = creature
        for note in design.things:
            if note.place not in due:
                continue
            area = self._visual_area_named(note.place)
            want = bare(note.name).casefold()
            for placement in area.placements:
                if bare(placement.name).casefold() != want:
                    continue
                record = state.things.get(placement.id)
                if record is None:
                    record = state.things[placement.id] = ThingRecord(
                        state="seen", name=placement.name
                    )
                record.tags = [t for t in note.tags if t and t not in record.tags] + record.tags
                if note.seen and not record.seen:
                    record.seen = note.seen
        for item in design.items:
            if item.place not in due:
                continue
            area = self._visual_area_named(item.place)
            kind = kind_of(item.name, item.kind)
            holder = None
            if item.inside:
                want = bare(item.inside).casefold()
                # On any floor of the place: a kit's farmhouse has its beds
                # upstairs.
                plan = art.layout_shown(area.place_id, "")
                floors = [area]
                for lv in plan.levels if plan is not None else []:
                    level = self.area(level_area_id(area.place_id, lv.index), world, art)
                    if level is not None:
                        floors.append(level)
                placements = [p for a in floors for p in a.placements]
                holder = next((p for p in placements if bare(p.name).casefold() == want), None)
                if holder is None:
                    # A kit's inside names it its own way: "a locked medicine
                    # cabinet" is the medicine cabinet the design means.
                    holder = next((p for p in placements if has_words(p.name, want)), None)
            if holder is not None:
                record = state.things.get(holder.id)
                if record is None:
                    record = state.things[holder.id] = ThingRecord(state="seen", name=holder.name)
                record.contents.append(Found(name=item.name, count=item.count, kind=kind))
                continue
            taken = taken_in(area)
            x, y = stand_near(area, (area.width // 2, area.height // 2), taken, seed=item.name)
            taken.add((x, y))
            state.things[ITEM_PREFIX + new_id()] = ThingRecord(
                state="dropped",
                name=item.name,
                spot=Spot(area=area.id, x=x, y=y),
                count=item.count,
                kind=kind,
            )
        for lock in design.locked:
            if lock.place not in due:
                continue
            inside = self._visual_area_named(lock.place)
            outside = self.area(inside.parent, world, art) if inside.parent else None
            if outside is None:
                continue
            for entrance in outside.entrances:
                if entrance.place_id == inside.id:
                    state.doors[door_id(outside.id, entrance.x, entrance.y)] = DoorState(
                        locked=True, key=lock.key, sealed=lock.sealed
                    )
        state.laid_places = sorted(laid | due)
        state.laid_out = True
        self._set_live(state)

    def visual_inventory(self) -> list[tuple[str, ThingRecord]]:
        """What the player carries: (record id, the record), in the order taken."""
        held = self.held_name()
        state = self.area_state()
        return [(rid, r) for rid, r in state.things.items() if r.state == "taken" and r.by == held]

    def visual_ability(self) -> tuple[int, tuple[str, int]]:
        """The player's combat ability and the weapon that adds to it."""
        design = self.visual_design()
        held = self.held_name() or ""
        base = design.abilities.get(held, design.default_ability) - self.needs_penalty()
        return max(1, base), best_weapon(r for _id, r in self.visual_inventory())

    def visual_hits_left(self) -> int:
        return max(0, self.visual_design().player_hits - self.area_state().wounds)

    def visual_item_actions(self, record: ThingRecord) -> tuple[str, ...]:
        """What a carried thing's menu offers: the rules' verbs for its kind,
        and the uses the keeper granted it."""
        verbs = item_actions(record.kind or kind_of(record.name))
        return tuple(verbs) + tuple(u for u in record.uses if u.lower() not in verbs)

    def visual_uses(self, placement_id: str) -> list[str]:
        """The uses the keeper granted a thing standing here."""
        record = self.area_state().things.get(placement_id)
        return list(record.uses) if record is not None else []

    def visual_attempt_words(self, verb: str, name: str, record_id: str = "") -> str:
        """A use chosen from a thing's menu, as the attempt the keeper is
        asked to rule on: the verb, the thing, and its state."""
        record = self.area_state().things.get(record_id) if record_id else None
        words = f"I {verb} the {bare(name)}"
        if record is not None and record.tags:
            words += f" ({', '.join(record.tags)})"
        return words + "."

    def _visual_me(self, state) -> tuple[Area | None, Spot | None]:
        me = state.spots.get(self.held_name() or "")
        return (self.area(me.area, things=state.things) if me is not None else None), me

    def _in_reach(self, area: Area, me: Spot, placement) -> bool:
        return any(max(abs(x - me.x), abs(y - me.y)) <= 1 for x, y in placement.tiles())

    def visual_take(self, placement_id: str) -> str:
        """Take a thing from the floor: the record says the player carries
        it. The line for the log, or why not."""
        state = self.area_state()
        area, me = self._visual_me(state)
        held = self.held_name()
        if area is None or me is None or held is None:
            return ""
        thing = next((p for p in area.placements if p.id == placement_id), None)
        if thing is None:
            return "It isn't here."
        if not self._in_reach(area, me, thing):
            return "You aren't close enough."
        record = state.things.get(placement_id)
        carried = record is not None and can_take_record(placement_id, record)
        if not can_be_taken(thing.name) and not carried:
            return f"The {bare(thing.name)} is too much to carry."
        if record is None:
            record = state.things[placement_id] = ThingRecord(state="taken", name=thing.name)
        record.state, record.by, record.spot = "taken", held, None
        record.kind = record.kind or kind_of(thing.name)
        source = state.things.get(record.found_at) if record.found_at else None
        if source is not None:
            # Found lying beside it: the thing it was found at is told it went.
            source.taken_out.append(bare(thing.name))
            record.found_at = ""
        self._visual_witness(state, f"took the {bare(thing.name)}")
        self._set_live(state)
        return f"You take the {bare(thing.name)}."

    def visual_take_content(self, holder_id: str, index: int) -> str:
        """Take one of the things a look found in or on a thing."""
        state = self.area_state()
        area, me = self._visual_me(state)
        held = self.held_name()
        record = state.things.get(holder_id)
        if area is None or me is None or held is None or record is None:
            return ""
        holder = next((p for p in area.placements if p.id == holder_id), None)
        if holder is not None and not self._in_reach(area, me, holder):
            return "You aren't close enough."
        if not 0 <= index < len(record.contents):
            return ""
        found = record.contents.pop(index)
        record.taken_out.append(bare(found.name))
        if not record.contents and _is_the_thing(record.name, record.taken_out):
            # The holder was the thing itself (the farmhouse's "rifle leaning
            # by the door" held the hunting rifle): nothing stands there now.
            record.state = "gone"
        state.things[ITEM_PREFIX + new_id()] = ThingRecord(
            state="taken",
            name=found.name,
            by=held,
            count=found.count,
            kind=kind_of(found.name, found.kind),
            seen=found.note,
        )
        self._set_live(state)
        what = f"{found.name} ×{found.count}" if found.count > 1 else found.name
        return f"You take {what}" + (f" from the {bare(record.name)}." if record.name else ".")

    def visual_drop(self, record_id: str) -> str:
        state = self.area_state()
        area, me = self._visual_me(state)
        record = state.things.get(record_id)
        if area is None or me is None or record is None or record.state != "taken":
            return ""
        taken = area.blocked() | {(sp.x, sp.y) for sp in state.spots.values() if sp.area == area.id}
        x, y = stand_near(area, (me.x, me.y), taken)
        record.state, record.by, record.spot = "dropped", "", Spot(area=area.id, x=x, y=y)
        self._set_live(state)
        return f"You put down the {bare(record.name)}."

    def visual_use(self, record_id: str, verb: str) -> str:
        """Eat, drink or use a carried thing: food and drink are used up;
        medicine heals a wound; anything else is a line."""
        state = self.area_state()
        record = state.things.get(record_id)
        if record is None or record.state != "taken":
            return ""
        kind = record.kind or kind_of(record.name)
        name = bare(record.name)
        if verb in ("eat", "drink") and kind in ("food", "drink"):
            record.count -= 1
            if record.count <= 0:
                record.state = "gone"
            self.note_ate(state, kind)
            self._set_live(state)
            return f"You {verb} the {name}." + (
                " That was the last of it." if record.state == "gone" else ""
            )
        if verb == "use" and (kind == "medicine" or is_first_aid(record.name)):
            words = self.visual_use_medicine(state, record)
            self._set_live(state)
            return words
        if verb == "use" and kind == "paper" and record.seen:
            return f"The {name}: {record.seen}"
        return ""  # no rule: the window sends it to the keeper as an attempt

    def visual_break(self, placement_id: str) -> str:
        state = self.area_state()
        area, me = self._visual_me(state)
        if area is None or me is None:
            return ""
        thing = next((p for p in area.placements if p.id == placement_id), None)
        if thing is None or not self._in_reach(area, me, thing):
            return "You aren't close enough."
        record = state.things.get(placement_id)
        if record is None:
            record = state.things[placement_id] = ThingRecord(state="broken", name=thing.name)
        record.state = "broken"
        self._visual_witness(state, f"broke the {bare(thing.name)}")
        self._set_live(state)
        lines = self.visual_tick(10, noise=(me.x, me.y), reach=FORCE_NOISE_M)
        return "\n".join([f"You break the {bare(thing.name)}. It makes a noise.", *lines])

    def visual_door(self, x: int, y: int) -> DoorState | None:
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        return state.doors.get(door_id(me.area, x, y)) if me is not None else None

    def visual_force(self, x: int, y: int) -> str:
        """Force a locked door: a strength check, and a noise."""
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        if me is None:
            return ""
        key = door_id(me.area, x, y)
        door = state.doors.get(key)
        if door is None or not door.locked:
            return "It isn't locked."
        if door.sealed:
            return "It won't give."
        base, weapon = self.visual_ability()
        roll = random.Random(f"{key}:{state.seconds}").randint(1, 6)
        if base + roll >= FORCE_DIFFICULTY + 3:
            door.forced = True
            words = "The door gives with a crack."
        else:
            words = "You throw your weight at the door; it holds."
        state.seconds += 20
        self._set_live(state)
        lines = self.visual_tick(0, noise=(me.x, me.y), reach=FORCE_NOISE_M)
        return "\n".join([words, *lines])

    def visual_attack(self, creature_id: str) -> list[str]:
        """The player attacks one of the dead beside them (the author's
        simple combat); a gun's noise draws the rest."""
        state = self.area_state()
        area, me = self._visual_me(state)
        creature = state.creatures.get(creature_id)
        if area is None or me is None or creature is None or creature.state == "down":
            return []
        if creature.area != area.id or max(abs(creature.x - me.x), abs(creature.y - me.y)) > 1:
            return ["It isn't within reach."]
        _base, weapon = self.visual_ability()
        rng = random.Random(f"{state.seconds}:{creature_id}")
        _outcome, lines = self._creature_round(state, area, me, creature, rng)
        state.seconds += 10
        if is_loud(weapon[0]):
            lines.append("The shot rolls out over the ground; anything near will have heard it.")
            self._visual_witness(state, f"fired the {bare(weapon[0])}")
        self._set_live(state)
        if state.dead:
            return lines
        noise = (me.x, me.y) if is_loud(weapon[0]) else None
        lines += self.visual_tick(0, noise=noise, reach=GUN_NOISE_M)
        return lines

    def visual_tick(
        self,
        seconds: int,
        *,
        noise: tuple[int, int] | None = None,
        reach: float = 0.0,
        resting: bool = False,
    ) -> list[str]:
        """Time passes: the injuries mend (or fester), the dead in the
        player's area move, and any beside the player attack (a round each,
        engine/health.py). `resting`: the time was spent resting (sleep, a
        wait), which heals faster. The lines for the log; the state saved."""
        state = self.area_state()
        area, me = self._visual_me(state)
        if area is None or me is None or state.dead:
            return []
        del reach  # every creature in the area hears a noise in it, for now
        lines: list[str] = self.needs_tick(state)
        lines += self.advance_healing(state, seconds, resting=resting)
        lines += self._step_people(state, area, me, seconds)
        self._keep_schedules(state, area)
        creatures = {c.id: c for c in state.creatures.values() if c.area == area.id}
        if not creatures:
            if state.wounds >= self.visual_design().player_hits:
                state.dead = True
                lines.append("That was one wound too many. You are dead.")
            self._set_live(state)
            return lines
        beside = step_creatures(
            area, creatures, (me.x, me.y), seconds, noise=noise, rng=random.Random(state.seconds)
        )
        rng = random.Random(f"{state.seconds}:tick")
        for cid in beside:
            if state.dead:
                break
            creature = creatures[cid]
            who = creature.name[0].upper() + creature.name[1:]
            _outcome, more = self._creature_round(state, area, me, creature, rng)
            lines += [f"{who} comes at you; {ln[0].lower()}{ln[1:]}" for ln in more[:1]] + more[1:]
        if state.wounds >= self.visual_design().player_hits:
            state.dead = True
            lines.append("That was one wound too many. You are dead.")
        self._set_live(state)
        return lines

    def visual_wait(self, minutes: int, *, resting: bool = True) -> list[str]:
        state = self.area_state()
        state.seconds += minutes * 60
        self._set_live(state)
        return self.visual_tick(minutes * 60, resting=resting)

    def visual_sleep(self) -> list[str]:
        """Sleep until seven in the morning; whoever is with the player
        (a companion, a friend here) sees to their injuries meanwhile."""
        clock = self.map_clock()
        now = clock[1] if clock else 0
        minutes = (SLEEP_UNTIL - now) % (24 * 60) or 24 * 60
        lines = [f"You sleep {minutes // 60} hours.", *self.visual_wait(minutes)]
        state = self.area_state()
        self.note_slept(state)
        area, _me = self._visual_me(state)
        if area is not None:
            lines += self.companions_tend(state, area)
        self._set_live(state)
        return lines

    def visual_report(self) -> list[str]:
        """What the player newly sees or hears where they stand, and the
        area's flavour line the first time they are in it."""
        state = self.area_state()
        area, me = self._visual_me(state)
        if area is None or me is None:
            return []
        lines: list[str] = []
        self._stock(state, area)
        if area.id not in state.flavour_said:
            design = self.visual_design()
            world = self.world()
            place = world.places.get(area.place_id)
            if is_cell(self._root_place(world, area.id)):
                # A made cell's building never takes the design's line by its
                # name (live, a burnt-out farmhouse north of the farm said
                # "Someone kept this place"): its own words, or the cell's.
                flavour = self._cell_flavour(world, area) or _made_flavour(place)
            else:
                flavour = design.flavour.get(place.name if place else area.name) or (
                    design.flavour.get(area.name)
                )
            state.flavour_said.append(area.id)
            if flavour:
                lines.append(flavour)
        lines += self.scout_report(state, area, me)
        people = {
            name: (sp.x, sp.y)
            for name, sp in state.spots.items()
            if sp.area == area.id and name != self.held_name()
        }
        seen, now = sight_report(
            area, (me.x, me.y), people, list(state.creatures.values()), state.seen_now
        )
        state.seen_now = now
        self._set_live(state)
        return lines + seen

    def visual_reset(self) -> None:
        """Start again from the opening: the live state dropped."""
        self.story.area_live = None
        self.save()

    # --- the people (engine/npc.py) --------------------------------------------------

    def visual_people(self) -> list[str]:
        """The people the design places or sheets, by name: those with a
        card. They stand where the design put them and where their intents
        took them (`settle` keeps their spots)."""
        design = self.visual_design()
        # As stored, never settled: `settle` asks this while settling.
        live = self.story.area_live
        placed = live.state.placed if live is not None else []
        names = [name for name in design.people if self.has_card(name)]
        return names + [n for n in placed if n not in names and self.has_card(n)]

    def visual_sheet(self, name: str) -> PersonSheet:
        """Who a person is: the design's sheet, or the one the keeper wrote
        for a stranger it brought in."""
        live = self.story.area_live
        strangers = live.state.strangers if live is not None else {}
        if name in strangers:
            return strangers[name]
        return sheet_of(name, self.visual_design().people)

    def visual_card(self, name: str):
        want = name.casefold()
        return next((c for c in (*self.cast, *self.supporting) if c.name.casefold() == want), None)

    def people_in_sight(self) -> list[str]:
        """The people in the player's area they can see or hear, nearest first."""
        state = self.area_state()
        area, me = self._visual_me(state)
        held = self.held_name()
        if area is None or me is None:
            return []
        found = []
        for name, spot in state.spots.items():
            if name == held or spot.area != area.id or name in state.people_down:
                continue
            here = (spot.x, spot.y)
            metres = math.hypot(spot.x - me.x, spot.y - me.y) * area.tile_m
            noticed = f"{name}@{area.id}" in state.noticed and sees(area, (me.x, me.y), here)
            if (
                (metres <= PERSON_SIGHT_M and sees(area, (me.x, me.y), here))
                or in_earshot(area, (me.x, me.y), here)
                or noticed
            ):
                found.append((abs(spot.x - me.x) + abs(spot.y - me.y), name))
        return [name for _d, name in sorted(found)]

    def talk_target(self, name: str) -> tuple[bool, str]:
        """Whether the player can speak to this person now: in their area,
        within earshot."""
        state = self.area_state()
        area, me = self._visual_me(state)
        spot = state.spots.get(name)
        if area is None or me is None or spot is None or spot.area != area.id:
            return False, f"{name} isn't here."
        if name in state.people_down:
            return False, f"{name} is down."
        tags = state.person_tags.get(name, [])
        if is_out(tags):
            return False, f"{name} is {', '.join(tags)} and can't answer."
        if not in_earshot(area, (me.x, me.y), (spot.x, spot.y)):
            return False, f"{name} is too far to hear you."
        return True, ""

    def start_talk(
        self, name: str, line: str, done: Callable[[NpcReply | None, str], None]
    ) -> bool:
        """What the player says to a person, answered by the model on a
        thread: `done` gets the reply (or None and why) on the thread;
        `apply_npc_reply` makes it real on the GUI thread. `line` "" is the
        encounter: the person has just seen the player."""
        if self.in_private:
            return False
        if line:
            can, _why = self.talk_target(name)
            if not can:
                return False
        elif name not in self.people_in_sight():
            return False  # the encounter: on sight, from any distance
        state = self.area_state()
        area, me = self._visual_me(state)
        card = self.visual_card(name)
        world = self.world()
        place = world.places.get(area.place_id)
        clock = self.map_clock()
        base, weapon = self.visual_ability()
        if line:
            state.talks.setdefault(name, []).append([self.held_name() or "You", line])
            self._heard(state, area.id, self.held_name() or "You", line)
            self.player_noticed_by(state, name)
            self._set_live(state)
        messages = npc_messages(
            name=name,
            heard=[
                (ln[1], ln[2])
                for ln in state.heard
                if ln[0] == area.id
                and ln[1] != name
                and self._within_hearing(area, state.spots.get(name), ln)
            ],
            sheet=self.visual_sheet(name),
            card=card,
            world=self.story.world_bible,
            where=f"{area.title}" + (f": {place.what}" if place is not None and place.what else ""),
            when=f"day {clock[0]}, {clock[1] // 60:02d}:{clock[1] % 60:02d}"
            if clock
            else "daytime",
            about=[n for n in self.people_in_sight() if n != name]
            + [
                "one of the dead"
                for c in state.creatures.values()
                if c.area == area.id and c.state != "down"
            ],
            player=self.held_name() or "the player",
            player_words=player_description(
                bare(weapon[0]) if weapon[0] else "",
                state.wounds,
                len(self.visual_inventory()),
                self.visual_condition(state)[1],
            ),
            standing=self.standing_of(name, state),
            witnessed=state.witnessed.get(name, []),
            memories=state.memories.get(name, []),
            talk=state.talks.get(name, []),
            line=line or self.texts["npc.encounter"],
            state=state.person_tags.get(name, []),
            texts=self.texts,
        )
        model = self.role_model("npc")
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("npc", model),
            messages=messages,
            params=self.side_params(model, max_tokens=NPC_MAX_TOKENS),
        )
        detached = getattr(self.provider, "detached", None)
        text = detached() if callable(detached) else self.provider
        thread = BackgroundThread(
            target=self._talk_run,
            args=(text, request, done, text is not self.provider),
            name="visual-talk",
            daemon=True,
        )
        self._talk_thread = thread
        thread.start()
        return True

    def _talk_run(self, text: ChatProvider, request: ChatRequest, done, own: bool) -> None:
        reply: NpcReply | None = None
        problem = ""
        try:
            log_ref = self._log("npc_request", {"payload": self.provider.build_payload(request)})
            try:
                answer, completed = text.complete(request)
                self._log(
                    "npc_response",
                    {"request_log_ref": log_ref, "text": answer, "usage": completed.raw_usage},
                )
                self.unreported_usage.append(self.priced(completed.usage, request.model))
                reply = parse_npc(answer)
            except (ProviderError, ValueError) as exc:
                problem = str(exc)[:300]
                self._log("npc_error", {"request_log_ref": log_ref, "error": problem})
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            problem = f"{type(exc).__name__}: {exc}"[:300]
            self._log("npc_error", {"error": problem})
        finally:
            if own:
                _close_quietly(text)
        done(reply, problem)

    def apply_npc_reply(self, name: str, reply: NpcReply) -> list[str]:
        """The person's words into the conversation, their memory and mood
        kept, and their intent carried out on the map (GUI thread). The
        lines for the log after their speech."""
        state = self.area_state()
        area, me = self._visual_me(state)
        if area is None or me is None:
            return []
        if reply.say:
            state.talks.setdefault(name, []).append([name, reply.say])
            self._heard(state, area.id, name, reply.say)
        if reply.remember:
            state.memories.setdefault(name, []).append(reply.remember)
            state.memories[name] = state.memories[name][-20:]
        if reply.mood:
            state.moods[name] = reply.mood
        lines: list[str] = []
        words = intent_words(name, reply, player=self.held_name() or "you")
        kind = reply.intent
        spot = state.spots.get(name)
        if kind == "follow":
            if name not in state.companions:
                state.companions.append(name)
            state.goals[name] = NpcGoal(intent="follow", toward=self.held_name() or "")
        elif kind in ("retreat", "hide", "leave", "fetch_help", "lead_to"):
            if name in state.companions:
                state.companions.remove(name)
            goal = self._npc_destination(state, area, me, spot, kind, reply.place)
            state.goals[name] = goal
        elif kind == "attack":
            if name in state.companions:
                state.companions.remove(name)
            state.goals[name] = NpcGoal(intent="attack", toward=self.held_name() or "")
        elif kind == "give":
            lines += self._npc_gives(state, name, reply.what)
            words = "" if lines else words
            state.goals[name] = NpcGoal(intent="stay")
        elif kind == "approach":
            state.goals[name] = NpcGoal(intent="approach", toward=self.held_name() or "")
        elif kind == "treat":
            tended = self.visual_treat_by(state, name)
            if tended:
                lines += tended
            elif self.nothing_more_to_do(state, name):
                lines.append(f"{name} looks you over: nothing more to be done for now.")
            else:
                lines.append(f"{name} has nothing to treat you with.")
            words = ""
            state.goals[name] = NpcGoal(intent="stay")
        else:
            state.goals[name] = NpcGoal(intent="stay")
        if reply.standing:
            said = self.set_standing(state, name, reply.standing)
            if said:
                lines.append(f"({said[:-1]} with you now.)")
        if words:
            lines.append(words)
        self._set_live(state)
        if kind == "attack":
            lines += self.visual_tick(0)
        return lines

    @staticmethod
    def _heard(state, area_id: str, who: str, words: str) -> None:
        """A line said out loud here, for the others to answer to: where
        it was said goes with it, so only those within hearing get it
        (live, the north gate's watchman "heard most of it" from 500 m)."""
        spot = state.spots.get(who)
        at = [spot.x, spot.y] if spot is not None and spot.area == area_id else [-1, -1]
        state.heard.append([area_id, who, words, *at])
        del state.heard[:-HEARD_KEPT]

    @staticmethod
    def _within_hearing(area: Area, listener: Spot | None, line: list) -> bool:
        if listener is None or len(line) < 5 or line[3] < 0:
            return True
        return math.hypot(line[3] - listener.x, line[4] - listener.y) * area.tile_m <= HEARD_M

    def visual_give(self, record_id: str, name: str) -> str:
        """Hand a carried thing to a person within reach: it is theirs, and
        they remember it (a toll paid, a trade). The line, or why not."""
        state = self.area_state()
        area, me = self._visual_me(state)
        record = state.things.get(record_id)
        spot = state.spots.get(name)
        held = self.held_name()
        if area is None or me is None or record is None or record.state != "taken" or held is None:
            return ""
        if spot is None or spot.area != area.id:
            return f"{name} isn't here."
        if math.hypot(spot.x - me.x, spot.y - me.y) * area.tile_m > GIVE_REACH_M:
            return f"{name} isn't within reach."
        record.by = name
        what = bare(record.name) + (f" ×{record.count}" if record.count > 1 else "")
        self._visual_witness(state, f"gave {name} the {what}")
        state.memories.setdefault(name, []).append(f"{held} gave me the {what}.")
        state.memories[name] = state.memories[name][-20:]
        self._set_live(state)
        return f"You give {name} the {what}."

    def people_in_reach(self) -> list[str]:
        """The people the player could hand something to: within reach."""
        state = self.area_state()
        area, me = self._visual_me(state)
        if area is None or me is None:
            return []
        return [
            n
            for n, sp in state.spots.items()
            if n != self.held_name()
            and sp.area == area.id
            and n not in state.people_down
            and math.hypot(sp.x - me.x, sp.y - me.y) * area.tile_m <= GIVE_REACH_M
        ]

    def talk_moment(self, name: str) -> str:
        """A conversation can move the plot (Doomsville's "The caves": the
        player "presses someone with pointed questions"): once every
        TALK_MOMENT_LINES of the player's lines to a person, when an event
        is armed whose trigger speaks of talking or asking, the keeper is
        asked about the moment, with the latest lines. "" otherwise."""
        plot = self.story.plot
        if plot is None:
            return ""
        state = self.area_state()
        lines = [pair for pair in state.talks.get(name, []) if pair[0] == self.held_name()]
        if not lines or len(lines) % TALK_MOMENT_LINES:
            return ""
        chronicle = self.visual_chronicle()
        items = [
            i
            for i in armed(plot, chronicle, self.plot_days(), mentioned=[chronicle.place or ""])
            if "begin" in i.kinds
            and TALK_TRIGGER.search(
                " ".join([i.event.trigger or "", *(v.trigger or "" for v in i.variants)])
            )
        ]
        if not items:
            return ""
        latest = "; ".join(f"{who}: {words}" for who, words in state.talks.get(name, [])[-6:])
        return f"the player is talking with {name} (the latest lines: {latest})"

    def _npc_gives(self, state, name: str, what: str) -> list[str]:
        """A person hands the player a thing they carry (by its words), or
        nothing they have."""
        want = {w for w in re.findall(r"[a-z]+", what.lower()) if len(w) > 2}
        for record in state.things.values():
            if record.state != "taken" or record.by != name:
                continue
            words = set(re.findall(r"[a-z]+", record.name.lower()))
            if not want or want & words:
                record.by = self.held_name() or record.by
                return [f"{name} hands you the {bare(record.name)}."]
        return [f"{name} has nothing of the kind to give."] if what else []

    def _npc_destination(self, state, area: Area, me: Spot, spot, kind: str, place: str) -> NpcGoal:
        """Where an intent takes a person: away from the player (retreat),
        out of sight (hide), out of the area by the nearest exit (leave,
        fetch_help), or toward a named place (lead_to: the area's door that
        leads there, else the far side)."""
        if spot is None:
            return NpcGoal(intent=kind)
        here = (spot.x, spot.y)
        free = [
            (x, y)
            for y in range(area.height)
            for x in range(area.width)
            if area.walkable(x, y) and (x, y) not in area.blocked()
        ]
        if not free:
            return NpcGoal(intent=kind, area=area.id, x=spot.x, y=spot.y)
        if kind == "retreat":
            far = [t for t in free if 6 <= abs(t[0] - me.x) + abs(t[1] - me.y) <= 14]
            goal = min(
                far or free,
                key=lambda t: (
                    abs(t[0] - here[0])
                    + abs(t[1] - here[1])
                    - (abs(t[0] - me.x) + abs(t[1] - me.y))
                ),
            )
        elif kind == "hide":
            hidden = [t for t in free if not sees(area, (me.x, me.y), t)]
            goal = min(hidden or free, key=lambda t: abs(t[0] - here[0]) + abs(t[1] - here[1]))
        elif kind == "lead_to" and place:
            world = self.world()
            want = place.casefold()
            doors = [
                (e.x, e.y)
                for e in area.entrances
                if (
                    world.places.get(e.place_id) is not None
                    and want in world.places[e.place_id].name.casefold()
                )
            ]
            goal = (
                doors[0]
                if doors
                else max(free, key=lambda t: abs(t[0] - here[0]) + abs(t[1] - here[1]))
            )
        else:
            exits = list(area.exits) + [(e.x, e.y) for e in area.entrances]
            edges = [t for t in free if t[0] in (0, area.width - 1) or t[1] in (0, area.height - 1)]
            goal = min(
                exits or edges or free, key=lambda t: abs(t[0] - here[0]) + abs(t[1] - here[1])
            )
        return NpcGoal(intent=kind, area=area.id, x=goal[0], y=goal[1])

    def _step_people(self, state, area: Area, me: Spot, seconds: int) -> list[str]:
        """The people in the player's area carry their intents out as time
        passes: a follower keeps beside the player, one going somewhere
        walks there (and is gone once there, for leave and fetch_help), one
        attacking closes and fights. Lines for the log."""
        lines: list[str] = []
        held = self.held_name()
        tiles = max(0, int(seconds * 1.2 / max(area.tile_m, 0.1)))
        taken = {(s.x, s.y) for n, s in state.spots.items() if s.area == area.id}
        for name, goal in list(state.goals.items()):
            spot = state.spots.get(name)
            if spot is None or spot.area != area.id or name == held or name in state.people_down:
                continue
            if is_held(state.person_tags.get(name, [])):
                continue  # held, bound or out cold: they stay where they are
            here = (spot.x, spot.y)
            if goal.intent == "follow":
                target = (me.x, me.y)
                if max(abs(here[0] - me.x), abs(here[1] - me.y)) <= 1:
                    continue
                stand = stand_near(area, target, taken - {here} | {target})
                route = path(area, here, stand, blocked=taken - {here})
                step = route[min(len(route) - 1, max(1, tiles * 2))] if len(route) > 1 else here
            elif goal.intent == "approach":
                self._approach_step(state, area, me, name, max(1, tiles))
                taken = {(s.x, s.y) for n, s in state.spots.items() if s.area == area.id}
                continue
            elif goal.intent == "attack":
                if max(abs(here[0] - me.x), abs(here[1] - me.y)) <= 1:
                    lines += self._person_attacks(state, name)
                    if state.dead:
                        break
                    continue
                stand = stand_near(area, (me.x, me.y), taken - {here} | {(me.x, me.y)})
                route = path(area, here, stand, blocked=taken - {here})
                step = route[min(len(route) - 1, max(1, tiles))] if len(route) > 1 else here
            elif goal.area == area.id and goal.x is not None and goal.y is not None:
                target = (goal.x, goal.y)
                # A way out is a door tile, which `settle` keeps people off:
                # beside it is there.
                arrived = here == target or (
                    goal.intent in ("leave", "fetch_help")
                    and max(abs(here[0] - target[0]), abs(here[1] - target[1])) <= 1
                )
                if arrived:
                    if goal.intent in ("leave", "fetch_help"):
                        away = self._away_spot(state, name, area)
                        if away is not None:
                            # One of the story's own people goes somewhere
                            # of theirs, not off the map for good (live, the
                            # town's leader "went to fetch others" and was
                            # never seen again).
                            state.spots[name] = away
                        else:
                            state.spots.pop(name, None)
                        state.goals.pop(name, None)
                        lines.append(f"{name} is gone.")
                    continue
                route = path(area, here, target, blocked=taken - {here} - {target})
                step = route[min(len(route) - 1, max(1, tiles))] if len(route) > 1 else here
            else:
                continue
            if step != here and step != (me.x, me.y):
                taken.discard(here)
                state.spots[name] = Spot(area=area.id, x=step[0], y=step[1])
                taken.add(step)
        return lines

    def _away_spot(self, state, name: str, area: Area) -> Spot | None:
        """Where someone who leaves the player's area goes: a place of
        theirs (their shift's, their sheet's) that isn't this area, else
        another building of the place they are in; None for a stranger with
        nowhere, who is simply gone."""
        sheet = self.visual_sheet(name)
        places = [sh.place for sh in sheet.schedule] + ([sheet.place] if sheet.place else [])
        if not places and name in state.strangers:
            return None
        world = self.world()
        root = self._root_place(world, area.id)
        for place in places:
            target = self._visual_area_named(place)
            if target is not None and target.id != area.id:
                spot = self._place_spot(state, place)
                if spot is not None and spot.area != area.id:
                    return spot
        for place in world.places.values():
            if place.inside != root or place.kind != "building":
                continue
            target = self._visual_area_named(place.name)
            if target is not None and target.id != area.id:
                spot = self._place_spot(state, place.name)
                if spot is not None and spot.area != area.id:
                    return spot
        return None

    def _keep_schedules(self, state, area: Area) -> None:
        """The design's people keep their hours (`PersonSheet.schedule`):
        one with nothing else to do, not in the player's area (nobody
        vanishes in sight), stands at the place their shift names when the
        clock is in it. Elena at the clinic by day, the church by night."""
        clock = self.map_clock()
        if clock is None:
            return
        minute = clock[1]
        for name in self.visual_people():
            sheet = self.visual_sheet(name)
            spot = state.spots.get(name)
            if not sheet.schedule or spot is None or spot.area == area.id:
                continue
            if name in state.companions or name in state.people_down:
                continue
            goal = state.goals.get(name)
            if (goal is not None and goal.intent != "stay") or is_held(
                state.person_tags.get(name, [])
            ):
                continue
            due = next((sh.place for sh in sheet.schedule if _in_shift(sh, minute)), None)
            if due is None:
                continue
            target = self._visual_area_named(due)
            if target is None or target.id == spot.area:
                continue
            new = self._place_spot(state, due)
            if new is not None:
                state.spots[name] = new

    def _person_attacks(self, state, name: str) -> list[str]:
        """A person beside the player fights them: a round (engine/health.py)."""
        area, me = self._visual_me(state)
        if area is None or me is None:
            return []
        rng = random.Random(f"{state.seconds}:{name}")
        _outcome, lines = self._person_round(state, area, me, name, rng)
        return [f"{name} comes at you; {ln[0].lower()}{ln[1:]}" for ln in lines[:1]] + lines[1:]

    def visual_attack_person(self, name: str) -> list[str]:
        """The player attacks a person: a fight; the person turns on them."""
        state = self.area_state()
        area, me = self._visual_me(state)
        spot = state.spots.get(name)
        if area is None or me is None or spot is None or spot.area != area.id:
            return []
        if max(abs(spot.x - me.x), abs(spot.y - me.y)) > 1:
            return [f"{name} isn't within reach."]
        self._visual_witness(state, f"attacked {name}")
        self.player_noticed_by(state, name)
        rng = random.Random(f"{state.seconds}:{name}:attack")
        outcome, lines = self._person_round(state, area, me, name, rng)
        if outcome.result not in ("down",) and name not in state.people_down:
            # Attacked, they fight back from now on.
            state.goals[name] = NpcGoal(intent="attack", toward=self.held_name() or "")
            if name in state.companions:
                state.companions.remove(name)
        state.seconds += 10
        self._set_live(state)
        if state.dead:
            return lines
        return lines + self.visual_tick(0)

    def _visual_witness(self, state, words: str) -> None:
        """What the player did, as everyone who could see them saw it."""
        area, me = self._visual_me(state)
        if area is None or me is None:
            return
        for name, spot in state.spots.items():
            if name == self.held_name() or spot.area != area.id:
                continue
            if sees(area, (spot.x, spot.y), (me.x, me.y)):
                state.witnessed.setdefault(name, []).append(words)
                state.witnessed[name] = state.witnessed[name][-20:]

    def _companions_to(self, spot: Spot) -> None:
        """Whoever travels with the player stands beside them where they
        arrive (through a door, after a journey)."""
        state = self.area_state()
        if not state.companions:
            return
        area = self.area(spot.area, things=state.things)
        if area is None:
            return
        taken = area.blocked() | {(s.x, s.y) for s in state.spots.values() if s.area == spot.area}
        for name in state.companions:
            if name in state.people_down:
                continue
            x, y = stand_near(area, (spot.x, spot.y), taken | {(spot.x, spot.y)})
            taken.add((x, y))
            state.spots[name] = Spot(area=spot.area, x=x, y=y)
        self._set_live(state)

    def encounter_due(self, name: str) -> bool:
        """Whether this person has just come into the player's sight here,
        unmet in this area so far."""
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        if me is None:
            return False
        if self.unaware_of_player(name, state):
            return False  # spotted first: they haven't seen the player yet
        return f"{name}@{me.area}" not in state.greeted

    def mark_encountered(self, name: str) -> None:
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        if me is None:
            return
        state.greeted.append(f"{name}@{me.area}")
        self._set_live(state)

    # --- the plot keeper (engine/keeper.py) ---------------------------------------

    def visual_stored_chronicle(self) -> Chronicle:
        """The plot's state as stored on the live area state, unsettled (for
        `hidden_ids`, which the map's fold asks while settling)."""
        live = self.story.area_live
        stored = live.state.chronicle if live is not None else None
        if stored is not None:
            return stored
        plot = self.story.plot
        return initial_chronicle(plot) if plot is not None else Chronicle()

    def visual_chronicle(self) -> Chronicle:
        """The plot's state as the game has it: the stored one (or the
        plot's start), its minutes the game's clock, the player's place the
        plot's place they stand in."""
        plot = self.story.plot
        state = self.area_state()
        chronicle = state.chronicle
        if plot is None:
            return chronicle or Chronicle()
        if chronicle is None:
            chronicle = initial_chronicle(plot)
        chronicle.minutes = plot.start_minutes + state.seconds // 60
        chronicle.held_id = self.story.held_character_id
        place = self._plot_place(state)
        if place is not None and chronicle.place != place:
            set_place(chronicle, place)
        elif place is None and chronicle.place is not None and self._in_made_cell(state):
            # Out in the wider world, in no place the plot names: elsewhere
            # (live, the plot kept them in Hellsville 40 km away, and "away
            # from Hellsville" could never hold).
            set_place(chronicle, None)
        return chronicle

    def _same_root(self, area_a: str, area_b: str) -> bool:
        """Whether two areas lie in one place: a town and its buildings."""
        world = self._world_read()
        return self._root_place(world, area_a) == self._root_place(world, area_b)

    def _in_made_cell(self, state) -> bool:
        me = state.spots.get(self.held_name() or "")
        if me is None:
            return False
        world = self._world_read()
        return is_cell(self._root_place(world, me.area))

    def _plot_place(self, state) -> str | None:
        """The plot's place the player stands in: the innermost map place
        of their spot, or one holding it, named in the plot's places."""
        plot = self.story.plot
        me = state.spots.get(self.held_name() or "")
        if plot is None or me is None:
            return None
        names = {p.name for p in plot.places}
        world = self.world()
        place_id = self.place_of_spot(me, world) or place_of_area(me.area) or me.area
        seen: set[str] = set()
        while place_id and place_id not in seen:
            seen.add(place_id)
            place = world.places.get(place_id)
            if place is None:
                break
            if place.name in names:
                return place.name
            place_id = place.inside or ""
        return None

    def _save_chronicle(self, state, chronicle: Chronicle) -> None:
        state.chronicle = chronicle
        self._set_live(state)

    def visual_plot_check(self, moment: str = "") -> tuple[list[str], str]:
        """After anything that moved the game: what the plot's code decides
        (offscreen happenings, windows closed), and whether the keeper
        should be asked now: on arriving somewhere the plot knows with an
        event armed there, on a new day with one due, when one is overdue,
        or at a `moment` of the window's own (the player has slept) with
        anything armed. (The lines for the log, why to ask or "")."""
        plot = self.story.plot
        if plot is None:
            return [], ""
        state = self.area_state()
        chronicle = self.visual_chronicle()
        days = self.plot_days()
        result = resolve_due(plot, chronicle, days, node_id=None)
        lines: list[str] = []
        for event, _variant in result.happened:
            if event.aftermath:
                lines.append(event.aftermath)
        items = [
            i
            for i in armed(plot, chronicle, days, mentioned=[chronicle.place or ""])
            if "begin" in i.kinds
        ]
        why = ""
        if any(i.overdue for i in items):
            why = "an event is overdue"
        place = chronicle.place or ""
        day = day_of(chronicle.minutes)
        here = [
            i
            for i in items
            if (i.event.place or any(v.place for v in i.variants)) in (None, place)
            or (i.event.place or "") == place
        ]
        if place and place not in state.plot_arrivals:
            state.plot_arrivals.append(place)
            if here and not why:
                why = f"the player has just arrived at {place}"
        if day not in state.plot_days_seen:
            state.plot_days_seen.append(day)
            if not why and any(
                i.event.kind == "timed" and days.get(i.event.id, day + 1) <= day for i in items
            ):
                why = f"day {day} has come"
        if moment and not why and items:
            why = moment
        self._save_chronicle(state, chronicle)
        return lines, why

    def visual_state_text(self) -> str:
        """Where things stand, for the keeper."""
        state = self.area_state()
        area, me = self._visual_me(state)
        held = self.held_name() or "the player"
        chronicle = self.visual_chronicle()
        clock = self.map_clock()
        when = f"day {clock[0]}, {clock[1] // 60:02d}:{clock[1] % 60:02d}" if clock else "?"
        where = area.title if area is not None else "nowhere"
        plot_place = chronicle.place or "no place the plot names"
        people = [
            f"{n}"
            + (f" ({state.moods[n]})" if n in state.moods else "")
            + self._how_far(area, me, sp)
            for n, sp in state.spots.items()
            if area is not None and sp.area == area.id and n != held and n not in state.people_down
        ]
        dead_up = sum(
            1
            for c in state.creatures.values()
            if area is not None and c.area == area.id and c.state != "down"
        )
        dead_down = sum(
            1
            for c in state.creatures.values()
            if area is not None and c.area == area.id and c.state == "down"
        )
        dead = f"{dead_up} up and about" + (f", {dead_down} down" if dead_down else "")
        elsewhere = []
        if area is not None:
            # Who is in the place's other parts (the clinic off the street),
            # so the keeper never brings someone in who is here already.
            for n, sp in state.spots.items():
                if n == held or sp.area == area.id or n in state.people_down:
                    continue
                if self._same_root(sp.area, area.id):
                    there = self.area(sp.area)
                    elsewhere.append(f"{n} ({there.title})" if there is not None else n)
        carried = [
            bare(r.name) + (f" ×{r.count}" if r.count > 1 else "")
            for _i, r in self.visual_inventory()
        ]
        companions = ", ".join(state.companions) or "nobody"
        visited = ", ".join(chronicle.visited) or "nowhere yet"
        # Whom they have met (spoken with, or been greeted by), so the keeper
        # never writes a reunion for a first meeting (live, Stillwater's
        # leader greeted John with "You made it back" on his first visit).
        met = sorted(
            {n for n in state.talks if n != held}
            | {g.split("@", 1)[0] for g in state.greeted if g.split("@", 1)[0] != held}
        )
        world = self.world()
        place_state = ""
        if area is not None:
            place = world.places.get(area.place_id)
            if place is not None and place.state:
                place_state = f" The place is {place.state}."
        region = self.overmap_region(state) if self.overmap() is not None else None
        wider = (
            f" The country round about: {region.name} ({region.words})."
            if region is not None
            else ""
        )
        if area is not None and self.overmap() is not None:
            # What the place is, so the keeper never puts it somewhere else
            # (live: Stillwater, 65 km away, "at the edge of Hellsville").
            here_place = self._world_read().places.get(
                self._root_place(self._world_read(), area.id) or ""
            )
            if here_place is not None and here_place.what:
                wider += f" {here_place.name}: {' '.join(here_place.what.split())[:300]}"
        short = self.visual_needs(state)
        faring = f" {held} is {', '.join(short)}." if short else ""
        _word, hurts = self.visual_condition(state)
        if hurts:
            faring += f" {held}'s injuries: " + "; ".join(hurts) + "."
        standings = [
            f"{n}: {self.standing_of(n, state)}"
            for n, sp in state.spots.items()
            if n != held and self.standing_of(n, state)
        ] + [f"{g} (all of them): {s}" for g, s in state.group_standing.items()]
        if standings:
            faring += " How people stand with them: " + "; ".join(standings) + "."
        # The story's own people not on the board yet, so the plot's band
        # comes as the plot wrote them (live, "First bandit meeting" brought
        # Evan with two invented names, Cole and Dutch unused).
        waiting = [
            f"{c.name} ({' '.join((c.summary or '').split())[:90]})"
            for c in (*self.cast, *self.supporting)
            if c.name != held
            and c.name not in state.spots
            and c.name not in state.people_down
            and c.id not in self.hidden_ids()
        ]
        unplaced = (
            " Characters of the story not on the board yet, for the plot to bring in by name: "
            + "; ".join(waiting)
            + "."
            if waiting
            else ""
        )
        return (
            f"{held} is at {where} ({plot_place}); it is {when}; wounds {state.wounds}."
            f"{wider}{faring}{place_state} "
            f"With them: {companions}. Here too: {', '.join(people) or 'nobody'}"
            + (f"; elsewhere in this place: {', '.join(elsewhere)}" if elsewhere else "")
            + f"; the dead here: {dead}. "
            f"Carrying: {', '.join(carried) or 'nothing'}. Places visited: {visited}. "
            f"People {held} has met so far: {', '.join(met) or 'nobody'}; everyone else is a "
            f"stranger to them.{unplaced}\n"
            f"THINGS HERE (name; state; within reach or not):\n{self._things_text(state, area, me)}"
        )

    @staticmethod
    def _how_far(area: Area | None, me: Spot | None, spot: Spot) -> str:
        """How far off someone stands, for the keeper (live, "Kane stands
        close" with Kane a hundred metres across the Yard)."""
        if area is None or me is None:
            return ""
        metres = math.hypot(spot.x - me.x, spot.y - me.y) * area.tile_m
        if metres < 3:
            return ", beside them"
        return f", about {round(metres / 5) * 5} m off" if metres >= 5 else ", close by"

    def _record_words(self, record: ThingRecord | None) -> str:
        if record is None:
            return ""
        parts = []
        if record.state == "broken":
            parts.append("broken")
        parts += record.tags
        if record.uses:
            parts.append("can be: " + ", ".join(record.uses))
        if record.contents:
            parts.append("holds " + ", ".join(f.name for f in record.contents[:6]))
        if record.seen and len(record.seen) < 160:
            parts.append(record.seen)
        return "; ".join(parts)

    def _things_text(self, state, area: Area | None, me: Spot | None) -> str:
        """The things in the player's area for the keeper, nearest first,
        each with its state in the rules' words."""
        if area is None or me is None:
            return "(nothing)"
        rows = []
        for placement in sorted(
            area.placements, key=lambda p: abs(p.centre[0] - me.x) + abs(p.centre[1] - me.y)
        )[:STATE_THINGS]:
            record = state.things.get(placement.id)
            words = self._record_words(record) or ("broken" if placement.broken else "as it stands")
            reach = "within reach" if self._in_reach(area, me, placement) else "not in reach"
            rows.append(f"- {bare(placement.name)}; {words}; {reach}")
        for _rid, record in self.visual_inventory():
            words = self._record_words(record)
            rows.append(f"- {bare(record.name)} (carried){'; ' + words if words else ''}")
        for name, spot in state.spots.items():
            tags = state.person_tags.get(name)
            if tags and name != self.held_name() and spot.area == area.id:
                rows.append(f"- {name} is {', '.join(tags)}")
        return "\n".join(rows) or "(nothing)"

    def _keeper_messages(self, kind: str, line: str):
        plot = self.story.plot
        chronicle = self.visual_chronicle()
        days = self.plot_days()
        names = {c.id: f"{c.name}: {c.summary or ''}" for c in (*self.cast, *self.supporting)}
        names.update({e.id: f"{e.title} (a place or lore)" for e in self.bundle.lore})
        items = [i for i in armed(plot, chronicle, days, mentioned=None)] if plot else []
        common = dict(
            world=self.story.world_bible,
            state=self.visual_state_text(),
            armed=armed_text(items, days, chronicle, names) if plot else "(no plot)",
            happened=aftermaths(plot, chronicle) if plot else "(no plot)",
            facts=facts_text(chronicle),
            texts=self.texts,
        )
        if kind == "direction":
            return direction_messages(line=line, **common)
        if kind == "action":
            return action_messages(line=line, **common)
        if kind == "journey":
            return journey_messages(reason=line, **common)
        return moment_messages(reason=line, **common)

    def start_keeper(self, kind: str, line: str, done: Callable[[object, str], None]) -> bool:
        """Ask the keeper on a thread: `kind` "moment" (why), "direction"
        (the Director's words) or "action" (what the player attempts).
        `done` gets a `KeeperReply` or a `Ruling` (or None and why) on the
        thread; `apply_keeper_reply` / `apply_ruling` make it real."""
        if self.in_private or (self.story.plot is None and kind == "moment"):
            return False
        messages = self._keeper_messages(kind, line)
        model = self.role_model("keeper")
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("keeper", model),
            messages=messages,
            params=self.side_params(model, max_tokens=KEEPER_MAX_TOKENS),
        )
        detached = getattr(self.provider, "detached", None)
        text = detached() if callable(detached) else self.provider
        thread = BackgroundThread(
            target=self._keeper_run,
            args=(text, request, kind, done, text is not self.provider),
            name="visual-keeper",
            daemon=True,
        )
        self._keeper_thread = thread
        thread.start()
        return True

    def _keeper_run(
        self, text: ChatProvider, request: ChatRequest, kind: str, done, own: bool
    ) -> None:
        reply = None
        problem = ""
        events = self.story.plot.events if self.story.plot is not None else []
        try:
            # "ask", not "kind": a "kind" field took the entry's own kind,
            # and every keeper request was logged as "moment" or "journey".
            log_ref = self._log(
                "keeper_request", {"ask": kind, "payload": self.provider.build_payload(request)}
            )
            try:
                answer, completed = text.complete(request)
                self._log(
                    "keeper_response",
                    {"request_log_ref": log_ref, "text": answer, "usage": completed.raw_usage},
                )
                self.unreported_usage.append(self.priced(completed.usage, request.model))
                reply = (
                    parse_ruling(answer, events)
                    if kind == "action"
                    else parse_keeper(answer, events)
                )
            except (ProviderError, ValueError) as exc:
                problem = str(exc)[:300]
                self._log("keeper_error", {"request_log_ref": log_ref, "error": problem})
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            problem = f"{type(exc).__name__}: {exc}"[:300]
            self._log("keeper_error", {"error": problem})
        finally:
            if own:
                _close_quietly(text)
        done(reply, problem)

    def apply_keeper_reply(
        self, reply: KeeperReply, *, kind: str = "", talking_to: str = ""
    ) -> list[str]:
        """Events fired (happened in the chronicle, their facts set, whoever
        they bring in on the board) and the scripts applied (GUI thread). On
        a journey (`kind`) the reply's own note says what is seen: a "note"
        operation beside it said it twice over, in other words (live). At a
        moment or on a journey a person's state (`tag_person`) is the
        keeper's own knowledge, not the player's (live, "Tom Bell is armed,
        circling behind player, unseen" was printed); and the person the
        player is talking with (`talking_to`) speaks through their own call
        (live, the keeper answered Marcus's question for him, in Marcus's
        voice, and Marcus then caught John out on it)."""
        lines: list[str] = []
        quiet = kind in ("journey", "moment")
        for firing in reply.fired:
            lines += self._fire(firing.event, firing.variant)
            lines += self.apply_script(firing.script, tags_said=not quiet)
        script = reply.script
        if quiet and reply.note:
            # The reply's own note says what is seen: a "note" operation
            # beside it said the same in other words (live, on arrivals).
            script = [op for op in script if op.op != "note"]
        if quiet:
            # The program has already moved the clock for what the moment
            # is about (the night slept, the journey made): live, the
            # keeper's "clock": 480 after a sleep woke John at 15:00.
            script = [op for op in script if op.op != "clock"]
        if kind == "moment" and talking_to:
            script = [
                op
                for op in script
                if not (op.op == "say" and op.get("who").casefold() == talking_to.casefold())
            ]
        lines += self.apply_script(script, tags_said=not quiet)
        if reply.note:
            lines.append(reply.note)
        # A note said twice (a "note" operation and the reply's own, live on
        # Sonnet 4.6): the fuller one, once.
        lines = [
            line
            for n, line in enumerate(lines)
            if line not in lines[:n]
            and not any(other != line and other.startswith(line.rstrip(".")) for other in lines)
        ]
        if reply.dropped:
            lines.append(
                "(The keeper asked for things the game can't do: "
                + ", ".join(reply.dropped[:4])
                + ".)"
            )
        return lines

    def apply_ruling(self, ruling: Ruling, line: str) -> list[str]:
        """An unusual action ruled on: the roll made by the dice, the
        outcome's script applied."""
        lines: list[str] = []
        if ruling.note:
            lines.append(ruling.note)
        if not ruling.possible:
            lines += self.apply_script(ruling.failure)
            return lines or ["That can't be done here."]
        succeeded = True
        if ruling.difficulty:
            character = next((c for c in self.cast if c.id == self.story.held_character_id), None)
            state = self.area_state()
            roll = roll_for(
                character,
                ruling.skill or None,
                ruling.difficulty,
                random.Random(f"{line}:{state.seconds}"),
            )  # type: ignore[arg-type]
            succeeded = roll.band in ("critical_success", "success", "success_at_a_cost")
            lines.append(explain(roll, self.held_name() or "You"))
        lines += self.apply_script(ruling.success if succeeded else ruling.failure)
        for firing in ruling.fired:
            lines += self._fire(firing.event, firing.variant)
            lines += self.apply_script(firing.script)
        return lines

    def _fire(self, title: str, variant_title: str) -> list[str]:
        plot = self.story.plot
        if plot is None:
            return []
        event = next((e for e in plot.events if e.title == title), None)
        if event is None:
            return []
        state = self.area_state()
        chronicle = self.visual_chronicle()
        variant = variant_for(event, variant_title, eligible(event, chronicle))
        happen(chronicle, event, variant, node_id=None, revealed=True)
        self._save_chronicle(state, chronicle)
        # Whoever it brings in with no place of the design's stands beside the player.
        self.visual_lay_out()
        state = self.area_state()
        lines = []
        for item_id in (*event.brings, *variant.brings):
            card = next((c for c in (*self.cast, *self.supporting) if c.id == item_id), None)
            if (
                card is not None
                and card.name not in state.spots
                and card.name not in state.people_down
            ):
                lines += self._stand_person(state, card.name, "beside the player")
        self._set_live(state)
        return lines

    def _place_spot(self, state, where: str) -> Spot | None:
        """A tile for `where`: a place by name (its anchor on its area, else
        the area's middle), or beside the player."""
        area, me = self._visual_me(state)
        words = where.strip().casefold()
        # "Snowbound fields (Hellsville)": what is in brackets is a gloss.
        plain = re.sub(r"\(.*?\)", "", words).strip(" ,.")
        if (
            area is not None
            and me is not None
            and plain
            and plain in {area.name.casefold(), area.title.casefold()}
        ):
            # The player's own area by its name: here, where they can see it
            # (live, the made cells share names, and the dead went to the
            # first "Snowbound fields"... then to Hellsville, by the gloss).
            return self._spot_off(state, area, me)
        if area is not None and me is not None and any(w in words for w in OFF_WORDS):
            # A little way off, in sight: someone coming, the dead shambling up.
            return self._spot_off(state, area, me)
        if not words or any(
            w in words for w in ("beside", "here", "player", "with them", "next to")
        ):
            if area is None or me is None:
                return None
            taken = area.blocked() | {(s.x, s.y) for s in state.spots.values() if s.area == area.id}
            x, y = stand_near(area, (me.x, me.y), taken | {(me.x, me.y)})
            return Spot(area=area.id, x=x, y=y)
        target = self._visual_area_named(where)
        world = self.world()
        place = _place_named(world, where)
        if target is None and place is not None:
            target = self._visual_area_named(place.name)
        if target is None:
            return None
        at = anchor_of(target, world, place.id) if place is not None else None
        taken = target.blocked() | {(s.x, s.y) for s in state.spots.values() if s.area == target.id}
        x, y = stand_near(target, at or (target.width // 2, target.height // 2), taken)
        return Spot(area=target.id, x=x, y=y)

    def _spot_off(self, state, area: Area, me: Spot) -> Spot:
        """A tile 10 to 20 tiles from the player that they can see, else the
        nearest free one."""
        taken = area.blocked() | {(s.x, s.y) for s in state.spots.values() if s.area == area.id}
        rng = random.Random(f"{area.id}:{state.seconds}:{len(state.spots)}")
        for _ in range(60):
            angle = rng.random() * math.tau
            reach = rng.randint(10, 20)
            x = int(me.x + math.cos(angle) * reach)
            y = int(me.y + math.sin(angle) * reach)
            if (
                0 <= x < area.width
                and 0 <= y < area.height
                and area.walkable(x, y, taken)
                and sees(area, (me.x, me.y), (x, y))
                and _reaches(area, (x, y))
            ):
                return Spot(area=area.id, x=x, y=y)
        x, y = stand_near(area, (me.x, me.y), taken | {(me.x, me.y)})
        return Spot(area=area.id, x=x, y=y)

    def _make_stranger(self, state, op: Op) -> list[str]:
        """Someone new to the story, as the keeper wrote them: a supporting
        card and a sheet, stood where it says."""
        name = " ".join(op.get("who").split())[:60]
        held = (self.held_name() or "").casefold()
        if not name or name.casefold() in {"the player", "player", held} | NOT_NAMES:
            # Live (Oct 26 2026) Sonnet wrote {"stranger": "who", …}: the
            # field's name for the person's.
            return [f"(The keeper brought someone in with no name: {op.get('card')[:80]})"]
        if self.visual_card(name) is None:
            self.supporting.append(
                Character(
                    name=name,
                    summary=op.get("card") or op.get("attitude") or "Someone met on the road.",
                    is_player_available=False,
                    origin="model",
                )
            )
        state.strangers[name] = PersonSheet(
            card=op.get("card"),
            personality=op.get("attitude"),
            wants=op.get("wants"),
            knows=op.get("knows"),
            speech=op.get("speech"),
            carries=op.words("carries"),
            ability=5,
            group=op.get("group"),
        )
        lines = self._stand_person(state, name, op.get("where") or "nearby")
        return lines

    def _stand_person(self, state, who: str, where: str) -> list[str]:
        card = self.visual_card(who)
        if card is None:
            return [f"(The keeper named {who}, who is nobody in this story.)"]
        spot = self._place_spot(state, where)
        if spot is None:
            return []
        state.spots[card.name] = spot
        if card.name not in state.placed:
            state.placed.append(card.name)
        for thing in self.visual_sheet(card.name).carries:
            if not any(r.by == card.name and r.name == thing for r in state.things.values()):
                state.things[ITEM_PREFIX + new_id()] = ThingRecord(
                    state="taken", name=thing, by=card.name, kind=kind_of(thing)
                )
        return []

    def _keeper_injures(self, state, op: Op) -> list[str]:
        """The keeper hurts the player: a kind of injury, or a serious wound."""
        from sealedlore.engine.health import MINOR_KINDS, SERIOUS_KINDS
        from sealedlore.models.visual import Injury

        kind = op.get("kind").strip().lower() if op.op == "injure" else "deep wound"
        if kind not in MINOR_KINDS + SERIOUS_KINDS:
            kind = "deep wound" if op.op == "wound" else "cut"
        words = " ".join(op.get("words").split()).rstrip(".")
        if not words or len(words) > 40 or words.count(" ") > 6:
            # A name for it, not the story of it (live, the whole fall).
            words = "bruises" if kind == "bruises" else f"a {kind}"
        words = words[0].lower() + words[1:]
        count = op.count(1) if op.op == "wound" else 1
        if any(i.kind == kind and i.serious and not i.healed for i in state.injuries):
            # The keeper spoke of an injury the player has (live, asked about
            # the broken leg, it "injured" a second one): nothing new.
            return []
        for _ in range(max(1, count)):
            state.injuries.append(
                Injury(kind=kind, serious=kind in SERIOUS_KINDS, at=state.seconds, words=words)
            )
        return [f"You are hurt: {words}."]

    def _keeper_treats(self, state, op: Op) -> list[str]:
        """The keeper sees to the player's injuries: first aid, proper care
        (a break set, a wound cleaned), or antibiotics."""
        from sealedlore.engine.health import care, first_aid, worst_untreated

        how = op.get("how").strip().lower()
        lines: list[str] = []
        if "antibiotic" in how:
            for injury in state.injuries:
                if injury.infected and not injury.healed:
                    lines.append(care(injury, state.seconds, antibiotics=True))
        elif "first" in how or "bandage" in how or "splint" in how or "dress" in how:
            injury = worst_untreated(state.injuries)
            if injury is not None:
                lines.append(first_aid(injury, state.seconds))
        else:
            for injury in state.injuries:
                if injury.serious and not injury.healed:
                    for words in (
                        care(injury, state.seconds, antibiotics=True),
                        care(injury, state.seconds),
                    ):
                        if words:
                            lines.append(words)
        return [ln for ln in lines if ln]

    def _doors_of_place(self, state, where: str) -> list[DoorState]:
        inside = self._visual_area_named(where)
        if inside is None:
            return []
        world, art = self.world(), self._art_read()
        outside = self.area(inside.parent, world, art) if inside.parent else None
        if outside is None:
            return []
        found = []
        for entrance in outside.entrances:
            if entrance.place_id == inside.id:
                key = door_id(outside.id, entrance.x, entrance.y)
                found.append(state.doors.setdefault(key, DoorState()))
        return found

    def _find_thing(
        self, state, area: Area | None, words: str, *, carried: bool | None = None
    ) -> tuple[str, ThingRecord | None, object | None] | None:
        """The thing a keeper's words name: among what the player carries
        (`carried` True), what stands or lies in their area (False), or both
        (None: carried first). The one sharing most words with the name.
        (record id or placement id, the record if any, the placement if any)."""
        want = set(re.findall(r"[a-z]+", words.lower())) - STOP_WORDS
        if not want:
            return None
        best: tuple[int, str, ThingRecord | None, object | None] | None = None

        def consider(key: str, record, placement, name: str) -> None:
            nonlocal best
            score = len(want & set(re.findall(r"[a-z]+", name.lower())))
            if score and (best is None or score > best[0]):
                best = (score, key, record, placement)

        held = self.held_name()
        if carried is not False:
            # From `state` itself, never `visual_inventory()` (a fresh copy:
            # live, a take_away marked the copy and the player kept the parts).
            for rid, record in state.things.items():
                if record.state == "taken" and record.by == held:
                    consider(rid, record, None, record.name)
            if best is not None and carried is True:
                return best[1:]
        if carried is not True and area is not None:
            for placement in area.placements:
                consider(placement.id, state.things.get(placement.id), placement, placement.name)
        return best[1:] if best is not None else None

    def _transform(self, state, area: Area | None, op: Op) -> list[str]:
        """A thing here or carried becomes what the keeper says it now is."""
        found = self._find_thing(state, area, op.get("what"))
        if found is None:
            return [f"(The keeper named {op.get('what') or 'a thing'}, which isn't here.)"]
        key, record, placement = found
        if record is None:
            record = state.things[key] = ThingRecord(
                state="seen", name=placement.name if placement is not None else op.get("what")
            )
        old = bare(record.name)
        if op.get("name"):
            record.name = op.get("name")
        if op.get("kind"):
            record.kind = kind_of(record.name, op.get("kind"))
        elif op.get("name"):
            record.kind = kind_of(record.name, record.kind)
        new_state = op.get("state").strip().lower()
        if new_state in ("broken",):
            record.state = "broken"
        elif new_state in ("gone", "destroyed", "used up"):
            record.state = "gone"
        elif new_state in ("whole", "repaired", "working", "fixed", "mended") or (
            new_state and record.state == "broken"
        ):
            record.state = (
                "dropped" if record.spot is not None else ("taken" if record.by else "seen")
            )
        if op.args.get("tags") is not None:
            record.tags = op.words("tags")
        if op.args.get("uses") is not None:
            record.uses = op.words("uses")
        if op.get("note"):
            record.seen = op.get("note")
        new = record.name.strip()
        words = f"The {old} is now {new}." if bare(new) != old else f"The {old} has changed."
        if record.state == "gone":
            words = f"The {old} is gone."
        elif record.tags or record.uses:
            words += (
                " ("
                + "; ".join(
                    part
                    for part in (
                        ", ".join(record.tags),
                        ("you can " + ", ".join(record.uses) + " it") if record.uses else "",
                    )
                    if part
                )
                + ")"
            )
        return [words]

    def apply_script(self, ops: list[Op], *, tags_said: bool = True) -> list[str]:
        """The keeper's operations, in order, on the live state; the lines
        the player reads. `tags_said`: a person's new state is told (an
        action's or a Direction's outcome); off, it is kept to the board."""
        state = self.area_state()
        lines: list[str] = []
        held = self.held_name() or ""
        area, me = self._visual_me(state)
        for op in ops:
            kind = op.op
            if kind == "note":
                lines.append(op.get("text"))
            elif kind == "say":
                who, text = op.get("who"), op.get("text")
                card = self.visual_card(who)
                spot = state.spots.get(card.name) if card is not None else None
                if card is not None and (
                    spot is None
                    or me is None
                    or area is None
                    or spot.area != me.area
                    or not (
                        in_earshot(area, (me.x, me.y), (spot.x, spot.y))
                        or (
                            math.hypot(spot.x - me.x, spot.y - me.y) * area.tile_m <= PERSON_SIGHT_M
                            and sees(area, (me.x, me.y), (spot.x, spot.y))
                        )
                    )
                ):
                    # Someone of the story who isn't here, or is out of
                    # earshot and sight, can't be heard: they greet in their
                    # own words when the player comes up (live, Stillwater's
                    # people "spoke" from 120 m off across the hamlet).
                    continue
                if text:
                    lines.append(f"{who}: {text}")
                    state.talks.setdefault(who, []).append([who, text])
                    if me is not None:
                        self._heard(state, me.area, who, text)
                    if me is not None and f"{who}@{me.area}" not in state.greeted:
                        state.greeted.append(f"{who}@{me.area}")  # no second greeting
            elif kind == "spawn":
                card = self.visual_card(op.get("who"))
                spot = state.spots.get(card.name) if card is not None else None
                if spot is not None and me is not None and self._same_root(spot.area, me.area):
                    # Already about, in this place or one of its buildings:
                    # left where they are (live, the welcome at Hellsville
                    # "spawned" Elena out of her clinic into the street).
                    continue
                lines += self._stand_person(state, op.get("who"), op.get("where"))
            elif kind == "stranger":
                lines += self._make_stranger(state, op)
            elif kind == "tell_of":
                self._set_live(state)
                told = self.overmap_tell_of(op.get("where"))
                state = self.area_state()
                area, me = self._visual_me(state)
                if told:
                    lines.append(told)
            elif kind == "spawn_dead":
                spot = self._place_spot(state, op.get("where"))
                target = self.area(spot.area, things=state.things) if spot is not None else None
                if target is not None:
                    taken = target.blocked() | {
                        (s.x, s.y) for s in state.spots.values() if s.area == target.id
                    }
                    for creature in spawn_creatures(
                        target, "the dead", op.count(1), 4, taken, f"{target.id}:{state.seconds}"
                    ):
                        state.creatures[creature.id] = creature
                    if me is None or target.id != me.area:  # here, they are seen
                        lines.append(f"The dead: {op.count(1)} more at {target.name}.")
            elif kind == "move":
                who, where = op.get("who"), op.get("where").casefold()
                card = self.visual_card(who)
                if card is None:
                    continue
                here = state.spots.get(card.name)
                plain = re.sub(r"\(.*?\)", "", where).strip(" ,.")
                if (
                    here is not None
                    and area is not None
                    and here.area == area.id
                    and plain in {area.name.casefold(), area.title.casefold()}
                ):
                    # Moved to the place they are in already: they stay
                    # where they stand (live, Ben "moved to The Hidden
                    # Caves" from his chamber in them to the player's side).
                    continue
                if any(w in where for w in ("away", "out", "gone", "leaves")):
                    state.spots.pop(card.name, None)
                    state.goals.pop(card.name, None)
                    if card.name in state.companions:
                        state.companions.remove(card.name)
                else:
                    spot = self._place_spot(state, op.get("where"))
                    if spot is not None:
                        state.spots[card.name] = spot
                        if card.name not in state.placed:
                            state.placed.append(card.name)
            elif kind == "remove":
                card = self.visual_card(op.get("who"))
                if card is not None:
                    state.spots.pop(card.name, None)
                    state.goals.pop(card.name, None)
                    if card.name in state.companions:
                        state.companions.remove(card.name)
            elif kind == "place":
                spot = self._place_spot(state, op.get("where"))
                what = op.get("what")
                if spot is not None and what:
                    state.things[ITEM_PREFIX + new_id()] = ThingRecord(
                        state="dropped",
                        name=what,
                        spot=spot,
                        count=op.count(1),
                        kind=kind_of(what, op.get("kind")),
                        tags=op.words("tags"),
                        uses=op.words("uses"),
                        seen=op.get("note"),
                    )
            elif kind == "give":
                what = op.get("what")
                if what and held:
                    state.things[ITEM_PREFIX + new_id()] = ThingRecord(
                        state="taken",
                        name=what,
                        by=held,
                        count=op.count(1),
                        kind=kind_of(what, op.get("kind")),
                        tags=op.words("tags"),
                        uses=op.words("uses"),
                        seen=op.get("note"),
                    )
                    lines.append(f"You now have {what}.")
            elif kind == "transform":
                lines += self._transform(state, area, op)
            elif kind == "go":
                spot = self._place_spot(state, op.get("where"))
                if spot is not None and held:
                    state.spots[held] = spot
                    there = self.area(spot.area, things=state.things)
                    if there is not None and spot.area != (me.area if me is not None else ""):
                        lines.append(f"You are at {there.title}.")
                    self._set_live(state)
                    self._companions_to(spot)
                    state = self.area_state()
                    area, me = self._visual_me(state)
            elif kind == "set_state":
                place = _place_named(self.world(), op.get("where"))
                if place is not None:
                    new_state = op.get("state")
                    if new_state:
                        state.place_states[place.id] = new_state
                    else:
                        state.place_states.pop(place.id, None)
                    lines.append(f"{place.name}: {new_state or 'as it was'}.")
            elif kind == "tag_person":
                card = self.visual_card(op.get("who"))
                if card is not None and card.name == held:
                    continue  # the player's state is the rules' ("John is hydrated", live)
                if card is not None:
                    tags = op.words("tags")
                    if tags:
                        state.person_tags[card.name] = tags
                        if tags_said:
                            lines.append(f"{card.name} is {', '.join(tags)}.")
                        if is_held(tags):
                            state.goals[card.name] = NpcGoal(intent="stay")
                            if card.name in state.companions:
                                state.companions.remove(card.name)
                    else:
                        state.person_tags.pop(card.name, None)
            elif kind == "take_away":
                found = self._find_thing(state, area, op.get("what"), carried=True)
                if found is not None and found[1] is not None and found[1].state == "taken":
                    record = found[1]
                    record.state = "gone"
                    lines.append(f"The {bare(record.name)} is gone from your hands.")
            elif kind == "break":
                want = set(re.findall(r"[a-z]+", op.get("what").lower())) - STOP_WORDS
                if area is not None:
                    for thing in area.placements:
                        if want & set(re.findall(r"[a-z]+", thing.name.lower())):
                            record = state.things.setdefault(
                                thing.id, ThingRecord(state="broken", name=thing.name)
                            )
                            record.state = "broken"
                            lines.append(f"The {bare(thing.name)} is broken.")
                            break
            elif kind in ("unlock", "lock", "seal", "unseal"):
                for door in self._doors_of_place(state, op.get("where")):
                    if kind == "unlock":
                        door.locked, door.sealed = False, False
                    elif kind == "lock":
                        door.locked, door.key = True, op.get("key")
                    elif kind == "seal":
                        door.locked, door.sealed = True, True
                    else:
                        door.sealed = False
            elif kind == "reveal":
                chronicle = self.visual_chronicle()
                want = op.get("what").casefold()
                for item in (*self.cast, *self.supporting, *self.bundle.lore):
                    name = getattr(item, "name", None) or getattr(item, "title", "")
                    if name.casefold() == want and item.id not in chronicle.brought_in:
                        chronicle.brought_in.append(item.id)
                state.chronicle = chronicle
                if self.overmap() is not None:
                    # A place the plot reveals is on the player's map too.
                    want_place = op.get("what")
                    if want_place and want_place not in state.overmap.known:
                        known = {p.name.casefold(): p.name for p in self.overmap().places}
                        if want_place.casefold() in known:
                            state.overmap.known.append(known[want_place.casefold()])
                            lines.append(f"Now on your map: {known[want_place.casefold()]}.")
            elif kind == "set_fact":
                chronicle = self.visual_chronicle()
                set_fact(chronicle, op.get("fact"), op.get("value"))
                state.chronicle = chronicle
            elif kind == "eat":
                self.note_ate(state, "food")
            elif kind == "drink":
                self.note_ate(state, "drink")
            elif kind in ("injure", "wound"):
                lines += self._keeper_injures(state, op)
            elif kind == "treat":
                lines += self._keeper_treats(state, op)
            elif kind == "heal":
                lines += self._keeper_treats(state, Op(op="treat", args={"how": "care"}))
                state.wounds = max(0, state.wounds - op.count(1))
            elif kind == "status":
                said = self.set_standing(state, op.get("who"), op.get("status"))
                if said:
                    lines.append(said)
            elif kind == "clock":
                clock = self.map_clock()
                state.seconds += op.minutes(clock[1] if clock else None) * 60
            elif kind == "end":
                state.ended = op.get("text") or "The story ends here."
                lines.append(state.ended)
        if state.wounds >= self.visual_design().player_hits:
            state.dead = True
            lines.append("That was one wound too many. You are dead.")
        self._set_live(state)
        self.visual_lay_out()  # a revealed place's people stand in it
        return lines
