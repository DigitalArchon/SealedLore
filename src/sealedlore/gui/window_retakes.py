"""The main window's side of three things asked of the storyteller in a few
words: a take steered by a note, a passage changed by instruction, and ideas
for the author's own next turn. A mixin for `MainWindow`; the dialog is
`gui/note_dialog.py`, the menu `gui/suggest_menu.py`.

Regenerate with a note… (⋯ on a passage): another take, as Regenerate makes
one, with the author's note for that take alone.

Change this passage… (⋯ on a passage): the passage written again with a
change the author asks for, as a new take in its place
(`StorySession.revise_passage`).

Suggest… (the composer's More row): a few things the author's character
might do next, asked of the storyteller outside the story
(`StorySession.suggest_turns`) and shown in a menu at the button. Choosing
one puts its words in the box to change and send; nothing is sent from here.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QMessageBox

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
from sealedlore.gui.suggest_menu import SuggestMenu
from sealedlore.tree import path_to

SUGGESTING = {
    False: "Asking the storyteller what your character might do next…",
    True: "Asking the model what you might write next…",
}


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

    # --- Suggest… ----------------------------------------------------------------

    def suggest_turn(self) -> None:
        """Suggest…: ask for a few options for the author's next turn, then
        show them at the button. A job like any other, so Stop ends it."""
        session = self.session
        if session is None or self._busy:
            return
        if not self._tee_allows_send():
            return
        if not session.can_suggest():
            self.statusBar().showMessage(
                "Suggestions are for the character you play: choose one under Playing.", 8000
            )
            return
        session.provider = self._current_provider()
        chat = self.in_chat
        made: dict[str, list[str]] = {}

        def job():
            made["options"] = yield from session.suggest_turns()

        def after() -> None:
            options = made.get("options")
            if options and self.session is session:
                self._show_suggestions(options)

        self._after_job = after
        self._start(job, streaming=False, busy_text=SUGGESTING[chat], job="Suggesting a turn")

    def _show_suggestions(self, options: list[str]) -> None:
        """The options in a menu at the Suggest… button (at the box, if the
        row was put away meanwhile)."""
        composer = self.composer
        button = composer.suggest_button
        anchor = button if button.isVisible() else composer.input
        menu = SuggestMenu(options, anchor, chat=self.in_chat)
        menu.chosen.connect(self._take_suggestion)
        # Deleted once it has gone: left hidden, a menu closed under the
        # pointer kept the pointer's events (the model picker, on Wayland).
        menu.aboutToHide.connect(menu.deleteLater)
        menu.aboutToHide.connect(self._suggestions_gone)
        self._suggest_menu = menu
        # Above the composer, where there is room; Qt keeps it on the screen.
        tall = menu.sizeHint().height()
        menu.popup(anchor.mapToGlobal(QPoint(0, 0)) - QPoint(0, tall))

    def _suggestions_gone(self) -> None:
        self._suggest_menu = None

    def _take_suggestion(self, text: str) -> None:
        """The option chosen goes into the box, to change and send: asked
        first when that would replace something written there."""
        if self.session is None:
            return
        composer = self.composer
        if not composer.input.isEnabled():
            self.statusBar().showMessage(
                "The box is busy with the current job: ask for suggestions again after it.",
                8000,
            )
            return
        if composer.input.toPlainText().strip():
            answer = QMessageBox.question(
                self._dialog_parent(),
                "Replace what you wrote?",
                "The box already holds something you wrote. Replace it with this suggestion?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        composer.take_suggestion(text)
        self.statusBar().showMessage(
            "The suggestion is in the box: change it as you like, then send it.", 8000
        )
