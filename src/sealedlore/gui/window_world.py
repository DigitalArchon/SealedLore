"""The main window's side of the map (gui/world_view.py). A mixin for
`MainWindow`.

A story that keeps a map (Story → Setup) makes it the window's: Map is the
Inspector's first page and the page the story opens on, the Inspector takes
half the window (window_layout.py), and the Stories list steps aside (View →
Stories list brings it back). The author: when the map is in use it should
be a core feature, not hidden away. Map, beside Find above the story, goes to
it. The map's art is drawn on a background thread
(engine/session_world_art.py); each piece that comes back is passed to the
GUI thread by a queued signal and redrawn there, a moment after the last.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QToolButton

from sealedlore.engine.picture_shapes import ISOMETRIC
from sealedlore.engine.session_area import world_key
from sealedlore.engine.world_pictures import shown_view, view_current, views_of
from sealedlore.gui.crop_dialog import cropped_picture
from sealedlore.gui.isometric_view import CHOICE_THUMB, ChooseViewDialog, ViewChoice
from sealedlore.gui.map_iso import iso_png
from sealedlore.gui.map_pictures_dialog import MapPicturesDialog
from sealedlore.gui.map_tiles import texture_colours
from sealedlore.gui.ref_images import PICTURE_FILTER, thumbnail_of
from sealedlore.gui.world_view import WorldView

# Pieces of art come back one after another: one redraw for a run of them.
ART_REDRAW_MS = 400
# How often the progress is read while the map is being drawn.
PROGRESS_MS = 700


class _ArtSignal(QObject):
    """Emitted on the art thread; delivered on the GUI thread (queued)."""

    drawn = Signal()


class WorldWindow:
    _world_drawn: tuple | None = None  # what the Region map was last built from
    # The art copy the key names by id: held, so the id can't be reused by
    # another copy at the same address while it is the one drawn.
    _drawn_art: object = None

    def _build_world(self) -> None:
        """Called from `__init__` before the centre is laid out."""
        self.world_view = WorldView()
        self.world_view.back_requested.connect(lambda: self.show_world(False))
        self.world_view.isometric_requested.connect(self._paint_isometric)
        self.world_view.own_isometric_requested.connect(self._own_isometric)
        self.world_view.choose_view_requested.connect(self._choose_isometric)
        self.world_view.redraw_requested.connect(self._redraw_plan)
        self.world_view.retry_requested.connect(self._retry_map_art)
        self.world_view.review_pictures_requested.connect(self._review_map_pictures)
        self.world_view.draw_pictures_requested.connect(self._draw_map_pictures)
        self.world_view.decline_pictures_requested.connect(self._decline_map_pictures)
        self.world_button = QToolButton()
        self.world_button.setObjectName("viewToggleButton")
        self.world_button.setText("Map")
        self.world_button.setCheckable(True)
        self.world_button.setToolTip("The map of the story, beside it: the places it has been")
        self.world_button.toggled.connect(self.show_world)
        self.world_button.hide()
        self._art_signal = _ArtSignal()
        self._art_signal.drawn.connect(self._art_drawn)
        self._art_timer = QTimer(self)
        self._art_timer.setSingleShot(True)
        self._art_timer.setInterval(ART_REDRAW_MS)
        self._art_timer.timeout.connect(self._on_art_timer)
        # The drawing's progress, read without redrawing the map.
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(PROGRESS_MS)
        self._progress_timer.timeout.connect(self._sync_progress)
        # Whether the window is laid out for a map now (`_sync_world`).
        self.map_layout = False

    def _init_world_dock(self) -> None:
        """Called once both docks are in place, before the menus are made
        (View takes `stories_list_action`)."""
        self.right_tabs.currentChanged.connect(self._on_page_changed)
        self.stories_list_action = self.stories_dock.toggleViewAction()
        self.stories_list_action.setText("&Stories list")
        self.stories_list_action.setStatusTip("Show or hide the list of stories")

    def world_shown(self) -> bool:
        return (
            self.map_layout
            and not self.inspector_dock.isHidden()
            and not self.right_tabs.is_empty_shown()
            and self.right_tabs.currentWidget() is self.world_view
        )

    def show_world(self, on: bool) -> None:
        """The map's page in the Inspector, or the page beside it."""
        on = bool(on and self.map_layout)
        if on:
            self.inspector_dock.show()
            self.right_tabs.setCurrentWidget(self.world_view)
            self.world_view.view.setFocus()
        elif self.right_tabs.currentWidget() is self.world_view:
            # A chat has no People page: its chapters.
            chat = self.session is not None and self.session.story.chat
            self.right_tabs.setCurrentWidget(self.summaries_panel if chat else self.people_page)
        self._sync_world_button()

    def _on_page_changed(self, _index: int) -> None:
        self._sync_world_button()
        self._sync_dock_title()
        if self.right_tabs.currentWidget() is self.people_page and self._people_dirty:
            self._build_people()
        if self.right_tabs.currentWidget() is self.images_panel:
            # The cards' pictures change on their own pages: read afresh.
            self.refresh_images()
        if self.world_shown():
            # Back from another page, the map is as it was left (the author's
            # call): only opening a story centres it on where the story is.
            self.refresh_world(keep_view=True)

    def _sync_world_button(self) -> None:
        shown = self.world_shown()
        if self.world_button.isChecked() != shown:
            self.world_button.blockSignals(True)
            self.world_button.setChecked(shown)
            self.world_button.blockSignals(False)

    def refresh_world(self, *, keep_view: bool = True) -> None:
        """Redraw the map from the session, if it is showing."""
        session = self.session
        if session is None or not self.world_shown():
            return
        held = session.map_held_name(session.story.held_character_id)
        self.world_view.picture_reader = session.pictures.read
        self.world_view.art_on = self.config.map_art
        self.world_view.drawing = session.map_art_running
        self.world_view.isometric_offer = session.isometric_offer()
        self.world_view.isometric_painting = session.isometric_painting
        self.world_view.set_progress(*session.map_art_progress(), now=session.map_art_now())
        # Map-first: the ring and the fog follow where the character stands.
        self.world_view.me_place = session.here_place
        self.world_view.play_area = self._play_area_id
        # The 2D map's roofs, walls and ground in the tile set's own colours,
        # so the plan and the play view show one farmhouse.
        info, read = session.tile_set()
        self.world_view.look_colours = texture_colours(info.name, read) if info.name else None
        # Built again only when something it draws from has changed: opening
        # a story asked for the map four times over (the controls, the
        # layout, the open itself), each a full build of the Region map
        # (Oct 19 2026, 7 s of a 7 s open with 27 designed regions).
        world = session.world()
        art = session._art_read()
        key = (
            session.story.id,
            session.story.active_leaf_id,
            world_key(world),
            session._art_version,
            id(art),
            held,
            session.here_place(),
            session.in_private,
            self.config.map_art,
            session.map_art_running,
            session.isometric_offer(),
            session.isometric_painting,
            info.name,
            self._play_area_id,
        )
        if key == self._world_drawn and self.world_view._built:
            if not keep_view:
                self.world_view.go_local()
        else:
            self.world_view.set_world(world, session.map_art_copy(), held, keep_view=keep_view)
            self._world_drawn = key
            self._drawn_art = art
        # Map-first play: the area to walk on (gui/window_area.py).
        self.refresh_area()

    def _sync_world(self) -> None:
        """The window laid out for a map or not, and the map's art under way.
        After a story opens or closes, a job finishes, or Setup is saved."""
        session = self.session
        on = session is not None and session.map_on
        self.cast_panel.set_map_first(on and session.map_first)
        if on != self.map_layout:
            self._set_map_layout(on)
        # Usable while the model writes: the map only reads the story.
        self.world_button.setVisible(on)
        self.world_button.setEnabled(on)
        if not on:
            return
        # A private scene pauses the map where it stood (the author, Oct 15
        # 2026): shown as the story was when the scene began, nothing drawn or
        # painted, nothing from the scene read into it. The approved summary
        # is read like any passage, and the map catches up from it.
        self.world_view.set_paused(session.in_private)
        session.on_map_art = self._art_signal.drawn.emit
        if not self._busy and not session.in_private:
            # Whatever the map still lacks (a story opened, the setting just
            # turned on, art that failed before) is drawn now, in the background.
            session.start_map_art()
        self._sync_progress()
        if self.world_shown():
            self.refresh_world(keep_view=True)

    def _set_map_layout(self, on: bool) -> None:
        self.map_layout = on
        index = self.right_tabs.indexOf(self.world_view)
        self.right_tabs.setTabVisible(index, on)
        if on:
            self.right_tabs.setCurrentWidget(self.world_view)
        self._sync_world_button()
        self._reshare_docks()
        if on:
            self.refresh_world(keep_view=False)

    def _art_drawn(self) -> None:
        self._sync_progress()
        self.cast_panel.refresh_avatar()  # an avatar drawn again lands here too
        if self.world_shown():
            self._art_timer.start(ART_REDRAW_MS)

    def _on_art_timer(self) -> None:
        self.refresh_world(keep_view=True)

    def _retry_map_art(self) -> None:
        """Try again on the Map page: what the map gave up drawing is asked
        for once more."""
        session = self.session
        if session is None or self._busy:
            return
        session.retry_map_art()
        self._sync_progress()
        self._progress_timer.start()

    def _sync_progress(self) -> None:
        """The map's progress from the session; read again while it draws,
        until it is done (`map_art_progress`)."""
        session = self.session
        if session is None or not self.map_layout:
            self._progress_timer.stop()
            return
        done, total = session.map_art_progress()
        self.world_view.art_on = self.config.map_art
        reading = session.map_reading
        activity = f"Reading the passage for the map with {reading}…" if reading else ""
        failed = session.map_art_failed() if not total and not activity else []
        self.world_view.set_progress(done, total, activity, failed, session.map_art_now())
        self._sync_waiting_pictures(drawing=bool(total))
        # A painting waiting for the place's pieces starts once they are in.
        self._paint_when_drawn()
        painting = session.isometric_painting
        if painting != self.world_view.isometric_painting:
            self.world_view.isometric_painting = painting
            self.world_view.isometric_offer = session.isometric_offer()
            self.world_view.sync_isometric()
        # Ticking too while a painting waits for the pieces, or it never starts.
        waiting = getattr(self, "_paint_waiting", None) is not None
        if (total or activity or waiting) and not self._progress_timer.isActive():
            self._progress_timer.start()
        elif not total and not activity and not waiting:
            self._progress_timer.stop()

    # --- map pictures that wait for the author (Ask before drawing, or too many) ----

    def _sync_waiting_pictures(self, *, drawing: bool) -> None:
        """The Map page's line for the map pictures that wait for approval;
        and once the map is idle, a run asked for while another drew (a
        portrait landing mid-run) or pictures approved meanwhile."""
        session = self.session
        waiting = session.map_pictures_waiting() if session is not None else []
        prices = [w.price for w in waiting]
        known = [p for p in prices if p is not None]
        self.world_view.set_waiting(
            len(waiting),
            sum(known) if known else None,
            unpriced=None in prices,
            over=session.map_pictures_ask_over if session is not None else 0,
        )
        if (
            session is not None
            and not drawing
            and not self._busy
            and not session.in_private
            and (session.map_pictures_approved_pending or session.map_art_wanted_again)
        ):
            session.start_map_art()

    def _review_map_pictures(self) -> None:
        """Every map picture waiting, with its prompt and price: the ticked
        ones are drawn as they stand, the rest left out for this session."""
        session = self.session
        if session is None:
            return
        waiting = session.map_pictures_waiting()
        if not waiting:
            self._sync_progress()
            return
        dialog = MapPicturesDialog(waiting, session.pictures.read, self)
        try:
            result = dialog.exec()
            approved, left_out = dialog.approved(), dialog.left_out()
        finally:
            dialog.deleteLater()
        if self.session is not session:
            return  # another story opened meanwhile
        if result == QDialog.Accepted:
            session.decline_map_pictures(left_out)
            self._approve_map_pictures(approved)
        elif result == MapPicturesDialog.DECLINED:
            session.decline_map_pictures(w.job.key for w in waiting)
        self._sync_progress()

    def _draw_map_pictures(self) -> None:
        """Draw them, all as they stand."""
        session = self.session
        if session is None:
            return
        self._approve_map_pictures([w.job for w in session.map_pictures_waiting()])
        self._sync_progress()

    def _approve_map_pictures(self, jobs) -> None:
        session = self.session
        if session is None or not jobs:
            return
        # While a job runs the worker owns the story: kept approved, and the
        # next run takes them (after the passage's reads, or once the worker
        # is done: `_sync_waiting_pictures`).
        start = not self._busy and not session.in_private
        session.approve_map_pictures(jobs, start=start)
        self._progress_timer.start()

    def _decline_map_pictures(self) -> None:
        session = self.session
        if session is None:
            return
        session.decline_map_pictures(w.job.key for w in session.map_pictures_waiting())
        self._sync_progress()

    def _paint_isometric(self, area_id: str) -> None:
        """Paint the place as the author asked on the 2D map: the program's
        isometric render of the plan is drawn for the model from the map as
        it stands now, at the size the model paints, on the painting's own
        thread (`iso_png` uses QImage alone)."""
        session = self.session
        if session is None:
            return
        world, art = session.world(), session.map_art_copy()
        if session.start_isometric(area_id, lambda size: iso_png(world, art, area_id, size)):
            self.world_view.isometric_painting = session.isometric_painting
        self.world_view.isometric_offer = session.isometric_offer()
        self.world_view.sync_isometric()
        self._sync_progress()

    def _own_isometric(self, area_id: str) -> None:
        """A picture of the author's own as the painting: chosen,
        cropped (first to the painting's shape), stripped and kept."""
        session = self.session
        if session is None:
            return
        if self._busy:
            self.statusBar().showMessage(
                "A view can be brought in once the story's current job is done.", 6000
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "A painting of your own", str(Path.home()), PICTURE_FILTER
        )
        if not path:
            return
        try:
            picture = cropped_picture(self, Path(path), ISOMETRIC)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Couldn't use that picture", str(exc))
            return
        if picture is None:
            return
        try:
            kept = session.use_own_isometric(area_id, *picture)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Couldn't keep that picture", str(exc))
            return
        if not kept:
            self.statusBar().showMessage("The map can't take a picture now.", 6000)
            return
        self.refresh_world()
        self.world_view.sync_isometric()

    def _choose_isometric(self, area_id: str) -> None:
        """Choose a view… on the isometric bar: the area's views, newest
        first, the one chosen laid over the map (`choose_isometric`)."""
        session = self.session
        if session is None:
            return
        world, art = session.world(), session.map_art_copy()
        area = world.places.get(area_id)
        if area is None:
            return
        shown = shown_view(world, art, area_id)
        choices = [
            ViewChoice(
                picture.id,
                thumbnail_of(session.pictures.read(picture.file), CHOICE_THUMB[0]),
                painted_by=picture.model,
                made_at=picture.made_at,
                current=view_current(world, art, area_id, picture),
                shown=shown is not None and picture.id == shown.id,
            )
            for picture in reversed(views_of(art, area_id))
        ]
        if len(choices) < 2:
            return
        dialog = ChooseViewDialog(area.name, choices, self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        chosen = dialog.chosen if dialog.exec() == QDialog.Accepted else None
        if chosen is not None:
            self.show_isometric_view(area_id, chosen)

    def show_isometric_view(self, area_id: str, picture_id: str) -> None:
        """This one of an area's views over the map (from the chooser, or
        Media's Show this view on the map), the map and Media redrawn."""
        session = self.session
        if session is None or not session.choose_isometric(area_id, picture_id):
            self.statusBar().showMessage("That view can't be shown on the map now.", 6000)
            return
        self.refresh_world()
        self.world_view.sync_isometric()
        self.refresh_images()

    def _redraw_plan(self, area_id: str) -> None:
        """Redraw plan… on the local map: asked first, since the plan the
        story has stood on is replaced (`redraw_plan`)."""
        session = self.session
        if session is None or area_id not in session.world().places:
            return
        if self._busy:
            # The worker may be starting the map's art after a passage.
            self.statusBar().showMessage(
                "The plan can be redrawn once the story's current job is done.", 6000
            )
            return
        problem = session.redraw_problem(area_id)
        if problem is not None:
            self.statusBar().showMessage(f"The plan can't be redrawn now. {problem}", 8000)
            return
        name = session.world().places[area_id].name
        answer = QMessageBox.question(
            self,
            "Redraw the plan?",
            f"Draw the plan of {name} again? The map art model draws a new one in the "
            "background, and it replaces the plan you see now once it is drawn (if it can't "
            "be drawn, this one stays). The places in it keep their names, the buildings you "
            "have been inside keep their own plans, and its paintings go out of date.",
        )
        if answer != QMessageBox.Yes:
            return
        if not session.redraw_plan(area_id):
            problem = session.redraw_problem(area_id) or ""
            self.statusBar().showMessage(f"The plan can't be redrawn now. {problem}", 6000)
            return
        self._sync_progress()
        self.refresh_world(keep_view=True)
