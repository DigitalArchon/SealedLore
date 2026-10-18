"""The main window's side of map-first play (gui/area_view.py,
engine/session_area.py). A mixin for `MainWindow`, beside WorldWindow.

The area is painted on a thread (gui/area_render.py: QImage and numpy only)
and kept by what it was painted from, so walking never repaints it; a
sprite that comes in for a thing in it repaints it once. Walking, doors and
travel go through the session, which keeps where everyone stands; a menu
choice walks the author's character up to whoever or whatever it is for
(Look stays where it is) and puts its turn in the composer (the author's
ruling: to be read, changed and sent).
"""

from __future__ import annotations

import logging
import threading
import time
import zlib
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog, QMessageBox

from sealedlore.engine.area import Area, point_tile, tile_point
from sealedlore.engine.marks import mark_token, marks_in
from sealedlore.engine.picture_shapes import FREE
from sealedlore.engine.tile_sets import head_noun
from sealedlore.engine.world import key
from sealedlore.engine.world_pictures import views_of
from sealedlore.gui.area_render import TileSource, render_area, sprite_image
from sealedlore.gui.area_view import AreaView, PlaceMark, Walker
from sealedlore.gui.crop_dialog import cropped_picture
from sealedlore.gui.isometric_view import IsometricDialog, made_when
from sealedlore.gui.map_art import picture_sprite, pixel_art
from sealedlore.gui.map_render import area_scene_png
from sealedlore.gui.map_tiles import person_sprite
from sealedlore.gui.ref_images import PICTURE_FILTER, image_of
from sealedlore.gui.tile_set_editor import TileSetEditor
from sealedlore.gui.tile_set_gallery import TileSetDesigner, TileSetGallery
from sealedlore.models.character import Character
from sealedlore.storage.repository import save_config

# Areas kept painted, most recent first.
PAINTED_KEPT = 6
# Verbs done standing beside the person or thing.
WALK_UP = ("talk", "attack", "use", "take", "eat", "destroy")
# How long the window waits for the painting threads as it closes.
PAINT_JOIN_SECONDS = 1.0
# A new portrait landed while a job ran: its avatar is seen to this often
# until the job is done.
AVATAR_RETRY_MS = 1000

log = logging.getLogger("sealedlore.gui")


class _Painted(QObject):
    """Emitted on the painting thread, delivered on the GUI thread."""

    done = Signal(object, object)


def _duration(seconds: int) -> str:
    minutes = seconds / 60
    if minutes < 1:
        return "under a minute on foot"
    if minutes < 90:
        return f"{round(minutes)} min on foot"
    hours = minutes / 60
    return f"{hours:.1f} h on foot" if hours < 10 else f"{hours:.0f} h on foot"


