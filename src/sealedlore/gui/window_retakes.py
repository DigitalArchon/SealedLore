"""The main window's side of the ways to steer a passage with a few words.
A mixin for `MainWindow`; the dialog is `gui/note_dialog.py`.

Regenerate with a note… (⋯ on a passage): another take, as Regenerate makes
one, with the author's note for that take alone.

Change this passage… (⋯ on a passage): the passage written again with a
change the author asks for, as a new take in its place
(`StorySession.revise_passage`).
"""

from __future__ import annotations

from sealedlore.gui.note_dialog import (
    CHANGE_ASKS,
    CHANGE_BUTTON,
    CHANGE_HINTS,
    CHANGE_PLACEHOLDER,
    CHANGE_TITLES,
    CHAT_RETAKE_HINT,
    RETAKE_ASKS,
    RETAKE_BUTTON,
    RETAKE_HINT,
    RETAKE_PLACEHOLDER,
    RETAKE_TITLE,
    NoteDialog,
)
from sealedlore.tree import path_to


class RetakesWindow:
    def _ask_note(self, **words: str) -> str | None:
        """The author's words from the note dialog, or None when they
        cancelled or wrote nothing."""
        dialog = NoteDialog(self._dialog_parent(), **words)
        try:
            if dialog.exec() != NoteDialog.Accepted:
                return None
            return dialog.note() or None
        finally:
            dialog.deleteLater()  # read, then freed

    def _regenerate_with_note(self, node_id: str) -> None:
        """⋯ → Regenerate with a note…: asked for the note once the take is
        known to be possible, so nothing typed is refused afterwards."""
        session = self.session
        if session is None or self._busy:
            return
        try:
            path = path_to(session.nodes, node_id)
        except KeyError:
            return
        if not self._tee_allows_send():
            return
        if (refusal := self._retake_refusal(path)) is not None:
            self.statusBar().showMessage(refusal, 8000)
            return
        note = self._ask_note(
            title=RETAKE_TITLE,
            asks=RETAKE_ASKS,
            button=RETAKE_BUTTON,
            hint=CHAT_RETAKE_HINT if self.in_chat else RETAKE_HINT,
            placeholder="" if self.in_chat else RETAKE_PLACEHOLDER,
        )
        if note is None or self.session is not session or self._busy:
            return
        self._regenerate_node(node_id, note=note)

    def _change_refusal(self, node_id: str) -> str:
        """Why the passage can't be changed by instruction, in words."""
        session = self.session
        what = "reply" if self.in_chat else "passage"
        if session.in_private and session.private_provider is None:
            return self._say(
                "This private scene has no model set up to write with: choose one under "
                "Settings → Private."
            )
        try:
            path = path_to(session.nodes, node_id)
        except (KeyError, ValueError):
            return f"That {what} is no longer in the story."
        refusal = self._retake_refusal(path)
        if refusal is not None:
            return refusal
        if not path[-1].content.strip():
            return f"This {what} has no words to change: Regenerate writes it again."
        return self._say("Leave the private scene to change a passage from before it.")

    def _change_passage(self, node_id: str) -> None:
        """⋯ → Change this passage…: asked what should change once the
        change is known to be possible; the result is a new take, in the
        passage's place when the story went on from it."""
        session = self.session
        if session is None or self._busy:
            return
        if not self._tee_allows_send():
            return
        if not session.can_revise(node_id):
            self.statusBar().showMessage(self._change_refusal(node_id), 8000)
            return
        chat = self.in_chat
        change = self._ask_note(
            title=CHANGE_TITLES[chat],
            asks=CHANGE_ASKS,
            button=CHANGE_BUTTON,
            hint=CHANGE_HINTS[chat],
            placeholder="" if chat else CHANGE_PLACEHOLDER,
        )
        if change is None or self.session is not session or self._busy:
            return
        if not session.can_revise(node_id):
            return
        path = session.full_path()
        latest = bool(path) and path[-1].id == node_id
        before = path_to(session.nodes, node_id)[:-1]
        # An older passage's new take streams in under its turn, so the
        # story is shown as it stood there: an edit open on a later message
        # is asked about first.
        if not latest and not self._edit_allows_leave(keeping={node.id for node in before}):
            return
        session.provider = self._current_provider()
        if not latest:
            self._show_nodes(before)
            self._focus_after_job = before[-1].id if before else None

        def after() -> None:
            result = session.last_result if self.session is session else None
            if self._failed is not None or result is None or result.node is not None:
                return
            if result.stopped_early:
                # A change stopped partway is no passage: nothing is kept.
                self.statusBar().showMessage(
                    "Stopped before the change was written: the "
                    f"{'reply' if chat else 'passage'} is as it was.",
                    8000,
                )

        self._after_job = after
        self.statusBar().showMessage("Writing the change as a new take", 6000)
        self._start(lambda: session.revise_passage(node_id, change), job="The change")
