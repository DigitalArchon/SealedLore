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
    in_earshot,
    path,
    sees,
    stand_near,
    walk_metres,
    walk_seconds,
)
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
    moment_messages,
    parse_keeper,
    parse_ruling,
    variant_for,
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
from sealedlore.models.plot import Chronicle
from sealedlore.models.visual import DoorState, NpcGoal, PersonSheet, VisualDesign
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
        self._companions_to(spot)
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
        """The design's dead, things and locks laid on the world, once; its
        people stood where it puts them, each once their place is on the
        map (a plot-hidden place has no one in it until it is revealed)."""
        state = self.area_state()
        design = self.visual_design()
        world, art = self.world(), self._art_read()
        taken_by_area: dict[str, set[tuple[int, int]]] = {}

        def taken_in(area: Area) -> set[tuple[int, int]]:
            return taken_by_area.setdefault(
                area.id,
                area.blocked()
                | {(sp.x, sp.y) for sp in state.spots.values() if sp.area == area.id},
            )

        changed = False
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
        if state.laid_out:
            if changed:
                self._set_live(state)
            return
        for spawn in design.creatures:
            area = self._visual_area_named(spawn.place)
            if area is None:
                continue
            for creature in spawn_creatures(
                area,
                spawn.kind,
                spawn.count,
                spawn.ability,
                taken_in(area),
                f"{area.id}:{spawn.kind}",
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
        del reach  # every creature in the area hears a noise in it, for now
        lines: list[str] = self._step_people(state, area, me, seconds)
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
            if sees(area, (me.x, me.y), here) or in_earshot(area, (me.x, me.y), here):
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
            self._set_live(state)
        messages = npc_messages(
            name=name,
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
                bare(weapon[0]) if weapon[0] else "", state.wounds, len(self.visual_inventory())
            ),
            witnessed=state.witnessed.get(name, []),
            memories=state.memories.get(name, []),
            talk=state.talks.get(name, []),
            line=line or self.texts["npc.encounter"],
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
        else:
            state.goals[name] = NpcGoal(intent="stay")
        if words:
            lines.append(words)
        self._set_live(state)
        if kind == "attack":
            lines += self.visual_tick(0)
        return lines

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
            here = (spot.x, spot.y)
            if goal.intent == "follow":
                target = (me.x, me.y)
                if max(abs(here[0] - me.x), abs(here[1] - me.y)) <= 1:
                    continue
                stand = stand_near(area, target, taken - {here} | {target})
                route = path(area, here, stand, blocked=taken - {here})
                step = route[min(len(route) - 1, max(1, tiles * 2))] if len(route) > 1 else here
            elif goal.intent == "attack":
                if max(abs(here[0] - me.x), abs(here[1] - me.y)) <= 1:
                    lines += self._person_attacks(state, name)
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

    def _person_attacks(self, state, name: str) -> list[str]:
        """A person beside the player fights them (the simple combat)."""
        from sealedlore.models.visual import Creature

        sheet = self.visual_sheet(name)
        base, weapon = self.visual_ability()
        stand_in = Creature(name=name, area="", x=0, y=0, ability=sheet.ability)
        result = fight(base, weapon, stand_in, attacking=False)
        if result.won:
            state.people_down.append(name)
            state.goals.pop(name, None)
            if name in state.companions:
                state.companions.remove(name)
            return [
                f"{name} comes at you; you put them down ({result.mine} against {result.theirs})."
            ]
        state.wounds += 1
        return [
            f"{name} comes at you and gets the better of you ({result.mine} against "
            f"{result.theirs}): you are hurt."
        ]

    def visual_attack_person(self, name: str) -> list[str]:
        """The player attacks a person: a fight; the person turns on them."""
        state = self.area_state()
        area, me = self._visual_me(state)
        spot = state.spots.get(name)
        if area is None or me is None or spot is None or spot.area != area.id:
            return []
        if max(abs(spot.x - me.x), abs(spot.y - me.y)) > 1:
            return [f"{name} isn't within reach."]
        from sealedlore.models.visual import Creature

        sheet = self.visual_sheet(name)
        base, weapon = self.visual_ability()
        stand_in = Creature(name=name, area="", x=0, y=0, ability=sheet.ability)
        result = fight(base, weapon, stand_in, attacking=True)
        self._visual_witness(state, f"attacked {name}")
        if result.won:
            state.people_down.append(name)
            state.goals.pop(name, None)
            if name in state.companions:
                state.companions.remove(name)
            lines = [f"You put {name} down ({result.mine} against {result.theirs})."]
        else:
            state.wounds += 1
            state.goals[name] = NpcGoal(intent="attack", toward=self.held_name() or "")
            lines = [
                f"{name} gets the better of you ({result.mine} against {result.theirs}): "
                "you are hurt."
            ]
        state.seconds += 10
        self._set_live(state)
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
        return chronicle

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

    def visual_plot_check(self) -> tuple[list[str], str]:
        """After anything that moved the game: what the plot's code decides
        (offscreen happenings, windows closed), and whether the keeper
        should be asked now: on arriving somewhere the plot knows with an
        event armed there, on a new day with one due, or when one is
        overdue. (The lines for the log, why to ask or "")."""
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
            f"{n}" + (f" ({state.moods[n]})" if n in state.moods else "")
            for n, sp in state.spots.items()
            if area is not None and sp.area == area.id and n != held and n not in state.people_down
        ]
        dead = sum(
            1
            for c in state.creatures.values()
            if area is not None and c.area == area.id and c.state != "down"
        )
        carried = [
            bare(r.name) + (f" ×{r.count}" if r.count > 1 else "")
            for _i, r in self.visual_inventory()
        ]
        companions = ", ".join(state.companions) or "nobody"
        visited = ", ".join(chronicle.visited) or "nowhere yet"
        return (
            f"{held} is at {where} ({plot_place}); it is {when}; wounds {state.wounds}. "
            f"With them: {companions}. Here too: {', '.join(people) or 'nobody'}; "
            f"the dead here: {dead}. "
            f"Carrying: {', '.join(carried) or 'nothing'}. Places visited: {visited}."
        )

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
            log_ref = self._log(
                "keeper_request", {"kind": kind, "payload": self.provider.build_payload(request)}
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

    def apply_keeper_reply(self, reply: KeeperReply) -> list[str]:
        """Events fired (happened in the chronicle, their facts set, whoever
        they bring in on the board) and the scripts applied (GUI thread)."""
        lines: list[str] = []
        for firing in reply.fired:
            lines += self._fire(firing.event, firing.variant)
            lines += self.apply_script(firing.script)
        lines += self.apply_script(reply.script)
        if reply.note:
            lines.append(reply.note)
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
        if not words or any(
            w in words for w in ("beside", "here", "player", "with them", "next to")
        ):
            if area is None or me is None:
                return None
            taken = area.blocked() | {(s.x, s.y) for s in state.spots.values() if s.area == area.id}
            x, y = stand_near(area, (me.x, me.y), taken | {(me.x, me.y)})
            return Spot(area=area.id, x=x, y=y)
        target = self._visual_area_named(where)
        if target is None:
            # A place named loosely: the one whose name holds the words.
            world = self.world()
            place = next((p for p in world.places.values() if words in p.name.casefold()), None)
            target = self._visual_area_named(place.name) if place is not None else None
        if target is None:
            return None
        world = self.world()
        place = next((p for p in world.places.values() if p.name.casefold() == words), None)
        at = anchor_of(target, world, place.id) if place is not None else None
        taken = target.blocked() | {(s.x, s.y) for s in state.spots.values() if s.area == target.id}
        x, y = stand_near(target, at or (target.width // 2, target.height // 2), taken)
        return Spot(area=target.id, x=x, y=y)

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

    def apply_script(self, ops: list[Op]) -> list[str]:
        """The keeper's operations, in order, on the live state; the lines
        the player reads."""
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
                if text:
                    lines.append(f"{who}: {text}")
                    state.talks.setdefault(who, []).append([who, text])
            elif kind == "spawn":
                lines += self._stand_person(state, op.get("who"), op.get("where"))
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
                    lines.append(f"The dead: {op.count(1)} more at {target.name}.")
            elif kind == "move":
                who, where = op.get("who"), op.get("where").casefold()
                card = self.visual_card(who)
                if card is None:
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
                    )
                    lines.append(f"You now have {what}.")
            elif kind == "take_away":
                want = set(re.findall(r"[a-z]+", op.get("what").lower())) - {"the", "a", "an"}
                for record in state.things.values():
                    if (
                        record.state == "taken"
                        and record.by == held
                        and want & set(re.findall(r"[a-z]+", record.name.lower()))
                    ):
                        record.state = "gone"
                        lines.append(f"The {bare(record.name)} is gone from your hands.")
                        break
            elif kind == "break":
                want = set(re.findall(r"[a-z]+", op.get("what").lower())) - {"the", "a", "an"}
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
            elif kind == "set_fact":
                chronicle = self.visual_chronicle()
                set_fact(chronicle, op.get("fact"), op.get("value"))
                state.chronicle = chronicle
            elif kind == "wound":
                state.wounds += op.count(1)
            elif kind == "heal":
                state.wounds = max(0, state.wounds - op.count(1))
            elif kind == "clock":
                state.seconds += op.count(0) * 60
            elif kind == "end":
                state.ended = op.get("text") or "The story ends here."
                lines.append(state.ended)
        if state.wounds >= self.visual_design().player_hits:
            state.dead = True
            lines.append("That was one wound too many. You are dead.")
        self._set_live(state)
        self.visual_lay_out()  # a revealed place's people stand in it
        return lines
