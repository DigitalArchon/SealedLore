"""The window's side of Your characters (gui/collection_page.py,
storage/collection.py): the page in the whole window, as the cast board
takes it, and everything it asks for. A mixin for `MainWindow`.

The author's characters are kept as a story's folder of their own, opened
here as a session of its own (`_collection`) and never as the story's: a
story stays open underneath, its job running, while the author looks after
their characters; Back to the story brings it back as it was. That session
is the one writer of their folder. Pictures for them are drawn, approved and
put on their cards by the very tools a story's cards use
(gui/window_images.py, given that session), and what drawing costs is in
their folder's own log, said on the page.
"""

from __future__ import annotations

import io
import re
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QFile, Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

from sealedlore.engine.look_sheet import keep_order, label_key
from sealedlore.engine.looks import look_words
from sealedlore.engine.session import StorySession
from sealedlore.engine.usage import usage_report
from sealedlore.gui.card_files_form import TYPES, link_problem
from sealedlore.gui.collection_page import (
    ADD_TO_STORY,
    BACKUP,
    CHAT,
    DELETE,
    DRAFT,
    DUPLICATE,
    EXPORT_CHARX,
    EXPORT_JSON,
    EXPORT_PNG,
    EXPRESSIONS,
    FROM_STORY,
    IMPORT,
    LINK,
    NEW,
    PAGE_LABEL,
    RENAME_VERSION,
    RESTORE,
    STORY_FROM_CARD,
    TAG,
    UNTAG,
    VERSION,
    CollectionPage,
)
from sealedlore.gui.fill_gaps import ask_fill_gaps
from sealedlore.gui.image_codec import encoded
from sealedlore.gui.picture_labels import edit_labels
from sealedlore.gui.ref_images import PICTURE_FILTER, clean_incoming, image_of, store_reference
from sealedlore.ids import new_id
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.tavern import CardAsset, TavernFields
from sealedlore.storage import collection
from sealedlore.storage.collection_backup import (
    BACKUP_SUFFIX,
    BackupError,
    new_log_entries,
    open_backup,
    restore,
    write_backup,
)
from sealedlore.storage.image_meta import (
    MetadataError,
    picture_kind,
    raster_kind,
    strip_metadata,
    stripped_or_same,
)
from sealedlore.storage.images import REFS_DIR, write_bytes_atomic
from sealedlore.storage.paths import COLLECTION_ID, collection_dir
from sealedlore.storage.picture_store import PictureStore
from sealedlore.storage.repository import append_api_log, read_api_log, stories_linking
from sealedlore.storage.tavern_cards import (
    MAX_INFLATED,
    CardError,
    TavernCard,
    card_charx,
    card_data,
    expression_refs,
    expressions_from_zip,
    read_card,
    tavern_from_file,
    write_card,
)
from sealedlore.storage.tavern_lore import (
    LorebookError,
    UnconvertibleLorebook,
    book_from_world,
    entries_of,
    open_lorebook,
    write_world,
)

COLLECTION_TIP = (
    "Characters you keep for any story or chat: import SillyTavern cards, make your own, "
    "give them pictures"
)
CARD_FILTER = "Character cards (*.png *.json *.charx);;All files (*)"


def _everything(card: Character) -> str:
    """What a CHARX of them carries, counted: "2 pictures and 18 files"."""
    pictures = len(card.reference_images)
    files = sum(1 for asset in (card.tavern.assets if card.tavern else []) if asset.held)
    parts = [f"{pictures} picture{'' if pictures == 1 else 's'}"]
    if files:
        parts.append(f"{files} file{'' if files == 1 else 's'}")
    return " and ".join(parts)


# A card written from here says where it came from when it says nothing itself.
MADE_HERE = "the author's own characters"
# Field edits are kept this long after the last keystroke.
SAVE_DELAY_MS = 400


def move_to_trash(path: Path) -> bool:
    """The desktop's trash, where there is one (tests put their own here)."""
    trashed = QFile.moveToTrash(str(path))
    if isinstance(trashed, tuple):  # PySide6 returns (ok, path in trash)
        trashed = trashed[0]
    return bool(trashed)


def _safe(name: str) -> str:
    return re.sub(r"[^\w\- ]", "_", name).strip() or "character"


@contextmanager
def _waiting():
    """The busy cursor over work that holds the window a moment (a backup of
    many characters), put back whatever happens."""
    QApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


