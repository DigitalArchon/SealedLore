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

import threading
import zlib

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QMessageBox

from sealedlore.engine.area import Area, point_tile, tile_point
from sealedlore.engine.marks import mark_token, marks_in
from sealedlore.gui.area_render import TileSource, render_area, sprite_image
from sealedlore.gui.area_view import AreaView, Walker
from sealedlore.gui.map_tiles import person_sprite
from sealedlore.gui.tile_set_editor import TileSetEditor

# Areas kept painted, most recent first.
PAINTED_KEPT = 6
# Verbs done standing beside the person or thing.
WALK_UP = ("talk", "attack", "use", "take", "eat", "destroy")


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
        self.area_view.edit_look_requested.connect(self._area_edit_look)
        # Marks (⌖): a spot clicked on either view goes into the turn.
        self.area_view.mark_requested.connect(self._mark_tile)
        self.area_view.point_of = self._point_of_tile
        self.world_view.mark_requested.connect(self._mark_point)
        self.world_view.go_requested.connect(self._go_point)
        self.world_view.me_point = self._me_point
        self.composer.input.textChanged.connect(self._sync_marks)
        self.area_view.edge_wanted.connect(self._area_edge)
        self.area_view.edge_chosen.connect(self._area_edge_go)
        self._painted = _Painted()
        self._painted.done.connect(self._area_painted)
        self._area_images: dict[object, QImage] = {}
        self._area_painting: set[object] = set()
        self._walker_images: dict[tuple[str, str], QImage | None] = {}
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
                self.area_view.set_message("A private scene is open: the map is paused.")
            return
        if self._area_story != session.story.id:
            self._area_story = session.story.id
            self._area_images.clear()
            self._walker_images.clear()
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
            Walker(name, spot.x, spot.y, self._walker_image(name, art), held=name == held)
            for name, spot in state.spots.items()
            if spot.area == me.area
        ]
        key = self._area_key(area, session, art)
        image = self._area_images.get(key)
        if image is None:
            self._paint(area, key)
        clock = session.map_clock()
        clock_text = f"Day {clock[0]}, {clock[1] // 60:02d}:{clock[1] % 60:02d}" if clock else ""
        self.area_view.set_locked(self._busy)
        self.area_view.set_area(area, image, walkers, key=key, clock=clock_text)
        self.area_view.set_edge(session.edge_here())
        self._sync_marks()

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
        things = tuple((p.name, p.x, p.y, p.w, p.h) for p in area.placements)
        return (
            area.place_id,
            rows,
            things,
            session.tile_set_name(art),
            items,
            tuple(sorted(art.tile_assets.items())),
            tuple(sorted(art.tile_overrides.items())),
            session.story.defaults.map_density,
        )

    def _paint(self, area: Area, key: object) -> None:
        if key in self._area_painting:
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
                image = QImage()
            signal.done.emit(key, image)

        threading.Thread(target=paint, name="area-paint", daemon=True).start()

    def _area_painted(self, key: object, image: QImage) -> None:
        self._area_painting.discard(key)
        if image.isNull():
            return
        self._area_images[key] = image
        while len(self._area_images) > PAINTED_KEPT:
            self._area_images.pop(next(iter(self._area_images)))
        self.refresh_area()

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
        return area.place_id if area is not None and self.world_view.play_shown() else None

    # --- walking ----------------------------------------------------------------

    def _area_walk(self, x: int, y: int, beside: bool) -> None:
        session = self.session
        if session is None or self._busy:
            return
        steps = session.walk_route(x, y, beside=beside)
        if not steps:
            self.statusBar().showMessage("There's no way there from here.", 4000)
            self._area_pending = None
            return
        self.area_view.walk(steps)

    def _area_walked(self, steps: list) -> None:
        session = self.session
        if session is None or self._busy:
            return  # a job runs: the worker is the only writer of the session
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
        if area is not None and spot is not None and spot.area == area.place_id:
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

    # --- the tile set --------------------------------------------------------------

    def _area_looks(self) -> None:
        session = self.session
        if session is None:
            return
        art = session.map_art_copy()
        self.area_view.set_looks(
            session.tile_set_choices(), session.tile_set_name(art), art.tile_set
        )

    def _area_look(self, name: str) -> None:
        session = self.session
        if session is None:
            return
        session.set_tile_set(name)
        self.refresh_area()

    def _area_own_look(self) -> None:
        """Draw a tile set for this story: asked first, with the price."""
        session = self.session
        if session is None:
            return
        count, price, problem = session.own_tile_set_offer()
        if problem is not None:
            QMessageBox.information(self, "Draw a tile set", problem)
            return
        cost = f"about ${price:.2f}" if price is not None else "at the model's listed price"
        answer = QMessageBox.question(
            self,
            "Draw a tile set for this story?",
            f"The map art model ({session.map_art_model}) writes what each picture should "
            f"show from the story's world, then {count} pictures are drawn by "
            f"{self.config.map_tile_model}, {cost}: floors, walls, ground, roads, water, "
            "roofs, trees and the like. The map is drawn in the new set once all of it is in; "
            "the set it has now stays until then. Things and people already drawn keep their "
            "sprites.",
        )
        if answer != QMessageBox.Yes:
            return
        if session.start_own_tile_set():
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
        self.refresh_area()
