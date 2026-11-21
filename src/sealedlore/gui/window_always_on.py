"""The window's side of always-on lore against the lore budget
(engine/session_lore_condense.py): the Lore page says when it is over, the
Always-on entries dialog makes entries optional and writes condensed
versions, one entry's is written from its own page, and a kept lorebook's
from Lorebooks (on Your characters' session, as Find the people… is).

Condensing is a paid call to the authoring model: never while a job runs,
never in a private scene or a chat."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QDialog, QMessageBox

from sealedlore.engine.lore_condense import (
    fit_standing,
    least_important_first,
    worth_condensing,
)
from sealedlore.gui.always_on_dialog import AlwaysOnDialog
from sealedlore.gui.modal_job import run_with_progress
from sealedlore.models.lore import LoreEntry, lore_fingerprint
from sealedlore.models.lorebook import StoredLorebook
from sealedlore.storage import lorebooks

CONDENSE_TITLE = "Condensing lore"
NOT_IN_PRIVATE = "Not in a private scene: nothing else is sent to a model until it ends."


def always_on_note(whole: int, sent: int, room: int, condensed: int) -> str:
    """The Lore page's line when always-on lore is more than the budget."""
    if whole <= room:
        return ""
    head = f"Always-on entries take {whole:,} tokens, more than the lore budget of {room:,}"
    if condensed and sent <= room:
        return f"{head}: {condensed} sent condensed, {sent:,} tokens."
    if condensed:
        return f"{head}, and {sent:,} with {condensed} condensed: make some optional."
    return f"{head}: make some optional, or write condensed versions."


