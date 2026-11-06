"""The main window's side of a story's plot: a plot written by the model, and
the plot editor working on a story (it is the editor's host,
gui/plot_editor.py). A mixin for `MainWindow`.

A plot is written on the worker, like any job that changes the story
(`StorySession.draft_plot`), from Story → Write a plot with the model…, the
new story guide's last step, or New story from a premise (`_then_plot`, run
once the story is set up and before Start). Once in, it is kept hidden unless
the author asks to see it (gui/plot_dialog.py).

The editor is its own window, kept while the app runs, but the story it works
on is the open story's session's: the session is the story's one writer, so
the editor reads the story and gives it a plot through here, and never while
a job holds the story.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from sealedlore.engine.plot_draft import PlotDraft, PlotWishes
from sealedlore.engine.session import SessionNotice
from sealedlore.gui.plot_dialog import EDIT, PlotDialog, PlotReadyDialog, plot_preview
from sealedlore.gui.plot_editor import PlotEditorWindow
from sealedlore.models.plot_file import PlotDocument
from sealedlore.providers.base import ProviderError, StreamCancelled, error_detail
from sealedlore.storage.library import story_listings
from sealedlore.storage.repository import StoryBundle

WRITING = "Writing the plot… it takes a minute or two."


class PlotWindow:
    _plot_editor: PlotEditorWindow | None

    # --- a plot written by the model -------------------------------------------

    def write_plot(self) -> None:
        """Story → Write a plot with the model…: what the author wants of it,
        then the plot, in place of any the story has."""
        session = self.session
        if session is None or session.story.chat:
            return
        if self._busy:
            self._say_wait("Writing a plot")
            return
        if session.in_private:
            self.statusBar().showMessage("Not in a private scene: end it first.", 6000)
            return
        if self._needs_provider():
            return
        dialog = PlotDialog(
            self._dialog_parent(),
            model=session.authoring_model,
            replacing=session.story.plot is not None,
            begun=bool(session.path()),
        )
        try:
            if dialog.exec() != QDialog.Accepted:
                return
            wishes = dialog.wishes()
        finally:
            dialog.deleteLater()
        if wishes is not None and self.session is session:
            self._draft_plot(wishes)

    def _then_plot(self, wishes: PlotWishes | None, then: Callable[[], None]) -> None:
        """A new story's plot, when one was asked for (the guide's last step,
        a premise draft's), then `then` (its Start), plot or none."""
        session = self.session
        if wishes is None or session is None:
            then()
            return
        if self.config.active_provider() is None and not self.use_mock:
            self.statusBar().showMessage(
                "No plot: set up an endpoint in Settings, then Story → Write a plot with the "
                "model… writes one.",
                12000,
            )
            then()
            return
        self._draft_plot(wishes, then=then)

    def _draft_plot(self, wishes: PlotWishes, *, then: Callable[[], None] | None = None) -> None:
        """The plot, written on the worker. A failure or a Stop leaves the
        story as it was, and `then` still follows."""
        session = self.session
        if session is None:
            return
        session.provider = self._current_provider()
        made: dict[str, object] = {}

        def job():
            try:
                made["draft"] = yield from session.draft_plot(wishes)
            except StreamCancelled:
                raise  # Stop: the worker ends the job quietly, and nothing changed
            except (ProviderError, ValueError) as exc:
                made["error"] = error_detail(exc)
            yield SessionNotice("The plot is written." if "draft" in made else "No plot.")

        def after() -> None:
            if self.session is not session:
                return
            draft = made.get("draft")
            if isinstance(draft, PlotDraft):
                self._after_plot_changed()
                self._plot_written(draft, then)
            elif "error" in made:
                self._plot_failed(str(made["error"]), wishes, then)
            else:
                self.statusBar().showMessage(
                    "No plot was written: Story → Write a plot with the model… writes one.",
                    10000,
                )
                if then is not None:
                    then()

        self._after_job = after
        self._start(job, streaming=False, busy_text=WRITING, job="Writing the plot", progress=True)

    def _plot_written(self, draft: PlotDraft, then: Callable[[], None] | None) -> None:
        """The plot is in: kept hidden unless the author asks to see it, then
        on to `then`, or to the plot editor (and the author starts the story
        when they are ready)."""
        session = self.session
        dialog = PlotReadyDialog(
            self._dialog_parent(),
            title=session.story.title,
            plot=plot_preview(draft.document),
            notes=draft.notes,
            go_on="Start the story…" if then is not None else "Close",
            spoilers_on=self.config.show_plot_spoilers,
        )
        try:
            dialog.exec()
            choice, spoilers_off = dialog.choice, dialog.keep_spoilers_off
        finally:
            dialog.deleteLater()
        if spoilers_off:
            self._on_spoilers_toggled(False)
            self._refresh_plot_panel()
        if choice == EDIT:
            self.edit_story_plot()
            if then is not None:
                self.statusBar().showMessage(
                    "When the plot is as you want it, start the story with Story → Start story….",
                    15000,
                )
            return
        if then is not None:
            then()
            return
        self.statusBar().showMessage(
            "The plot is written. Story → Edit the plot in the plot editor… shows it whole.",
            10000,
        )

    def _plot_failed(
        self, message: str, wishes: PlotWishes, then: Callable[[], None] | None
    ) -> None:
        """Nothing usable came back: try again, or go on without a plot."""
        box = QMessageBox(self._dialog_parent())
        box.setIcon(QMessageBox.Warning)
        # The endpoint's own words may be in it: never rendered as HTML.
        box.setTextFormat(Qt.PlainText)
        box.setWindowTitle("No plot")
        box.setText("The plot couldn't be written.")
        box.setInformativeText(message)
        again = box.addButton("Try again", QMessageBox.AcceptRole)
        box.addButton(
            "Start without a plot" if then is not None else "Close", QMessageBox.RejectRole
        )
        box.setDefaultButton(again)
        box.exec()
        if box.clickedButton() is again:
            self._draft_plot(wishes, then=then)
        elif then is not None:
            then()

    # --- the plot editor ---------------------------------------------------------

    def _plot_editor_window(self) -> PlotEditorWindow:
        """The plot editor, one window, made on first use."""
        if self._plot_editor is None:
            self._plot_editor = PlotEditorWindow(self, host=self)
            self._plot_editor.start_requested.connect(self._start_from_plot_file)
        return self._plot_editor

    def _show_plot_editor(self) -> None:
        editor = self._plot_editor_window()
        editor.show()
        editor.raise_()
        editor.activateWindow()

    def edit_story_plot(self) -> None:
        """Story → Edit the plot in the plot editor…: the open story's plot,
        or a plot to give it, its world and cast filled in."""
        session = self.session
        if session is None or session.story.chat:
            return
        if self._busy:
            self._say_wait("The plot editor")
            return
        if self._plot_editor_window().open_story(session.story.id):
            self._show_plot_editor()

    def _plot_editor_parent(self) -> QWidget:
        editor = self._plot_editor
        return editor if editor is not None and editor.isVisible() else self

    def _tell_plot_editor(self, title: str, text: str) -> None:
        box = QMessageBox(
            QMessageBox.Warning, title, text, QMessageBox.Ok, self._plot_editor_parent()
        )
        box.exec()

    # --- the plot editor's host -------------------------------------------------

    def plot_editor_stories(self) -> list[tuple[str, str]]:
        """Every story that can have a plot: not a simple chat, not one that
        can't be read."""
        choices = []
        for listing in story_listings(self.root):
            if listing.error is not None or listing.chat:
                continue
            plot = "has a plot" if listing.plot else "no plot yet"
            played = f"{listing.messages} messages" if listing.messages else "not begun"
            choices.append((listing.id, f"{listing.title} — {plot}, {played}"))
        return choices

    def plot_editor_bundle(self, story_id: str) -> StoryBundle | None:
        """The story, opened here if another is open (asking what opening a
        story asks, a memory-only chat left behind among it)."""
        if self._busy:
            self._tell_plot_editor(
                "The story is busy",
                "SealedLore is working on the story. Try again once the current job is done.",
            )
            return None
        if self.session is None or self.session.story.id != story_id:
            self.open_story(story_id)
        session = self.session
        if session is None or session.story.id != story_id:
            return None
        if session.story.chat:
            self._tell_plot_editor("A simple chat", "A simple chat has no plot.")
            return None
        return session.bundle

    def plot_editor_save(self, story_id: str, document: PlotDocument) -> list[str] | None:
        """The editor's Save to the story: the document's plot given to the
        story (StorySession.set_plot_document)."""
        if self.plot_editor_bundle(story_id) is None:
            return None
        session = self.session
        try:
            notes = session.set_plot_document(document)
        except ValueError as exc:
            self._tell_plot_editor("Not saved", str(exc))
            return None
        self._after_plot_changed()
        return notes

    def _after_plot_changed(self) -> None:
        """A plot given or changed: everything that shows the story's cards
        and its plot follows, and a review's Undo, which would take it back,
        is forgotten."""
        session = self.session
        if session is None:
            return
        session.forget_review_undo()
        self.refresh_panels()
        self._refresh_plot_panel()
        self._refresh_people()
        self._reassemble_inspector()
        self._update_controls()
        self.stories.refresh(selected_id=session.story.id)
