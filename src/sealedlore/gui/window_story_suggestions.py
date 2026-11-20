"""The window's side of Story → Suggestions from the story… (the
`StorySuggestionsWindow` mixin): the dialog, its Apply and Not this through
the session, and Look over the recent story as a job.

Nothing is applied while a job runs (the worker owns the story then): the
dialog's buttons wait, and `_suggestions_job_done` wakes them."""

from __future__ import annotations

from sealedlore.gui.story_suggestions_dialog import StorySuggestionsDialog
from sealedlore.models.story_suggestion import StorySuggestion

ACTION_WORDS = "Suggestions from the &story…"
ACTION_TIP = (
    "What the story has made part of its world, or changed in it, suggested for the "
    "lore and the cards as it is archived: apply, change or turn each down"
)


class StorySuggestionsWindow:
    _suggestions_dialog: StorySuggestionsDialog | None = None

    def story_suggestion_count(self) -> int:
        return len(self.session.story_suggestions) if self.session is not None else 0

    def show_story_suggestions(self) -> None:
        if self.session is None:
            return
        if self.session.in_private:
            self.statusBar().showMessage(
                "Paused while the private scene is open: end it first", 5000
            )
            return
        dialog = StorySuggestionsDialog(
            lambda: list(self.session.story_suggestions) if self.session is not None else [],
            current=self._suggestion_current,
            apply=self._apply_story_suggestion,
            decline=self._decline_story_suggestion,
            look_over=self._look_over_story,
            parent=self,
        )
        self._suggestions_dialog = dialog
        dialog.set_busy(self._busy)
        try:
            dialog.exec()
        finally:
            self._suggestions_dialog = None
            dialog.deleteLater()
        self._update_suggestions_action()

    def _suggestion_current(self, suggestion: StorySuggestion) -> str:
        if self.session is None or suggestion.kind != "lore_change":
            return ""
        entry = next((e for e in self.session.bundle.lore if e.id == suggestion.target_id), None)
        return entry.content if entry is not None else ""

    def _apply_story_suggestion(self, suggestion: StorySuggestion, values: dict) -> str | None:
        if self.session is None or self._busy:
            return None
        try:
            said = self.session.apply_story_suggestion(suggestion.id, **values)
        except (KeyError, ValueError) as exc:
            return f"⚠ Not applied: {str(exc).strip(chr(39))}."
        # A card or the lore changed: the pages and the cast's room follow.
        self.refresh_panels()
        self._update_suggestions_action()
        return said

    def _decline_story_suggestion(self, suggestion: StorySuggestion) -> None:
        if self.session is None or self._busy:
            return
        try:
            self.session.decline_story_suggestion(suggestion.id)
        except KeyError:
            pass
        self._update_suggestions_action()

    def _look_over_story(self) -> None:
        if self.session is None or self._busy:
            return
        if not self.session.path():
            self.statusBar().showMessage("Nothing written yet to look through", 4000)
            return
        if self._suggestions_dialog is not None:
            self._suggestions_dialog.set_busy(True)
        self.session.provider = self._current_provider()
        self._start(
            self.session.suggest_from_recent,
            streaming=False,
            busy_text="Reading the recent story for lore and card changes…",
            job="Suggestions from the story",
        )

    def _suggestions_job_done(self) -> None:
        """After any job (an archive may have brought suggestions)."""
        if self._suggestions_dialog is not None:
            self._suggestions_dialog.set_busy(False)
            self._suggestions_dialog.refresh()
        self._update_suggestions_action()

    def _update_suggestions_action(self) -> None:
        action = getattr(self, "story_suggestions_action", None)
        if action is None:
            return
        count = self.story_suggestion_count()
        action.setText(ACTION_WORDS + (f" ({count})" if count else ""))
        action.setEnabled(self.session is not None and not self.session.in_private)
