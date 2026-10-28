"""The visual window's side of the play view (gui/area_view.py): what the
session's area rules say, shown. Adapted from the story window's
`AreaWindow` (gui/window_area.py) for a mode with no turns: the figure walks
as soon as the session has moved it, through doors with a fade, and the
areas are painted on a thread from their backdrops (the predesigned
paintings) or their tiles.

Nothing here calls a model, and nothing is drawn: a visual story's world
comes predesigned (the author, Oct 24 2026).
"""

from __future__ import annotations

import logging
import threading
import time
import zlib
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter

from sealedlore.engine.area import (
    Area,
    Placement,
    exit_side,
    front_of,
    inside_door,
    path,
    stairs_landing,
)
from sealedlore.engine.backdrops import loose_things
from sealedlore.engine.deck_plan import DRAWN_PIECES
from sealedlore.engine.tile_sets import head_noun
from sealedlore.engine.visual_rules import DEAD_SPRITE
from sealedlore.engine.world import key
from sealedlore.gui.area_render import (
    Ground,
    ThingPicture,
    TileSource,
    _building_boxes,
    px_for,
    render_ground,
    sprite_image,
    thing_light,
    thing_picture,
)
from sealedlore.gui.area_view import AreaView, PlaceMark, Walker
from sealedlore.gui.backdrop_render import backdrop_ground
from sealedlore.gui.map_art import picture_sprite, pixel_art
from sealedlore.gui.map_tiles import person_sprite
from sealedlore.storage.paths import story_dir
from sealedlore.threads import BackgroundThread

# Areas kept painted, most recent first.
PAINTED_KEPT = 8
# Things' pictures kept, before the oldest are let go.
THING_PICTURES_KEPT = 600
# How long the window waits for the painting threads as it closes.
PAINT_JOIN_SECONDS = 1.0

log = logging.getLogger("sealedlore.gui")


class _Painted(QObject):
    """Emitted on the painting thread, delivered on the GUI thread."""

    done = Signal(object, object)


