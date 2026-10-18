"""Edit Mode on the map, the session's side (engine/map_edit.py; the
author, Oct 18 2026: a map the story grows opens in Edit Mode once its first
area is drawn, and Edit Mode is there whenever a story has a map).

The author's tiles are art, kept beside the plan they change (`WorldArt.
tile_edits`): the same on every branch, as the plan is. Buildings added or
taken off are places on the map, so they are corrections (engine/
session_corrections.py): on the latest message, each branch its own. Undo
puts back the last edit, either kind."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from sealedlore.engine.area import tile_point
from sealedlore.engine.map_edit import building_at, cleared, edit_key
from sealedlore.models.world import TileEdits, WorldPlace

# Edits Undo can put back, most recent last.
UNDO_KEPT = 40
# The kinds of place Add a place… offers, with their words.
PLACE_KINDS: dict[str, str] = {
    "building": "A building",
    "settlement": "A village, town or city",
    "site": "A site (a camp, a ruin, a mine)",
    "area": "An area (a field, a wood, a lake)",
}


@dataclass
class _Edit:
    what: str
    # The tiles of one area as they were: (area id, the area's size then,
    # {"x,y": the edit there before, or None for none}).
    tiles: tuple[str, tuple[int, int], dict[str, str | None]] | None = None
    # A correction made with it (a building added or taken off), put back
    # with `undo_correction`.
    correction: bool = False


class MapEditRuntime:
    # --- whether the map can be edited now -----------------------------------------

    def map_edit_problem(self) -> str | None:
        """Why the map can't be edited now, or None."""
        if not self.map_first:
            return "This story isn't played on a map."
        if self.in_private:
            return "A private scene is open: the map is paused."
        if not self.path():
            return "The story hasn't begun: its map is drawn from the opening."
        return None

    def map_setup_due(self) -> bool:
        """Whether the map should open in Edit Mode now: a story whose map
        it grows (not a plot file's, drawn beforehand: the samples are made
        by hand), once its opening is in and before the author's first turn,
        and only once."""
        return (
            self.map_first
            and not self.in_private
            and self.story.world_base is None
            and not self.story.map_edit_offered
            and bool(self.path())
            and not self._played()
        )

    def map_edit_offered(self) -> None:
        """Edit Mode was offered at the start (opened, or the line answered)."""
        changed = False
        for name in ("map_edit_offered", "objects_offered"):
            if not getattr(self.story, name):
                setattr(self.story, name, True)
                changed = True
        if changed:
            self.save()

    # --- tiles ---------------------------------------------------------------------

    def area_edits(self, area_id: str) -> TileEdits | None:
        return self.map_art_copy().tile_edits.get(area_id)

    def edit_tiles(
        self, area_id: str, tiles: Mapping[tuple[int, int], str], what: str = "Tiles painted"
    ) -> bool:
        """The author's tiles on an area: each painted, or (painted back to
        what the plan has there) its edit taken off. Undo puts them back."""
        if self.map_edit_problem() is not None or not tiles:
            return False
        before = self._paint(area_id, tiles)
        if before is None:
            return False
        self._remember(_Edit(what=what, tiles=before))
        return True

    def _paint(
        self, area_id: str, tiles: Mapping[tuple[int, int], str]
    ) -> tuple[str, tuple[int, int], dict[str, str | None]] | None:
        base = self._base_area(area_id)
        if base is None:
            return None
        size = (base.width, base.height)
        art = self.world_art
        with self._lock():
            edits = art.tile_edits.get(area_id)
            if edits is None or tuple(edits.size) != size:
                edits = TileEdits(size=size)
            was: dict[str, str | None] = {}
            for (x, y), ch in tiles.items():
                if not (0 <= x < base.width and 0 <= y < base.height):
                    continue
                spot = edit_key(x, y)
                was.setdefault(spot, edits.tiles.get(spot))
                if ch == base.at(x, y):
                    edits.tiles.pop(spot, None)
                else:
                    edits.tiles[spot] = ch
            if edits.tiles:
                art.tile_edits[area_id] = edits
            else:
                art.tile_edits.pop(area_id, None)
            self._save_art(art)
        self._forget_areas()
        return (area_id, size, was) if was else None

    def _forget_areas(self) -> None:
        self._area_cache = None
        self.__dict__.pop("_laid_cache", None)
        self.__dict__.pop("_edited_cache", None)

    def _remember(self, edit: _Edit) -> None:
        done = self.__dict__.setdefault("_edits_done", [])
        done.append(edit)
        del done[:-UNDO_KEPT]

    @property
    def edit_undo(self) -> str | None:
        """What Undo would put back ("Tiles painted"), or None."""
        done = self.__dict__.get("_edits_done") or []
        return done[-1].what if done else None

    def undo_edit(self) -> bool:
        """Put the last edit back."""
        done = self.__dict__.get("_edits_done") or []
        if not done:
            return False
        edit = done.pop()
        if edit.correction and self.correction_undo is not None:
            self.undo_correction()
        if edit.tiles is not None:
            area_id, size, was = edit.tiles
            art = self.world_art
            with self._lock():
                edits = art.tile_edits.get(area_id)
                if edits is None or tuple(edits.size) != size:
                    edits = TileEdits(size=size)
                for spot, ch in was.items():
                    if ch is None:
                        edits.tiles.pop(spot, None)
                    else:
                        edits.tiles[spot] = ch
                if edits.tiles:
                    art.tile_edits[area_id] = edits
                else:
                    art.tile_edits.pop(area_id, None)
                self._save_art(art)
        self._forget_areas()
        self.save()
        return True

    # --- buildings and places --------------------------------------------------------

    def add_building(
        self, area_id: str, x0: int, y0: int, x1: int, y1: int, name: str, what: str = ""
    ) -> str | None:
        """A building on an outdoor area, where the author drew it: a place
        on the map (in the area's place), as wide as what was drawn, which
        the plan then boxes with its walls and door, and whose inside is
        drawn as any building's is. Its id, or None."""
        area = self.area(area_id)
        if area is None or area.indoor or self.map_edit_problem() is not None:
            return None
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        world = self.world()
        a = tile_point(world, area, x0, y0)
        b = tile_point(world, area, x1 + 1, y1 + 1)
        if a is None or b is None:
            return None
        size = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        place_id = self.correct_add_place(
            name,
            "building",
            inside=area.place_id or None,
            x=(a[0] + b[0]) / 2,
            y=(a[1] + b[1]) / 2,
            size=size,
            what=what,
        )
        if place_id is not None:
            self._remember(_Edit(what=f"{' '.join(name.split())} added", correction=True))
        return place_id

    def remove_building(self, area_id: str, x: int, y: int) -> str | None:
        """The building under (x, y) taken off: one the map knows comes off
        the map (whoever and whatever was in it go to the place round it),
        and its tiles are open ground; one only the plan drew is painted
        over. Its name ("a building" for one the plan drew), or None."""
        area = self.area(area_id)
        if area is None or self.map_edit_problem() is not None:
            return None
        place_id, tiles = building_at(area, x, y)
        if not tiles:
            return None
        if place_id is not None:
            place = self.world().places.get(place_id)
            name = place.name if place is not None else "the building"
            if not self.correct_remove_place(place_id):
                return None
            before = self._paint(area_id, cleared(area, tiles))
            self._remember(_Edit(what=f"{name} taken off", tiles=before, correction=True))
            return name
        before = self._paint(area_id, cleared(area, tiles))
        if before is None:
            return None
        self._remember(_Edit(what="A building taken off", tiles=before))
        return "a building"

    def add_place(
        self, name: str, kind: str, x: float, y: float, size: float, what: str = ""
    ) -> str | None:
        """A place on the map where the author put it (Region or World: Add
        a place here…), in the place it falls in. Its id, or None."""
        if self.map_edit_problem() is not None or kind not in PLACE_KINDS:
            return None
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(size)) or size <= 0:
            return None
        inside = self._holding(x, y, size)
        place_id = self.correct_add_place(name, kind, inside=inside, x=x, y=y, size=size, what=what)
        if place_id is not None:
            self._remember(_Edit(what=f"{' '.join(name.split())} added", correction=True))
        return place_id

    def _holding(self, x: float, y: float, size: float) -> str | None:
        """The smallest place on the map wider than `size` whose ground the
        point is on (a farm round a new barn), or None."""
        best: WorldPlace | None = None
        for place in self.world().places.values():
            if place.x is None or place.y is None or not place.size or place.size <= size:
                continue
            if place.kind in ("building", "room", "vehicle", "route"):
                continue
            half = place.size / 2
            if abs(x - place.x) <= half and abs(y - place.y) <= half:
                if best is None or (best.size or 0) > place.size:
                    best = place
        return best.id if best is not None else None
