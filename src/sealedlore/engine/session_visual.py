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
    area_of,
    bare,
    stand_near,
    walk_metres,
    walk_seconds,
)
from sealedlore.engine.levels import place_of_area
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
from sealedlore.engine.session_area import (
    DEFAULT_START_MINUTE,
    DOOR_SECONDS,
    WINDING,
    _close_quietly,
)
from sealedlore.engine.takeable import can_be_taken
from sealedlore.engine.visual_rules import (
    FORCE_DIFFICULTY,
    FORCE_NOISE_M,
    GUN_NOISE_M,
    best_weapon,
    can_open,
    door_id,
    fight,
    is_loud,
    item_actions,
    kind_of,
    sight_report,
    spawn_creatures,
    step_creatures,
)
from sealedlore.engine.world_art import is_window
from sealedlore.engine.world_layout import position
from sealedlore.ids import new_id
from sealedlore.models.area import Found, Spot, ThingRecord
from sealedlore.models.visual import DoorState, VisualDesign
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


# An item taken out of something, or laid by the design: a record of its own.
ITEM_PREFIX = "item:"
# Waiting and sleeping, in minutes.
WAIT_MINUTES = 60
SLEEP_UNTIL = 7 * 60
# The noise of forcing a door or a gunshot reaches the dead in the area.


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

    def visual_lay_out(self) -> None:
        """The design's dead, things and locks laid on the world, once."""
        state = self.area_state()
        if state.laid_out:
            return
        design = self.visual_design()
        taken_by_area: dict[str, set[tuple[int, int]]] = {}
        for spawn in design.creatures:
            area = self._visual_area_named(spawn.place)
            if area is None:
                continue
            taken = taken_by_area.setdefault(
                area.id,
                area.blocked()
                | {(sp.x, sp.y) for sp in state.spots.values() if sp.area == area.id},
            )
            for creature in spawn_creatures(
                area, spawn.kind, spawn.count, spawn.ability, taken, f"{area.id}:{spawn.kind}"
            ):
                state.creatures[creature.id] = creature
        for item in design.items:
            area = self._visual_area_named(item.place)
            if area is None:
                continue
            kind = kind_of(item.name, item.kind)
            holder = None
            if item.inside:
                want = bare(item.inside).casefold()
                holder = next((p for p in area.placements if bare(p.name).casefold() == want), None)
            if holder is not None:
                record = state.things.get(holder.id)
                if record is None:
                    record = state.things[holder.id] = ThingRecord(state="seen", name=holder.name)
                record.contents.append(Found(name=item.name, count=item.count, kind=kind))
                continue
            taken = taken_by_area.setdefault(
                area.id,
                area.blocked()
                | {(sp.x, sp.y) for sp in state.spots.values() if sp.area == area.id},
            )
            x, y = stand_near(area, (area.width // 2, area.height // 2), taken, seed=item.name)
            taken.add((x, y))
            state.things[ITEM_PREFIX + new_id()] = ThingRecord(
                state="dropped",
                name=item.name,
                spot=Spot(area=area.id, x=x, y=y),
                count=item.count,
                kind=kind,
            )
        world, art = self.world(), self._art_read()
        for lock in design.locked:
            inside = self._visual_area_named(lock.place)
            if inside is None:
                continue
            outside = self.area(inside.parent, world, art) if inside.parent else None
            if outside is None:
                continue
            for entrance in outside.entrances:
                if entrance.place_id == inside.id:
                    state.doors[door_id(outside.id, entrance.x, entrance.y)] = DoorState(
                        locked=True, key=lock.key, sealed=lock.sealed
                    )
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
        base = design.abilities.get(held, design.default_ability)
        return base, best_weapon(r for _id, r in self.visual_inventory())

    def visual_hits_left(self) -> int:
        return max(0, self.visual_design().player_hits - self.area_state().wounds)

    def visual_item_actions(self, record: ThingRecord) -> tuple[str, ...]:
        return item_actions(record.kind or kind_of(record.name))

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
        if not can_be_taken(thing.name) and not (record is not None and record.kind):
            return f"The {bare(thing.name)} is too much to carry."
        if record is None:
            record = state.things[placement_id] = ThingRecord(state="taken", name=thing.name)
        record.state, record.by, record.spot = "taken", held, None
        record.kind = record.kind or kind_of(thing.name)
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
            self._set_live(state)
            return f"You {verb} the {name}." + (
                " That was the last of it." if record.state == "gone" else ""
            )
        if verb == "use" and kind == "medicine":
            if state.wounds <= 0:
                return "You aren't hurt."
            state.wounds -= 1
            record.count -= 1
            if record.count <= 0:
                record.state = "gone"
            self._set_live(state)
            return f"You see to your wounds with the {name}."
        if verb == "use" and kind == "paper" and record.seen:
            return f"The {name}: {record.seen}"
        return f"You can't do much with the {name} here yet."

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
        base, weapon = self.visual_ability()
        result = fight(base, weapon, creature, attacking=True)
        lines = [result.words]
        if result.won:
            creature.hits -= 1
            if creature.hits <= 0:
                creature.state = "down"
            else:
                lines[-1] = f"You hit {creature.name}; it keeps coming."
        else:
            state.wounds += 1
        state.seconds += 10
        self._set_live(state)
        noise = (me.x, me.y) if is_loud(weapon[0]) else None
        lines += self.visual_tick(0, noise=noise, reach=GUN_NOISE_M)
        return lines

    def visual_tick(
        self, seconds: int, *, noise: tuple[int, int] | None = None, reach: float = 0.0
    ) -> list[str]:
        """Time passes: the dead in the player's area move, and any beside
        the player attack. The lines for the log; the state saved."""
        state = self.area_state()
        area, me = self._visual_me(state)
        if area is None or me is None or state.dead:
            return []
        creatures = {c.id: c for c in state.creatures.values() if c.area == area.id}
        if not creatures:
            return []
        del reach  # every creature in the area hears a noise in it, for now
        beside = step_creatures(
            area, creatures, (me.x, me.y), seconds, noise=noise, rng=random.Random(state.seconds)
        )
        lines: list[str] = []
        base, weapon = self.visual_ability()
        for cid in beside:
            creature = creatures[cid]
            result = fight(base, weapon, creature, attacking=False)
            who = creature.name[0].upper() + creature.name[1:]
            if result.won:
                creature.hits -= 1
                if creature.hits <= 0:
                    creature.state = "down"
                    lines.append(f"{who} comes at you; {result.words[0].lower()}{result.words[1:]}")
                else:
                    lines.append(f"{who} comes at you; you beat it back.")
            else:
                state.wounds += 1
                lines.append(f"{who} comes at you: {result.words}")
        if state.wounds >= self.visual_design().player_hits:
            state.dead = True
            lines.append("That was one wound too many. You are dead.")
        self._set_live(state)
        return lines

    def visual_wait(self, minutes: int) -> list[str]:
        state = self.area_state()
        state.seconds += minutes * 60
        self._set_live(state)
        return self.visual_tick(minutes * 60)

    def visual_sleep(self) -> list[str]:
        """Sleep until seven in the morning."""
        clock = self.map_clock()
        now = clock[1] if clock else 0
        minutes = (SLEEP_UNTIL - now) % (24 * 60) or 24 * 60
        return [f"You sleep {minutes // 60} hours.", *self.visual_wait(minutes)]

    def visual_report(self) -> list[str]:
        """What the player newly sees or hears where they stand, and the
        area's flavour line the first time they are in it."""
        state = self.area_state()
        area, me = self._visual_me(state)
        if area is None or me is None:
            return []
        lines: list[str] = []
        if area.id not in state.flavour_said:
            design = self.visual_design()
            world = self.world()
            place = world.places.get(area.place_id)
            flavour = design.flavour.get(place.name if place else area.name) or design.flavour.get(
                area.name
            )
            state.flavour_said.append(area.id)
            if flavour:
                lines.append(flavour)
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
