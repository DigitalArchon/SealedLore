"""The main window's side of follow-ups: asking the model for changes to a
story or its plot, and undoing them (gui/followup_dialog.py,
engine/session_followup.py). A mixin for `MainWindow`.

Offered after a premise draft (before Setup), from the plot's own dialog once
the model has written it, from the transcript of a story not yet begun, and
from the Story menu. Each request runs on the worker like any job that
changes the story; the dialog stays open across them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtWidgets import QMessageBox

from sealedlore.engine.followup import PLOT, STORY
from sealedlore.engine.session import SessionNotice
from sealedlore.engine.story_plot import document_from_story, safe_prose
from sealedlore.gui.followup_dialog import FollowUpDialog
from sealedlore.gui.plot_dialog import plot_preview
from sealedlore.providers.base import ProviderError, StreamCancelled, error_detail
from sealedlore.storage.repository import StoryBundle

CHANGING = {
    STORY: "Changing the story… it takes a minute or two.",
    PLOT: "Writing the plot again… it takes a minute or two.",
}


def story_preview(bundle: StoryBundle) -> str:
    """The story as it is now, to read beside a request: its world, opening,
    characters and lore. Whoever and whatever the plot keeps hidden is left
    out: that is the plot's, and a spoiler. So are the storyteller's notes,
    the story's own secrets (Setup shows them when asked)."""
    story, setup = bundle.story, bundle.story.setup
    parts = [f"# {story.title}"]
    if (setup.description or "").strip():
        parts.append(setup.description.strip())
    if (story.world_bible or "").strip():
        parts += ["## World", safe_prose(story.world_bible, under=2)]
    if setup.opening_text.strip():
        parts += ["## Opening", safe_prose(setup.opening_text, under=2)]
    for heading, cards in (
        ("Characters", bundle.cast),
        ("Others with a card", bundle.supporting.characters),
    ):
        shown = [c for c in cards if not c.plot_hidden and c.name.strip()]
        if shown:
            parts.append(f"## {heading}")
            for card in shown:
                text = card.full_description or card.summary or ""
                parts.append(f"### {card.name}\n\n{safe_prose(text, under=3)}")
    lore = [e for e in bundle.lore if not e.plot_hidden and e.title.strip()]
    if lore:
        parts.append("## Lore")
        parts += [f"### {entry.title}\n\n{safe_prose(entry.content, under=3)}" for entry in lore]
    return "\n\n".join(parts)


class FollowUpWindow:
    _follow_up_dialog: FollowUpDialog | None = None

    def change_story_with_model(
        self,
        *,
        then: Callable[[], None] | None = None,
        go_on: str = "Close",
        drafted: bool = False,
        notes: Sequence[str] = (),
    ) -> None:
        """Story → Change the story with the model…: requests for changes to
        a story not yet begun. `then` follows when the dialog closes (a
        premise draft's Setup); `notes`: what the draft left out."""
        session = self.session
        if session is None or session.story.chat:
            return
        if session.nodes:
            self.statusBar().showMessage(
                "The story has begun: Story → Review settings with the model… changes one "
                "under way.",
                10000,
            )
        elif self._may_follow_up("Changing the story"):
            self._open_follow_up(STORY, then=then, go_on=go_on, drafted=drafted, notes=notes)
            return
        # Not offered: what was to follow goes on without it.
        if then is not None:
            then()

    def change_plot_with_model(
        self, *, then: Callable[[], None] | None = None, go_on: str = "Close"
    ) -> None:
        """Story → Change the plot with the model…: requests for changes to
        the story's plot, which is written again with each."""
        session = self.session
        if session is None or session.story.chat:
            return
        if session.story.plot is None:
            self.statusBar().showMessage(
                "The story has no plot: Story → Write a plot with the model… writes one.", 8000
            )
        elif self._may_follow_up("Changing the plot"):
            self._open_follow_up(PLOT, then=then, go_on=go_on)
            return
        if then is not None:
            then()

    def _may_follow_up(self, what: str) -> bool:
        if self._busy:
            self._say_wait(what)
            return False
        if self.in_private:
            self.statusBar().showMessage("Not in a private scene: end it first.", 6000)
            return False
        return not self._needs_provider()

    def _open_follow_up(
        self,
        target: str,
        *,
        then: Callable[[], None] | None,
        go_on: str,
        drafted: bool = False,
        notes: Sequence[str] = (),
    ) -> None:
        session = self.session
        story = session.story
        # A draft's notes for the storyteller: said to be there, never shown.
        unread = drafted and target == STORY and story.storyteller_notes_by == "model"
        dialog = FollowUpDialog(
            self._dialog_parent(),
            target=target,
            title=story.title,
            go_on=go_on,
            history=session.follow_up_history(target),
            notes=notes,
            can_undo=session.follow_up_to_undo(target) is not None,
            preview=lambda: self._follow_up_preview(target),
            drafted=drafted,
            secret_words=len((story.storyteller_notes or "").split()) if unread else 0,
        )
        dialog.send_requested.connect(lambda text: self._send_follow_up(dialog, target, text))
        dialog.undo_requested.connect(lambda: self._undo_follow_up(dialog, target))
        self._follow_up_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._follow_up_dialog = None
            dialog.deleteLater()
        if then is not None and self.session is session:
            then()

    def _follow_up_preview(self, target: str) -> str:
        session = self.session
        if session is None:
            return ""
        if target == PLOT:
            return plot_preview(document_from_story(session.bundle))
        return story_preview(session.bundle)

    def _send_follow_up(self, dialog: FollowUpDialog, target: str, request: str) -> None:
        session = self.session
        if session is None or self._busy:
            return
        session.provider = self._current_provider()
        made: dict[str, object] = {}

        def job():
            try:
                if target == STORY:
                    made["turn"] = yield from session.follow_up_story(request)
                else:
                    made["turn"], _draft = yield from session.follow_up_plot(request)
            except StreamCancelled:
                raise  # Stop: the worker ends the job quietly, and nothing changed
            except (ProviderError, ValueError) as exc:
                made["error"] = error_detail(exc)
            yield SessionNotice("Done." if "turn" in made else "Nothing was changed.")

        def after() -> None:
            dialog.set_busy(False)
            if self.session is not session:
                return
            turn = made.get("turn")
            if turn is not None:
                dialog.add_turn(turn)
                dialog.changed()
                self._after_follow_up()
            elif "error" in made:
                dialog.show_error(str(made["error"]))
            else:
                dialog.show_error("stopped before the model had answered.")
            dialog.set_can_undo(session.follow_up_to_undo(target) is not None)

        dialog.set_busy(True)
        self._after_job = after
        self._start(
            job,
            streaming=False,
            busy_text=CHANGING[target],
            job="Changing the story" if target == STORY else "Changing the plot",
            progress=True,
        )

    def _undo_follow_up(self, dialog: FollowUpDialog, target: str) -> None:
        session = self.session
        if session is None or self._busy:
            return
        if session.follow_up_changed_since():
            answer = QMessageBox.question(
                dialog,
                "Undo the last change?",
                "The story has changed since that request. Undoing it puts the story back "
                "as it was before the request, and those later changes go too.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        if session.undo_follow_up(target):
            dialog.mark_undone()
            self._after_follow_up()
        dialog.set_can_undo(session.follow_up_to_undo(target) is not None)

    def _after_follow_up(self) -> None:
        """A follow-up done or undone: everything that shows the story
        follows (as a plot given to it does)."""
        self._after_plot_changed()
        self._refresh_scene_panel()
        self.reload_transcript()
