"""The window's side of always-on lore against the lore budget
(engine/session_lore_condense.py): the Lore page says when it is over, the
Always-on entries dialog makes entries optional and writes condensed
versions, one entry's is written from its own page, and a kept lorebook's
from Lorebooks (on Your characters' session, as Find the people… is).

Condensing is a paid call to the authoring model: never while a job runs,
never in a private scene or a chat.

The vectors lore from the full entry is ranked by are made ahead of the turns
(engine/session_lore_vectors.py): a moment after a story opens or its lore
changes (condensed versions written, entries made optional, the rules or the
model changed), while nothing else runs, and taken in by a poll once made."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer
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
# The lore's vectors are seen to this long after the story opened or its lore
# last changed (typing in an entry changes it at every key), and a job out is
# looked for this often.
LORE_VECTORS_AFTER_MS = 2_000
LORE_VECTORS_POLL_MS = 1_000


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

    def _init_always_on(self) -> None:
        self._lore_vectors_soon = QTimer(self)
        self._lore_vectors_soon.setSingleShot(True)
        self._lore_vectors_soon.setInterval(LORE_VECTORS_AFTER_MS)
        self._lore_vectors_soon.timeout.connect(self._lore_vectors_if_needed)
        self._lore_vectors_timer = QTimer(self)
        self._lore_vectors_timer.setInterval(LORE_VECTORS_POLL_MS)
        self._lore_vectors_timer.timeout.connect(self._poll_lore_vectors)

    # --- the vectors lore from the full entry is ranked by -----------------------

    def _lore_vectors_later(self) -> None:
        """The lore's vectors seen to a moment from now (`_lore_vectors_if_needed`)."""
        self._lore_vectors_soon.start()

    def _lore_vectors_if_needed(self) -> None:
        """Have the vectors the turns' ranking lacks made in the background,
        while nothing else runs; while a dialog's thread works on the story,
        once it is done with it."""
        session = self.session
        if session is None or session.in_private or session.story.chat:
            return
        if self._story_in_use(session):
            # The window's job, or a dialog's thread: once it is done.
            self._wait_for_story(session, "vectors")
            return
        if session.start_lore_vectors():
            self._lore_vectors_timer.start()

    def _poll_lore_vectors(self) -> None:
        """Take the vectors in once made, when nothing else is writing the
        story. A turn started meanwhile takes them in itself."""
        session = self.session
        if session is None or not session.lore_vectors_out():
            self._lore_vectors_timer.stop()
            return
        if self._story_in_use(session) or not session.lore_vectors_ready():
            return
        notices = session.adopt_lore_vectors()
        self._lore_vectors_timer.stop()
        self._reassemble_inspector()
        if notices:
            self.statusBar().showMessage(" ".join(notices), 12000)

    def _after_lore_vectors_job(self) -> None:
        """After a job: its passage may have set the vectors going; if none
        is out and some are wanted, a moment from now. A Stop during the
        reads after a passage closes the job before the end of send(), where
        they are set going, so the Lore page said they were being made for
        good."""
        session = self.session
        if session is None:
            return
        if session.lore_vectors_out():
            self._lore_vectors_timer.start()
        elif session.lore_vectors_wanted():
            self._lore_vectors_later()

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
            # Under SillyTavern's rules an entry's patterns are keys too.
            fired_keys=(lambda entry: session._fire_entry(entry).keys)
            if session.fires_like_tavern
            else None,
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
        # What goes condensed may have changed, condensed versions been written.
        self._lore_vectors_later()

    def _keep_condensed_lore(self, entry: LoreEntry, text: str) -> None:
        """The author's own condensed version typed on the Lore page: the
        session keeps it (`set_condensed_lore`). The page shows only the open
        story's lore."""
        if self.session is not None:
            self.session.set_condensed_lore(entry, text)

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
        self._lore_vectors_later()

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
        # On Your characters' own client, as Find the people… is: it took the
        # story's here, so this Stop cut the passage the story was writing,
        # the story's Stop cut this, and the session kept the story's client.
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
