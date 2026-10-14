"""The main window's side of the map (gui/world_view.py). A mixin for
`MainWindow`.

Map, beside Find above the story, shows the world the story has been
through, in the transcript's place, for a story that keeps a map (Story →
Setup). Branches, on the branch bar, is the other diagram that takes that
place: one shows at a time. The map's art is drawn on a background thread
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
        self.world_button.setToolTip(
            "The map of the story: the places it has been, drawn as it grows (Esc to go back)"
        )
        self.world_button.toggled.connect(self.show_world)
        self.world_button.hide()
        self._art_signal = _ArtSignal()
        self._art_signal.drawn.connect(self._art_drawn)
        self._art_timer = QTimer(self)
        self._art_timer.setSingleShot(True)
        self._art_timer.setInterval(ART_REDRAW_MS)
        self._art_timer.timeout.connect(lambda: self.refresh_world(keep_view=True))

    def world_shown(self) -> bool:
        return self.centre_stack.currentWidget() is self.world_view

    def show_world(self, on: bool) -> None:
        """The map, in the transcript's place."""
        session = self.session
        on = bool(on and session is not None and session.map_on and not session.in_private)
        if on and self.map_shown():
            self.show_map(False)
        if on:
            self.centre_stack.setCurrentWidget(self.world_view)
        elif self.world_shown():
            self.centre_stack.setCurrentWidget(self.transcript)
        self.composer.setVisible(not (on or self.map_shown()))
        self.view_toggle.setEnabled(not (on or self.map_shown()))
        if self.world_button.isChecked() != on:
            self.world_button.blockSignals(True)
            self.world_button.setChecked(on)
            self.world_button.blockSignals(False)
        if on:
            self.refresh_world(keep_view=False)
            self.world_view.view.setFocus()

    def refresh_world(self, *, keep_view: bool = True) -> None:
        """Redraw the map from the session, if it is showing."""
        session = self.session
        if session is None or not self.world_shown():
            return
        held = session._character(session.story.held_character_id)
        self.world_view.set_world(
            session.world(),
            session.map_art_copy(),
            held.name if held is not None else None,
            keep_view=keep_view,
        )

    def _sync_world(self) -> None:
        """The Map button for a story that keeps a map, and its art under way.
        After a story opens, a job finishes, or Setup is saved."""
        session = self.session
        on = session is not None and session.map_on
        self.world_button.setVisible(on)
        self.world_button.setEnabled(on and not self._busy and not session.in_private)
        if not on:
            if self.world_shown():
                self.show_world(False)
            return
        session.on_map_art = self._art_signal.drawn.emit
        if not self._busy and not session.in_private:
            # Whatever the map still lacks (a story opened, the setting just
            # turned on, art that failed before) is drawn now, in the background.
            session.start_map_art()
        if self.world_shown():
            self.refresh_world(keep_view=True)

    def _art_drawn(self) -> None:
        if self.world_shown():
            self._art_timer.start()
