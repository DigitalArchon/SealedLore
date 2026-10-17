"""Quick corrections on the map (the author, Oct 17 2026: "quick manual editing
features to make it easier for the user to correct AI mistakes";
SealedLoreDocs design/map-corrections.md). A mixin for `StorySession`.

The author puts right what a read got wrong, on the map itself, with no
model call: someone moved, named, taken off the map or put on it; a thing
moved, renamed, broken or mended, taken off or put down; a place renamed,
taken off, merged into its duplicate, put inside another or moved.

Kept as the author's edits of the scene card are, on the latest message, so
each branch keeps its own and the next read starts from the corrected map:

- where people stand and what became of things: the live area state
  (`Story.area_live`), as walking is; the next turn carries it in its
  snapshot, and Take back puts it back;
- what the map knows (names, places, the map's things): a delta of its own
  for each correction on the latest message (`NodeMeta.world_fixes`),
  folded after its own read in the order made (`world.world_at`). The
  reads' rules (quotes, fixed things, places placed once) are for the
  reads; a correction is the author's.

Someone moved to or from the author's character's company is put on or
taken off the scene card's roster too (`set_scene`), so the card and the
map agree. One Undo, for the last correction. Never in a private scene (the
map is paused there), never while a passage is being written (the window
locks the map).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sealedlore.engine.area import Area, in_earshot, sees
from sealedlore.engine.scene_state import same_person
from sealedlore.engine.world import key
from sealedlore.ids import new_id
from sealedlore.models.area import AreaState, LiveArea, Spot, ThingRecord
from sealedlore.models.character import Character
from sealedlore.models.scene import SceneState
from sealedlore.models.world import WorldDelta, WorldMap, WorldPlace, WorldThing

# The last corrections that can be undone, newest last.
UNDO_KEPT = 20


@dataclass
class _Before:
    """What a correction changed, to put back: the latest message's id, its
    corrections and scene snapshot, and the story's live area and scene."""

    leaf_id: str | None
    world_fixes: list[WorldDelta]
    leaf_scene: SceneState | None
    area_live: LiveArea | None
    scene: SceneState
    what: str


