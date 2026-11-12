"""The main window's side of the ways to steer a passage with a few words.
A mixin for `MainWindow`; the dialog is `gui/note_dialog.py`.

Regenerate with a note… (⋯ on a passage): another take, as Regenerate makes
one, with the author's note for that take alone.
"""

from __future__ import annotations

from sealedlore.gui.note_dialog import (
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
