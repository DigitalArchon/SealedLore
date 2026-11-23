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
it again, which is how the turn is redone. Regenerate beside Edit asks that
character's model for the answer again (the author: "a regenerate option that
regenerates just their response"), and offers the same passage again once
the new answer is in.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox

from sealedlore.engine.reactions import independent_cast
from sealedlore.gui.character_prompt_dialog import show_character_prompt
from sealedlore.gui.note_dialog import NoteDialog
from sealedlore.models.node import Node, Reaction

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
EDIT_HINT_PRIVATE = (
    "The passage after it is a private scene's, which isn't written again from here. Saving "
    "keeps your words here; nothing is written again."
)


# An answer's Regenerate (beside its Edit): their model asked again for it.
REGENERATE_BUSY = "Asking {name}'s model for their answer again…"
# The job's name, in "… failed" when it does.
REGENERATE_JOB = "Asking {name}'s model again"
# Once the new answer is in, when a passage was written from the old one.
REDO_TITLE = "Write the next passage again?"
REDO_ASKS = "{name} answered again. Write the passage after it again from the new answer?"
REDO_HINT = (
    "The passage there now was written from their answer as it was. Written again, it is a "
    "new take, and the one there now is kept (‹ ›). Kept as it is, the new answer goes with "
    "the next take of that passage you ask for."
)
REDO_WRITE = "Write it again"
REDO_KEEP = "Keep the passage"
# Said in the status bar once the new answer is in, as Edit's hints say it.
ANSWERED_AGAIN_OPEN = (
    "{name} answered again. The storyteller is given the new answer with your next turn, "
    "as what they mean to do."
)
ANSWERED_AGAIN_TURN = (
    "{name} answered your turn again. The storyteller is given it when the passage is "
    "written (Regenerate)."
)
ANSWERED_AGAIN_SPENT = (
    "{name} answered again. The passage after it wasn't written from this answer, so "
    "nothing is written again."
)
ANSWERED_AGAIN_PRIVATE = (
    "{name} answered again. The passage after it is a private scene's, so nothing is written again."
)
ANSWERED_AGAIN_KEPT = "{name}'s new answer is kept; the passage after it stays as it was."
STOPPED_KEPT = "Stopped: {name}'s answer stays as it was."
# The most of the new answer the question quotes: the rest is under the message.
QUOTED_MOST = 600


# Why the answers on a message are spent (`written_from`): the turn after them
# was answered after it was sent, or what came next is a private scene's.
SPENT_AFTER = "after"
SPENT_PRIVATE = "private"