class CorrectionRuntime:
    _corrections_done: list[_Before] | None = None

    # --- whether a correction can be made now ------------------------------------

    def correction_problem(self) -> str | None:
        """Why the map can't be corrected now, or None."""
        if not self.map_first:
            return "This story isn't played on a map."
        if self.in_private:
            return "A private scene is open: the map is paused."
        if not self.path():
            return "The story hasn't begun."
        return None

    def _begin_correction(self, what: str) -> None:
        if self.correction_problem() is not None:
            raise ValueError(self.correction_problem())
        path = self.path()
        leaf = path[-1]
        done = self.__dict__.setdefault("_corrections_done", [])
        done.append(
            _Before(
                leaf_id=leaf.id,
                world_fixes=[f.model_copy(deep=True) for f in leaf.meta.world_fixes],
                leaf_scene=leaf.meta.scene.model_copy(deep=True)
                if leaf.meta.scene is not None
                else None,
                area_live=self.story.area_live.model_copy(deep=True)
                if self.story.area_live is not None
                else None,
                scene=self.story.scene.model_copy(deep=True),
                what=what,
            )
        )
        del done[:-UNDO_KEPT]
        leaf.meta.world_fixes.append(WorldDelta(added=[]))

    def _fix(self) -> WorldDelta:
        """This correction's own delta on the latest message, to fill in."""
        return self.path()[-1].meta.world_fixes[-1]

    def _done(self, state: AreaState | None = None) -> None:
        """A correction made: the live area (when it changed) and the story
        saved, and anything the area cached forgotten."""
        leaf = self.path()[-1]
        if leaf.meta.world_fixes and leaf.meta.world_fixes[-1].empty():
            leaf.meta.world_fixes.pop()  # it changed nothing the map knows
        if state is not None:
            self._set_live(state)
        self._area_cache = None
        self.__dict__.pop("_laid_cache", None)
        self.save()

    @property
    def correction_undo(self) -> str | None:
        """What Undo would put back ("Ruth moved"), or None: only the last
        correction made on the message the story is at."""
        done = self.__dict__.get("_corrections_done") or []
        path = self.path()
        if not done or not path or done[-1].leaf_id != path[-1].id:
            return None
        return done[-1].what

    def undo_correction(self) -> bool:
        """Put the last correction back."""
        done = self.__dict__.get("_corrections_done") or []
        path = self.path()
        if not done or not path or done[-1].leaf_id != path[-1].id:
            return False
        before = done.pop()
        leaf = path[-1]
        leaf.meta.world_fixes = before.world_fixes
        leaf.meta.scene = before.leaf_scene
        self.story.area_live = before.area_live
        self.story.scene = before.scene
        self._done()
        return True

    # --- people ------------------------------------------------------------------

    def _state(self) -> AreaState:
        return self.area_state().model_copy(deep=True)

    def _free(self, area: Area, x: int, y: int, state: AreaState, who: str = "") -> bool:
        taken = {(s.x, s.y) for n, s in state.spots.items() if n != who and s.area == area.id}
        return area.walkable(x, y, area.blocked() | taken)

    def correct_move_person(self, name: str, area_id: str, x: int, y: int) -> bool:
        """Someone stands on this tile (no walk, no time passes); in another
        place, the map has them there."""
        state = self._state()
        world = self.world()
        area = self.area(area_id, world)
        if area is None or name not in state.spots or not self._free(area, x, y, state, name):
            return False
        self._begin_correction(f"{name} moved")
        state.spots[name] = Spot(area=area.id, x=x, y=y)
        self._where(name, state.spots[name], world)
        self._done(state)
        self._roster_follows(name, state)
        return True

    def correct_put_person(self, name: str, area_id: str, x: int, y: int) -> bool:
        """Someone the story has (a card, or a name the map keeps) stands
        here, on the map."""
        state = self._state()
        world = self.world()
        area = self.area(area_id, world)
        if area is None or not name.strip() or not self._free(area, x, y, state, name):
            return False
        self._begin_correction(f"{name} put here")
        state.spots[name] = Spot(area=area.id, x=x, y=y)
        fix = self._fix()
        if name in fix.left:
            fix.left.remove(name)
        self._where(name, state.spots[name], world)
        self._done(state)
        self._roster_follows(name, state)
        return True

    def correct_not_here(self, name: str) -> bool:
        """Someone taken off the map: they aren't where it had them (nor in
        the scene); never the author's character."""
        state = self._state()
        if name not in state.spots or name == self.held_name():
            return False
        self._begin_correction(f"{name} taken off the map")
        state.spots.pop(name)
        fix = self._fix()
        fix.where.pop(name, None)
        if name not in fix.left:
            fix.left.append(name)
        self._done(state)
        self._roster_follows(name, state)
        return True

    def correct_name(self, name: str, new: str, *, description: str = "") -> bool:
        """Someone is someone else: an unknown named ("Unknown 1" is Ruth),
        a wrong naming put right (the watchwoman named Marcus Webb is
        another), or, with `description`, someone named wrongly made not
        named yet again ("the watchwoman")."""
        new = new.strip()
        world = self.world()
        state = self._state()
        if description:
            new = f"Unknown {world.unknowns_made + 1}"
        if not new or new == name or new == self.held_name():
            return False
        if name not in state.spots and name not in world.where:
            return False
        self._begin_correction(f"{name} is {description or new}")
        fix = self._fix()
        if description:
            fix.unknowns[new] = description
        if name in world.unknowns and not description:
            fix.named[name] = new
        else:
            fix.renamed_people[name] = new
        if name in state.spots:
            spot = state.spots.pop(name)
            state.spots.pop(new, None)  # the right one stands where this one stood
            state.spots[new] = spot
        for record in state.things.values():
            if record.by == name:
                record.by = new
        self._done(state)
        self._rename_in_roster(name, new, description)
        return True

    def _where(self, name: str, spot: Spot, world: WorldMap) -> None:
        """The map's `where` follows a spot in another place."""
        place = self.place_of_spot(spot, world)
        if place is not None and world.where.get(name) != place:
            self._fix().where[name] = place

    def _with_me(self, name: str, state: AreaState) -> bool:
        """Whether `name` stands where the author's character sees or hears them."""
        me = state.spots.get(self.held_name() or "")
        them = state.spots.get(name)
        if me is None or them is None or me.area != them.area:
            return False
        area = self.area(me.area)
        if area is None:
            return False
        return sees(area, (me.x, me.y), (them.x, them.y)) or in_earshot(
            area, (me.x, me.y), (them.x, them.y)
        )

    def _roster_follows(self, name: str, state: AreaState) -> None:
        """The scene card's roster with the map: on it when with the author's
        character, off it when not (the card's own edit, as the Scene page
        makes)."""
        if name == self.held_name():
            return
        here = self._with_me(name, state)
        scene = self.story.scene.model_copy(deep=True)
        card = next((c for c in self.visible_cast() if same_person(name, c)), None)
        if card is not None:
            if here and card.id not in scene.present_character_ids:
                scene.present_character_ids.append(card.id)
            elif not here and card.id in scene.present_character_ids:
                scene.present_character_ids.remove(card.id)
            else:
                return
        else:
            person = Character(id="", name=name)
            listed = [o for o in scene.present_others if same_person(o, person)]
            if here and not listed and name not in self.world().unknowns:
                scene.present_others.append(name)
            elif not here and listed:
                scene.present_others = [o for o in scene.present_others if o not in listed]
            else:
                return
        self.set_scene(scene)

    def _rename_in_roster(self, name: str, new: str, description: str) -> None:
        scene = self.story.scene.model_copy(deep=True)
        person = Character(id="", name=name)
        others = [o for o in scene.present_others if same_person(o, person)]
        card = next((c for c in self.visible_cast() if same_person(new, c)), None)
        if not others and card is None:
            return
        scene.present_others = [o for o in scene.present_others if o not in others]
        if card is not None:
            if card.id not in scene.present_character_ids and others:
                scene.present_character_ids.append(card.id)
        elif not description:
            scene.present_others.append(new)
        self.set_scene(scene)

    # --- things ------------------------------------------------------------------

    def _placement(self, thing_id: str, area_id: str):
        """The thing as the play view shows it in `area_id`, with the area."""
        area = self.area(area_id, self.world())
        found = next((p for p in (area.placements if area else []) if p.id == thing_id), None)
        return (area, found) if found is not None else (None, None)

    def correct_move_thing(
        self, thing_id: str, area_id: str, x: int, y: int, *, to_area: str | None = None
    ) -> bool:
        """A thing shown in `area_id` lies on this tile (its top-left) of
        `to_area` (the same area by default), whatever it is: the author's
        word, so a fixed thing moves too."""
        _area, p = self._placement(thing_id, area_id)
        world = self.world()
        target = self.area(to_area or area_id, world)
        if p is None or target is None:
            return False
        state = self.area_state()
        others = [q for q in target.placements if q.id != thing_id]
        blocked = {t for q in others for t in q.tiles()}
        blocked |= {(s.x, s.y) for s in state.spots.values() if s.area == target.id}
        fp = [(x + dx, y + dy) for dy in range(p.h) for dx in range(p.w)]
        if not all(target.walkable(*t, blocked) for t in fp):
            return False
        self._begin_correction(f"{_bare(p.name)} moved")
        state = self._state()
        record = state.things.get(thing_id)
        broken = p.broken or (record is not None and record.state == "broken")
        state.things[thing_id] = ThingRecord(
            state="broken" if broken else "dropped",
            name=p.name,
            spot=Spot(area=target.id, x=x, y=y),
            w=p.w,
            h=p.h,
        )
        if thing_id in world.things:
            place = self.place_of_spot(state.things[thing_id].spot, world)
            copy = world.things[thing_id].model_copy(deep=True)
            copy.carried_by = None
            if place is not None:
                copy.at = place
            self._put_thing(copy)
        self._done(state)
        return True

    def correct_remove_thing(self, thing_id: str, area_id: str) -> bool:
        """A thing taken off the play view (and off the map's list)."""
        _area, p = self._placement(thing_id, area_id)
        if p is None:
            return False
        self._begin_correction(f"{_bare(p.name)} removed")
        state = self._state()
        state.things[thing_id] = ThingRecord(state="gone", name=p.name, w=p.w, h=p.h)
        if thing_id in self.world().things:
            fix = self._fix()
            fix.things = [t for t in fix.things if t.id != thing_id]
            if thing_id not in fix.things_removed:
                fix.things_removed.append(thing_id)
        self._done(state)
        return True

    def correct_rename_thing(self, thing_id: str, area_id: str, name: str) -> bool:
        """A thing is called something else (what it is, too: the menu's
        verbs and its picture follow the name)."""
        name = " ".join(name.split())
        area, p = self._placement(thing_id, area_id)
        if p is None or not name or name == p.name:
            return False
        self._begin_correction(f"{_bare(p.name)} renamed")
        state = self._state()
        world = self.world()
        if thing_id in world.things:
            copy = world.things[thing_id].model_copy(deep=True)
            copy.name = name
            self._put_thing(copy)
            record = state.things.get(thing_id)
            if record is not None:
                record.name = name
        else:
            record = state.things.get(thing_id)
            broken = p.broken or (record is not None and record.state == "broken")
            state.things[thing_id] = ThingRecord(
                state="broken" if broken else "dropped",
                name=name,
                spot=Spot(area=area.id, x=p.x, y=p.y),
                w=p.w,
                h=p.h,
            )
        self._done(state)
        return True

    def correct_broken(self, thing_id: str, area_id: str, broken: bool) -> bool:
        """A thing is broken, or whole again."""
        _area, p = self._placement(thing_id, area_id)
        if p is None or p.broken == broken:
            return False
        self._begin_correction(f"{_bare(p.name)} {'broken' if broken else 'mended'}")
        state = self._state()
        record = state.things.get(thing_id)
        spot = record.spot if record is not None else None
        if broken:
            state.things[thing_id] = ThingRecord(
                state="broken", name=p.name, spot=spot, w=p.w, h=p.h
            )
        elif spot is not None:
            state.things[thing_id] = ThingRecord(
                state="dropped", name=p.name, spot=spot, w=p.w, h=p.h
            )
        else:
            state.things.pop(thing_id, None)
        world = self.world()
        if thing_id in world.things:
            copy = world.things[thing_id].model_copy(deep=True)
            copy.state = "broken" if broken else ""
            self._put_thing(copy)
        self._done(state)
        return True

    def correct_put_thing(self, name: str, area_id: str, x: int, y: int) -> str | None:
        """A thing put on this tile: on the map's list at the place there,
        and lying here. Its id, or None."""
        name = " ".join(name.split())
        world = self.world()
        area = self.area(area_id, world)
        if area is None or not name:
            return None
        state = self._state()
        if not self._free(area, x, y, state):
            return None
        self._begin_correction(f"{name} put here")
        spot = Spot(area=area.id, x=x, y=y)
        thing = WorldThing(id=new_id(), name=name, at=self.place_of_spot(spot, world))
        self._put_thing(thing)
        state.things[thing.id] = ThingRecord(state="dropped", name=name, spot=spot)
        self._done(state)
        return thing.id

    def _put_thing(self, thing: WorldThing) -> None:
        fix = self._fix()
        fix.things = [t for t in fix.things if t.id != thing.id] + [thing]
        if thing.id in fix.things_removed:
            fix.things_removed.remove(thing.id)

    # --- places ------------------------------------------------------------------

    def _place(self, place_id: str) -> WorldPlace | None:
        return self.world().places.get(place_id)

    def correct_rename_place(self, place_id: str, name: str) -> bool:
        """A place is called something else; its old name stays as another
        name for it, so a read that uses it still finds it."""
        name = " ".join(name.split())
        place = self._place(place_id)
        if place is None or not name or name == place.name:
            return False
        self._begin_correction(f"{place.name} renamed {name}")
        self._fix().renamed[place_id] = name
        self._done()
        return True

    def correct_remove_place(self, place_id: str) -> bool:
        """A place taken off the map: what was inside it, and anyone or
        anything there, go to the place it was in."""
        place = self._place(place_id)
        if place is None:
            return False
        self._begin_correction(f"{place.name} taken off the map")
        fix = self._fix()
        if place_id not in fix.removed:
            fix.removed.append(place_id)
        self._done()
        return True

    def correct_merge_place(self, place_id: str, into_id: str) -> bool:
        """A duplicate place merged into the one it duplicates: its people,
        things and places inside go there, and its names become that
        place's other names (the read made "the farmhouse kitchen" beside
        the plan's kitchen)."""
        world = self.world()
        place, into = world.places.get(place_id), world.places.get(into_id)
        if place is None or into is None or place_id == into_id:
            return False
        if into_id in _within(world, place_id):
            return False  # never into what is inside it
        self._begin_correction(f"{place.name} merged into {into.name}")
        fix = self._fix()
        kept = into.model_copy(deep=True)
        for alias in [place.name, *place.aliases]:
            if key(alias) != key(kept.name) and alias not in kept.aliases:
                kept.aliases.append(alias)
        fix.places = [p for p in fix.places if p.id != into_id] + [kept]
        for child in world.places.values():
            if child.inside == place_id:
                moved = child.model_copy(deep=True)
                moved.inside = into_id
                fix.places = [p for p in fix.places if p.id != moved.id] + [moved]
        for who, where in world.where.items():
            if where == place_id:
                fix.where[who] = into_id
        for thing in world.things.values():
            if thing.at == place_id:
                copy = thing.model_copy(deep=True)
                copy.at = into_id
                self._put_thing(copy)
        if place_id not in fix.removed:
            fix.removed.append(place_id)
        self._done()
        return True

    def correct_place_inside(self, place_id: str, parent_id: str | None) -> bool:
        """A place is part of another (or of none)."""
        world = self.world()
        place = world.places.get(place_id)
        if place is None or place.inside == parent_id or place_id == parent_id:
            return False
        if parent_id is not None and (
            parent_id not in world.places or parent_id in _within(world, place_id)
        ):
            return False
        parent = world.places.get(parent_id or "")
        self._begin_correction(
            f"{place.name} put inside {parent.name}" if parent else f"{place.name} on its own"
        )
        copy = place.model_copy(deep=True)
        copy.inside = parent_id
        fix = self._fix()
        fix.places = [p for p in fix.places if p.id != place_id] + [copy]
        self._done()
        return True

    def correct_move_place(self, place_id: str, x: float, y: float) -> bool:
        """A place stands at this point on the map (metres east and north):
        what is inside it goes with it."""
        place = self._place(place_id)
        if place is None or place.x is None or not (math.isfinite(x) and math.isfinite(y)):
            return False
        self._begin_correction(f"{place.name} moved")
        copy = place.model_copy(deep=True)
        copy.x, copy.y = float(x), float(y)
        fix = self._fix()
        fix.places = [p for p in fix.places if p.id != place_id] + [copy]
        if place_id not in fix.moved:
            fix.moved.append(place_id)
        self._done()
        return True

    # --- for the menus -------------------------------------------------------------

    def people_to_put(self) -> list[str]:
        """Who can be put on the map here: the cast and supporting cards the
        story shows, and anyone the map keeps by a name, less whoever stands
        in the author's character's area already."""
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        here = {n for n, s in state.spots.items() if me is not None and s.area == me.area}
        world = self.world()
        names = [c.name for c in (*self.visible_cast(), *self.visible_supporting())]
        names += [n for n in world.where if n not in world.unknowns]
        return [n for n in dict.fromkeys(names) if n not in here]


def _within(world: WorldMap, place_id: str) -> set[str]:
    out: set[str] = set()
    todo = [place_id]
    while todo:
        current = todo.pop()
        for p in world.places.values():
            if p.inside == current and p.id not in out:
                out.add(p.id)
                todo.append(p.id)
    return out


def _bare(name: str) -> str:
    import re

    return re.sub(r"^(the|a|an)\s+", "", " ".join(name.split()), flags=re.IGNORECASE)