class AlwaysOnWindow:
    _always_on_said: set[str]

    def _sync_always_on(self) -> None:
        """The Lore page's line, and once a story a word in the status bar."""
        session = self.session
        if session is None or self.in_chat:
            self.lore_panel.set_always_on("", False)
            return
        entries = session.always_on_entries()
        fit = session.standing_fit()
        note = always_on_note(fit.whole, fit.sent, fit.room, len(fit.condensed))
        self.lore_panel.set_always_on(note, bool(entries))
        said = getattr(self, "_always_on_said", None)
        if said is None:
            said = self._always_on_said = set()
        if note and fit.over and session.story.id not in said:
            said.add(session.story.id)
            self.statusBar().showMessage(f"{note} (Lore page → Always-on entries…)", 12000)

    def open_always_on(self) -> None:
        session = self.session
        if session is None or self.in_chat:
            return
        if self._busy:
            self._say_wait("Always-on entries")
            return
        if session.in_private:
            self.statusBar().showMessage(NOT_IN_PRIVATE, 6000)
            return
        importance = session._importance()
        room = session.standing_room()

        def fit(on: list[LoreEntry], use: bool):
            return fit_standing(
                on,
                room=room,
                count_tokens=session._count_lore_tokens,
                least_first=least_important_first(on, importance),
                use_condensed=use,
            )

        dialog = AlwaysOnDialog(
            session.always_on_entries(),
            use_condensed=session.story.defaults.condense_lore,
            fit=fit,
            tokens=session._count_lore_tokens,
            condense=lambda entries: self._condense_story_lore(entries, parent=dialog),
            parent=self._dialog_parent(),
        )
        accepted = dialog.exec() == QDialog.Accepted
        flags, use = dialog.chosen()
        dialog.deleteLater()
        if accepted and self.session is session:
            changed = session.story.defaults.condense_lore != use
            for entry in session.bundle.lore:
                if entry.id in flags and entry.always_on != flags[entry.id]:
                    entry.always_on = flags[entry.id]
                    changed = True
            if changed:
                session.story.defaults.condense_lore = use
                session.forget_review_undo()
                session.save()
        self.refresh_panels()
        self._reassemble_inspector()

    def condense_lore_entry(self, entry_id: str) -> None:
        """The Lore page's Write a condensed version: one entry."""
        session = self.session
        if session is None or self.in_chat:
            return
        entry = next((e for e in session.bundle.lore if e.id == entry_id), None)
        if entry is None:
            return
        if not worth_condensing(entry.content):
            self.statusBar().showMessage(
                "This entry is too short for a condensed version to save anything.", 6000
            )
            return
        if entry.id not in {e.id for e in session.visible_lore()}:
            self.statusBar().showMessage(
                "Not while the entry is kept from every model (the plot, or held back).", 6000
            )
            return
        self._condense_story_lore([entry], parent=self._dialog_parent())
        self.refresh_panels()
        self.lore_panel.select_entry(entry_id)
        self._reassemble_inspector()

    def _condense_story_lore(self, entries: list[LoreEntry], *, parent) -> None:  # noqa: ANN001
        session = self.session
        if session is None or not entries:
            return
        if self._busy:
            self._say_wait("Condensing lore")
            return
        if session.in_private:
            self.statusBar().showMessage(NOT_IN_PRIVATE, 6000)
            return
        if self.config.active_provider() is None and not self.use_mock:
            QMessageBox.information(
                parent, "No endpoint set up", "Set up an endpoint and a model in Settings first."
            )
            return
        session.provider = self._current_provider()
        result: dict[str, Any] = {}
        said: list[str] = []

        def job():
            for notice in session.condense_lore(entries):
                said.append(notice.text)
                yield notice

        run_with_progress(
            parent,
            session,
            f"{session.authoring_model} is condensing {len(entries)} "
            f"{'entry' if len(entries) == 1 else 'entries'}…",
            job,
            result,
            title=CONDENSE_TITLE,
            follow_notices=True,
        )
        self._story_spent(session)
        problem = result.get("error") or next(
            (text for text in said if text.startswith("Couldn't condense")), None
        )
        if problem:
            self._warn_plain("Not every entry was condensed", str(problem))

    # --- Lorebooks -------------------------------------------------------------------

    def _condense_kept_lorebook(self, book: StoredLorebook) -> None:
        """Lorebooks' Condense the always-on entries…: kept beside the book,
        for the stories made from it afterwards to have."""
        if self.session is not None and self.session.in_private:
            self._warn_plain(CONDENSE_TITLE, NOT_IN_PRIVATE)
            return
        if self._collection_unwritable():
            return
        if self.config.active_provider() is None and not self.use_mock:
            QMessageBox.information(
                self, "No endpoint set up", "Set up an endpoint and a model in Settings first."
            )
            return
        entries = [
            entry
            for entry in lorebooks.lore_of(book).entries
            if entry.enabled
            and entry.always_on
            and worth_condensing(entry.content)
            and not entry.condensed_now()
        ]
        if not entries:
            QMessageBox.information(
                self._dialog_parent(),
                CONDENSE_TITLE,
                "Every always-on entry long enough to condense has a condensed version already.",
            )
            return
        session = self._collection_session()
        answer = QMessageBox.question(
            self._dialog_parent(),
            CONDENSE_TITLE,
            f"Write condensed versions of {len(entries)} always-on "
            f"{'entry' if len(entries) == 1 else 'entries'} of {book.name}, with "
            f"{session.authoring_model}? One call an entry. The entries themselves are kept "
            "as they are; stories made from the lorebook afterwards have both, and start with "
            "the whole texts.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        session.provider = self._current_provider()
        written: dict[str, str] = {}
        failed: list[str] = []
        result: dict[str, Any] = {}
        # By the key the book keeps them under: the entry's text as a story
        # reads it (`tavern_lore.condensed_key`), which `lore_of` gives here.
        items = [(lore_fingerprint(entry.content), entry.title, entry.content) for entry in entries]

        def job():
            yield from session.condense_texts(items, written, failed)

        run_with_progress(
            self._dialog_parent(),
            session,
            f"{session.authoring_model} is condensing the always-on entries of {book.name}…",
            job,
            result,
            title=CONDENSE_TITLE,
            follow_notices=True,
        )
        session.unreported_usage.clear()
        if written:
            book.condensed.update(written)
            self._save_lorebook(book)
            self._refresh_lorebooks(book.id)
            self.lorebooks_page.form.refresh_entry()
        if result.get("error") or failed:
            self._warn_plain(
                "Not every entry was condensed",
                str(result.get("error") or "Couldn't condense: " + "; ".join(failed) + "."),
            )
        elif written:
            self.statusBar().showMessage(
                f"{len(written)} condensed {'version' if len(written) == 1 else 'versions'} "
                f"kept with {book.name}.",
                8000,
            )
