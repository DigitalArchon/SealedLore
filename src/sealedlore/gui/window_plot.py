"""The main window's side of a story's plot: the plot editor working on a
story (it is the editor's host, gui/plot_editor.py). A mixin for `MainWindow`.

The editor is its own window, kept while the app runs, but the story it works
on is the open story's session's: the session is the story's one writer, so
the editor reads the story and gives it a plot through here, and never while
a job holds the story.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from sealedlore.gui.plot_editor import PlotEditorWindow
from sealedlore.models.plot_file import PlotDocument
from sealedlore.storage.library import story_listings
from sealedlore.storage.repository import StoryBundle


class PlotWindow:
    _plot_editor: PlotEditorWindow | None

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