class CollectionWindow:
    def _init_collection(self) -> None:
        # Opened the first time it is needed (`_collection_session`); why
        # their folder couldn't be read, when it couldn't.
        self._collection: StorySession | None = None
        self._collection_problem = ""
        self.collection_page = CollectionPage(self._collection_pictures)
        self.centre_stack.addWidget(self.collection_page)
        page = self.collection_page
        page.back_requested.connect(lambda: self.show_collection(False))
        page.action_requested.connect(self._collection_action)
        page.chosen_requested.connect(self._collection_chosen)
        page.cards_dropped.connect(self._import_to_collection)
        page.files_dropped.connect(
            lambda paths, card_id: self._add_card_files(self._kept_card(card_id), paths)
        )
        page.changed.connect(self._collection_changed)
        page.pictures_changed.connect(self._collection_pictures_changed)
        page.picture_requested.connect(
            lambda what, card_id, ref_id: self._on_card_picture(
                card_id, ref_id, what, session=self._collection_session()
            )
        )
        page.lorebook_requested.connect(self._collection_lorebook)
        page.file_requested.connect(self._collection_file)
        page.character_shown.connect(lambda _card_id: self._refresh_collection_sheet())
        board = page.board
        board.picture_action.connect(self._collection_board_picture)
        board.fill_requested.connect(self._collection_fill)
        board.sheet_changed.connect(self._collection_pictures_changed)
        board.files_requested.connect(self._collection_files)
        board.files_dropped.connect(
            lambda card_id, look, expression, paths: self._collection_files(
                card_id, look, expression, paths=paths
            )
        )
        board.restyle_requested.connect(self._collection_restyle)
        board.portrait_requested.connect(
            lambda card_id: self._on_card_picture(
                card_id, "", "new", session=self._collection_session()
            )
        )
        # What the page put away, each as it was, to bring back as it was.
        self._collection_away: list[tuple[object, bool]] = []
        self._collection_save = QTimer(self)
        self._collection_save.setSingleShot(True)
        self._collection_save.setInterval(SAVE_DELAY_MS)
        self._collection_save.timeout.connect(self._save_collection)

    def _collection_menu_action(self, menu) -> None:
        from PySide6.QtGui import QAction

        self.collection_action = QAction(f"{PAGE_LABEL}…", self)
        self.collection_action.setStatusTip(COLLECTION_TIP)
        self.collection_action.triggered.connect(lambda: self.show_collection(True))
        menu.addAction(self.collection_action)

    # --- the session they are kept by ---------------------------------------------------

    def _collection_session(self) -> StorySession:
        if self._collection is None:
            try:
                bundle = collection.load_collection(self.root)
            except (OSError, ValueError) as exc:
                # Nothing read, nothing written over: the folder stays as it
                # is for the author to look into, and the page says why.
                bundle = collection.new_collection()
                self._collection_problem = (
                    f"Your characters couldn't be read ({str(exc).splitlines()[0][:200]}), so "
                    f"nothing is kept or changed there until they can be: "
                    f"{collection_dir(self.root)}"
                )
            self._collection = StorySession(
                bundle,
                self.config,
                # A client of its own: a Stop in the story never reaches a
                # prompt being written for one of these, nor the other way.
                self._build_provider(),
                root=self.root,
                estimator=self.estimator,
                learn_corrections=not self.use_mock,
            )
            if self._collection_problem:
                self._collection.save = lambda: None  # never over a folder unread
            else:
                self._settle_collection_files()
                self._forget_unused_pictures()
        return self._collection

    def _settle_collection_files(self) -> None:
        """Card files kept before each character had a folder of their own,
        moved into it once: the new places saved first, the old files
        removed only then."""
        session = self._collection
        old = collection.settle_files(session.cast, session.pictures)
        if not old:
            return
        try:
            session.save()
        except OSError:
            return  # the old records stand on disk, and so do their files
        for file in old:
            session.pictures.remove(file)

    def _collection_pictures(self) -> PictureStore | None:
        """Their pictures' store, once their session is open: the page's
        widgets ask as they are built, and the window starts without reading
        the folder at all."""
        return self._collection.pictures if self._collection is not None else None

    def _collection_store(self) -> PictureStore:
        """Their pictures' store, their session opened for it: for whatever
        reads or writes one of their pictures now. Keep in Your characters…
        is the Cast page's, and nothing had opened them before it."""
        return self._collection_session().pictures

    def _save_collection(self) -> bool:
        """Kept on disk; whether it was. A picture no card names any more goes
        with the save that drops it (`_forget_unused_pictures`)."""
        self._collection_save.stop()
        if self._collection_problem:
            # Said once a run, not at every pause in typing; the page's line
            # over the tabs says it until the folder can be read.
            if not getattr(self, "_collection_problem_said", False):
                self._collection_problem_said = True
                self._warn_plain("Your characters weren't kept", self._collection_problem)
            return False
        if self._collection is None:
            return False
        try:
            self._collection.save()
        except OSError as exc:
            self._warn_plain("Couldn't keep your characters", str(exc))
            return False
        except (ValueError, RecursionError) as exc:
            # Not the disk's doing: something one of them holds can't be
            # written (data nested past what can be). Said, rather than every
            # edit after it going unkept without a word.
            reason = (
                "one of them holds data nested too deep to be written"
                if isinstance(exc, RecursionError)
                else str(exc).splitlines()[0][:200]
            )
            self._warn_plain(
                "Couldn't keep your characters",
                f"Your characters couldn't be saved ({reason}), so the changes since the last "
                "save aren't kept.",
            )
            return False
        self._forget_unused_pictures()
        return True

    def _collection_unwritable(self) -> bool:
        """Your characters couldn't be read: nothing goes into the folder,
        pictures included, and no story is linked to a card never kept;
        said each time it is tried."""
        self._collection_session()
        if self._collection_problem:
            self._warn_plain("Not kept", self._collection_problem)
            return True
        return False

    def _flush_collection(self) -> None:
        """An edit still waiting to be kept, kept now. Nothing else is
        written: everything but a field's edit is kept as it is made, and an
        untouched folder stays untouched."""
        if self._collection_save.isActive():
            self._save_collection()

    # --- all of them at once: a backup, and putting one back -------------------------------

    def _backup_collection(self) -> None:
        """Every character, versions, pictures and files, into one file
        (Backup → Back up all your characters…): their ids kept, so a story's
        links to them hold once it is put back, here or on another computer."""
        session = self._collection_session()
        if self._collection_problem:
            self._warn_plain("Not backed up", self._collection_problem)
            return
        if not session.cast:
            return
        self._flush_collection()
        chosen, _ = QFileDialog.getSaveFileName(
            self._dialog_parent(),
            "Back up all your characters",
            str(Path.home() / f"Your characters{BACKUP_SUFFIX}"),
            f"Backups of your characters (*{BACKUP_SUFFIX});;All files (*)",
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix.lower() != ".zip":
            path = path.with_name(path.name + BACKUP_SUFFIX)
        try:
            with _waiting():
                written = write_backup(
                    path,
                    session.cast,
                    session.pictures,
                    images=session.pictures.images(),
                    log=read_api_log(COLLECTION_ID, root=self.root),
                )
        except OSError as exc:
            self._warn_plain("Couldn't back up your characters", str(exc))
            return
        count = len(session.cast)
        self.statusBar().showMessage(
            f"Backed up {count} character{'s' if count != 1 else ''} with {written} "
            f"file{'s' if written != 1 else ''} to {path.name}.",
            10000,
        )

    def restore_collection(self, path: Path | None = None) -> None:
        """A backup of characters put back (Backup → Restore from a backup…,
        or File → Import… of one): those not here come back under their own
        ids; those here already are kept, or replaced, as the author says."""
        if path is None:
            chosen, _ = QFileDialog.getOpenFileName(
                self._dialog_parent(),
                "Restore your characters",
                str(Path.home()),
                f"Backups of your characters (*{BACKUP_SUFFIX} *.zip);;All files (*)",
            )
            if not chosen:
                return
            path = Path(chosen)
        if self._collection_unwritable():
            return
        session = self._collection_session()
        self._flush_collection()
        try:
            with open_backup(path) as backup:
                here = {character.id for character in session.cast}
                again = [c.name for c in backup.characters if c.id in here]
                replace = self._ask_replace_kept(again) if again else False
                if replace is None:
                    return
                with _waiting():
                    done = restore(
                        backup,
                        session.bundle,
                        session.pictures,
                        replace=replace,
                        clean=self._clean_picture,
                    )
                    # What drawing them cost comes back with them (the log is
                    # append-only), an entry at a time: never held whole.
                    log = read_api_log(COLLECTION_ID, root=self.root)
                    for entry in new_log_entries(backup, log):
                        append_api_log(COLLECTION_ID, entry, root=self.root)
        except BackupError as exc:
            self._warn_plain("Couldn't restore your characters", str(exc))
            return
        self._save_collection()
        self.collection_page.forget_pictures()
        self.collection_page.forget_forms()
        self.show_collection(True)
        self._refresh_collection()
        lead = (
            f"Restored from {path.name}: {len(done.added)} added"
            + (f", {len(done.replaced)} replaced" if done.replaced else "")
            + (f", {len(done.kept)} kept as you had them" if done.kept else "")
            + "."
        )
        if done.missing:
            self._say_left_out(
                PAGE_LABEL,
                lead,
                [
                    f"{done.missing} file{'s' if done.missing != 1 else ''} the backup names "
                    "weren't in it, or couldn't be cleaned of their hidden data, so they were "
                    "left out."
                ],
            )
        else:
            self.statusBar().showMessage(lead, 10000)

    def _ask_replace_kept(self, names: list[str]) -> bool | None:
        """Characters the backup has that are here already: keep yours (the
        default), use the backup's, or Cancel (None)."""
        shown = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
        box = QMessageBox(self._dialog_parent())
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Restore your characters")
        box.setText(
            f"{len(names)} of the characters in this backup are in Your characters already: "
            f"{shown}."
        )
        box.setInformativeText(
            "Keep yours as they are, or put the backup's in their place? The others in the "
            "backup are added either way."
        )
        box.setTextFormat(Qt.PlainText)
        mine = box.addButton("Keep mine", QMessageBox.AcceptRole)
        theirs = box.addButton("Use the backup's", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(mine)
        box.exec()
        clicked = box.clickedButton()
        if clicked is mine:
            return False
        if clicked is theirs:
            return True
        return None

    def _close_collection(self) -> None:
        """When the window closes: kept, and its client let go."""
        self._flush_collection()
        if self._collection is not None:
            self._collection.close(wait_seconds=0)
            close = getattr(self._collection.provider, "close", None)
            if close is not None:
                close()

    def _renew_collection_provider(self) -> None:
        """The settings changed: a client built from them."""
        if self._collection is None:
            return
        old, self._collection.provider = self._collection.provider, self._build_provider()
        close = getattr(old, "close", None)
        if close is not None:
            close()

    def _kept(self) -> list[Character]:
        return self._collection_session().cast

    def _kept_card(self, card_id: str) -> Character | None:
        return next((c for c in self._kept() if c.id == card_id), None)

    # --- the page in the whole window ------------------------------------------------------

    def collection_shown(self) -> bool:
        return self.centre_stack.currentWidget() is self.collection_page

    def show_collection(self, on: bool = True, card_id: str | None = None) -> None:
        """The page in the whole window, on `card_id`'s card (None: as it
        was, or everyone), or what was there back as it was."""
        page = self.collection_page
        if on:
            if not self.collection_shown():
                self.close_find()
                self.show_cast_board(False)
                self.show_map(False)
                # Hidden by hand or not: `isVisible` is False for all while the
                # window itself isn't shown.
                self._collection_away = [(w, not w.isHidden()) for w in self._board_hides()]
                for widget, _visible in self._collection_away:
                    widget.setVisible(False)
                self.centre_stack.setCurrentWidget(page)
                # The card may have changed since (a story saved to it).
                page.forget_forms()
            self._hide_banners()
            if self.session is None:
                page.set_back_words("Close")
            else:
                page.set_back_words("Back to the chat" if self.in_chat else "Back to the story")
            # Not while a private scene is open: the story is paused.
            page.set_story_open(
                self.session is not None and not self.in_chat and not self.session.in_private
            )
            page.set_characters(self._kept())
            if card_id is not None:
                page.show_character(card_id)
            self._refresh_collection_sheet()
            self._collection_spent()
            if self._collection_problem:
                page.set_stats(self._collection_problem)
            page.setFocus()
            return
        if not self.collection_shown():
            return
        self._flush_collection()
        away, self._collection_away = self._collection_away, []
        self.centre_stack.setCurrentWidget(self.transcript)
        for widget, visible in away:
            widget.setVisible(visible)
        # The list as chosen meanwhile (View → Stories list), not as it was.
        self._sync_stories_list()
        if self.session is None:
            # The start page as it is now (a story made meanwhile is on it).
            self.reload_transcript()
            return
        self.refresh_summaries()
        self._sync_held_away()
        self.refresh_branch_bar()
        self._sync_place_button()
        self._sync_stage()
        self._sync_card_link()
        self._reshare_docks()

    def _refresh_collection(self) -> None:
        """The page from the characters as they are, when it is showing; the
        fields of the one shown are left as they are."""
        if self._collection is None or not self.collection_shown():
            return
        self.collection_page.set_characters(self._kept())
        self._refresh_collection_sheet()
        self._collection_spent()

    def _refresh_collection_sheet(self) -> None:
        """The sheet of the character shown, what is being drawn for them in it."""
        page = self.collection_page
        card = page.current_character()
        if card is None or self._collection is None:
            return
        drawing: dict[tuple[str, str, str], int] = {}
        for job in self.image_jobs.pending(COLLECTION_ID):
            request = job.request
            if request.card_id == card.id and not request.card_first:
                key = (card.id, label_key(request.card_look), label_key(request.card_expression))
                drawing[key] = drawing.get(key, 0) + 1
        for story_id, waiting in self._board_waiting:
            if story_id == COLLECTION_ID and waiting.card_id == card.id:
                key = (card.id, label_key(waiting.look), label_key(waiting.expression))
                drawing[key] = drawing.get(key, 0) + 1
        page.board.aim(card.id)
        page.board.set_texts(self._collection.texts)
        page.board.set_cards(
            [card], shown={}, wanted={}, drawing=drawing, begun=False, can_change=True
        )

    def _collection_spent(self) -> None:
        """What drawing their pictures (and writing the prompts) has cost, on
        the page: from their folder's own log."""
        if self._collection is None:
            return
        total = usage_report(
            read_api_log(COLLECTION_ID, root=self.root),
            self.config.model_prices,
            cache_ttl=self.config.cache_ttl,
        ).total
        words = ""
        if total.calls:
            about = "~" if total.estimated else ""
            words = f"Pictures for them have cost {about}${total.cost:.4f} so far."
            if total.unpriced:
                words += f" ({total.unpriced} unpriced)"
        self.collection_page.set_stats(words)

    def _collection_pictures_landed(self) -> None:
        """Pictures drawn for them are in: onto their cards, and shown."""
        session = self._collection
        if session is None:
            return
        if self._story_in_use(session):
            # A dialog's thread is writing their prompts: taken once it is done.
            self._wait_for_story(session, "pictures")
            return
        names = session.take_card_pictures()
        self._start_waiting()
        if names:
            self.statusBar().showMessage(f"Picture ready: {', '.join(names)}.", 8000)
        if self.collection_shown():
            self.collection_page.refresh_pictures()
            self._refresh_collection_sheet()
            self._collection_spent()

    # --- what the page asks for -------------------------------------------------------------

    def _collection_action(self, what: str, card_id: str) -> None:
        actions = {
            NEW: lambda: self._new_kept(),
            IMPORT: lambda: self._import_to_collection(),
            LINK: lambda: self.import_from_link("collection"),
            EXPRESSIONS: lambda: self._collection_expressions(card_id),
            FROM_STORY: lambda: self._collection_from_story(),
            EXPORT_PNG: lambda: self._export_kept(card_id, "png"),
            EXPORT_CHARX: lambda: self._export_kept(card_id, "charx"),
            EXPORT_JSON: lambda: self._export_kept(card_id, "json"),
            DUPLICATE: lambda: self._duplicate_kept(card_id),
            VERSION: lambda: self._new_version(card_id),
            RENAME_VERSION: lambda: self._rename_version(card_id),
            DELETE: lambda: self._delete_kept(card_id),
            STORY_FROM_CARD: lambda: self._story_from_kept(card_id),
            ADD_TO_STORY: lambda: self._add_shown_to_story(card_id),
            CHAT: lambda: self.chat_with_kept(card_id),
            DRAFT: lambda: self.draft_with_kept(card_id),
            BACKUP: lambda: self._backup_collection(),
            RESTORE: lambda: self.restore_collection(),
        }
        action = actions.get(what)
        if action is not None:
            action()

    def _collection_from_story(self) -> None:
        """Import → Characters from the open story…: the story's characters
        saved here (gui/window_card_link.py), the first of them shown."""
        made = self.save_to_collection()
        if made and self.collection_shown():
            self.collection_page.show_character(made[0].id)

    def _collection_changed(self, card_id: str) -> None:
        """A card's fields changed on the page: kept a moment after the last
        keystroke."""
        card = self._kept_card(card_id)
        if card is not None:
            collection.touch(card)
            self._collection_save.start()

    def _collection_pictures_changed(self, card_id: str) -> None:
        """Pictures added, removed or labelled on the page: kept at once, and
        every view of them brought up to date."""
        card = self._kept_card(card_id)
        if card is None:
            return
        collection.touch(card)
        self._save_collection()
        self._forget_unused_pictures()
        self.collection_page.refresh_pictures()
        self._refresh_collection_sheet()

    def _forget_unused_pictures(self) -> None:
        """A picture taken off a card here is gone from the disk too, once no
        character of theirs has it (the review before 3.0.0b2: kept "for an
        Undo of the card" that Your characters doesn't have, the folder only
        grew). Only files the cards were seen to name before are removed: a
        picture still landing was never one of them. Run after every save, so
        a picture added and taken off between two of them is seen too."""
        session = self._collection
        if session is None or self._collection_problem:
            return
        now = {ref.file for card in session.cast for ref in card.reference_images}
        seen = getattr(self, "_collection_pictures_seen", None)
        self._collection_pictures_seen = now
        if seen is None:
            return
        for file in seen - now:
            if file.startswith(f"{REFS_DIR}/"):
                session.pictures.remove(file)

    def _added_to_collection(self, characters: list[Character], notes: list[str], lead: str):
        session = self._collection_session()
        if self._collection_problem and characters:
            # Nothing is kept: never say it was.
            self._warn_plain("Not kept", self._collection_problem)
            return
        session.cast.extend(characters)
        self._save_collection()
        if not characters:
            return
        self.show_collection(True, characters[-1].id)
        if notes:
            self._say_left_out(PAGE_LABEL, lead, notes)
        else:
            self.statusBar().showMessage(lead, 8000)

    def _new_kept(self) -> None:
        name, ok = QInputDialog.getText(self._dialog_parent(), "New character", "Their name:")
        if not ok or not name.strip():
            return
        character = collection.kept(name)
        self._added_to_collection([character], [], f"{character.name} is one of your characters.")
        self.collection_page.show_tab("Card")

    def _clean_picture(self, data: bytes) -> bytes:
        """A picture whose hidden data couldn't be stripped, re-encoded."""
        return clean_incoming({"picture": data}).get("picture", data)

    def _kept_from_card(self, card: TavernCard, came_from: str) -> tuple[Character, list[str]]:
        session = self._collection_session()
        character, notes = collection.kept_from_card(
            card, session.pictures, came_from=came_from, clean=self._clean_picture
        )
        if any(c.name.casefold() == character.name.casefold() for c in self._kept()):
            notes.append(f"You already had a character named {character.name}: both are kept.")
        return character, notes

    def _card_into_collection(self, card: TavernCard, came_from: str) -> None:
        """A SillyTavern card read in (File → Import…, a link): kept among the
        author's characters, its page shown."""
        if self._collection_unwritable():
            return
        character, notes = self._kept_from_card(card, came_from)
        self._added_to_collection([character], notes, f"{character.name} is kept in {PAGE_LABEL}.")

    def _import_to_collection(self, paths: list[str] | None = None) -> None:
        """Cards chosen in a file dialog, or dropped on the page."""
        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(
                self._dialog_parent(), "Import character cards", str(Path.home()), CARD_FILTER
            )
        if not paths or self._collection_unwritable():
            return
        added: list[Character] = []
        notes: list[str] = []
        for path in map(Path, paths):
            try:
                card = read_card(path)
            except CardError as exc:
                notes.append(str(exc))
                continue
            character, said = self._kept_from_card(card, path.name)
            added.append(character)
            notes += [f"{character.name}: {note}" for note in said] if len(paths) > 1 else said
        if not added and notes:
            self._warn_plain("Could not import", "\n".join(notes))
            return
        names = ", ".join(character.name for character in added)
        self._added_to_collection(added, notes, f"Kept in {PAGE_LABEL}: {names}.")

    def _duplicate_kept(self, card_id: str) -> None:
        card = self._kept_card(card_id)
        if card is None:
            return
        copy = collection.copied(
            card,
            self._collection_store(),
            name=collection.unique_name(card.name + collection.COPY_SUFFIX, self._kept()),
            came_from=f"a copy of {card.name}",
        )
        self._added_to_collection([copy], [], f"{copy.name} is a character of its own now.")

    def _new_version(self, card_id: str) -> None:
        card = self._kept_card(card_id)
        if card is None:
            return
        main = collection.main_of(card, self._kept())
        name, ok = QInputDialog.getText(
            self._dialog_parent(),
            "New version",
            f"A whole copy of this card, kept with {main.name}'s main card, to change as you "
            "like. What is this version called?",
            text="Version " + str(len(collection.family_of(main, self._kept())) + 1),
        )
        if not ok or not name.strip():
            return
        version = collection.copied(
            card, self._collection_store(), family=main.id, version=name.strip()
        )
        self._added_to_collection([version], [], f"A new version of {main.name}: {name.strip()}.")

    def _rename_version(self, card_id: str) -> None:
        card = self._kept_card(card_id)
        if card is None or collection.is_main(card, self._kept()):
            return
        name, ok = QInputDialog.getText(
            self._dialog_parent(),
            "Rename this version",
            "What this version is called:",
            text=collection.info(card).version,
        )
        if ok and name.strip():
            collection.info(card).version = name.strip()
            collection.touch(card)
            self._save_collection()
            self._refresh_collection()

    def _delete_kept(self, card_id: str) -> None:
        card = self._kept_card(card_id)
        if card is None:
            return
        family = collection.family_of(card, self._kept())
        main = family[0] is card
        version = collection.info(card).version
        what = card.name if main else f"the version “{version}” of {card.name}"
        text = f"Delete {what}? Their card goes to the Trash as a CHARX file, which can be "
        text += "imported again."
        if main and len(family) > 1:
            text += f"\n\nTheir {len(family) - 1} other version(s) stay; the first becomes the "
            text += "main card."
        linked = stories_linking({card.id}, self.root)
        if linked:
            # A story keeps its character, but no longer in step with a card:
            # said, where it used to break without a word.
            names = ", ".join(f"“{title}”" for title in linked[:5])
            names += f" and {len(linked) - 5} more" if len(linked) > 5 else ""
            text += f"\n\nLinked to this card in {names}: "
            text += "the story keeps its character, no longer linked to a card."
        answer = QMessageBox.question(
            self._dialog_parent(),
            "Delete a character",
            text,
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        store = self._collection_store()
        trashed = self._to_trash(card)
        if not trashed:
            answer = QMessageBox.warning(
                self._dialog_parent(),
                "Delete a character",
                f"The Trash isn't available.\n\nDelete {what} for good? This can't be undone.",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        files = collection.forget(self._collection_session().bundle, card)
        # Their files go only once the folder no longer names them: a save
        # that failed left a card on disk with its pictures gone.
        if self._save_collection():
            for file in files:
                store.remove(file)
        # A main card's heir, or a version's main card: never back to everyone.
        if main:
            self.collection_page.show_character(family[1].id if len(family) > 1 else None)
        else:
            self.collection_page.show_character(family[0].id)
        self._refresh_collection()
        self.statusBar().showMessage(
            f"Deleted {what}" + (" (their card is in the Trash)" if trashed else ""), 8000
        )

    def _to_trash(self, card: Character) -> bool:
        """The card into the desktop's Trash as a CHARX file, every picture
        and file with it; whether it went (no Trash, or a failed write: not)."""
        folder = collection_dir(self.root) / "deleted"
        path = folder / f"{_safe(card.name)}.charx"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            write_bytes_atomic(path, self._charx_of(card, shared=False))
            return bool(move_to_trash(path))
        except (OSError, CardError):
            return False
        finally:
            path.unlink(missing_ok=True)

    # --- several at once (chosen in the list with Ctrl or Shift) ----------------------------

    def _collection_chosen(self, what: str, ids: list[str]) -> None:
        """An action on the characters chosen in the list: each with every
        version of theirs, as the list shows one name for them all."""
        kept = self._kept()
        mains = [card for card_id in ids if (card := self._kept_card(card_id)) is not None]
        if not mains:
            return
        if what == ADD_TO_STORY:
            self.add_from_collection(ticked=[card.id for card in mains])
            return
        families = [collection.family_of(main, kept) for main in mains]
        cards = [card for family in families for card in family]
        if what == DELETE:
            self._delete_chosen(mains, cards)
        elif what in (EXPORT_CHARX, EXPORT_PNG, EXPORT_JSON):
            kind = {EXPORT_CHARX: "charx", EXPORT_PNG: "png", EXPORT_JSON: "json"}[what]
            self._export_chosen(cards, kind)
        elif what == TAG:
            self._tag_chosen(mains, cards)
        elif what == UNTAG:
            self._untag_chosen(mains, cards)

    def _names(self, mains: list[Character]) -> str:
        shown = ", ".join(card.name for card in mains[:6])
        return shown + (f" and {len(mains) - 6} more" if len(mains) > 6 else "")

    def _delete_chosen(self, mains: list[Character], cards: list[Character]) -> None:
        versions = len(cards) - len(mains)
        text = f"Delete {len(mains)} characters ({self._names(mains)})"
        text += (
            f" and their {versions} other version{'s' if versions != 1 else ''}" if versions else ""
        )
        text += "? Each card goes to the Trash as a CHARX file, which can be imported again."
        linked = stories_linking({card.id for card in cards}, self.root)
        if linked:
            names = ", ".join(f"“{title}”" for title in linked[:5])
            names += f" and {len(linked) - 5} more" if len(linked) > 5 else ""
            text += f"\n\nLinked to them in {names}: each story keeps its characters, no "
            text += "longer linked to a card."
        answer = QMessageBox.question(
            self._dialog_parent(),
            "Delete characters",
            text,
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        with _waiting():
            trashed = [self._to_trash(card) for card in cards]
        if not all(trashed):
            answer = QMessageBox.warning(
                self._dialog_parent(),
                "Delete characters",
                f"{trashed.count(False)} of their cards couldn't go to the Trash.\n\nDelete "
                "them all for good anyway? This can't be undone for those.",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        bundle = self._collection_session().bundle
        # Versions before their main card, so no heir is made of one going too.
        files = [
            file
            for card in sorted(cards, key=lambda c: collection.is_main(c, bundle.cast))
            for file in collection.forget(bundle, card)
        ]
        if self._save_collection():
            store = self._collection_store()
            for file in files:
                store.remove(file)
        self.collection_page.show_character(None)
        self._refresh_collection()
        self.statusBar().showMessage(
            f"Deleted {len(mains)} characters"
            + (" (their cards are in the Trash)" if all(trashed) else ""),
            8000,
        )

    def _export_chosen(self, cards: list[Character], kind: str) -> None:
        """Each card, versions too, into a folder chosen, as Export writes one;
        a name used twice gets a number. What couldn't go is said."""
        chosen = QFileDialog.getExistingDirectory(
            self._dialog_parent(), f"Export {len(cards)} cards into a folder", str(Path.home())
        )
        if not chosen:
            return
        folder, kept, taken = Path(chosen), self._kept(), set()
        failed: list[str] = []
        written = 0
        with _waiting():
            for card in cards:
                version = collection.info(card).version
                stem = _safe(f"{card.name} - {version}" if version else card.name)
                name, number = f"{stem}.{kind}", 2
                while name.casefold() in taken or (folder / name).exists():
                    name, number = f"{stem} {number}.{kind}", number + 1
                taken.add(name.casefold())
                try:
                    self._write_kept(card, kind, folder / name)
                    written += 1
                except (OSError, CardError) as exc:
                    failed.append(f"{collection.version_name(card, kept)} {card.name}: {exc}")
        lead = f"Exported {written} card{'s' if written != 1 else ''} to {folder}."
        if failed:
            self._say_left_out("Export characters", lead, failed)
        else:
            self.statusBar().showMessage(lead, 10000)

    def _tag_chosen(self, mains: list[Character], cards: list[Character]) -> None:
        tag, ok = QInputDialog.getText(
            self._dialog_parent(),
            "Add a tag",
            f"A tag for {len(mains)} characters ({self._names(mains)}) and their versions:",
        )
        tag = " ".join(tag.split())
        if not ok or not tag:
            return
        for card in cards:
            if card.tavern is None:
                card.tavern = TavernFields()
            if not collection.has_tag(card, tag):
                card.tavern.tags.append(tag)
                collection.touch(card)
        self.collection_page.tags_changed()
        self._save_collection()
        self._refresh_collection()
        self.statusBar().showMessage(f"Tagged {len(mains)} characters “{tag}”.", 8000)

    def _untag_chosen(self, mains: list[Character], cards: list[Character]) -> None:
        tags = collection.tags_of(cards)
        if not tags:
            self.statusBar().showMessage("None of them has a tag to take off.", 6000)
            return
        tag, ok = QInputDialog.getItem(
            self._dialog_parent(),
            "Take off a tag",
            f"The tag to take off {len(mains)} characters and their versions:",
            tags,
            0,
            False,
        )
        if not ok or not tag:
            return
        for card in cards:
            if card.tavern is not None and collection.has_tag(card, tag):
                card.tavern.tags = [t for t in card.tavern.tags if t.casefold() != tag.casefold()]
                collection.touch(card)
        self.collection_page.tags_changed()
        self._save_collection()
        self._refresh_collection()
        self.statusBar().showMessage(f"Took “{tag}” off {len(mains)} characters.", 8000)

    # --- cards out, and a story from one ------------------------------------------------------

    def _charx_of(self, card: Character, *, shared: bool = True) -> bytes:
        """The character as a CHARX with every picture, labelled. `shared`: it
        leaves this computer (an export), so a picture whose hidden data can't
        be removed stops it; a copy kept here (the Trash's, one read back to
        make a story) takes every picture as it is."""
        store = self._collection_store()
        pictures = []
        for ref in card.reference_images:
            data = store.read(ref.file)
            if data:
                pictures.append((ref, data))
        files = [
            (asset, store.read(asset.file) if asset.held else None)
            for asset in (card.tavern.assets if card.tavern else [])
        ]
        _first, third = card_data(card, origin=MADE_HERE)
        return card_charx(third, pictures, files, shared=shared)

    def _kept_portrait_png(self, card: Character) -> bytes | None:
        if not card.reference_images:
            return None
        image = image_of(self._collection_store().read(card.reference_images[0].file))
        # Encoded afresh whatever it was, and stripped: Qt copies a file's
        # text into what it writes.
        return None if image.isNull() else strip_metadata(encoded(image, "PNG"))

    def _export_kept(self, card_id: str, kind: str) -> None:
        card = self._kept_card(card_id)
        if card is None:
            return
        filters = {
            "png": "PNG card, with the portrait (*.png)",
            "charx": f"CHARX card, with {_everything(card)} (*.charx)",
            "json": "JSON card (*.json)",
        }
        chosen, _ = QFileDialog.getSaveFileName(
            self._dialog_parent(),
            f"Export {card.name}",
            str(Path.home() / f"{_safe(card.name)}.{kind}"),
            filters[kind],
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix.lower() != f".{kind}":
            path = path.with_name(path.name + f".{kind}")
        try:
            self._write_kept(card, kind, path)
        except (OSError, CardError) as exc:
            self._warn_plain("Could not export the card", f"{path}: {exc}")
            return
        self.statusBar().showMessage(f"Saved {card.name}'s card to {path.name}.", 8000)

    def _write_kept(self, card: Character, kind: str, path: Path) -> None:
        """The card written at `path` as a CHARX, PNG or JSON card. Raises
        OSError or CardError (a PNG card needs a portrait)."""
        if kind == "charx":
            write_bytes_atomic(path, self._charx_of(card))
            return
        png = self._kept_portrait_png(card) if kind == "png" else None
        if kind == "png" and png is None:
            raise CardError("their portrait couldn't be read")
        first, third = card_data(card, picture=png is not None, origin=MADE_HERE)
        write_card(path, first, third, png)

    def _story_from_kept(self, card_id: str) -> None:
        """A new story from the character's card, as a SillyTavern card makes
        one (gui/tavern_dialog.py): its scenario the world, a greeting the
        opening. The card goes as a CHARX would, every picture with it."""
        card = self._kept_card(card_id)
        if card is None or self._story_job_says_wait():
            return
        try:
            tavern = tavern_from_file(
                io.BytesIO(self._charx_of(card, shared=False)), f"{card.name}.charx"
            )
        except CardError as exc:
            self._warn_plain("Could not make the story", str(exc))
            return
        if isinstance(tavern, TavernCard):
            self._story_from_card(tavern, kept=card)

    # --- their own lorebook, and expression pictures ------------------------------------------

    def _collection_lorebook(self, what: str, card_id: str) -> None:
        card = self._kept_card(card_id)
        if card is None or card.tavern is None:
            return
        tavern = card.tavern
        # What converting another app's lorebook lost, said once it is kept.
        lost: list[str] = []
        if what == "remove":
            answer = QMessageBox.question(
                self._dialog_parent(),
                "Remove the lorebook",
                f"Take {card.name}'s own lorebook ({tavern.book_entries} entries) off their card?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            tavern.character_book = None
        elif what == "export":
            book = tavern.character_book or {}
            count = len(entries_of(book))
            chosen, _ = QFileDialog.getSaveFileName(
                self._dialog_parent(),
                f"Export {card.name}'s lorebook",
                str(Path.home() / f"{_safe(card.name)} lorebook.json"),
                "SillyTavern lorebooks (*.json)",
            )
            if not chosen:
                return
            path = Path(chosen)
            if path.suffix.lower() != ".json":
                path = path.with_name(path.name + ".json")
            try:
                # Every setting kept, as SillyTavern makes the file from a card.
                write_world(path, book)
            except OSError as exc:
                self._warn_plain("Could not export the lorebook", f"{path}: {exc}")
                return
            self.statusBar().showMessage(f"Saved {count} entries to {path.name}.", 8000)
            return
        else:
            chosen, _ = QFileDialog.getOpenFileName(
                self._dialog_parent(),
                f"A lorebook for {card.name}",
                str(Path.home()),
                "Lorebooks and character cards (*.json *.lorebook *.png *.charx);;All files (*)",
            )
            if not chosen:
                return
            path = Path(chosen)
            book = None
            problem = f"{path.name} has no lorebook."
            try:
                read = open_lorebook(path)
                book, lost = read.book, read.notes()
            except LorebookError as exc:
                if isinstance(exc, UnconvertibleLorebook):
                    # Another app's that can't be converted: why, in words.
                    problem = str(exc)
                other = self._read_card(path, quietly=True)
                book = other.book if other is not None else None
            if book is None:
                self._warn_plain("Could not read the lorebook", problem)
                return
            # Kept whole, every setting of it to edit on the Lorebook tab: a
            # card's own as it came, SillyTavern's lorebook file as SillyTavern
            # itself puts one on a card (another app's once SillyTavern has
            # converted it into one).
            whole = book_from_world(book, name=path.stem)
            count = len(entries_of(whole))
            if tavern.character_book and (
                QMessageBox.question(
                    self._dialog_parent(),
                    "Replace the lorebook?",
                    f"{card.name} has a lorebook of {tavern.book_entries} entries. Replace it "
                    f"with the {count} from {path.name}?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            tavern.character_book = whole
            said = f"{count} entries are {card.name}'s own lorebook now."
            self.statusBar().showMessage(said, 8000)
        collection.touch(card)
        self._save_collection()
        self.collection_page.show_book()
        self._refresh_collection()
        if lost:
            self._say_left_out(f"A lorebook for {card.name}", said, lost)

    def _collection_file(self, what: str, index: int, card_id: str) -> None:
        """One of a card's own files (the Files tab): seen full size, saved
        where the author says, a copy made one of their pictures, renamed,
        or taken off; or new ones added (`index` -1)."""
        card = self._kept_card(card_id)
        if card is None:
            return
        if what == "add":
            self._add_card_files(card)
            return
        if what == "link":
            self._add_card_link(card)
            return
        assets = card.tavern.assets if card.tavern else []
        if not 0 <= index < len(assets):
            return
        if what == "remove":
            self._remove_card_file(card, assets[index])
            return
        if what == "replace":
            if assets[index].held:
                self._replace_card_file(card, index)
            return
        if what == "renamed":
            store = self._collection_store()
            try:
                old = collection.rename_file(card, assets[index], store)
            except OSError as exc:
                # The file stays under its old name, and its record with it.
                self._warn_plain("Couldn't rename the file", str(exc))
                self.collection_page.show_files(index)
                return
            if old is not None:
                collection.touch(card)
                self._save_collection()
                store.remove(old)
                for file in (old, assets[index].file):
                    self.collection_page.forget_file(file)
            return
        if not assets[index].held:
            return
        asset = assets[index]
        store = self._collection_store()
        data = store.read(asset.file)
        if data is None:
            self._warn_plain("The file is missing", f"{asset.name or asset.type}: {asset.file}")
            return
        if what == "whole":
            self._show_whole(f"{card.name} · {asset.name or asset.type}", data)
            return
        if what == "save":
            name = _safe(asset.name or asset.type)
            if not name.lower().endswith(f".{asset.ext}"):
                name = f"{name}.{asset.ext}"
            chosen, _ = QFileDialog.getSaveFileName(
                self._dialog_parent(), f"Save {asset.name or asset.type}", str(Path.home() / name)
            )
            if not chosen:
                return
            try:
                write_bytes_atomic(Path(chosen), data)
            except OSError as exc:
                self._warn_plain("Couldn't save the file", str(exc))
                return
            self.statusBar().showMessage(f"Saved {Path(chosen).name}", 6000)
            return
        if what == "picture":
            data = self._clean_picture(data)
            suffix = picture_kind(data)
            if suffix is None:
                self._warn_plain(
                    "Not a picture SealedLore keeps",
                    f"{asset.name or asset.type} isn't a JPEG, PNG or WebP picture.",
                )
                return
            ref_id = new_id()
            ref = ImageRef(id=ref_id, file=f"{REFS_DIR}/{ref_id}{suffix}", caption=asset.name)
            if asset.type == "emotion":
                ref.expression = asset.name
            store.write(ref.file, data)
            card.reference_images.append(ref)
            self._save_collection()
            self._refresh_collection()
            self.collection_page.refresh_pictures()
            self.statusBar().showMessage(
                f"{asset.name or 'The picture'} is one of {card.name}'s pictures now.", 8000
            )

    def _files_changed(self, card: Character, chosen: int | None = None) -> None:
        collection.touch(card)
        self._save_collection()
        # Read again before they show: one added under the name of a file
        # taken off showed the old one's picture.
        for asset in card.tavern.assets if card.tavern else ():
            if asset.file:
                self.collection_page.forget_file(asset.file)
        self.collection_page.show_files(chosen)
        self._refresh_collection()

    def _add_card_files(self, card: Character | None, chosen: list[str] | None = None) -> None:
        """Files from disk onto their card (chosen, or dropped on the Files
        tab): a picture stripped of its hidden data as any picture coming in
        is (re-encoded where it can't be), a video stripped too, anything
        else kept as it is."""
        if card is None:
            return
        if chosen is None:
            chosen, _ = QFileDialog.getOpenFileNames(
                self._dialog_parent(), f"Files for {card.name}'s card", str(Path.home())
            )
        chosen = [name for name in chosen if Path(name).is_file()]
        if not chosen:
            return
        store = self._collection_store()
        refused: list[str] = []
        for name in chosen:
            path = Path(name)
            read = self._card_file_from(path)
            if isinstance(read, str):
                refused.append(f"{path.name}: {read}")
                continue
            data, ext = read
            collection.add_file(card, store, path.stem, ext, data, TYPES[0][0])
        added = len(chosen) - len(refused)
        if added:
            self._files_changed(card, len(card.tavern.assets) - 1)
            self.statusBar().showMessage(
                f"Added {added} file{'s' if added != 1 else ''} to {card.name}'s card.", 6000
            )
        if refused:
            self._say_left_out("Files for a card", f"Added {added}.", refused)

    def _card_file_from(self, path: Path) -> tuple[bytes, str] | str:
        """A file from disk as a card keeps one: (its bytes, its ending), or
        why it can't be. A picture is stripped of its hidden data as any
        picture coming in is (re-encoded where it can't be), a video stripped
        too, anything else kept as it is."""
        try:
            # No larger than a card read back takes one (a CHARX's files are
            # each read to this): larger, it went out with an export and was
            # lost from the Trash's copy or a story made from it.
            if path.stat().st_size > MAX_INFLATED:
                return f"larger than a card's file may be ({MAX_INFLATED // (1024 * 1024)} MB)."
            data = path.read_bytes()
        except OSError as exc:
            return str(exc)
        ext = path.suffix.lstrip(".")
        if not raster_kind(data):
            return stripped_or_same(data), ext
        # Any picture made of pixels, not only those stripped in place: a GIF,
        # a TIFF or an iPhone's HEIC kept its hidden data (GPS and all) and
        # went out with it in an export.
        data = self._clean_picture(data)
        try:
            data = strip_metadata(data)
        except MetadataError:
            return "its hidden data couldn't be removed."
        return data, (picture_kind(data) or f".{ext}").lstrip(".").replace("jpeg", "jpg")

    def _replace_card_file(self, card: Character, index: int) -> None:
        """A file's content from another on disk (the Files tab's Replace…):
        its name, kind and place on the card kept, so the greetings that show
        it still do."""
        asset = card.tavern.assets[index]
        chosen, _ = QFileDialog.getOpenFileName(
            self._dialog_parent(), f"Replace {asset.name or asset.type}", str(Path.home())
        )
        if not chosen:
            return
        read = self._card_file_from(Path(chosen))
        if isinstance(read, str):
            self._warn_plain("Not replaced", f"{Path(chosen).name}: {read}")
            return
        store = self._collection_store()
        try:
            old = collection.replace_file(card, asset, store, *read)
        except OSError as exc:
            self._warn_plain("Not replaced", str(exc))
            return
        collection.touch(card)
        if self._save_collection() and old is not None:
            store.remove(old)
        for file in (old, asset.file):
            if file:
                self.collection_page.forget_file(file)
        self.collection_page.show_files(index)
        self.statusBar().showMessage(
            f"Replaced {asset.name or asset.type} on {card.name}'s card.", 6000
        )

    def _add_card_link(self, card: Character) -> None:
        address, ok = QInputDialog.getText(
            self._dialog_parent(),
            "Add a link",
            f"The address of a file {card.name}'s card points to (https://…). It is kept as a "
            "link and never fetched.",
        )
        address = address.strip()
        if not ok or not address:
            return
        problem = link_problem(address)
        if problem:
            self._warn_plain("Not added", problem)
            return
        collection.add_link(card, address, TYPES[0][0])
        self._files_changed(card, len(card.tavern.assets) - 1)

    def _remove_card_file(self, card: Character, asset: CardAsset) -> None:
        name = asset.name or asset.type
        answer = QMessageBox.question(
            self._dialog_parent(),
            "Remove this file?",
            f"Take {name} off {card.name}'s card?"
            + (" The file kept for it is deleted." if asset.held else ""),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes or card.tavern is None or asset not in card.tavern.assets:
            return
        row = card.tavern.assets.index(asset)
        card.tavern.assets.remove(asset)
        self._files_changed(card, min(row, len(card.tavern.assets) - 1))
        used = {file for other in self._kept() for file in collection.files_of(other)}
        if asset.held and asset.file not in used:
            self._collection_store().remove(asset.file)

    def _collection_expressions(self, card_id: str) -> None:
        card = self._kept_card(card_id)
        if card is None:
            self.statusBar().showMessage("Choose a character first.", 6000)
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self._dialog_parent(),
            f"Expression pictures for {card.name}",
            str(Path.home()),
            "Expression packs (*.zip);;All files (*)",
        )
        if not chosen:
            return
        try:
            expressions, notes = expressions_from_zip(Path(chosen))
        except CardError as exc:
            self._warn_plain("Could not read the expression pictures", str(exc))
            return
        store = self._collection_store()

        def keep(data: bytes, suffix: str, label: str):
            ref = store_reference(self._clean_picture(data), suffix, store)
            ref.expression = label
            return ref

        refs, skipped = expression_refs(expressions, keep)
        card.reference_images += refs
        if skipped:
            notes.append(f"{skipped} were in a format SealedLore doesn't keep.")
        self._collection_pictures_changed(card.id)
        lead = (
            f"Added {len(refs)} expression picture{'s' if len(refs) != 1 else ''} to "
            f"{card.name}, labelled by their names."
        )
        if notes:
            self._say_left_out("Expression pictures", lead, notes)
        else:
            self.statusBar().showMessage(lead, 8000)

    # --- their pictures, on the sheet -----------------------------------------------------------

    def _collection_board_picture(self, what: str, card_id: str, ref_id: str) -> None:
        card = self._kept_card(card_id)
        ref = next((r for r in card.reference_images if r.id == ref_id), None) if card else None
        if ref is None:
            return
        if what == "whole":
            words = look_words(ref) if (ref.look or ref.expression) else ref.caption
            self._show_whole(
                f"{card.name} · {words}" if words else card.name,
                self._collection_store().read(ref.file),
            )
        elif what in ("edit", "again", "set"):
            self._on_card_picture(card_id, ref_id, what, session=self._collection_session())
        elif what == "labels":
            # Their sheet's rows and columns kept where they are first.
            keep_order(card)
            if edit_labels(self, ref, card.reference_images):
                self._collection_pictures_changed(card_id)
        elif what == "portrait":
            keep_order(card)
            card.reference_images.remove(ref)
            card.reference_images.insert(0, ref)
            self._collection_pictures_changed(card_id)
        elif what == "remove":
            answer = QMessageBox.question(
                self,
                "Remove this picture?",
                f"Take this picture off {card.name}'s card?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer == QMessageBox.Yes and ref in card.reference_images:
                keep_order(card)
                card.reference_images.remove(ref)
                self._collection_pictures_changed(card_id)

    def _collection_fill(self, card_id: str, gaps: list) -> None:
        session = self._collection_session()
        card = self._kept_card(card_id)
        if card is None or self._card_pictures_refused(session):
            return
        requests, waiting = ask_fill_gaps(
            self._dialog_parent(),
            session,
            card,
            gaps,
            catalog=self.image_catalog,
            browse=self._browse_pictures,
        )
        self._drain_side_costs(session)
        self._board_waiting += [(COLLECTION_ID, item) for item in waiting]
        if requests:
            self._start_card_pictures(requests, session)
        if requests or waiting:
            self._refresh_collection_sheet()

    def _collection_files(
        self, card_id: str, look: str, expression: str, *, paths: list | None = None
    ) -> None:
        """Pictures from files onto their card, labelled for the place they
        were asked for or dropped on; each offered for cropping first."""
        card = self._kept_card(card_id)
        if card is None:
            return
        from sealedlore.gui.crop_dialog import cropped_picture

        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(
                self, f"Pictures of {card.name}", str(Path.home()), PICTURE_FILTER
            )
        store = self._collection_store()
        keep_order(card)
        added, problems = 0, []
        for number, path in enumerate(paths, 1):
            counter = f"{number} of {len(paths)}" if len(paths) > 1 else ""
            try:
                picture = cropped_picture(self, Path(path), "portrait", counter=counter)
                if picture is None:
                    continue
                ref = store_reference(*picture, store)
                ref.look, ref.expression = look, expression
                card.reference_images.append(ref)
                added += 1
            except (OSError, ValueError) as exc:
                problems.append(str(exc))
        if problems:
            QMessageBox.warning(self, "Couldn't add", "\n".join(problems))
        if added:
            self._collection_pictures_changed(card_id)

    def _collection_restyle(self, card_id: str) -> None:
        card = self._kept_card(card_id)
        if card is not None:
            self.restyle_pictures([card], session=self._collection_session())
