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

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QToolButton

from sealedlore.gui.map_render import plan_png
from sealedlore.gui.world_view import WorldView

# Pieces of art come back one after another: one redraw for a run of them.
ART_REDRAW_MS = 400
# How often the progress is read while the map is being drawn.
PROGRESS_MS = 700


class _ArtSignal(QObject):
    """Emitted on the art thread; delivered on the GUI thread (queued)."""

    drawn = Signal()


class WorldWindow:
    def _build_world(self) -> None:
        """Called from `__init__` before the centre is laid out."""
        self.world_view = WorldView()
        self.world_view.back_requested.connect(lambda: self.show_world(False))
        self.world_view.isometric_requested.connect(self._paint_isometric)
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
            self.right_tabs.setCurrentWidget(self.scene_panel)
        self._sync_world_button()

    def _on_page_changed(self, _index: int) -> None:
        self._sync_world_button()
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
        held = session._character(session.story.held_character_id)
        self.world_view.picture_reader = session.pictures.read
        self.world_view.art_on = self.config.map_art
        self.world_view.drawing = session.map_art_running
        self.world_view.isometric_offer = session.isometric_offer()
        self.world_view.isometric_painting = session.isometric_painting
        self.world_view.set_progress(*session.map_art_progress())
        self.world_view.set_world(
            session.world(),
            session.map_art_copy(),
            held.name if held is not None else None,
            keep_view=keep_view,
        )

    def _sync_world(self) -> None:
        """The window laid out for a map or not, and the map's art under way.
        After a story opens or closes, a job finishes, or Setup is saved."""
        session = self.session
        on = session is not None and session.map_on
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
        if self.world_shown():
            self._art_timer.start(ART_REDRAW_MS)

    def _on_art_timer(self) -> None:
        self.refresh_world(keep_view=True)

    def _sync_progress(self) -> None:
        """The map's progress from the session; read again while it draws,
        until it is done (`map_art_progress`)."""
        session = self.session
        if session is None or not self.map_layout:
            self._progress_timer.stop()
            return
        done, total = session.map_art_progress()
        self.world_view.art_on = self.config.map_art
        self.world_view.set_progress(done, total)
        painting = session.isometric_painting
        if painting != self.world_view.isometric_painting:
            self.world_view.isometric_painting = painting
            self.world_view.isometric_offer = session.isometric_offer()
            self.world_view.sync_isometric()
        if total and not self._progress_timer.isActive():
            self._progress_timer.start()
        elif not total:
            self._progress_timer.stop()

    def _paint_isometric(self, area_id: str) -> None:
        """Paint the isometric view the author asked for: the plan is drawn
        for the model from the map as it stands now, on the painting's own
        thread (`plan_png` uses QImage alone)."""
        session = self.session
        if session is None:
            return
        world, art = session.world(), session.map_art_copy()
        if session.start_isometric(area_id, lambda: plan_png(world, art, area_id)):
            self.world_view.isometric_painting = session.isometric_painting
        self.world_view.isometric_offer = session.isometric_offer()
        self.world_view.sync_isometric()
        self._sync_progress()