class AreaWindow:
    def _build_area(self) -> None:
        """Called once the map's page is made (`_build_world`)."""
        self.area_view = AreaView()
        self.world_view.attach_play(self.area_view)
        self.world_view.play_ready = self._area_ready
        self.area_view.walk_requested.connect(self._area_walk)
        self.area_view.walk_finished.connect(self._area_walked)
        self.area_view.action_chosen.connect(self._area_action)
        self.area_view.travel_wanted.connect(self._area_travel_menu)
        self.area_view.travel_chosen.connect(self._area_travel)
        self.area_view.looks_wanted.connect(self._area_looks)
        self.area_view.look_chosen.connect(self._area_look)
        self.area_view.own_look_requested.connect(self._area_own_look)
        self.area_view.gallery_requested.connect(self._area_gallery)
        self.area_view.edit_look_requested.connect(self._area_edit_look)
        self.area_view.offer_dismissed.connect(self._area_offer_dismissed)
        # Marks (⌖): a spot clicked on either view goes into the turn.
        self.area_view.mark_requested.connect(self._mark_tile)
        self.area_view.point_of = self._point_of_tile
        self.world_view.mark_requested.connect(self._mark_point)
        self.world_view.go_requested.connect(self._go_point)
        self.world_view.me_point = self._me_point
        self.composer.input.textChanged.connect(self._sync_marks)
        self.area_view.edge_wanted.connect(self._area_edge)
        self.area_view.edge_chosen.connect(self._area_edge_go)
        self.area_view.carry_wanted.connect(self._area_carry_menu)
        self.area_view.carry_chosen.connect(self._area_carry)
        # Paintings of the place stood in (gui/map_render.area_scene_png).
        self.area_view.paintings_wanted.connect(self._area_paintings_menu)
        self.area_view.paint_place_requested.connect(self._area_paint_place)
        self.area_view.painting_chosen.connect(self._area_show_painting)
        self._painted = _Painted()
        self._painted.done.connect(self._area_painted)
        self._area_images: dict[object, QImage] = {}
        self._area_painting: set[object] = set()
        # Keys whose painting failed: the plain map stands in for them until
        # what they are painted from changes (they were painted again on
        # every refresh, and failed again).
        self._area_failed: set[object] = set()
        self._paint_threads: list[threading.Thread] = []
        self._walker_images: dict[tuple[str, str], QImage | None] = {}
        self._place_images: dict[tuple[str, str], QImage | None] = {}
        self._area_pending: tuple[str, str, str] | None = None
        self._area_story: str | None = None

    # --- showing --------------------------------------------------------------

    def _area_ready(self) -> bool:
        """There is an area for the author's character to walk on now."""
        session = self.session
        if session is None or not session.map_first or session.in_private:
            return False
        held = session.held_name()
        me = session.area_state().spots.get(held or "")
        return me is not None and session.area(me.area) is not None

    def refresh_area(self) -> None:
        """The area the author's character stands in, as the session has it."""
        session = self.session
        if session is None or not session.map_first or session.in_private:
            if session is not None and session.in_private:
                # A walk still going is forgotten first: finished, it would
                # write map state during the scene.
                self.area_view.drop_walk()
                self.area_view.set_message("A private scene is open: the map is paused.")
            return
        if self._area_story != session.story.id:
            self._area_story = session.story.id
            self._area_images.clear()
            self._area_failed.clear()
            self._walker_images.clear()
            self._place_images.clear()
            self.area_view.drop_walk()  # the last story's walk is nobody's
        state = session.area_state()
        held = session.held_name()
        me = state.spots.get(held or "")
        area = session.area(me.area) if me is not None else None
        if me is None or area is None:
            self.area_view.set_message(self._no_area_words(session))
            return
        art = session.map_art_copy()
        walkers = [
            Walker(
                name,
                spot.x,
                spot.y,
                self._walker_image(name, art),
                held=name == held,
                carded=name == held or session.has_card(name),
            )
            for name, spot in state.spots.items()
            if spot.area == me.area
        ]
        # Whoever is at a door from outside, shown at it (seen through it).
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
        places = self._place_marks(area, session, art)
        key = self._area_key(area, session, art)
        image = self._area_images.get(key)
        failed = key in self._area_failed
        if image is None and not failed:
            self._paint(area, key)
        clock = session.map_clock()
        clock_text = f"Day {clock[0]}, {clock[1] // 60:02d}:{clock[1] % 60:02d}" if clock else ""
        self.area_view.set_locked(self._busy)
        self.area_view.set_area(
            area,
            image,
            walkers,
            key=key,
            clock=clock_text,
            minute=clock[1] if clock else None,
            failed=failed,
            places=places,
            weather=session.world().weather,
        )
        self.area_view.set_edge(session.edge_here())
        self.area_view.set_carrying(session.carried_things(), session.people_in_reach())
        self.area_view.set_offer(session.tile_set_offer(art))
        self.area_view.set_setup_offer(session.objects_offer_line())
        self._sync_objects_panel()
        self._sync_marks()
        self.cast_panel.refresh_carrying()

    def _close_area(self) -> None:
        """The story is closing: nothing of its play view stays for the
        next (a walk still going, the view, the painted areas and figures)."""
        if not hasattr(self, "area_view"):
            return
        self.area_view.drop_walk()
        self.area_view.set_offer(None)
        self.area_view.set_message("No area here yet.")
        self._paint_waiting = None  # the next story's map paints nothing of this one
        self._area_images.clear()
        self._area_failed.clear()
        self._walker_images.clear()
        self._place_images.clear()
        self._area_pending = None
        self._area_story = None

    def _join_paint_threads(self, seconds: float = PAINT_JOIN_SECONDS) -> None:
        """Before the window goes: wait for the painting threads, this long
        at most, so none emits into a window being torn down."""
        deadline = time.monotonic() + seconds
        for thread in self._paint_threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        self._paint_threads = [t for t in self._paint_threads if t.is_alive()]

    def _no_area_words(self, session) -> str:
        if not self.config.map_art:
            return (
                "This place has no plan to walk on, and Draw the map is off (Settings → Models). "
                "Region and World show where you are."
            )
        if session.map_art_running:
            return "This place's plan is being drawn: it opens here when it is in."
        return (
            "This place has no plan to walk on yet: it is drawn after the next passage. "
            "Region and World show where you are."
        )

    def _area_key(self, area: Area, session, art) -> object:
        items = tuple(sorted(p.id for p in art.pictures if p.subject == "item"))
        rows = zlib.crc32("".join(area.rows).encode())
        things = tuple((p.name, p.x, p.y, p.w, p.h, p.broken) for p in area.placements)
        name = session.tile_set_name(art)
        own = art.own_set_for(name)
        return (
            area.id,
            rows,
            things,
            name,
            items,
            tuple(sorted(own[1].assets.items())) if own is not None else (),
            tuple(sorted(art.tile_overrides.items())),
            session.story.defaults.map_density,
        )

    def _paint(self, area: Area, key: object) -> None:
        if key in self._area_painting or key in self._area_failed:
            return
        session = self.session
        info, read = session.tile_set()
        source = TileSource(info, read)
        dress = session.story.defaults.map_density != "sparse"
        self._area_painting.add(key)
        signal = self._painted

        def paint() -> None:
            try:
                image = render_area(area, source, dress=dress)
            except Exception:  # noqa: BLE001 - a picture that fails is drawn plainly
                log.exception("The area %r couldn't be painted; a plain map stands in", area.name)
                image = QImage()
            signal.done.emit(key, image)

        thread = threading.Thread(target=paint, name="area-paint", daemon=True)
        self._paint_threads = [t for t in self._paint_threads if t.is_alive()] + [thread]
        thread.start()

    def _area_painted(self, key: object, image: QImage) -> None:
        self._area_painting.discard(key)
        if image.isNull():
            self._area_failed.add(key)
            self.refresh_area()  # the plain map, said to be standing in
            return
        self._area_images[key] = image
        while len(self._area_images) > PAINTED_KEPT:
            self._area_images.pop(next(iter(self._area_images)))
        self.refresh_area()

    def _place_marks(self, area: Area, session, art) -> list[PlaceMark]:
        """The map's places in the area that the picture doesn't draw as
        buildings (a rift, a well, a clearing): marked where they stand, with
        their sprite when the map has one."""
        world = session.world()
        by_name = {key(p.name): p for p in world.places.values()}
        boxed = {key(n) for n in area.names.values()} | {key(area.name)}
        marks: list[PlaceMark] = []
        own = key(area.name)
        for name, (x, y) in area.labels.items():
            if key(name) in boxed:
                continue
            place = by_name.get(key(name))
            if place is None and (
                key(name).startswith(own) or head_noun(name) == head_noun(area.name)
            ):
                # The plan naming itself ("Old Two-Storey Farmhouse" in the
                # farmhouse, "Kestrel's Due - hull" aboard her): no place of
                # its own, and marked as one in the middle of a room.
                continue
            image = self._place_image(place, art) if place is not None else None
            marks.append(PlaceMark(name, x, y, image, place.what if place is not None else ""))
        return marks

    def _place_image(self, place, art) -> QImage | None:
        """A place's sprite for the play view: its map picture cut out, else
        the map's pixel-art sprite, else None (a marker stands for it)."""
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

    def _walker_image(self, name: str, art) -> QImage | None:
        """A person's sprite: drawn from their portrait for this story, else
        one of the tile set's generic people (the same one each time), else
        the program's little figure."""
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

    def _sync_area_lock(self) -> None:
        if hasattr(self, "area_view"):
            self.area_view.set_locked(self._busy)
            self.world_view.locked = self._busy

    def _finish_walk_before_job(self) -> None:
        """Before any job starts: a walk still animating ends where the
        figure is and goes to the session, so the turn carries it and
        nothing of the walk is written while the worker runs."""
        if hasattr(self, "area_view") and self.area_view.walking():
            self._area_pending = None  # a menu choice mid-walk: the job came first
            self.area_view.finish_walk()

    def _play_area_id(self) -> str | None:
        area = self.area_view.area if hasattr(self, "area_view") else None
        # A window of open country has no plan to redraw: nothing to act on.
        return (
            area.place_id
            if area is not None and not area.window and self.world_view.play_shown()
            else None
        )

    # --- walking ----------------------------------------------------------------

    def _area_walk(self, x: int, y: int, beside: bool) -> None:
        session = self.session
        if session is None or self._busy:
            return
        if self.area_view.walking():
            # A click mid-walk: the walk so far goes to the session first, so
            # the route starts where the figure stands (it started from the
            # walk's start, and `walked` refused it: the click was lost and
            # the figure snapped back). A menu choice being walked up to is
            # dropped: the click is the author's new mind.
            self._area_pending = None
            before = self.area_view.area.id if self.area_view.area is not None else None
            self.area_view.finish_walk()
            after = self.area_view.area.id if self.area_view.area is not None else None
            if after != before:
                return  # the walk ended on a door and went through
        steps = session.walk_route(x, y, beside=beside)
        if not steps:
            self.statusBar().showMessage("There's no way there from here.", 4000)
            self._area_pending = None
            return
        self.area_view.walk(steps)

    def _area_walked(self, steps: list) -> None:
        session = self.session
        if session is None or self._busy or session.in_private:
            # A job runs: the worker is the only writer of the session. A
            # private scene: the map is paused, and nothing of it is written.
            return
        session.walked([tuple(s) for s in steps])
        door = session.door_here()
        pending, self._area_pending = self._area_pending, None
        if door is not None and pending is None:
            if session.go_through() is not None:
                self.statusBar().showMessage(f"You go through to {door[1]}.", 4000)
                # A building walked into is furnished, and its things drawn, now.
                if not session.in_private:
                    session.start_tile_art()
                    self._sync_progress()
        if pending is not None:
            self._fill_turn(*pending)
        self.refresh_area()

    def _area_action(self, verb: str, name: str, target_id: str) -> None:
        session = self.session
        if session is None or self._busy:
            return
        if verb in WALK_UP:
            spot = self._target_tile(name, target_id)
            if spot is not None:
                steps = session.walk_route(*spot, beside=True)
                if len(steps) > 1:
                    self._area_pending = (verb, name, target_id)
                    self.area_view.walk(steps)
                    return
        self._fill_turn(verb, name, target_id)

    def _target_tile(self, name: str, target_id: str) -> tuple[int, int] | None:
        area = self.area_view.area
        if area is None:
            return None
        if target_id:
            thing = next((p for p in area.placements if p.id == target_id), None)
            if thing is not None:
                return int(thing.centre[0]), int(thing.centre[1])
        person = next((w for w in self.area_view.walkers if w.name == name), None)
        return (person.x, person.y) if person is not None else None

    def _fill_turn(self, verb: str, name: str, target_id: str) -> None:
        """The menu's turn in the composer, as the author's character, for
        the author to read, change and send."""
        session = self.session
        text = session.map_action(verb, name, target_id)
        self._put_turn(text)

    def _put_turn(self, text: str) -> None:
        held = self.session.story.held_character_id if self.session else None
        self.composer.input.setPlainText(text)
        if held:
            self.composer.select_speaker(held)
        self.composer.focus_input()
        cursor = self.composer.input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.composer.input.setTextCursor(cursor)

    # --- marks (engine/marks.py) -----------------------------------------------------

    def _point_of_tile(self, x: int, y: int) -> tuple[float, float] | None:
        session = self.session
        area = self.area_view.area
        if session is None or area is None:
            return None
        return tile_point(session.world(), area, x, y)

    def _me_point(self) -> tuple[float, float] | None:
        session = self.session
        if session is None or not session.map_first or session.in_private:
            return None
        return session.held_point()

    def _mark_tile(self, x: int, y: int) -> None:
        point = self._point_of_tile(x, y)
        if point is not None:
            self._insert_mark(mark_token(*point))

    def _mark_point(self, x: float, y: float) -> None:
        self._insert_mark(mark_token(x, y))

    def _insert_mark(self, token: str) -> None:
        """The mark at the composer's cursor, spaced from the words round it."""
        editor = self.composer.input
        cursor = editor.textCursor()
        text = editor.toPlainText()
        at = cursor.position()
        before = text[at - 1] if at > 0 else ""
        after = text[at] if at < len(text) else ""
        piece = ("" if not before or before.isspace() else " ") + token
        piece += "" if after and after.isspace() else " "
        cursor.insertText(piece)
        editor.setTextCursor(cursor)
        self.composer.focus_input()

    def _sync_marks(self) -> None:
        """Pins on both views for the spots the turn being written marks."""
        if not hasattr(self, "area_view"):
            return
        session = self.session
        found = marks_in(self.composer.input.toPlainText()) if session is not None else []
        points = [(m.x, m.y) for m in found]
        self.world_view.set_marks(points)
        area = self.area_view.area
        tiles = []
        if session is not None and area is not None and points:
            world = session.world()
            for number, (x, y) in enumerate(points, start=1):
                tile = point_tile(world, area, x, y)
                if tile is not None:
                    tiles.append((number, tile))
        self.area_view.set_marks(tiles)

    def _go_point(self, x: float, y: float) -> None:
        """Go here, on the map: a spot in this area is walked to; one in
        another is travelled to, the turn in the composer."""
        session = self.session
        if session is None or self._busy or not session.map_first:
            return
        area = self.area_view.area
        spot = session.spot_of_point(x, y)
        if area is not None and spot is not None and spot.area == area.id:
            self.world_view.go_local()
            free = area.walkable(spot.x, spot.y, area.blocked())
            self._area_walk(spot.x, spot.y, not free)
            return
        text = session.go_to_point(x, y)
        if text:
            self._put_turn(text)
        self.refresh_area()
        if self._area_ready():
            self.world_view.go_local()

    # --- an area's edges ---------------------------------------------------------------

    def _area_edge(self, side: str) -> None:
        session = self.session
        if session is None or not side:
            return
        choices = []
        for choice_id, name, seconds in session.edge_choices(side):
            if choice_id == "area":
                choices.append(("area", f"Walk on into {name}"))
            else:
                choices.append((choice_id, f"{name} — {_duration(seconds)}"))
        self.area_view.set_edge_choices(side, choices)

    def _area_edge_go(self, side: str, choice_id: str) -> None:
        session = self.session
        if session is None or self._busy:
            return
        if choice_id == "area":
            if session.cross_edge(side) is not None:
                self.refresh_area()
                self.statusBar().showMessage(f"You walk on, {side}.", 4000)
                session.start_tile_art()
                self._sync_progress()
            else:
                self.statusBar().showMessage(f"The way {side} is barred here.", 4000)
            return
        self._area_travel(choice_id)

    # --- travel -------------------------------------------------------------------

    def _area_travel_menu(self) -> None:
        session = self.session
        if session is None:
            self.area_view.set_travel([])
            return
        choices = [
            (place_id, name, _duration(seconds))
            for place_id, name, seconds in session.travel_choices()[:16]
        ]
        self.area_view.set_travel(choices)

    def _area_travel(self, place_id: str) -> None:
        session = self.session
        if session is None or self._busy:
            return
        text = session.travel(place_id)
        if text:
            self._put_turn(text)
        self.refresh_area()

    # --- what is carried -------------------------------------------------------------

    def _area_carry_menu(self) -> None:
        session = self.session
        if session is None:
            self.area_view.set_carrying([], [])
            return
        self.area_view.set_carrying(session.carried_things(), session.people_in_reach())

    def _area_carry(self, verb: str, name: str, thing_id: str, to: str) -> None:
        """Drop or Give from Carrying ▾: the turn in the composer; the record
        changes when it is sent (`stamp_turn`)."""
        session = self.session
        if session is None or self._busy:
            return
        self._put_turn(session.map_action(verb, name, thing_id, to=to))

    def _carrying_of(self, name: str) -> list[str]:
        """What the map records `name` as carrying, for the Cast page."""
        session = self.session
        if session is None or not session.map_first or session.in_private:
            return []
        return [thing for _id, thing in session.carried_things(name)]

    # --- paintings of the place ------------------------------------------------------

    def _area_paintings_menu(self) -> None:
        """Paintings ▾ about to show: Paint this place… with its model and
        price (or why it can't be painted now), then the paintings the place
        stood in already has, newest first (`set_paintings`)."""
        session = self.session
        area = self.area_view.area
        if session is None or area is None:
            self.area_view.set_paintings("this place", ("", None, "No place to paint."), [])
            return
        model, price, problem = session.isometric_offer()
        if area.window or area.place_id not in session.world().places:
            problem = problem or "Open country has no plan to paint."
        art = session.map_art_copy()
        paintings = [
            (
                picture.id,
                (f"Painted {made_when(picture.made_at)}" if picture.made_at else "A painting")
                + (f" by {picture.model}" if picture.model else ", your own picture")
                + (f" · {picture.scene}" if picture.scene else ""),
            )
            for picture in reversed(views_of(art, area.place_id))
        ]
        self.area_view.set_paintings(area.name, (model, price, problem), paintings)

    def _area_paint_place(self) -> None:
        """Paint this place…: the area as the play view shows it now (its
        tiles, everyone and everything standing on it, `area_scene_png`)
        goes to the model, drawn at the size the model paints on the
        painting's own thread (the engine draws nothing). The press is the
        approval; the price was in the menu."""
        session = self.session
        area = self.area_view.area
        if session is None or area is None or area.window:
            return
        if session.tile_art_running and session.map_pictures_on:
            # Painted from the play view as it stands: while its pieces are
            # still being drawn that is the tile set's stand-ins (AfterDark's
            # new playthrough, Oct 18 2026). It starts once they are in.
            self._paint_waiting = area.place_id
            self.area_view.hint.setText(
                f"{area.name} will be painted as soon as the map has drawn its pieces."
            )
            self.statusBar().showMessage(
                "The map is still drawing this place's pieces: the painting starts once "
                "they are in.",
                8000,
            )
            self._sync_progress()
            return
        self._paint_waiting = None
        info, read = session.tile_set()
        source = TileSource(info, read)
        dress = session.story.defaults.map_density != "sparse"
        walkers = list(self.area_view.walkers)
        places = [item.mark for item in self.area_view.places.values()]

        def render(size: tuple[int, int]) -> bytes:
            return area_scene_png(area, source, walkers, places, size, dress=dress)

        if session.start_isometric(area.place_id, render):
            self.world_view.isometric_painting = session.isometric_painting
            self.area_view.hint.setText(f"Painting {area.name}…")
        else:
            problem = session.isometric_offer()[2] or "The painting can't be started now."
            self.statusBar().showMessage(problem, 6000)
        self.world_view.isometric_offer = session.isometric_offer()
        self.world_view.sync_isometric()
        self._sync_progress()

    def _paint_when_drawn(self) -> None:
        """A painting asked for while the place's pieces were drawn: started
        once they are in, if the play view still shows that place."""
        waiting = getattr(self, "_paint_waiting", None)
        session = self.session
        if waiting is None or session is None or session.tile_art_running:
            return
        self._paint_waiting = None
        area = self.area_view.area
        if area is not None and area.place_id == waiting and not session.in_private:
            self._area_paint_place()

    def _area_show_painting(self, picture_id: str) -> None:
        """One of the place's paintings, from Paintings ▾, seen whole."""
        session = self.session
        if session is None:
            return
        picture = next((p for p in session.map_art_copy().pictures if p.id == picture_id), None)
        if picture is None:
            return
        image = image_of(session.pictures.read(picture.file))
        if image.isNull():
            self.statusBar().showMessage("That painting's file can't be read.", 6000)
            return
        dialog = IsometricDialog(picture.name, image, picture.model, self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.exec()

    # --- the tile set --------------------------------------------------------------

    def _area_looks(self) -> None:
        session = self.session
        if session is None:
            return
        art = session.map_art_copy()
        self.area_view.set_looks(
            session.tile_set_choices(),
            session.tile_set_name(art),
            "" if art.tile_set_auto else art.tile_set,
        )

    def _area_look(self, name: str) -> None:
        session = self.session
        if session is None:
            return
        session.set_tile_set(name)
        self.refresh_area()

    def _area_gallery(self) -> None:
        """Every tile set with its pictures (gui/tile_set_gallery.py): use
        one, remove an own one, or go on to the designer or the editor."""
        session = self.session
        if session is None or self._busy:
            return
        session.tile_sets_offered()  # the offer, answered
        self.area_view.set_offer(None)
        dialog = TileSetGallery(session, self)
        try:
            result = dialog.exec()
        finally:
            dialog.deleteLater()
        self._walker_images.clear()
        self._place_images.clear()
        self.refresh_area()
        if result == TileSetGallery.DESIGN:
            self._area_own_look()
        elif result == TileSetGallery.EDIT:
            self._area_edit_look()

    def _area_offer_dismissed(self) -> None:
        session = self.session
        if session is None or self._busy:
            return
        session.tile_sets_offered()
        self.area_view.set_offer(None)

    def _area_own_look(self) -> None:
        """Draw a new tile set for this story (gui/tile_set_gallery.py): the
        author describes it, the designer writes the words and every prompt,
        and nothing is drawn until they have approved them with the price."""
        session = self.session
        if session is None or self._busy:
            return
        problem = session.own_tile_set_offer()[2]
        if problem is not None:
            QMessageBox.information(self, "Draw a new tile set", problem)
            return
        dialog = TileSetDesigner(session, self, browse=self._browse_models)
        try:
            accepted = dialog.exec() == QDialog.Accepted
            designer = dialog.designer_model()
        finally:
            dialog.deleteLater()
        # The designer picked in the dialog sticks (as the picture dialogs'
        # prompt writer does); never from a chat kept in memory only, which
        # teaches the settings nothing.
        if not session.memory_only and designer != (self.config.tileset_model or ""):
            self.config.tileset_model = designer or None
            save_config(self.config, root=self.root)
        # The designer's call, paid whether or not the set is drawn.
        for usage in session.unreported_usage:
            self.status_strip.add_cost(usage)
        session.unreported_usage.clear()
        self._refresh_story_cost()
        if accepted:
            self._sync_progress()
            self.statusBar().showMessage("Drawing this story's tile set in the background…", 6000)

    def _area_edit_look(self) -> None:
        """Every asset the map is drawn with, one at a time
        (gui/tile_set_editor.py)."""
        session = self.session
        if session is None or self._busy:
            return
        dialog = TileSetEditor(session, self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()
        self._walker_images.clear()
        self._place_images.clear()
        self.refresh_area()
        self.cast_panel.refresh_avatar()

    # --- a card's avatar on the map ------------------------------------------------

    def _wire_avatars(self) -> None:
        """Called once the Cast page is made: each card's avatar on the map
        (gui/avatar_view.py) is answered here."""
        self.cast_panel.avatar_of = self._avatar_of
        self.cast_panel.avatar_choices_of = self._avatar_choices
        self.cast_panel.carrying_of = self._carrying_of
        self.cast_panel.avatar_requested.connect(self._on_avatar_request)
        self.cast_panel.portrait_added.connect(lambda card_id: self._offer_avatars([card_id]))

    def _avatar_of(self, name: str) -> tuple[QImage | None, str, bool]:
        """(the figure the map walks for them, where it came from, whether
        it is being drawn now), for the Cast page."""
        session = self.session
        if session is None or not session.map_first:
            return None, "", False
        art = session.map_art_copy()
        data, source = session.walker_asset(name)
        if data is None and session.generic_people():
            source = "the set's"
        return (
            self._walker_image(name, art),
            source,
            session.tile_asset_drawing == f"walker/{name}",
        )

    def _avatar_choices(self, name: str) -> list[tuple[str, QImage | None, str, bool]]:
        """Every avatar kept for a person, newest first: (picture id, the
        figure, how it came to be, whether it walks for them now)."""
        session = self.session
        if session is None or not session.map_first:
            return []
        in_use = session.walker_file(name) or ""
        out = []
        for picture in session.walker_pictures(name):
            image = sprite_image(session.pictures.read(picture.file))
            how = f"drawn by {picture.model}" if picture.model else "your own picture"
            when = picture.made_at[:10]
            out.append(
                (picture.id, image, f"{how}, {when}" if when else how, picture.file == in_use)
            )
        return out

    def _avatar_card(self, card_id: str, *, drawn: bool = True) -> Character | None:
        """A card whose avatar may change now; `drawn` (by an image model):
        not one the plot keeps hidden, whose picture or name would be the
        first a model heard of them."""
        session = self.session
        card = session.card(card_id) if session is not None and session.map_first else None
        if not isinstance(card, Character) or session.in_private:
            return None
        return None if drawn and card.id in session.hidden_ids() else card

    def _on_avatar_request(self, card_id: str, what: str) -> None:
        """The Cast page's avatar: drawn (from the portrait, else the card),
        a file of the author's own, or the tile set's own figure again."""
        drawn = what in ("draw", "words")
        card = self._avatar_card(card_id, drawn=drawn)
        session = self.session
        if card is None and drawn and self._avatar_card(card_id, drawn=False) is not None:
            QMessageBox.information(
                self,
                "Draw the avatar",
                "Not until the story brings them in: the plot keeps them hidden, and their "
                "picture would be the first a model heard of them.",
            )
            return
        if card is None or self._busy:
            return
        role = f"walker/{card.name}"
        if what == "draw":
            self._draw_avatar(card)
            return
        if what == "words":
            self._draw_avatar(card, ask_words=True)
            return
        if what.startswith("use:"):
            if not session.use_walker_picture(card.name, what[4:]):
                return
        elif what == "file":
            path, _ = QFileDialog.getOpenFileName(
                self, f"An avatar for {card.name}", str(Path.home()), PICTURE_FILTER
            )
            if not path:
                return
            try:
                picture = cropped_picture(self, Path(path), FREE)
            except (OSError, ValueError) as exc:
                self._warn_plain("Couldn't use the picture", str(exc))
                return
            if picture is None:
                return
            session.use_own_tile_asset(role, picture[0])
        elif what == "reset":
            session.reset_tile_asset(role)
        else:
            return
        self._walker_images.clear()
        self._place_images.clear()
        self.refresh_area()
        self.cast_panel.refresh_avatar()

    def _draw_avatar(self, card: Character, *, ask_words: bool = False) -> bool:
        """The Cast page's button: ask, with the model and price, then draw
        their avatar in the background (the map and the card take it when it
        is in). `ask_words`: the author says what it should look like first
        (drawn from those words, not the portrait); every avatar drawn is
        kept, and the author chooses among them."""
        session = self.session
        role = f"walker/{card.name}"
        words = ""
        if ask_words:
            words, ok = QInputDialog.getMultiLineText(
                self,
                "Draw the avatar with instructions",
                f"What should {card.name}'s avatar look like? A few words of their looks, "
                "clothes and bearing; it is drawn from these, in the tile set's style.",
                "",
            )
            words = " ".join(words.split())
            if not ok or not words:
                return False
        model, price, problem = session.tile_asset_offer(role, words)
        if problem is not None:
            QMessageBox.information(self, "Draw the avatar", problem)
            return False
        cost = f"about ${price:.3f}" if price is not None else "at the model's listed price"
        source = (
            "your instructions"
            if words
            else "their portrait"
            if card.reference_images
            else "what their card says of them"
        )
        answer = QMessageBox.question(
            self,
            "Draw the avatar?",
            f"Draw {card.name}'s avatar for the map from {source}, by {model}, {cost}? "
            "It counts against today's map pictures, and it walks for them on the map once "
            "it is in; the portrait stays as it is.",
        )
        if answer != QMessageBox.Yes:
            return False
        started = session.redraw_tile_asset(role, words)
        self.cast_panel.refresh_avatar()
        return started

    def _ask_again(self, session, card_ids: list[str]) -> None:
        if self.session is session:
            self._ask_avatars(card_ids)

    def _offer_avatars(self, card_ids: list[str]) -> None:
        """A new portrait landed on these cards (drawn, or chosen from a
        file): offer their avatars drawn from it, in one question, once the
        handler that took the pictures is done (asked in the middle of it,
        the question held up the rest of the pictures)."""
        ids = list(card_ids)
        QTimer.singleShot(0, self, lambda: self._ask_avatars(ids))

    def _ask_avatars(self, card_ids: list[str]) -> None:
        """Every character has an avatar, drawn from the portrait they have
        now (the author, Oct 17 2026). With map pictures on, the map draws
        it from a new portrait itself (or offers it among the pictures that
        wait, when it asks first: `avatar_due`), unless the author chose the
        avatar or it is a sample's: then, and with map pictures off, one
        question for every card that got a new portrait, and one run drawing
        them all. Live, portraits landing
        one after another were each asked about alone, and every answer
        after the first was refused ("Another asset is being drawn now"):
        the author's own character got no avatar."""
        session = self.session
        if session is None:
            return
        if self._busy:
            # The worker owns the story: asked once it is done.
            QTimer.singleShot(AVATAR_RETRY_MS, self, lambda: self._ask_again(session, card_ids))
            return
        cards: list[Character] = []
        drawn_by_the_map = False
        for card_id in card_ids:
            card = self._avatar_card(card_id)
            if card is None:
                continue
            if session.avatar_due(card.name):
                drawn_by_the_map = True
                continue
            cards.append(card)
        if drawn_by_the_map:
            session.start_tile_art()
            self._sync_progress()
        if not cards:
            return
        model, price, problem = session.tile_asset_offer(f"walker/{cards[0].name}", "")
        if problem is not None:
            return
        cost = f"about ${price:.3f}" if price is not None else "at the model's listed price"
        if len(cards) == 1:
            name = cards[0].name
            text = (
                f"{name} has a new portrait. Draw their avatar for the map from their "
                f"portrait, by {model}, {cost}? It counts against today's map pictures, and "
                "it walks for them on the map once it is in; the portrait stays as it is."
            )
        else:
            names = ", ".join(card.name for card in cards)
            text = (
                f"{len(cards)} cards have new portraits: {names}. Draw their avatars for the "
                f"map from their portraits, by {model}, {cost} each? Each counts against "
                "today's map pictures, and walks for them on the map once it is in; the "
                "portraits stay as they are."
            )
        answer = QMessageBox.question(self, "Draw the avatars?", text)
        if answer != QMessageBox.Yes:
            return
        session.redraw_tile_assets([(f"walker/{card.name}", "") for card in cards])
        self.cast_panel.refresh_avatar()
