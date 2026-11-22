"""The window's side of the own-model trial (engine/reactions.py): what
characters played by a model of their own answered a passage, seen and edited
in the story.

The answers to the latest passage are the round still open: hidden until the
author sends, so the author answers the passage as the characters did,
without seeing theirs, unless they ask to see them (View → Show the
characters' answers: the author's "visible as a spoiler"). Editing an answer
is the author's ruling too: change what a character means to do, and redo
the turn with it. An answer to the latest passage is simply kept for the
author's next turn; one to an older passage writes the passage that followed
it again, which is how the turn is redone.
"""

from __future__ import annotations

from PySide6.QtGui import QAction

from sealedlore.engine.reactions import independent_cast
from sealedlore.gui.character_prompt_dialog import show_character_prompt
from sealedlore.gui.note_dialog import NoteDialog

SHOW_ANSWERS = "Show the characters' ans&wers"
SHOW_ANSWERS_TIP = (
    "Show what characters played by their own model answered the latest passage before you "
    "send your turn. Hidden, you answer it as they did, without seeing theirs; the storyteller "
    "gets all of them with your turn either way"
)
EDIT_TITLE = "Edit what {name} means to do"
EDIT_ASKS = "What does {name} mean to do? Their own model wrote this; it's yours to change."
EDIT_KEEP = "Save"
EDIT_REDO = "Save and write the next passage again"
EDIT_HINT_OPEN = (
    "The storyteller is given this with your next turn, as what they mean to do, and decides "
    "what comes of it."
)
EDIT_HINT_TURN = (
    "They answered your turn, which has no passage yet: the storyteller is given this with "
    "it when the passage is written (Regenerate)."
)
EDIT_HINT_REDO = (
    "The passage after it was written from their answer as it was. Saving writes that passage "
    "again from this one, as a new take; the one there now is kept (‹ ›)."
)


EDIT_HINT_SPENT = (
    "The passage after it wasn't written from this answer: the story has them answer after "
    "your turn now, and the turn that followed was answered that way. Saving keeps your "
    "words here; nothing is written again."
)


class AnswersWindow:
    def _answers_action(self, view_menu) -> None:
        """View → Show the characters' answers: off whenever the window opens
        (a spoiler is shown when asked, not by default)."""
        self.answers_action = QAction(SHOW_ANSWERS, self)
        self.answers_action.setCheckable(True)
        self.answers_action.setStatusTip(SHOW_ANSWERS_TIP)
        self.answers_action.toggled.connect(self._show_answers)
        self.answers_action.setVisible(False)
        view_menu.addAction(self.answers_action)

    def _sync_answers_action(self) -> None:
        """Offered in a story with a character played by their own model,
        answering at the same time as the author (after it, there is nothing
        to keep hidden)."""
        session = self.session
        offered = (
            session is not None
            and not session.story.chat
            and session.story.defaults.round_order == "together"
            and bool(independent_cast(session.cast))
        )
        self.answers_action.setVisible(offered)

    def _show_answers(self, on: bool) -> None:
        self.transcript.answers_shown = on
        # Not while a job runs, as the other view switches: rebuilt then, the
        # passage being written lost the text streamed so far. The job's own
        # rebuild shows the choice.
        if self.session is not None and not self._busy:
            self._reload_unless_editing()

    def _edit_answer(self, node_id: str, character_id: str) -> None:
        session = self.session
        if session is None or self._busy:
            return
        path = session.full_path()
        index = next((i for i, node in enumerate(path) if node.id == node_id), None)
        if index is None:
            return
        answer = next(
            (a for a in path[index].meta.reactions if a.character_id == character_id), None
        )
        if answer is None:
            return
        # The passage after the author's turn that this answer went with: the
        # one written from it, and the one written again when it changes.
        after = next((n for n in path[index + 1 :] if n.kind == "assistant"), None)
        # An answer to a passage goes with the next turn only while that turn
        # wasn't answered after it was sent (`round_after`): the story changed
        # to "after your turn" since, another take of the passage after it is
        # written from the turn's own answers and never reads this one. A
        # retake was offered all the same, and paid for.
        answered = path[path.index(after) - 1] if after is not None else None
        spent = (
            answered is not None
            and path[index].kind == "assistant"
            and answered.kind == "user"
            and answered.meta.round_after
        )
        if spent:
            after = None
        dialog = NoteDialog(
            self._dialog_parent(),
            title=EDIT_TITLE.format(name=answer.name),
            asks=EDIT_ASKS.format(name=answer.name),
            button=EDIT_REDO if after is not None else EDIT_KEEP,
            hint=EDIT_HINT_REDO
            if after is not None
            else EDIT_HINT_SPENT
            if spent
            else EDIT_HINT_TURN
            if path[index].kind == "user"
            else EDIT_HINT_OPEN,
            text=answer.text,
        )
        try:
            if dialog.exec() != NoteDialog.Accepted:
                return
            text = dialog.note()
        finally:
            dialog.deleteLater()  # read, then freed
        if self.session is not session or self._busy or not text:
            return
        if text != answer.text.strip():
            try:
                session.edit_reaction(node_id, character_id, text)
            except ValueError as error:
                self.statusBar().showMessage(str(error), 8000)
                return
        if after is not None:
            self._regenerate_node(after.id)
        else:
            self._reload_unless_editing()

    def _show_character_prompt(self, character_id: str) -> None:
        """Played by → What their model is sent…: their request, your notes
        for their model, their memory (gui/character_prompt_dialog.py)."""
        session = self.session
        if session is None:
            return
        if self._busy or session.in_private:
            self.statusBar().showMessage(
                "Their model's prompt opens once the story is idle, outside a private scene.",
                8000,
            )
            return
        show_character_prompt(self._dialog_parent(), session, character_id)
