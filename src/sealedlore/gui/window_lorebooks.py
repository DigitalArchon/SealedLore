"""The main window's side of Lorebooks (gui/lorebooks_page.py) and of the
people a lorebook describes (engine/lore_people.py).

Lorebooks are kept one file each (storage/lorebooks.py), read the first time
the page or an import needs them, and saved a moment after each change, as
Your characters are. A lorebook arriving (File → Import…, a link, a file
dropped) is kept, starts a story, or joins the open story's lore, as the
author says. A story from one (`story_from_lorebook`) takes a copy of its
entries, then anyone from Your characters, then, if asked, the people the
lore describes made characters, before Setup and Start.

Find the people… runs the authoring model on a dialog's thread
(gui/modal_job.py): for a kept lorebook on Your characters' session (the
people found are kept there), for a story's lore on the story's (they become
its cast or supporting cards, the author choosing, and its instructions may
move into the style's notes).
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

from sealedlore.engine.lore_people import LorePeople, first_sentence
from sealedlore.gui import file_dialogs
from sealedlore.gui.lore_people_dialog import LorePeopleDialog
from sealedlore.gui.lorebook_story import LorebookStoryDialog
from sealedlore.gui.lorebooks_page import (
    CONDENSE,
    DELETE,
    DUPLICATE,
    EXPORT,
    IMPORT,
    INTO_STORY,
    LINK,
    NEW,
    PAGE_LABEL,
    PEOPLE,
    STORY,
    LorebooksPage,
)
from sealedlore.gui.modal_job import run_with_progress
from sealedlore.models.character import Character
from sealedlore.models.lore import lore_fingerprint
from sealedlore.models.lorebook import StoredLorebook
from sealedlore.providers.base import StreamCancelled
from sealedlore.storage import collection, lorebooks
from sealedlore.storage.scenario import bundle_from_scenario
from sealedlore.storage.tavern_cards import keep_placeholders
from sealedlore.storage.tavern_lore import (
    LOREBOOK_SUFFIXES,
    LorebookError,
    ReadLorebook,
    book_from_lore,
    new_book,
    open_lorebook,
)

SAVE_DELAY_MS = 400
# Lore added with at least this many entries lacking a vector is embedded at
# once (`embed_lore_now`).
EMBED_NOW_LEAST = 20
LOREBOOKS_TIP = "Lorebooks kept for any story: import, change, export, start stories from them"
FILTER = "Lorebooks and character cards (*.json *.lorebook *.png *.charx);;All files (*)"
FIND_TITLE = "Finding the people"
# What the author says to a lorebook arriving (`_ask_lorebook_arrival`).
KEEP, START, INTO = "keep", "start", "into"


class LorebooksWindow:
    def _init_lorebooks(self) -> None:
        self._shelf: lorebooks.Shelf | None = None
        self.lorebooks_page = LorebooksPage()
        self.centre_stack.addWidget(self.lorebooks_page)
        page = self.lorebooks_page
        page.back_requested.connect(lambda: self.show_lorebooks(False))
        page.action_requested.connect(self._lorebook_action)
        page.changed.connect(self._lorebook_changed)
        page.files_dropped.connect(lambda paths: self.import_lorebooks([Path(p) for p in paths]))
        self._lorebooks_away: list[tuple[object, bool]] = []
        self._lorebooks_dirty: set[str] = set()
        self._lorebook_save = QTimer(self)
        self._lorebook_save.setSingleShot(True)
        self._lorebook_save.setInterval(SAVE_DELAY_MS)
        self._lorebook_save.timeout.connect(self._flush_lorebooks)

    def _lorebooks_menu_action(self, menu) -> None:
        from PySide6.QtGui import QAction

        self.lorebooks_action = QAction(f"{PAGE_LABEL}…", self)
        self.lorebooks_action.setStatusTip(LOREBOOKS_TIP)
        self.lorebooks_action.triggered.connect(lambda: self.show_lorebooks(True))
        menu.addAction(self.lorebooks_action)

    # --- the shelf ------------------------------------------------------------------------

    def _lorebooks(self) -> list[StoredLorebook]:
        if self._shelf is None:
            self._shelf = lorebooks.load_lorebooks(self.root)
            if self._shelf.unreadable:
                listed = "; ".join(f"{p.name}: {why}" for p, why in self._shelf.unreadable)
                self.statusBar().showMessage(
                    f"Some lorebooks couldn't be read and were left as they are ({listed}).",
                    15000,
                )
        return self._shelf.books

    def _kept_lorebook(self, book_id: str) -> StoredLorebook | None:
        return next((book for book in self._lorebooks() if book.id == book_id), None)

    def _save_lorebook(self, book: StoredLorebook) -> bool:
        try:
            lorebooks.save_lorebook(book, self.root)
        except (OSError, ValueError) as exc:
            self._warn_plain("Couldn't save the lorebook", f"{book.name}: {exc}")
            return False
        return True

    def _lorebook_changed(self, book_id: str) -> None:
        book = self._kept_lorebook(book_id)
        if book is None:
            return
        lorebooks.touch(book)
        self._lorebooks_dirty.add(book_id)
        self._lorebook_save.start()

    def _flush_lorebooks(self) -> None:
        # A name still being typed (the page takes it a moment after the last
        # key): quit within that moment, the old name stayed on disk.
        self.lorebooks_page.take_name()
        self._lorebook_save.stop()
        dirty, self._lorebooks_dirty = self._lorebooks_dirty, set()
        for book_id in dirty:
            book = self._kept_lorebook(book_id)
            if book is not None:
                self._save_lorebook(book)

    def keep_lorebook(self, read: ReadLorebook, name: str, came_from: str) -> StoredLorebook | None:
        """A lorebook read in, kept: a copy of its own, named for itself (or
        its file), numbered past a name another kept one has."""
        books = self._lorebooks()
        book = lorebooks.kept_from(read, name, came_from)
        book.name = lorebooks.unique_name(book.name, books)
        if not self._save_lorebook(book):
            return None
        books.append(book)
        return book

    # --- the page in the whole window --------------------------------------------------------

    def lorebooks_shown(self) -> bool:
        return self.centre_stack.currentWidget() is self.lorebooks_page

    def show_lorebooks(self, on: bool = True, book_id: str | None = None) -> None:
        page = self.lorebooks_page
        if on:
            if not self.lorebooks_shown():
                self.close_find()
                self.show_collection(False)
                self.show_cast_board(False)
                self.show_map(False)
                self._lorebooks_away = [(w, not w.isHidden()) for w in self._board_hides()]
                for widget, _visible in self._lorebooks_away:
                    widget.setVisible(False)
                self.centre_stack.setCurrentWidget(page)
            self._hide_banners()
            if self.session is None:
                page.set_back_words("Close")
            else:
                page.set_back_words("Back to the chat" if self.in_chat else "Back to the story")
            page.set_story_open(
                self.session is not None and not self.in_chat and not self.session.in_private
            )
            page.set_books(self._lorebooks())
            if book_id is not None:
                page.show_book(book_id)
            page.forget_form()
            page.setFocus()
            return
        if not self.lorebooks_shown():
            return
        self._flush_lorebooks()
        away, self._lorebooks_away = self._lorebooks_away, []
        self.centre_stack.setCurrentWidget(self.transcript)
        for widget, visible in away:
            widget.setVisible(visible)
        self._sync_stories_list()
        if self.session is None:
            self.reload_transcript()
            return
        self.refresh_summaries()
        self._sync_held_away()
        self.refresh_branch_bar()
        self._sync_place_button()
        self._sync_stage()
        self._sync_card_link()
        self._reshare_docks()

    def _refresh_lorebooks(self, book_id: str | None = None) -> None:
        if self.lorebooks_shown():
            self.lorebooks_page.set_books(self._lorebooks())
            if book_id is not None:
                self.lorebooks_page.show_book(book_id)

    # --- the page's actions ------------------------------------------------------------------

    def _lorebook_action(self, what: str, book_id: str) -> None:
        if what == NEW:
            self._new_lorebook()
            return
        if what == IMPORT:
            self.import_lorebooks()
            return
        if what == LINK:
            self.import_from_link("lorebooks")
            return
        book = self._kept_lorebook(book_id)
        if book is None:
            return
        self._flush_lorebooks()
        if what == EXPORT:
            self._export_kept_lorebook(book)
        elif what == DUPLICATE:
            name = lorebooks.unique_name(f"{book.name} (copy)", self._lorebooks())
            copy = lorebooks.copied(book, name)
            if self._save_lorebook(copy):
                self._lorebooks().append(copy)
                self._refresh_lorebooks(copy.id)
        elif what == DELETE:
            self._delete_kept_lorebook(book)
        elif what == STORY:
            self.story_from_lorebook(book.id)
        elif what == PEOPLE:
            self._people_in_kept_lorebook(book)
        elif what == INTO_STORY:
            self._kept_lorebook_into_story(book)
        elif what == CONDENSE:
            self._condense_kept_lorebook(book)

    def _new_lorebook(self) -> None:
        books = self._lorebooks()
        book = StoredLorebook(name=lorebooks.unique_name("New lorebook", books), book=new_book())
        if self._save_lorebook(book):
            books.append(book)
            self._refresh_lorebooks(book.id)
            self.lorebooks_page.name.setFocus()
            self.lorebooks_page.name.selectAll()

    def _read_lorebook_file(self, path: Path, *, quietly: bool = False) -> ReadLorebook | None:
        """A lorebook file (any kind SillyTavern imports), else a card's own
        lorebook; said in words when it is neither, unless `quietly`."""
        problem = f"{path.name} has no lorebook in it."
        if path.suffix.lower() in LOREBOOK_SUFFIXES:
            try:
                return open_lorebook(path)
            except LorebookError as exc:
                problem = str(exc)
        card = self._read_card(path, quietly=True)
        if card is not None and card.book is not None:
            return ReadLorebook(card.book)
        if not quietly:
            self._warn_plain("Could not read the lorebook", problem)
        return None

    def _name_for(self, path: Path) -> str:
        card = self._read_card(path, quietly=True) if path.suffix.lower() != ".json" else None
        return f"{card.name}'s lorebook" if card is not None else path.stem

    def import_lorebooks(self, paths: list[Path] | None = None) -> None:
        """Lorebook files (or cards, for their own lorebooks) into Lorebooks."""
        if paths is None:
            chosen, _ = file_dialogs.open_file_names(
                self._dialog_parent(), "Import lorebooks", str(Path.home()), FILTER
            )
            paths = [Path(p) for p in chosen]
        last = None
        for path in paths:
            read = self._read_lorebook_file(path)
            if read is not None:
                last = self.keep_lorebook(read, self._name_for(path), path.name) or last
        if last is not None:
            self.show_lorebooks(True, last.id)
            self.statusBar().showMessage(f"{last.name} is kept in Lorebooks.", 8000)

    def _export_kept_lorebook(self, book: StoredLorebook) -> None:
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in book.name).strip()
        path, _ = file_dialogs.save_file_name(
            self._dialog_parent(),
            "Export the lorebook",
            str(Path.home() / f"{safe or 'lorebook'}.json"),
            "SillyTavern lorebooks (*.json)",
        )
        if not path:
            return
        try:
            lorebooks.export(book, Path(path))
        except OSError as exc:
            self._warn_plain("Could not export the lorebook", f"{path}: {exc}")
            return
        self.statusBar().showMessage(f"Exported {book.name} to {path}.", 8000)

    def _delete_kept_lorebook(self, book: StoredLorebook) -> None:
        answer = QMessageBox.question(
            self._dialog_parent(),
            "Delete this lorebook?",
            f"Delete {book.name} from Lorebooks? A copy goes to the Trash as a SillyTavern "
            "lorebook file. Stories made from it keep their own copy of its lore.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        from sealedlore.gui.window_collection import move_to_trash

        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in book.name).strip()
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / f"{safe or 'lorebook'}.json"
            try:
                lorebooks.export(book, copy)
                trashed = move_to_trash(copy)
            except OSError:
                trashed = False
        if not trashed:
            answer = QMessageBox.question(
                self._dialog_parent(),
                "No Trash",
                f"{book.name} couldn't be put in the Trash. Delete it for good?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        try:
            lorebooks.lorebook_path(book.id, self.root).unlink(missing_ok=True)
        except OSError as exc:
            self._warn_plain("Couldn't delete the lorebook", f"{book.name}: {exc}")
            return
        self._lorebooks_dirty.discard(book.id)
        self._lorebooks().remove(book)
        self._refresh_lorebooks()

    # --- a lorebook arriving (File → Import…, a link) ---------------------------------------

    def lorebook_arrived(self, read: ReadLorebook, name: str, came_from: str) -> None:
        """A lorebook read in by File → Import… (or a link): kept in Lorebooks,
        a new story started with it (kept too), or into the open story's lore,
        as the author says (`_ask_lorebook_arrival`)."""
        story_here = self.session is not None and not self.in_chat and not self.in_private
        answer = self._ask_lorebook_arrival(read, name, story_here)
        if answer == INTO and story_here:
            # {{user}} and {{char}} as written: filled as each request goes out.
            imported = read.lore(keep_placeholders, f"the lorebook {name}")
            self._lore_into_story(imported, name)
            return
        if answer not in (KEEP, START):
            return
        if not lorebooks.stats(read.book).entries:
            self._warn_plain("A lorebook", f"{name} has no entries.")
            return
        book = self.keep_lorebook(read, name, came_from)
        if book is None:
            return
        if answer == START:
            self.story_from_lorebook(book.id)
        else:
            self.show_lorebooks(True, book.id)
            self.statusBar().showMessage(f"{book.name} is kept in Lorebooks.", 8000)

    def _ask_lorebook_arrival(self, read: ReadLorebook, name: str, story_here: bool) -> str:
        """KEEP, START, INTO (only with a story open to take it) or "" for
        Cancel."""
        box = QMessageBox(self._dialog_parent())
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("A lorebook")
        named = lorebooks.name_of(read.book, name)
        box.setText(f"{named} is a lorebook ({lorebooks.stats_words(read.book)}).")
        box.setInformativeText(
            "Keep it in Lorebooks, to change it and start any number of stories in its world "
            "with anyone from Your characters; start a story with it now (it is kept too)"
            + ("; or add its entries to this story's lore." if story_here else ".")
        )
        keep = box.addButton("Keep in Lorebooks", QMessageBox.AcceptRole)
        start = box.addButton("Start a new story", QMessageBox.AcceptRole)
        into = box.addButton("Add to this story", QMessageBox.AcceptRole) if story_here else None
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        clicked = box.clickedButton()
        box.deleteLater()
        for button, answer in ((keep, KEEP), (start, START), (into, INTO)):
            if button is not None and clicked is button:
                return answer
        return ""

    # --- a story from a lorebook --------------------------------------------------------------

    def story_from_lorebook(self, book_id: str) -> None:
        book = self._kept_lorebook(book_id)
        if book is None:
            return
        if self._busy:
            self._say_wait("A new story")
            return
        self._flush_lorebooks()
        can_ask = self.config.active_provider() is not None or self.use_mock
        kept = len(self._kept()) if not self._collection_problem else 0
        dialog = LorebookStoryDialog(book, kept=kept, can_ask=can_ask, parent=self._dialog_parent())
        accepted = dialog.exec() == QDialog.Accepted
        chosen = dialog.chosen()
        dialog.deleteLater()
        if not accepted or not self._leave_for_new():
            return
        scenario, notes = lorebooks.scenario_from(
            book,
            title=chosen.title,
            player=chosen.player,
            about_player=chosen.about_player,
            lore_firing=chosen.lore_firing,
        )
        bundle = bundle_from_scenario(scenario)

        def before_setup() -> None:
            if chosen.from_collection:
                self.add_from_collection(show=False)
            if chosen.find_people:
                self.find_people_in_story(source=book.name)
            self.embed_lore_now()

        if notes:
            self._say_left_out(
                "A new story from a lorebook",
                f"{scenario.title} has {len(scenario.lore)} lore entries from {book.name}. "
                "Not everything in the lorebook came along:",
                notes,
            )
        self.show_lorebooks(False)
        self._open_new_bundle(bundle, review=True, before_setup=before_setup)

    def embed_lore_now(self, *, least: int = EMBED_NOW_LEAST) -> None:
        """The open story's lore embedded now, while a progress dialog says so,
        when retrieval will want vectors and `least` entries or more have none:
        a story's first turn otherwise waited for the whole lorebook, saying
        nothing (most of a minute for 320 entries, Nov 20 2026). Stop leaves
        the rest for the first turn, as before."""
        session = self.session
        if session is None or self.in_chat or self._busy or session.in_private:
            return
        if session.embeddings is None or session.lore_layout().mode != "select":
            return
        if session.fires_like_tavern:
            # Fired by SillyTavern's rules, the lore is never ranked by its
            # vectors: every entry was sent to be embedded, for nothing.
            return
        pending = session.lore_without_vectors()
        if len(pending) < least:
            return
        result: dict[str, Any] = {}
        stopping = threading.Event()

        def job():
            try:
                session.ensure_lore_vectors(stopped=stopping.is_set)
            except StreamCancelled:
                return  # what was embedded is kept; the first turn does the rest
            session.save()
            return
            yield  # a generator, so GenerationWorker can run it

        run_with_progress(
            self._dialog_parent(),
            session,
            f"Embedding {len(pending)} lore entries, so each turn can find what it needs…",
            job,
            result,
            title="Embedding the lore",
            on_stop=stopping.set,
        )
        if result.get("error"):
            self.statusBar().showMessage(
                f"The lore couldn't be embedded now ({result['error']}): the first turn will "
                "try again.",
                10000,
            )

    # --- the people a lorebook describes ---------------------------------------------------

    def _find_people(self, session, entries, world: str) -> LorePeople | None:
        """The finder over `entries` on `session`'s authoring model, while a
        progress dialog waits; None when stopped or nothing could be read."""
        found = LorePeople()
        result: dict[str, Any] = {}

        def job():
            yield from session.find_lore_people(entries, found, world)

        run_with_progress(
            self._dialog_parent(),
            session,
            f"{session.authoring_model} is reading the lore of {world}…",
            job,
            result,
            title=FIND_TITLE,
            follow_notices=True,
        )
        if result.get("stopped"):
            return None
        if result.get("error"):
            self._warn_plain("Couldn't find the people", str(result["error"]))
            return None
        if not found.people and not found.instructions:
            words = "Nobody in it was found to have an entry of their own"
            if found.warnings:
                words += " (" + "; ".join(found.warnings) + ")"
            QMessageBox.information(self._dialog_parent(), FIND_TITLE, words + ".")
            return None
        return found

    def find_people_in_story(self, source: str | None = None) -> None:
        """The Lore page's Find the people…: the open story's lore read, and
        the people found made its cards as the author says."""
        session = self.session
        if session is None or self.in_chat or self._busy:
            if self._busy:
                self._say_wait("Find the people")
            return
        if session.in_private:
            self.statusBar().showMessage("Not in a private scene.", 5000)
            return
        if self.config.active_provider() is None and not self.use_mock:
            QMessageBox.information(
                self, "No endpoint set up", "Set up an endpoint and a model in Settings first."
            )
            return
        # Never what the plot hides, nor lore held back until it is
        # mentioned: no model has heard of either, and someone found in a
        # held-back entry would be a card every model reads from then on.
        hidden = session.hidden_ids() | session.held_back_ids()
        entries = [e for e in session.bundle.lore if e.enabled and e.id not in hidden]
        if not entries:
            self.statusBar().showMessage("The story has no lore to look in.", 6000)
            return
        session.provider = self._current_provider()
        world = source or session.story.title
        found = self._find_people(session, entries, world)
        self._story_spent(session)
        if found is None or self.session is not session:
            return
        # Who has a card already, as the Cast board shows them: someone the
        # plot still hides is named only with spoilers on.
        if self.config.show_plot_spoilers:
            cards = (*session.cast, *session.supporting)
        else:
            cards = (*session.visible_cast(), *session.visible_supporting())
        carded = [c.name for c in cards]
        dialog = LorePeopleDialog(
            found, source=world, story=True, carded=carded, parent=self._dialog_parent()
        )
        accepted = dialog.exec() == QDialog.Accepted
        placements, kept, rules = dialog.placements(), dialog.kept(), dialog.rules()
        dialog.deleteLater()
        if not accepted or self.session is not session:
            return
        took = session.take_lore_people(placements, rules)
        made = {card.name: card for card in (*took.cast, *took.supporting)}
        to_keep = [made[p.name] for p in kept if p.name in made]
        loose = [p for p in kept if p.name not in made]
        if to_keep:
            self._save_characters(to_keep)
        if loose:
            self._keep_found(loose, world, tag=world)
        self.refresh_panels()
        self._refresh_people()
        self._refresh_scene_panel()
        self._refresh_composer()
        self._reassemble_inspector()
        said = []
        if took.cast:
            said.append(f"{len(took.cast)} joined the cast")
        if took.supporting:
            said.append(f"{len(took.supporting)} are supporting characters")
        if took.removed or took.shortened:
            said.append(
                f"{took.removed} lore entries went with them"
                + (f", {took.shortened} shortened" if took.shortened else "")
            )
        if rules:
            said.append(f"{len(rules)} instructions moved into the style's notes")
        if kept:
            said.append(f"{len(kept)} kept in Your characters")
        if said:
            words = "; ".join(said)
            self.statusBar().showMessage(words[0].upper() + words[1:] + ".", 15000)

    def _story_spent(self, session) -> None:
        for usage in session.unreported_usage:
            self.status_strip.add_cost(usage)
        session.unreported_usage.clear()
        self._refresh_story_cost()

    def _people_in_kept_lorebook(self, book: StoredLorebook) -> None:
        """Find the people… in Lorebooks: those ticked kept in Your characters,
        the lorebook left as it is."""
        if self.session is not None and self.session.in_private:
            self._warn_plain(
                FIND_TITLE,
                "A private scene is open in the story: nothing else is sent to a model until "
                "it ends.",
            )
            return
        if self._collection_unwritable():
            return
        if self.config.active_provider() is None and not self.use_mock:
            QMessageBox.information(
                self, "No endpoint set up", "Set up an endpoint and a model in Settings first."
            )
            return
        session = self._collection_session()
        entries = [e for e in lorebooks.lore_of(book).entries if e.enabled]
        found = self._find_people(session, entries, book.name)
        session.unreported_usage.clear()
        if found is None:
            return
        dialog = LorePeopleDialog(
            found, source=book.name, story=False, parent=self._dialog_parent()
        )
        accepted = dialog.exec() == QDialog.Accepted
        kept = dialog.kept()
        dialog.deleteLater()
        if accepted and kept:
            made = self._keep_found(kept, book.name, tag=book.name)
            if made:
                self.statusBar().showMessage(
                    f"{len(made)} from {book.name} kept in Your characters.", 10000
                )

    def _keep_found(self, people, world: str, *, tag: str) -> list[Character]:
        """People found in a lorebook kept in Your characters, each tagged with
        its name: their profile their description, as the lorebook has it."""
        if self._collection_unwritable():
            return []
        kept_session = self._collection_session()
        made: list[Character] = []
        for person in people:
            card = collection.kept(person.name)
            card.aliases = list(person.aliases)
            card.summary = person.summary or first_sentence(person.text) or None
            card.full_description = person.text
            collection.info(card).came_from = f"the lorebook {world}"
            if tag and card.tavern is not None:
                card.tavern.tags = [tag]
            kept_session.cast.append(card)
            made.append(card)
        if not self._save_collection():
            for card in made:
                kept_session.cast.remove(card)
            return []
        self._refresh_collection()
        return made

    # --- between the stories and Lorebooks ---------------------------------------------------

    def lorebook_from_shelf(self) -> None:
        """The Lore page's From Lorebooks…: a kept lorebook chosen, its entries
        into the story's lore (`_kept_lorebook_into_story`)."""
        if self.session is None or self.in_chat or self._busy or self.session.in_private:
            return
        books = sorted(self._lorebooks(), key=lambda book: book.name.casefold())
        if not books:
            self.statusBar().showMessage(
                "No lorebooks kept yet: File → Lorebooks… imports or starts one.", 8000
            )
            return
        book = books[0]
        if len(books) > 1:
            names = [f"{b.name} ({lorebooks.stats_words(b.book)})" for b in books]
            chosen, ok = QInputDialog.getItem(
                self._dialog_parent(), "From Lorebooks", "Which lorebook?", names, 0, False
            )
            if not ok:
                return
            book = books[names.index(chosen)]
        self._kept_lorebook_into_story(book)

    def _kept_lorebook_into_story(self, book: StoredLorebook) -> None:
        """A kept lorebook's entries into the open story's lore, those chosen
        and as changed for that story alone (gui/lorebook_pick.py)."""
        session = self.session
        if session is None or self.in_chat or self._busy or session.in_private:
            return
        imported = lorebooks.lore_of(book)
        picked = self._choose_their_lore([(book.name, imported.entries)])
        if picked is None:
            return
        imported.entries = picked[0]
        self.show_lorebooks(False)
        self._lore_into_story(imported, book.name)

    def story_lore_to_lorebooks(self) -> None:
        """The Lore page's Keep in Lorebooks…: the open story's lore kept as a
        lorebook of its own (its plot-hidden entries only with spoilers on)."""
        session = self.session
        if session is None or self.in_chat:
            return
        hidden = set() if self.config.show_plot_spoilers else session.hidden_ids()
        entries = [e for e in session.bundle.lore if e.id not in hidden]
        if not entries:
            self.statusBar().showMessage("The story has no lore to keep.", 6000)
            return
        name = lorebooks.unique_name(f"{session.story.title} lore", self._lorebooks())
        book = StoredLorebook(
            name=name,
            came_from=f"the story {session.story.title}",
            book=book_from_lore(entries),
            # Its condensed versions along, under the key the book reads them by
            # (an entry's text as a story reads it: `tavern_lore.condensed_key`).
            condensed={
                lore_fingerprint(entry.content): shorter
                for entry in entries
                if (shorter := entry.condensed_now())
            },
        )
        if self._save_lorebook(book):
            self._lorebooks().append(book)
            self.statusBar().showMessage(f"The story's lore is kept in Lorebooks as {name}.", 8000)

    def card_lorebook_to_lorebooks(self, card: Character) -> None:
        """Your characters' More → Keep their lorebook in Lorebooks: a copy of a
        character's own lorebook kept on its own, for any story."""
        book = card.tavern.character_book if card.tavern else None
        if not book:
            self.statusBar().showMessage(f"{card.name} has no lorebook of their own.", 6000)
            return
        read = ReadLorebook(book)
        kept = self.keep_lorebook(read, f"{card.name}'s lorebook", card.name)
        if kept is not None:
            self.show_lorebooks(True, kept.id)
