"""The main window's side of what is changed on the Cast, Scene, Lore and Style
pages: in the story at once, saved a moment after the last key typed. A mixin
for `MainWindow`.

Every key typed in one of their fields used to save the whole story and
assemble the prompt again, on the GUI thread: about 60 ms a key on a story
of 500 messages (the save half of it, the prompt the rest), so typing into a
card stuttered. Now a key (a page's `edited`) changes the story in memory at
once, so whatever reads it next (a turn sent, a backup) has it, and the save
and the pages that follow it wait until `SAVE_DELAY_MS` after the last key,
as on Your characters and Lorebooks. A choice made on a page (its `changed`:
a card added, a box ticked) is kept at once, with whatever was typed before.

What relies on the story being saved, or on the window having caught up,
keeps what waits first (`_flush_edits`): a job starting (a turn, Regenerate),
another story opened or this one closed, quitting, a private scene beginning,
the Prompt page shown, the story's folder or a copy of it read. A thread
working on the story when the moment comes (gui/workers.py) has it kept once
it is done with it (`_story_returned`).
"""

from __future__ import annotations

from PySide6.QtCore import QTimer

from sealedlore.gui.window_collection import SAVE_DELAY_MS

# The pages, as `_story_edited` is told which changed.
CAST, SCENE, LORE, STYLE = "cast", "scene", "lore", "style"


class EditsWindow:
    def _init_edits(self) -> None:
        """Called from `_build_ui` once the four pages are made."""
        # The session typed into, and its pages whose changes wait.
        self._edits_session = None
        self._edits_waiting: set[str] = set()
        self._edits_save = QTimer(self)
        self._edits_save.setSingleShot(True)
        self._edits_save.setInterval(SAVE_DELAY_MS)
        self._edits_save.timeout.connect(self._edits_due)
        self.cast_panel.edited.connect(lambda: self._story_edited(CAST, typed=True))
        self.scene_panel.edited.connect(lambda: self._story_edited(SCENE, typed=True))
        self.lore_panel.edited.connect(lambda: self._story_edited(LORE, typed=True))
        self.style_panel.edited.connect(lambda: self._story_edited(STYLE, typed=True))

    def _story_edited(self, kind: str, *, typed: bool = False) -> None:
        """A page (`kind`) changed the story, which has the change already:
        kept now, or (`typed`) a moment after the last key."""
        session = self.session
        if session is None:
            return
        if self._edits_session is not None and self._edits_session is not session:
            self._flush_edits()  # another story's, kept before this one's
        if kind == SCENE:
            # Stamped on the passage the story has reached, now: the author's
            # card is the scene from here, and the next read (or a branch
            # gone to and back) starts from it.
            session.set_scene(session.story.scene, save=False)
            self.scene_panel.show_changes(session.story.scene, passage=0, can_undo=False)
            self.scene_panel.show_age(
                session.scene_age(), kept=self.config.scene_reads == "every_turn"
            )
        else:
            # A review's Undo puts the settings back whole: it would take this
            # change with it.
            session.forget_review_undo()
        self._edits_session = session
        self._edits_waiting.add(kind)
        if typed:
            self._edits_save.start()
        else:
            self._flush_edits()

    def _edits_due(self) -> None:
        """A moment after the last key. While a thread works on the story,
        once it is done with it: nothing changes it under the thread."""
        session = self._edits_session
        if session is not None and self._story_in_use(session):
            self._wait_for_story(session, "edits")
            return
        self._flush_edits()

    def _flush_edits(self) -> None:
        """What waits from the pages, kept now, and what follows it brought up
        to date. Nothing waiting, nothing done."""
        self._edits_save.stop()
        session, self._edits_session = self._edits_session, None
        kinds, self._edits_waiting = self._edits_waiting, set()
        if session is None or not kinds:
            return
        session.save()
        if session is not self.session:
            return  # a story left: kept, and nothing of it is shown
        if CAST in kinds:
            # Presence and the selectors are keyed on the cast, so they follow
            # it (and the People page with the scene).
            self._refresh_scene_panel()
        if kinds & {CAST, SCENE}:
            self._refresh_composer()
        if LORE in kinds:
            self.lore_panel.set_held_back(session.held_back_ids())
        if STYLE in kinds:
            self.composer.set_default_length(session.story.style.response_style)
        self._reassemble_inspector()
        if CAST in kinds:
            self._cast_changed_soon()
        if LORE in kinds:
            # Vectors are keyed by content hash, so an edited entry re-embeds
            # itself on the next turn; what lore from the full entry is ranked
            # by is made ahead, a moment after the last change.
            self._lore_vectors_later()
