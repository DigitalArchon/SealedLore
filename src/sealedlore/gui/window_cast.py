"""The main window's side of the cast: cards written for a published world's
people (Fill with lore characters…), and the Cast page lent to the new story
guide. A mixin for `MainWindow`."""

from __future__ import annotations

from PySide6.QtCore import QFile
from PySide6.QtWidgets import QMessageBox, QVBoxLayout, QWidget

from sealedlore.engine.session import SessionNotice
from sealedlore.gui.lore_cast_dialog import LoreCastDialog
from sealedlore.storage.paths import story_dir
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from sealedlore.storage.repository import delete_story as remove_story


class CastWindow:
    def _init_cast(self) -> None:
        """Called from `_build_ui` once the Cast page is made: the page is a
        holder with the panel in it, so the guide can borrow the panel and
        the Inspector keeps its page (gui/new_story_guide.py)."""
        # (story id) -> the world and names last asked for, offered again.
        self._lore_cast_asked: dict[str, tuple[str, str]] = {}
        # The new story guide, and Setup, while open: dialogs stand over them.
        self._guide: QWidget | None = None
        self._setup_dialog: QWidget | None = None
        self.cast_page = QWidget()
        layout = QVBoxLayout(self.cast_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.cast_panel)
        self.cast_panel.lore_fill_requested.connect(self.fill_lore_cast)

    def show_cast_page(self) -> None:
        self.right_tabs.setCurrentWidget(self.cast_page)

    # --- the new story guide (gui/new_story_guide.py) -------------------------

    def guide_make_story(self, bundle: StoryBundle) -> bool:
        """The guide's story, saved and opened behind it."""
        save_story_bundle(bundle, root=self.root)
        self.stories.refresh(selected_id=bundle.story.id)
        self.open_story(bundle.story.id)
        return self.session is not None and self.session.story.id == bundle.story.id

    def guide_story_changed(self, *, reopen: bool = False) -> None:
        session = self.session
        if session is None:
            return
        session.save()
        if reopen:
            self.open_story(session.story.id)
        else:
            self.refresh_panels()
        self.stories.refresh(selected_id=session.story.id)

    def guide_discard_story(self) -> None:
        """The guide's story, to the trash: Cancel, and Discard it."""
        session = self.session
        if session is None:
            return
        story_id = session.story.id
        self._close_story()
        folder = story_dir(story_id, self.root)
        trashed = QFile.moveToTrash(str(folder))
        if isinstance(trashed, tuple):  # PySide6 returns (ok, path in trash)
            trashed = trashed[0]
        if not trashed:
            # Nothing played and nothing written but the guide's: no second
            # question, as deleting a played story asks.
            try:
                remove_story(story_id, root=self.root)
            except OSError:
                pass
        self.stories.refresh()

    def guide_busy(self) -> bool:
        return self._busy

    # --- the guide borrows the Cast page -------------------------------------

    def lend_cast_panel(self) -> QWidget:
        """The Cast page's panel, taken out to stand in a dialog (the guide's
        Characters step, Setup's Characters tab); give it back with
        `return_cast_panel`."""
        self.cast_panel.setParent(None)
        # Suggestions come from the story's prose, and there is none yet.
        self.cast_panel.suggestions_box.setVisible(False)
        return self.cast_panel

    def return_cast_panel(self) -> None:
        if self.cast_panel.parent() is not self.cast_page:
            self.cast_page.layout().addWidget(self.cast_panel)
            self.cast_panel.suggestions_box.setVisible(True)
            self.cast_panel.show()

    # --- Fill with lore characters --------------------------------------------

    def fill_lore_cast(self) -> None:
        session = self.session
        if session is None:
            return
        if self._busy:
            self.statusBar().showMessage(
                "Cards can be written once the story's current job is done.", 6000
            )
            return
        if session.in_private:
            self.statusBar().showMessage("Not in a private scene.", 5000)
            return
        if self.config.active_provider() is None and not self.use_mock:
            QMessageBox.information(
                self,
                "No provider configured",
                "Set up an endpoint and a model in Settings first.",
            )
            return
        lore, wanted = self._lore_cast_asked.get(session.story.id, ("", ""))
        dialog = LoreCastDialog(
            self._dialog_parent(), model=session.authoring_model, lore=lore, wanted=wanted
        )
        try:
            if not dialog.exec():
                return
            lore, wanted = dialog.values()
        finally:
            dialog.deleteLater()
        if self.session is not session or not lore:
            return
        self._lore_cast_asked[session.story.id] = (lore, wanted)
        session.provider = self._current_provider()
        made: dict[str, object] = {}

        def job():
            result = session.fill_lore_cast(lore, wanted)
            made["result"] = result
            yield SessionNotice(f"{result.added} cards for {lore}.")

        def after() -> None:
            result = made.get("result")
            if result is None or self.session is not session:
                return
            self.refresh_panels()
            self._refresh_people()
            first = (*result.cast, *result.supporting)[:1]
            if first:
                self.cast_panel.select_character(first[0].id)
            self._say_lore_cast(lore, result)

        self._after_job = after
        self._start(
            job,
            streaming=False,
            busy_text=f"Writing cards for the people of {lore}…",
            job="Lore characters",
        )

    def _dialog_parent(self) -> QWidget:
        """A dialog opened from the guide or Setup (the Cast page lent to
        them) stands over it."""
        for host in (getattr(self, "_setup_dialog", None), getattr(self, "_guide", None)):
            if host is not None and host.isVisible():
                return host
        return self

    def _say_lore_cast(self, lore: str, result) -> None:
        if not result.added:
            words = f"No new cards for {lore}: everyone it wrote is on file already."
        else:
            words = (
                f"{result.added} new card{'s' if result.added != 1 else ''} for {lore}: "
                f"{len(result.cast)} in the cast, {len(result.supporting)} supporting. "
                "Marked ✦: look them over."
            )
        if result.skipped:
            words += f" Already on file: {', '.join(result.skipped)}."
        self.statusBar().showMessage(words, 15000)
        notes = [result.notes] if result.notes else []
        notes += result.warnings
        if notes:
            box = QMessageBox(self._dialog_parent())
            box.setWindowTitle("Lore characters")
            box.setText(words)
            box.setInformativeText("\n\n".join(notes))
            box.exec()