def written_from(path: Sequence[Node], index: int) -> tuple[Node | None, str]:
    """(the passage written from the answers on `path[index]`, why they are
    spent, "" when they aren't): what Edit and Regenerate on an answer offer
    to write again, worked out once for both, so the two never disagree about
    when another take would read the answer. The passage is the next on the
    path, the one after the author's turn the answers went with; None for the
    open round or a turn with no passage yet. Spent (`SPENT_AFTER`): answers
    to a passage go with the next turn only while that turn wasn't answered
    after it was sent (`round_after`). The story changed to "after your turn"
    since, another take of the passage after it is written from the turn's
    own answers and never reads these: a retake was offered all the same, and
    paid for. Spent too (`SPENT_PRIVATE`) when what came next is a private
    scene's (its messages, its summary): `path` is the page's, which shows an
    ended scene, and its passage was offered to be written again from here."""
    for node in path[index + 1 :]:
        if node.meta.private_span is not None or node.meta.private_summary_of is not None:
            return None, SPENT_PRIVATE
        if node.kind == "assistant":
            break
    after = next((n for n in path[index + 1 :] if n.kind == "assistant"), None)
    answered = path[path.index(after) - 1] if after is not None else None
    spent = (
        answered is not None
        and path[index].kind == "assistant"
        and answered.kind == "user"
        and answered.meta.round_after
    )
    return (None if spent else after), SPENT_AFTER if spent else ""


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
        after, spent = written_from(path, index)
        dialog = NoteDialog(
            self._dialog_parent(),
            title=EDIT_TITLE.format(name=answer.name),
            asks=EDIT_ASKS.format(name=answer.name),
            button=EDIT_REDO if after is not None else EDIT_KEEP,
            hint=EDIT_HINT_REDO
            if after is not None
            else EDIT_HINT_PRIVATE
            if spent == SPENT_PRIVATE
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

    def _regenerate_answer(self, node_id: str, character_id: str) -> None:
        """An answer's Regenerate: that character's model is asked for it
        again, nobody else's, as a job (Stop keeps the answer there). Asked
        at once, with nothing to decide first: whether the passage after it
        is written again is asked once the new answer is in and can be read
        (`_answer_regenerated`), so a passage is paid for only from an answer
        the author has seen; and the open round's answers have no passage
        after them to ask about."""
        session = self.session
        if session is None or self._busy:
            return
        refusal = session.reaction_refusal(node_id, character_id)
        if refusal is not None:
            self.statusBar().showMessage(refusal, 8000)
            return
        node = next((n for n in session.full_path() if n.id == node_id), None)
        answer = (
            next((a for a in node.meta.reactions if a.character_id == character_id), None)
            if node is not None
            else None
        )
        if answer is None:
            return
        session.provider = self._current_provider()
        self._after_job = lambda: self._answer_regenerated(session, node_id, character_id, answer)
        self._start(
            lambda: session.regenerate_reaction(node_id, character_id),
            streaming=False,
            busy_text=REGENERATE_BUSY.format(name=answer.name),
            job=REGENERATE_JOB.format(name=answer.name),
        )

    def _answer_regenerated(
        self, session, node_id: str, character_id: str, before: Reaction
    ) -> None:
        """Once their new answer is in: when a passage was written from the
        old one, that passage offered again, as Edit offers it
        (`written_from`); otherwise said where the new answer goes. A failure
        left the old answer and its dialog said so; a Stop left it too, and
        is said here. Run before any next job starts, so `_worker` is still
        this one's."""
        if self.session is not session:
            return
        path = session.full_path()
        index = next((i for i, node in enumerate(path) if node.id == node_id), None)
        if index is None:
            return
        answer = next(
            (a for a in path[index].meta.reactions if a.character_id == character_id), None
        )
        stopped = self._worker is not None and self._worker.stopping
        if answer is None or answer is before:
            if stopped:
                self.statusBar().showMessage(STOPPED_KEPT.format(name=before.name), 10000)
            return
        after, spent = written_from(path, index)
        if after is not None and stopped:
            # In before the Stop landed: kept, and nothing more is paid for.
            self.statusBar().showMessage(ANSWERED_AGAIN_KEPT.format(name=answer.name), 10000)
            return
        if after is None:
            words = (
                ANSWERED_AGAIN_PRIVATE
                if spent == SPENT_PRIVATE
                else ANSWERED_AGAIN_SPENT
                if spent
                else ANSWERED_AGAIN_TURN
                if path[index].kind == "user"
                else ANSWERED_AGAIN_OPEN
            )
            self.statusBar().showMessage(words.format(name=answer.name), 10000)
            return
        if self._busy or not self._write_again_asked(answer):
            self.statusBar().showMessage(ANSWERED_AGAIN_KEPT.format(name=answer.name), 10000)
            return
        self._regenerate_node(after.id)

    def _write_again_asked(self, answer: Reaction) -> bool:
        """Whether the author has the passage after a new answer written
        again from it. Keep is the default: Enter mustn't pay for a passage."""
        text = answer.text.strip()
        if len(text) > QUOTED_MOST:
            text = text[:QUOTED_MOST].rstrip() + "…"
        box = QMessageBox(self._dialog_parent())
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(REDO_TITLE)
        box.setTextFormat(Qt.PlainText)
        box.setText(REDO_ASKS.format(name=answer.name))
        box.setInformativeText(f"{text}\n\n{REDO_HINT}")
        write = box.addButton(REDO_WRITE, QMessageBox.AcceptRole)
        keep = box.addButton(REDO_KEEP, QMessageBox.RejectRole)
        box.setDefaultButton(keep)
        try:
            box.exec()
            return box.clickedButton() is write
        finally:
            box.deleteLater()  # read, then freed

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
