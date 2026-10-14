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

from sealedlore.gui.world_view import WorldView

# Pieces of art come back one after another: one redraw for a run of them.
ART_REDRAW_MS = 400


class _ArtSignal(QObject):
    """Emitted on the art thread; delivered on the GUI thread (queued)."""

    drawn = Signal()


class WorldWindow:
    def _build_world(self) -> None:
        """Called from `__init__` before the centre is laid out."""
        self.world_view = WorldView()
        self.world_view.back_requested.connect(lambda: self.show_world(False))
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
        self._art_timer.timeout.connect(lambda: self.refresh_world(keep_view=True))
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
            self.refresh_world(keep_view=False)

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
        on = session is not None and session.map_on and not session.in_private
        if on != self.map_layout:
            self._set_map_layout(on)
        self.world_button.setVisible(on)
        self.world_button.setEnabled(on and not self._busy)
        if not on:
            return
        session.on_map_art = self._art_signal.drawn.emit
        if not self._busy:
            # Whatever the map still lacks (a story opened, the setting just
            # turned on, art that failed before) is drawn now, in the background.
            session.start_map_art()
        if self.world_shown():
            self.refresh_world(keep_view=True)

    def _set_map_layout(self, on: bool) -> None:
        self.map_layout = on
        index = self.right_tabs.indexOf(self.world_view)
        self.right_tabs.setTabVisible(index, on)
        # The Stories list steps aside for the map, and comes back after.
        self.stories_dock.setVisible(not on)
        if on:
            self.right_tabs.setCurrentWidget(self.world_view)
        self._sync_world_button()
        self._reshare_docks()
        if on:
            self.refresh_world(keep_view=False)

    def _art_drawn(self) -> None:
        if self.world_shown():
            self._art_timer.start()
