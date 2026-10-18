"""The window's side of quick corrections on the map (engine/
session_corrections.py; the author, Oct 17 2026: "quick manual editing
features to make it easier for the user to correct AI mistakes"). A mixin
for `MainWindow`: the play view's and the region map's corrections asked
for words where they need them, made on the session, and everything that
shows the map, the scene card or the cast redrawn from it."""

from __future__ import annotations

from PySide6.QtWidgets import QInputDialog

# The choice in Is really… for someone named wrongly who is nobody on file.
NOT_NAMED_YET = "Someone not named yet…"


class CorrectionWindow:
    def _wire_corrections(self) -> None:
        self.area_view.correction_requested.connect(self._area_correct)
        self.world_view.place_correction.connect(self._place_correct)

    def _correction_blocked(self) -> bool:
        """Say why nothing can be corrected now, if so."""
        session = self.session
        if session is None:
            return True
        if self._busy:
            self.statusBar().showMessage("The story is being written: the map waits for it.", 4000)
            return True
        problem = session.correction_problem()
        if problem is not None:
            self.statusBar().showMessage(problem, 4000)
            return True
        return False

    def _after_correction(self, done: bool, words: str) -> None:
        """Redraw what shows the map, and say what happened (or that it
        couldn't be done)."""
        session = self.session
        if session is None:
            return
        if done:
            undo = session.correction_undo
            self.statusBar().showMessage(
                f"{words}. Undo is in the map's right-click menu." if undo else words, 5000
            )
        else:
            self.statusBar().showMessage(f"{words}: that can't be done there.", 4000)
        self.area_view.set_undo(session.correction_undo)
        self.world_view.set_undo(session.correction_undo)
        self.refresh_world()
        self.refresh_area()
        self._refresh_scene_panel()

    # --- the play view ----------------------------------------------------------

    def _area_correct(self, action: str, args: tuple) -> None:
        session = self.session
        if action == "refresh":
            self.refresh_area()
            return
        if self._correction_blocked():
            self.refresh_area()  # a drag let go: back where it stood
            return
        area = self.area_view.area
        if area is None:
            return
        if action == "undo":
            done = session.undo_correction()
            self._after_correction(done, "Put back")
        elif action == "move_person":
            name, x, y = args
            self._after_correction(
                session.correct_move_person(name, area.id, x, y), f"{name} moved"
            )
        elif action == "move_thing":
            ident, x, y = args
            self._after_correction(session.correct_move_thing(ident, area.id, x, y), "Moved")
        elif action == "not_here":
            (name,) = args
            self._after_correction(session.correct_not_here(name), f"{name} taken off the map")
        elif action == "name":
            self._correct_name(args[0])
        elif action == "rename_thing":
            (ident,) = args
            thing = next((p for p in area.placements if p.id == ident), None)
            if thing is None:
                return
            name, ok = QInputDialog.getText(self, "Rename", "What is it called?", text=thing.name)
            if ok and name.strip():
                self._after_correction(
                    session.correct_rename_thing(ident, area.id, name), "Renamed"
                )
        elif action == "broken":
            ident, broken = args
            self._after_correction(
                session.correct_broken(ident, area.id, broken),
                "Broken" if broken else "Whole again",
            )
        elif action == "remove_thing":
            (ident,) = args
            self._after_correction(session.correct_remove_thing(ident, area.id), "Removed")
        elif action == "put_thing":
            x, y = args
            name, ok = QInputDialog.getText(self, "Put a thing here", "What is it?")
            if ok and name.strip():
                done = session.correct_put_thing(name, area.id, x, y) is not None
                self._after_correction(done, f"{name.strip()} put here")
        elif action == "place_object":
            name, x, y = args
            done = session.place_object(name, area.id, x, y) is not None
            self._after_correction(done, f"{name[:1].upper() + name[1:]} placed")
        elif action == "put_person":
            x, y = args
            people = session.people_to_put()
            name, ok = QInputDialog.getItem(
                self, "Put someone here", "Who? (or type a name)", people, 0, True
            )
            if ok and name.strip():
                self._after_correction(
                    session.correct_put_person(name.strip(), area.id, x, y),
                    f"{name.strip()} put here",
                )

    def _correct_name(self, name: str) -> None:
        """Name… (someone not named yet) or Is really… (someone named wrongly)."""
        session = self.session
        world = session.world()
        described = world.unknowns.get(name)
        people = [
            c.name
            for c in (*session.visible_cast(), *session.visible_supporting())
            if c.name != name
        ]
        if described is None:
            people.append(NOT_NAMED_YET)
            title, label = "Is really…", f"{name} is really:"
        else:
            title, label = "Name…", f"Who is {described}? (choose, or type a name)"
        choice, ok = QInputDialog.getItem(self, title, label, people, 0, True)
        if not ok or not choice.strip():
            return
        if choice == NOT_NAMED_YET:
            words, ok = QInputDialog.getText(
                self, "Not named yet", "How does the story describe them?"
            )
            if ok and words.strip():
                done = session.correct_name(name, "", description=words.strip())
                self._after_correction(done, f"{name} is someone not named yet")
            return
        done = session.correct_name(name, choice.strip())
        self._after_correction(done, f"{name} is {choice.strip()}")

    # --- the region map -------------------------------------------------------------

    def _place_correct(self, action: str, place_id: str, args: tuple) -> None:
        session = self.session
        if self._correction_blocked():
            return
        if action == "undo":
            self._after_correction(session.undo_correction(), "Put back")
            return
        world = session.world()
        place = world.places.get(place_id)
        if place is None:
            return
        if action == "rename":
            name, ok = QInputDialog.getText(self, "Rename this place", "Its name:", text=place.name)
            if ok and name.strip():
                self._after_correction(
                    session.correct_rename_place(place_id, name), f"{place.name} renamed"
                )
        elif action == "remove":
            self._after_correction(
                session.correct_remove_place(place_id), f"{place.name} taken off the map"
            )
        elif action == "move":
            x, y = args
            self._after_correction(
                session.correct_move_place(place_id, x, y), f"{place.name} moved"
            )
        elif action in ("merge", "inside"):
            others = sorted(
                (p for p in world.places.values() if p.id != place_id and p.id in world.seen),
                key=lambda p: p.name.lower(),
            )
            names = [p.name for p in others]
            if action == "inside":
                names.insert(0, "Nowhere: it stands on its own")
            prompt = (
                f"Merge {place.name} into (it is the same place as):"
                if action == "merge"
                else f"{place.name} is part of:"
            )
            choice, ok = QInputDialog.getItem(self, "Correct the map", prompt, names, 0, False)
            if not ok:
                return
            target = next((p.id for p in others if p.name == choice), None)
            if action == "merge":
                done = target is not None and session.correct_merge_place(place_id, target)
                self._after_correction(done, f"{place.name} merged into {choice}")
            else:
                done = session.correct_place_inside(place_id, target)
                self._after_correction(done, f"{place.name} put inside {choice}")
