"""Edit Mode on the map, the window's side (engine/session_edit.py,
gui/map_edit_bar.py; the author, Oct 18 2026). A map the story grows opens in
it once its first area is drawn; ✎ Edit map on the play view opens it any
time. The tools change the session's map; this wires them up and says what
happened."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from sealedlore.engine.map_edit import building_at, walled_box

# Smaller than this each way, a box is no room or building.
SMALLEST_BOX = 3


class MapEditWindow:
    _map_editing = False
    _edit_area_id: str | None = None

    def _wire_map_edit(self) -> None:
        view = self.area_view
        view.edit_requested.connect(self.enter_map_edit)
        view.edit_done.connect(self.leave_map_edit)
        view.edit_undo.connect(self._edit_undo)
        view.edit_back.connect(self._edit_back)
        view.edit_outside.connect(self._edit_outside)
        view.tiles_painted.connect(self._edit_paint)
        view.box_drawn.connect(self._edit_box)
        view.building_clicked.connect(self._edit_building)
        # The line offered while a story's first area is set up (a plot
        # file's map, drawn beforehand) opens Edit Mode.
        view.setup_offer_button.clicked.disconnect()
        view.setup_offer_button.clicked.connect(self.enter_map_edit)

    # --- in and out -----------------------------------------------------------------

    def enter_map_edit(self, *, at_start: bool = False) -> None:
        session = self.session
        if session is None:
            return
        problem = session.map_edit_problem()
        if problem is not None:
            self.statusBar().showMessage(problem, 6000)
            return
        if self._busy:
            self.statusBar().showMessage("The story is being written: the map waits for it.", 5000)
            return
        self._map_editing = True
        self._edit_area_id = None
        session.map_edit_offered()
        self.area_view.set_setup_offer(None)
        if self.right_tabs.currentWidget() is not self.world_view:
            self.right_tabs.setCurrentWidget(self.world_view)
        self.world_view.go_local()
        self.area_view.set_editing(True)
        self.area_view.edit_bar.set_back(None)
        self._sync_edit_undo()
        self.refresh_area()
        if at_start:
            words = (
                "Your map is drawn. Before you play, change anything you like: paint walls, "
                "doors and floors, draw rooms, add or take off buildings, place things. Press "
                "Done when it is as you want it; ✎ Edit map brings this back any time."
            )
            self.area_view.edit_bar.say(words)
            self.statusBar().showMessage("The map opens in Edit Mode.", 8000)

    def leave_map_edit(self, *, refresh: bool = True) -> None:
        """`refresh` False: the story is being left; the next one draws its
        own area."""
        if not self._map_editing:
            return
        self._map_editing = False
        self._edit_area_id = None
        self.area_view.focus = None
        self.area_view.set_placing(None)
        self.area_view.set_removing(False)
        self.area_view.set_editing(False)
        if refresh and hasattr(self, "refresh_area") and self.session is not None:
            self.refresh_area()

    def _edit_outside(self) -> None:
        """Indoors: edit the ground round the building, without walking out."""
        area = self.area_view.area
        session = self.session
        around = session.outside_area(area.id) if area is not None and area.indoor else None
        if around is None or session.area(around) is None:
            self._after_edit("Its outside isn't drawn yet: the map draws it as the story goes.")
            return
        self._edit_area_id = around
        # Looking at the building just left, at its door.
        ground = session.area(around)
        door = next(
            (e for e in ground.entrances if e.place_id == area.place_id),
            None,
        )
        box = ground.buildings.get(area.place_id or "")
        self.area_view.focus = (
            (door.x, door.y)
            if door is not None
            else ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            if box is not None
            else None
        )
        held = session.held_name()
        me = session.area_state().spots.get(held or "")
        if me is not None and me.area != around:
            here = session.area(me.area)
            self.area_view.edit_bar.set_back(here.title if here is not None else "where you are")
        else:
            self.area_view.edit_bar.set_back(None)
            self._edit_area_id = None
        self.refresh_area()

    def _sync_edit_places(self) -> None:
        """The bar's ways about: outside a building being edited."""
        area = self.area_view.area
        session = self.session
        outside = None
        if self._map_editing and area is not None and area.indoor and session is not None:
            around = session.outside_area(area.id)
            ground = session.area(around) if around is not None else None
            outside = ground.title if ground is not None else None
        self.area_view.edit_bar.set_outside(outside)

    def _sync_map_edit(self) -> None:
        """After the play view is shown again: Edit Mode left when it no
        longer can be (a private scene), and opened when a story's grown map
        is first ready to be set up."""
        session = self.session
        if session is None:
            return
        problem = session.map_edit_problem()
        self.area_view.edit_button.setEnabled(problem is None)
        self._sync_edit_places()
        if self._map_editing and problem is not None:
            self.leave_map_edit()
            return
        if (
            not self._map_editing
            and not self._busy
            and session.map_setup_due()
            and self._area_ready()
        ):
            self.enter_map_edit(at_start=True)

    def _edit_blocked(self) -> bool:
        session = self.session
        if session is None:
            return True
        if self._busy:
            self.statusBar().showMessage("The story is being written: the map waits for it.", 5000)
            self.refresh_area()
            return True
        problem = session.map_edit_problem()
        if problem is not None:
            self.statusBar().showMessage(problem, 6000)
            return True
        return False

    def _after_edit(self, words: str = "") -> None:
        session = self.session
        if words:
            self.statusBar().showMessage(words, 6000)
        self._sync_edit_undo()
        if session is not None:
            self.area_view.set_undo(session.correction_undo)
            self.world_view.set_undo(session.correction_undo)
        self.refresh_world()
        self.refresh_area()
        self._refresh_scene_panel()

    def _sync_edit_undo(self) -> None:
        session = self.session
        self.area_view.edit_bar.set_undo(session.edit_undo if session is not None else None)

    # --- the tools ------------------------------------------------------------------

    def _edit_paint(self, tiles: dict) -> None:
        area = self.area_view.area
        if area is None or self._edit_blocked():
            return
        self.session.edit_tiles(area.id, tiles)
        self._after_edit()

    def _edit_box(self, tool: str, x0: int, y0: int, x1: int, y1: int) -> None:
        area = self.area_view.area
        if area is None or self._edit_blocked():
            return
        if abs(x1 - x0) + 1 < SMALLEST_BOX or abs(y1 - y0) + 1 < SMALLEST_BOX:
            self._after_edit(f"Draw it at least {SMALLEST_BOX} tiles each way.")
            return
        if tool in ("room", "fence"):
            tiles = walled_box(area, x0, y0, x1, y1, "#" if tool == "room" else "%")
            what = "Room drawn" if tool == "room" else "Fence drawn"
            done = self.session.edit_tiles(area.id, tiles, what)
            self._after_edit(what if done else f"{what}: it can't be drawn there.")
            return
        if tool != "building" or area.indoor:
            return
        name, what = self._ask_building()
        if not name:
            self.refresh_area()
            return
        place_id = self.session.add_building(area.id, x0, y0, x1, y1, name, what)
        if place_id is None:
            self._after_edit(f"{name} can't be put there.")
            return
        self._after_edit(
            f"{name} is on the map, with walls and a door; its inside is drawn as any "
            "building's is (Go inside to edit it once it is)."
        )
        self._sync_world()  # its inside drawn by the map's art

    def _ask_building(self) -> tuple[str, str]:
        """A new building's name, and what it is like (optional)."""
        dialog = QDialog(self)
        dialog.setWindowTitle("A new building")
        name = QLineEdit()
        name.setPlaceholderText("The smithy")
        what = QLineEdit()
        what.setPlaceholderText("Optional: a low stone forge with an anvil by the door")
        form = QFormLayout()
        form.addRow("Name", name)
        form.addRow("What it is", what)
        note = QLabel(
            "It goes on the map where you drew it: the storyteller is told of it, and its "
            "inside is drawn by the map's art model."
        )
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)
        try:
            if dialog.exec() != QDialog.Accepted:
                return "", ""
            return " ".join(name.text().split()), " ".join(what.text().split())
        finally:
            dialog.deleteLater()

    def _edit_building(self, tool: str, x: int, y: int) -> None:
        area = self.area_view.area
        session = self.session
        if area is None or self._edit_blocked():
            return
        place_id, tiles = building_at(area, x, y)
        if tool == "remove":
            if not tiles:
                self._after_edit("No building there: click its walls or its roof.")
                return
            place = session.world().places.get(place_id or "")
            name = place.name if place is not None else "this building"
            answer = QMessageBox.question(
                self,
                "Take it off the map?",
                f"Take {name} off the map? "
                + (
                    "Whoever and whatever is in it go to the place around it. "
                    if place is not None
                    else ""
                )
                + "Undo puts it back.",
            )
            if answer != QMessageBox.Yes:
                return
            removed = session.remove_building(area.id, x, y)
            self._after_edit(
                f"{removed[0].upper()}{removed[1:]} taken off the map."
                if removed
                else "It can't be taken off."
            )
            return
        if tool == "inside":
            if place_id is None:
                self._after_edit("Only a building the map knows has an inside of its own.")
                return
            if session.area(place_id) is None:
                self._after_edit(
                    "Its inside isn't drawn yet: the map draws it, and Go inside works once it is."
                )
                self._sync_world()
                return
            held = session.held_name()
            me = session.area_state().spots.get(held or "")
            if me is not None and me.area == place_id:
                # Inside the building the author's character stands in: home.
                self._edit_area_id = None
                self.area_view.edit_bar.set_back(None)
            else:
                self._edit_area_id = place_id
                here = session.area(me.area) if me is not None else None
                self.area_view.edit_bar.set_back(
                    here.title if here is not None else "where you are"
                )
            self.refresh_area()

    def _edit_back(self) -> None:
        self._edit_area_id = None
        self.area_view.focus = None
        self.area_view.edit_bar.set_back(None)
        self.refresh_area()

    def _edit_undo(self) -> None:
        if self._edit_blocked():
            return
        what = self.session.edit_undo
        if self.session.undo_edit():
            self._after_edit(f"Undone: {what}")