class VisualPresenter(QObject):
    """Shows the session's area on the view, and walks the figure where the
    session has moved the player."""

    # A door is being gone through: the area left, the area arrived in
    # (the window fades through it).
    through_door = Signal(str, str)

    def __init__(self, view: AreaView, session_of: Callable[[], object], root: Path | None):
        super().__init__()
        self.view = view
        self._session_of = session_of
        self.root = root
        self._painted = _Painted()
        self._painted.done.connect(self._area_painted)
        self._area_images: dict[object, Ground] = {}
        self._area_painting: set[object] = set()
        self._area_failed: set[object] = set()
        self._paint_threads: list[threading.Thread] = []
        self._thing_source: TileSource | None = None
        self._thing_source_key: tuple | None = None
        self._thing_images: dict[tuple, ThingPicture] = {}
        self._walker_images: dict[tuple[str, str], QImage | None] = {}
        self._place_images: dict[tuple[str, str], QImage | None] = {}
        # A walk being shown through a door: the area it arrives in, shown
        # (with the walk on from its door) once the walk to the door is over.
        self._area_then: str | None = None
        self._story: str | None = None
        view.walk_finished.connect(self._walked)

    @property
    def session(self):
        return self._session_of()

    # --- showing -----------------------------------------------------------------

    def refresh(self, *, snap: bool = False) -> None:
        """The area the player stands in, as the session has it. Where the
        figure shown and the session disagree on the player, the figure
        walks over (through the door when the areas differ), unless `snap`:
        a journey, and they are simply there."""
        session = self.session
        view = self.view
        if session is None:
            return
        if self._story != session.story.id:
            self._story = session.story.id
            self._area_images.clear()
            self._thing_source_key = None
            self._area_failed.clear()
            self._walker_images.clear()
            self._place_images.clear()
            view.drop_walk()
        state = session.area_state()
        held = session.held_name()
        me = state.spots.get(held or "")
        area = session.area(me.area) if me is not None else None
        if me is None or area is None:
            self._area_then = None
            view.set_message("Nowhere to stand yet: this place has no plan to walk on.")
            return
        art = session.map_art_copy()
        walking = view.walking()
        walk = None if snap or walking else self._walk_shown(session, area, me)
        if walk is not None and walk[0] != area.id:
            # Through a door: the walk to it first, in the area being left;
            # this area, with the walk on from its door, once there.
            self._area_then = area.id
            view.walk(walk[1])
            return
        self._area_then = None
        walkers = [
            Walker(
                name,
                spot.x,
                spot.y,
                self._walker_image(name, art),
                held=name == held,
                carded=name == held or session.has_card(name),
                frames=self._walker_frames(name, art),
            )
            for name, spot in state.spots.items()
            if spot.area == area.id
        ]
        if walk is not None:
            x0, y0 = walk[1][0]
            walkers = [
                Walker(w.name, x0, y0, w.image, held=True, carded=w.carded, frames=w.frames)
                if w.held
                else w
                for w in walkers
            ]
        walkers += [
            Walker(
                name,
                bx,
                by,
                self._walker_image(name, art),
                door=(dx, dy),
                carded=session.has_card(name),
            )
            for name, bx, by, dx, dy in session.at_the_door()
        ]
        # The dead in this area (models/visual.py), no name over them; one
        # put down lies where it fell, as does a person beaten.
        walkers += [
            Walker(
                creature.id,
                creature.x,
                creature.y,
                self._creature_image(art),
                label="",
                fallen=creature.state == "down",
            )
            for creature in state.creatures.values()
            if creature.area == area.id
        ]
        walkers = [
            Walker(
                w.name,
                w.x,
                w.y,
                w.image,
                held=w.held,
                door=w.door,
                carded=w.carded,
                fallen=True,
                frames=w.frames,
            )
            if w.name in state.people_down
            else w
            for w in walkers
        ]
        view.creature_ids = {c.id for c in state.creatures.values() if c.area == area.id}
        view.fallen = set(state.people_down) | {
            c.id for c in state.creatures.values() if c.state == "down"
        }
        places = self._place_marks(area, session, art)
        built = session.built_area(area.id)
        avoid = list(built.placements) if built is not None else list(area.placements)
        ground: Ground | None = None
        failed = False
        backdrop = session.backdrop_of(area)
        if backdrop is not None:
            key_ = ("backdrop", backdrop.id, area.id, px_for(area))
            ground = self._area_images.get(key_)
            failed = key_ in self._area_failed
            if ground is None and not failed:
                ground = backdrop_ground(session.pictures.read(backdrop.file), area)
                if ground is None:
                    self._area_failed.add(key_)
                    failed = True
                else:
                    self._keep(key_, ground)
            if failed:
                backdrop = None
        if backdrop is None:
            key_ = self._area_key(area, session, art, avoid)
            ground = self._area_images.get(key_)
            failed = key_ in self._area_failed
            if ground is None and not failed:
                self._paint(area, key_, avoid)
        clock = session.map_clock()
        clock_text = f"Day {clock[0]}, {clock[1] // 60:02d}:{clock[1] % 60:02d}" if clock else ""
        if not (walking and self._area_then):
            view.set_area(
                area,
                ground,
                walkers,
                key=key_,
                clock=clock_text,
                minute=clock[1] if clock else None,
                failed=failed,
                places=places,
                weather=session.world().weather or session.visual_design().weather,
                things=self._thing_pictures(
                    area, session, art, loose=backdrop is not None and backdrop.things_painted
                ),
            )
        if walk is not None:
            view.walk(walk[1])
        view.set_edge(session.edge_here(), outdoor=not area.indoor)

    def _keep(self, key_: object, ground: Ground) -> None:
        self._area_images[key_] = ground
        while len(self._area_images) > PAINTED_KEPT:
            self._area_images.pop(next(iter(self._area_images)))

    def _walk_shown(self, session, area: Area, me) -> tuple[str, list[tuple[int, int]]] | None:
        """How the figure gets from where the view shows the player to where
        the session has them (`me`, in `area`): (the area the walk is in, its
        tiles); None when there is nothing to show."""
        view = self.view
        shown = view.held()
        shown_area = view.area
        if shown is None or shown_area is None:
            return None
        state = session.area_state()
        held = session.held_name()
        if shown_area.id == area.id:
            if (shown.x, shown.y) == (me.x, me.y):
                return None
            others = {(s.x, s.y) for n, s in state.spots.items() if n != held and s.area == area.id}
            route = path(area, (shown.x, shown.y), (me.x, me.y), blocked=others)
            return (area.id, route) if len(route) > 1 else None
        if self._area_then == area.id:
            start = self._door_landing(shown_area, area, shown)
            if start is None or start == (me.x, me.y):
                return None
            route = path(area, start, (me.x, me.y))
            return (area.id, route) if len(route) > 1 else None
        door = self._door_toward(shown_area, area, shown)
        if door is None:
            return None
        others = {
            (s.x, s.y) for n, s in state.spots.items() if n != held and s.area == shown_area.id
        }
        route = path(shown_area, (shown.x, shown.y), door, blocked=others)
        return (shown_area.id, route) if len(route) > 1 else None

    @staticmethod
    def _door_toward(here: Area, there: Area, me) -> tuple[int, int] | None:
        doors = [(e.x, e.y) for e in here.entrances if e.place_id == there.id]
        if not doors and here.indoor and here.parent == there.id:
            doors = list(here.exits)
        if not doors:
            return None
        return min(doors, key=lambda d: abs(d[0] - me.x) + abs(d[1] - me.y))

    @staticmethod
    def _door_landing(left: Area, here: Area, at) -> tuple[int, int] | None:
        entrance = left.entrance_at(at.x, at.y)
        if entrance is not None and entrance.stairs:
            return stairs_landing(here, (at.x, at.y))
        if not left.indoor:
            return inside_door(here, entrance.side if entrance else None)
        return front_of(here, left.place_id, exit_side(left, at.x, at.y))

    def _walked(self, _steps: list) -> None:
        """The walk shown is over: through the door (the window fades), or
        simply the view as the session has it."""
        if self.session is None:
            return
        if self._area_then is not None:
            left = self.view.area.id if self.view.area is not None else ""
            self.through_door.emit(left, self._area_then)
            return
        self.refresh()

    def cancel_walk(self) -> None:
        """A walk cut short: no door to go through at its end."""
        self._area_then = None

    def arrive(self) -> None:
        """After the fade: the area arrived in, the walk on from its door."""
        self.refresh()

    # --- painting -----------------------------------------------------------------

    def _cut_dir(self) -> Path | None:
        session = self.session
        if session is None or session.memory_only:
            return None
        return story_dir(session.story.id, self.root) / "cache" / "cut"

    def _set_key(self, session, art) -> tuple:
        name = session.tile_set_name(art)
        own = art.own_set_for(name)
        return (
            name,
            tuple(sorted(own[1].assets.items())) if own is not None else (),
            tuple(sorted(art.tile_overrides.items())),
            tuple(sorted(p.id for p in art.pictures if p.subject == "item")),
        )

    def _area_key(self, area: Area, session, art, avoid: list[Placement]) -> object:
        set_name, own, overrides, _items = self._set_key(session, art)
        px = px_for(area)
        lights = tuple(
            (p.id, p.x, p.y)
            for p in area.placements
            if thing_light(p, px, indoor=area.indoor) is not None
        )
        pieces = tuple(
            (p.id, p.piece, p.x, p.y, p.w, p.h, p.broken)
            for p in area.placements
            if p.piece in DRAWN_PIECES
        )
        floor = ("_" if area.indoor else ".") * 10
        rows = "".join(area.rows).translate(str.maketrans("0123456789", floor))
        return (
            area.id,
            zlib.crc32(rows.encode()),
            lights,
            tuple(sorted(self._lit(area, session))),
            self._dark(area, session),
            pieces,
            tuple((p.x, p.y, p.w, p.h) for p in avoid),
            set_name,
            own,
            overrides,
            session.story.defaults.map_density,
        )

    @staticmethod
    def _lit(area: Area, session) -> set[str]:
        """The buildings whose windows show a light (the session's
        `lit_buildings`; every one, as before, for a session without it)."""
        lit = getattr(session, "lit_buildings", None)
        return lit(area) if callable(lit) else set(area.buildings)

    @staticmethod
    def _dark(area: Area, session) -> bool:
        """No lamp lit: a place nobody has lived in for years."""
        abandoned = getattr(session, "place_abandoned", None)
        return bool(abandoned(area.id)) if callable(abandoned) else False

    def _paint(self, area: Area, key_: object, avoid: list[Placement]) -> None:
        if key_ in self._area_painting or key_ in self._area_failed:
            return
        session = self.session
        info, read = session.tile_set()
        source = TileSource(info, read, cut_dir=self._cut_dir())
        dress = session.story.defaults.map_density != "sparse"
        lit = self._lit(area, session)
        dark = self._dark(area, session)
        self._area_painting.add(key_)
        signal = self._painted

        def paint() -> None:
            try:
                ground = render_ground(area, source, dress=dress, avoid=avoid, lit=lit, unlit=dark)
            except Exception:  # noqa: BLE001 - a picture that fails is drawn plainly
                log.exception("The area %r couldn't be painted; a plain map stands in", area.name)
                ground = None
            signal.done.emit(key_, ground)

        thread = BackgroundThread(target=paint, name="visual-paint", daemon=True)
        self._paint_threads = [t for t in self._paint_threads if t.is_alive()] + [thread]
        thread.start()

    def _area_painted(self, key_: object, ground: Ground | None) -> None:
        self._area_painting.discard(key_)
        if self.session is None or self._story is None:
            return
        if ground is None or ground.image.isNull():
            self._area_failed.add(key_)
        else:
            self._keep(key_, ground)
        self.refresh()

    def join_paint_threads(self, seconds: float = PAINT_JOIN_SECONDS) -> None:
        deadline = time.monotonic() + seconds
        for thread in self._paint_threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        self._paint_threads = [t for t in self._paint_threads if t.is_alive()]

    def close(self) -> None:
        """The story is closing: nothing of it stays."""
        self.view.drop_walk()
        self._area_images.clear()
        self._thing_source, self._thing_source_key = None, None
        self._thing_images.clear()
        self._area_failed.clear()
        self._walker_images.clear()
        self._place_images.clear()
        self._area_then = None
        self._story = None
        self.join_paint_threads()

    # --- what stands on the ground ----------------------------------------------------

    def _thing_pictures(
        self, area: Area, session, art, *, loose: bool = False
    ) -> list[tuple[Placement, ThingPicture]]:
        set_key = self._set_key(session, art)
        if self._thing_source_key != set_key:
            self._thing_source_key = set_key
            self._thing_source = TileSource(*session.tile_set(art), cut_dir=self._cut_dir())
            self._thing_images.clear()
        px = px_for(area)
        out = []
        shown = loose_things(area, session.area_state().things) if loose else area.placements
        aged = self._dark(area, session)
        for f in shown:
            if f.piece in DRAWN_PIECES:
                continue
            look = (f.name, f.w, f.h, f.kind, f.broken, "take" in f.verbs, px, area.indoor, aged)
            picture = self._thing_images.get(look)
            if picture is None:
                picture = thing_picture(self._thing_source, f, px, indoor=area.indoor, aged=aged)
                if len(self._thing_images) > THING_PICTURES_KEPT:
                    self._thing_images.clear()
                self._thing_images[look] = picture
            out.append((f, picture))
        return out

    def _place_marks(self, area: Area, session, art) -> list[PlaceMark]:
        world = session.world()
        by_name = {key(p.name): p for p in world.places.values()}
        boxed = {key(n) for n in area.names.values()} | {key(area.name)}
        roofs = _building_boxes(area) if not area.indoor else []
        marks: list[PlaceMark] = []
        own = key(area.name)
        # A room's name (a world kit's template names its rooms) is no place
        # to mark: the farmhouse showed a gold diamond in every room.
        rooms = {key(r.name) for r in area.rooms if r.name}
        for name, (x, y) in area.labels.items():
            if key(name) in boxed:
                continue
            place = by_name.get(key(name))
            if place is None and key(name) in rooms:
                continue
            if place is None and any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in roofs):
                continue
            if place is None and (
                key(name).startswith(own) or head_noun(name) == head_noun(area.name)
            ):
                continue
            image = self._place_image(place, art) if place is not None else None
            marks.append(PlaceMark(name, x, y, image, place.what if place is not None else ""))
        return marks

    def _place_image(self, place, art) -> QImage | None:
        session = self.session
        picture = art.picture_shown("place", place.id, place.state)
        sprite = art.sprite_shown(place.id, place.state) if picture is None else None
        which = picture.id if picture is not None else sprite.id if sprite is not None else ""
        if not which:
            return None
        cache_key = (place.id, which)
        if cache_key in self._place_images:
            return self._place_images[cache_key]
        image: QImage | None = None
        if picture is not None:
            image = picture_sprite(picture.id, 96, lambda: session.pictures.read(picture.file))
        elif sprite is not None:
            image = pixel_art(sprite.svg)
        self._place_images[cache_key] = image
        return image

    def _creature_image(self, art) -> QImage | None:
        """One of the dead: the world's own sprite of them (a walker picture
        of "the dead", shipped with Doomsville), else the tile set's first
        generic person, greyed and greened."""
        session = self.session
        drawn = art.picture_for("walker", DEAD_SPRITE)
        if drawn is not None:
            cache_key = ("", "creature", drawn.file)
            if cache_key in self._walker_images:
                return self._walker_images[cache_key]
            image = sprite_image(session.pictures.read(drawn.file))
            if image is not None:
                self._walker_images[cache_key] = image
                return image
        cache_key = ("", "creature")
        if cache_key in self._walker_images:
            return self._walker_images[cache_key]
        image = None
        people = session.generic_people()
        if people:
            _info, read = session.tile_set(art)
            image = sprite_image(read(people[0]))
        if image is None:
            small = person_sprite("the dead")
            image = small.scaled(
                small.width() * 8, small.height() * 8, Qt.KeepAspectRatio, Qt.FastTransformation
            )
        tinted = image.convertToFormat(QImage.Format_ARGB32)
        painter = QPainter(tinted)
        painter.setCompositionMode(QPainter.CompositionMode_SourceAtop)
        painter.fillRect(tinted.rect(), QColor(70, 110, 60, 150))
        painter.end()
        self._walker_images[cache_key] = tinted
        return tinted

    def _walker_frames(self, name: str, art) -> list[QImage]:
        """A person's stride frames (`walker_frame_files`), one list per
        set of files, so the view can tell a change by identity."""
        session = self.session
        files = tuple(session.walker_frame_files(name, art))
        if not files:
            return []
        cache_key = (name, "frames", files)
        found = self._walker_images.get(cache_key)
        if found is None:
            found = [
                image
                for image in (sprite_image(session.pictures.read(f)) for f in files)
                if image is not None
            ]
            self._walker_images[cache_key] = found
        return found

    def _walker_image(self, name: str, art) -> QImage | None:
        session = self.session
        file = session.walker_file(name, art) or ""
        cache_key = (name, file)
        if cache_key in self._walker_images:
            return self._walker_images[cache_key]
        image: QImage | None = None
        if file:
            image = sprite_image(session.pictures.read(file))
        if image is None:
            people = session.generic_people()
            if people:
                _info, read = session.tile_set(art)
                image = sprite_image(read(people[zlib.crc32(name.encode()) % len(people)]))
        if image is None:
            small = person_sprite(name)
            image = small.scaled(
                small.width() * 8, small.height() * 8, Qt.KeepAspectRatio, Qt.FastTransformation
            )
        self._walker_images[cache_key] = image
        return image
