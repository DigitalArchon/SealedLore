"""The main window's side of SillyTavern's character cards and lorebooks
(storage/tavern_cards.py, storage/tavern_lore.py): a new story from a card
(File → Import…), a card into the open story or out of it (the Cast page,
Story → Export), and lorebooks both ways (the Lore page, Story → Export);
each import from a file or by link (providers/card_links.py, fetched on a
counted thread while a progress dialog offers Stop). A mixin for
`MainWindow`."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QInputDialog,
    QMessageBox,
    QProgressDialog,
)

from sealedlore.gui.image_codec import encoded
from sealedlore.gui.ref_images import clean_incoming, image_of, store_reference
from sealedlore.gui.tavern_dialog import CardImportDialog, LinkDialog
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.card_links import (
    ASK_BYTES,
    Fetched,
    LinkError,
    LinkLarge,
    LinkStopped,
    fetch_link,
    read_link,
)
from sealedlore.storage.image_meta import picture_kind, strip_metadata
from sealedlore.storage.scenario import bundle_from_scenario
from sealedlore.storage.tavern_cards import (
    PORTRAIT_CAPTION,
    CardError,
    TavernCard,
    card_data,
    character_from_card,
    lore_from_card,
    macro_filler,
    read_card,
    scenario_from_card,
    tavern_from_file,
    write_card,
)
from sealedlore.storage.tavern_lore import (
    LorebookError,
    LoreImport,
    lore_from_book,
    read_lorebook,
    write_lorebook,
)
from sealedlore.threads import BackgroundThread

CARD_FILTER = "Character cards (*.png *.json *.charx);;All files (*)"
LINK_FAILED = "Could not import from the link"
NOT_FOR_A_CHAT = "A lorebook is lore for a story"


class _LinkFetch(QObject):
    """One link fetched on a counted thread (sealedlore.threads), its answer
    brought back to the GUI thread by signal (to the window's own methods, so
    queued there). Stopped, it ends at the next piece it reads, or at its
    timeout, and what it brought is closed unread."""

    fetched = Signal(object, object)  # this fetch, Fetched
    failed = Signal(object, str)  # this fetch, the reason in words
    large = Signal(object, object)  # this fetch, the size in bytes or None

    def __init__(self, link: str, parent: QObject, *, large_ok: bool = False) -> None:
        super().__init__(parent)
        self.link = link
        self.large_ok = large_ok
        self.stopped = threading.Event()

    def start(self) -> None:
        link, stopped, large_ok = self.link, self.stopped, self.large_ok

        def run() -> None:
            try:
                result = fetch_link(link, stop=stopped, large_ok=large_ok)
            except LinkStopped:
                return
            except LinkLarge as exc:
                self._say(self.large, exc.size)
                return
            except LinkError as exc:
                self._say(self.failed, str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - the window must hear of it
                self._say(self.failed, f"Something went wrong: {exc}")
                return
            if stopped.is_set() or not self._say(self.fetched, result):
                result.close()

        BackgroundThread(target=run, name="card-link").start()

    def _say(self, signal, value) -> bool:
        try:
            signal.emit(self, value)
        except RuntimeError:  # the window has gone
            return False
        return True

    def stop(self) -> None:
        self.stopped.set()


class TavernWindow:
    def _init_tavern(self) -> None:
        self.cast_panel.card_import_requested.connect(lambda: self.import_card_to_cast())
        self.cast_panel.card_link_requested.connect(lambda: self.import_from_link("cast"))
        self.cast_panel.card_export_requested.connect(self.export_card)
        self.lore_panel.lorebook_import_requested.connect(lambda: self.import_lorebook())
        self.lore_panel.lorebook_link_requested.connect(lambda: self.import_from_link("lore"))
        self.lore_panel.lorebook_export_requested.connect(self.export_lorebook)
        self._link_fetch: _LinkFetch | None = None
        self._link_where = "story"
        self._link_progress: QProgressDialog | None = None

    def _say_left_out(self, title: str, lead: str, notes: list[str]) -> None:
        """What a card or a lorebook held that didn't come along, said once."""
        box = QMessageBox(
            QMessageBox.Information, title, lead, QMessageBox.Ok, self._dialog_parent()
        )
        box.setTextFormat(Qt.PlainText)
        box.setInformativeText("\n".join(f"• {note}" for note in notes))
        box.exec()

    def _read_card(self, path: Path, *, quietly: bool = False) -> TavernCard | None:
        try:
            return read_card(path)
        except CardError as exc:
            if not quietly:
                self._warn_plain("Could not read the card", str(exc))
            return None

    # --- a new story from a card (File → Import…) ------------------------------

    def _import_card(self, path: Path) -> None:
        card = self._read_card(path)
        if card is not None:
            self._story_from_card(card)

    def _story_from_card(self, card: TavernCard) -> None:
        dialog = CardImportDialog(card, self._dialog_parent())
        accepted = dialog.exec() == QDialog.Accepted
        options = dialog.options()
        dialog.deleteLater()
        if not accepted:
            return
        imported = scenario_from_card(card, **options)
        bundle = bundle_from_scenario(imported.scenario)
        bundle.pending_files = clean_incoming(bundle.pending_files)
        if imported.notes:
            self._say_left_out(
                "A new story from a card",
                f"{card.name} is ready to set up. Not everything on the card came along:",
                imported.notes,
            )
        self._open_new_bundle(bundle, review=True)

    # --- a card into the open story, or out of it (the Cast page) --------------

    def _held(self) -> Character | None:
        session = self.session
        held_id = session.story.held_character_id if session is not None else None
        return next((c for c in session.cast if c.id == held_id), None) if held_id else None

    def import_card_to_cast(self, path: Path | None = None) -> None:
        session = self.session
        if session is None or self._busy or self.in_chat:
            return
        if path is None:
            chosen, _ = QFileDialog.getOpenFileName(
                self._dialog_parent(), "Import a character card", str(Path.home()), CARD_FILTER
            )
            if not chosen:
                return
            path = Path(chosen)
        card = self._read_card(path)
        if card is not None:
            self._card_into_story(card)

    def _card_into_story(self, card: TavernCard) -> None:
        """A card's character into the open story, the cast or the supporting
        characters, with its lorebook if wanted."""
        session = self.session
        held = self._held()
        fill = macro_filler(card.name, held.name if held is not None else None)
        character = character_from_card(card, fill)
        lore = lore_from_card(card, fill)
        where = self._ask_where_card_goes(character, len(lore.entries))
        if where is None:
            return
        to_cast, with_lore = where
        notes: list[str] = []
        if card.picture is not None:
            picture = clean_incoming({"card": card.picture})["card"]
            suffix = picture_kind(picture)
            if suffix is not None:
                ref = store_reference(picture, suffix, session.pictures, PORTRAIT_CAPTION)
                character.reference_images.append(ref)
            else:
                notes.append("Its picture is in a format SealedLore doesn't keep.")
        if any(
            c.name.lower() == character.name.lower() for c in (*session.cast, *session.supporting)
        ):
            notes.append(f"The story already had a card named {character.name}: both are kept.")
        written = f"{character.full_description or ''} {character.voice_notes or ''}".lower()
        if held is None and "{{user}}" in written:
            notes.append(
                "Nobody is being played yet, so {{user}} stays as written on the card: "
                "replace it with your character's name."
            )
        if card.greetings or card.text("scenario").strip():
            notes.append(
                "The card's scenario and greetings belong to a story of their own: "
                "File → Import… makes one from the card."
            )
        if with_lore:
            notes += [f"Lorebook: left out {note}." for note in lore.notes()]
        session.add_card(character, to_cast=to_cast, lore=lore.entries if with_lore else ())
        session.forget_review_undo()
        self.refresh_panels()
        self._refresh_people()
        self._refresh_scene_panel()
        self._refresh_composer()
        self._reassemble_inspector()
        self.show_cast_page()
        self.cast_panel.select_character(character.id)
        where_words = "the cast" if to_cast else "the supporting characters"
        if notes:
            self._say_left_out(
                "Import a character card", f"{character.name} joined {where_words}.", notes
            )
        else:
            self.statusBar().showMessage(f"{character.name} joined {where_words}.", 8000)

    def _ask_where_card_goes(self, character: Character, lore: int) -> tuple[bool, bool] | None:
        """(to the cast, with the card's lore), or None for Cancel."""
        box = QMessageBox(self._dialog_parent())
        box.setWindowTitle("Import a character card")
        box.setText(f"Add {character.name} to this story?")
        box.setInformativeText(
            "The cast is on every turn's roster, and the storyteller has their card always; "
            "a supporting character's card goes with the turns that name them."
        )
        cast = box.addButton("Add to the cast", QMessageBox.AcceptRole)
        supporting = box.addButton("Add as a supporting character", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cast)
        with_lore = None
        if lore:
            # The box as its parent: setCheckBox takes no ownership in PySide6.
            with_lore = QCheckBox(f"With the card's {lore} lore entries", box)
            with_lore.setChecked(True)
            box.setCheckBox(with_lore)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (cast, supporting):
            return None
        return clicked is cast, with_lore is not None and with_lore.isChecked()

    def _shown_cards(self) -> list[Character]:
        """The cards the Cast page shows: a plot-hidden one only with spoilers."""
        session = self.session
        if session is None:
            return []
        hidden = set() if self.config.show_plot_spoilers else session.hidden_ids()
        return [c for c in (*session.cast, *session.supporting) if c.id not in hidden]

    def _shown_lore(self) -> list[LoreEntry]:
        session = self.session
        if session is None:
            return []
        hidden = set() if self.config.show_plot_spoilers else session.hidden_ids()
        return [entry for entry in session.bundle.lore if entry.id not in hidden]

    def export_card(self, character_id: str | None = None) -> None:
        """A character as a SillyTavern card: a PNG with their portrait when
        they have one, else JSON."""
        session = self.session
        if session is None or self.in_chat:
            return
        cards = self._shown_cards()
        if character_id is None:
            if not cards:
                self.statusBar().showMessage("The story has no characters to export.", 6000)
                return
            names = [c.name for c in cards]
            chosen, ok = QInputDialog.getItem(
                self._dialog_parent(),
                "Export a character card",
                "Which character:",
                names,
                0,
                False,
            )
            if not ok:
                return
            character_id = cards[names.index(chosen)].id
        character = next((c for c in cards if c.id == character_id), None)
        if character is None:
            return
        png = self._portrait_png(character)
        lore = self._shown_lore()
        with_lore = False
        if lore:
            answer = QMessageBox.question(
                self._dialog_parent(),
                "Export a character card",
                f"Put the story's {len(lore)} lore entries in the card too, as its lorebook?",
            )
            with_lore = answer == QMessageBox.Yes
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in character.name)
        kinds = (
            "PNG card, with the portrait (*.png);;JSON card (*.json)"
            if png
            else "JSON card (*.json)"
        )
        chosen, kind = QFileDialog.getSaveFileName(
            self._dialog_parent(),
            "Export a character card",
            str(Path.home() / f"{safe.strip() or 'character'}{'.png' if png else '.json'}"),
            kinds,
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix.lower() not in (".png", ".json"):
            path = path.with_name(path.name + (".png" if kind.startswith("PNG") else ".json"))
        if path.suffix.lower() == ".png" and png is None:
            path = path.with_suffix(".json")
        first, third = card_data(
            character,
            story_title=session.story.title,
            lore=lore if with_lore else (),
            picture=png is not None and path.suffix.lower() == ".png",
        )
        try:
            write_card(path, first, third, png)
        except (OSError, CardError) as exc:
            self._warn_plain("Could not export the card", f"{path}: {exc}")
            return
        self.statusBar().showMessage(f"Saved {character.name}'s card to {path.name}.", 8000)

    def _portrait_png(self, character: Character) -> bytes | None:
        """The character's first picture as a PNG with nothing hidden in it,
        or None when they have none (or it can't be read)."""
        session = self.session
        if session is None or not character.reference_images:
            return None
        data = session.pictures.read(character.reference_images[0].file)
        if not data:
            return None
        image = image_of(data)
        if image.isNull():
            return None
        # Encoded afresh whatever it was, and stripped: Qt copies a file's
        # text into what it writes.
        return strip_metadata(encoded(image, "PNG"))

    # --- lorebooks (the Lore page) ------------------------------------------------

    def import_lorebook(self, path: Path | None = None) -> None:
        """A SillyTavern lorebook (or a card's own) into the open story's lore."""
        session = self.session
        if session is None or self._busy or self.in_chat:
            return
        if path is None:
            chosen, _ = QFileDialog.getOpenFileName(
                self._dialog_parent(),
                "Import a lorebook",
                str(Path.home()),
                "Lorebooks and character cards (*.json *.png *.charx);;All files (*)",
            )
            if not chosen:
                return
            path = Path(chosen)
        held = self._held()
        user = held.name if held is not None else None
        imported: LoreImport | None = None
        problem = f"{path.name} has no lorebook in it."
        if path.suffix.lower() == ".json":
            try:
                # A lorebook of its own has no {{char}} to name: it stays.
                imported = lore_from_book(read_lorebook(path), macro_filler("{{char}}", user))
            except LorebookError as exc:
                problem = str(exc)
        if imported is None:
            # A card's own lorebook will do as well.
            card = self._read_card(path, quietly=True)
            if card is not None and card.book is not None:
                imported = lore_from_card(card, macro_filler(card.name, user))
        if imported is None:
            self._warn_plain("Could not read the lorebook", problem)
            return
        self._lore_into_story(imported, path.name)

    def _lore_into_story(self, imported: LoreImport, source: str) -> None:
        """A lorebook's entries into the open story's lore; `source` names it."""
        session = self.session
        if not imported.entries:
            self._warn_plain("Import a lorebook", f"{source} has no entries with content.")
            return
        session.add_lore(imported.entries)
        session.forget_review_undo()
        self.refresh_panels()
        self._reassemble_inspector()
        self.right_tabs.setCurrentWidget(self.lore_panel)
        count = len(imported.entries)
        lead = f"Added {count} lore {'entry' if count == 1 else 'entries'} from {source}."
        if imported.notes():
            self._say_left_out("Import a lorebook", lead, imported.notes())
        else:
            self.statusBar().showMessage(lead, 8000)

    def export_lorebook(self) -> None:
        session = self.session
        if session is None or self.in_chat:
            return
        lore = self._shown_lore()
        if not lore:
            self.statusBar().showMessage("The story has no lore to export.", 6000)
            return
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in session.story.title)
        chosen, _ = QFileDialog.getSaveFileName(
            self._dialog_parent(),
            "Export the lore as a lorebook",
            str(Path.home() / f"{safe.strip() or 'story'} lorebook.json"),
            "SillyTavern lorebooks (*.json)",
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix.lower() != ".json":
            path = path.with_name(path.name + ".json")
        try:
            write_lorebook(path, lore)
        except OSError as exc:
            self._warn_plain("Could not export the lorebook", f"{path}: {exc}")
            return
        self.statusBar().showMessage(f"Saved {len(lore)} lore entries to {path.name}.", 8000)

    # --- by link (File → Import from a link…, the Cast and Lore pages) ----------

    def import_from_link(self, where: str = "story") -> None:
        """A card or a lorebook by link. `where`: "story" (File: a card makes a
        new story, a lorebook goes into the open one), "cast" or "lore" (the
        pages' menus: into the open story)."""
        if self._busy or self._link_fetch is not None:
            return
        if where != "story" and (self.session is None or self.in_chat):
            return
        if where == "story" and not self._private_allows_close():
            return
        title, lead = {
            "story": (
                "Import from a link",
                "A SillyTavern character card makes a new story; a lorebook goes into the "
                "open story's lore. Paste the link to its page or its file:",
            ),
            "cast": (
                "Import a card from a link",
                "The card's character joins this story. Paste the link to the card's page "
                "or its file:",
            ),
            "lore": (
                "Import a lorebook from a link",
                "A lorebook, or a card's own, goes into this story's lore. Paste the link to "
                "its page or its file:",
            ),
        }[where]
        dialog = LinkDialog(title, lead, self._dialog_parent())
        accepted = dialog.exec() == QDialog.Accepted
        link = dialog.link()
        dialog.deleteLater()
        if accepted and link:
            self._fetch_link(link, where)

    def _fetch_link(self, link: str, where: str, *, large_ok: bool = False) -> None:
        try:
            host = read_link(link).host
        except LinkError as exc:
            self._warn_plain(LINK_FAILED, str(exc))
            return
        fetch = _LinkFetch(link, self, large_ok=large_ok)
        fetch.fetched.connect(self._on_link_fetched)
        fetch.failed.connect(self._on_link_failed)
        fetch.large.connect(self._on_link_large)
        self._link_fetch, self._link_where = fetch, where
        dialog = QProgressDialog(f"Fetching from {host}…", "Stop", 0, 0, self._dialog_parent())
        dialog.setWindowTitle("SealedLore")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumWidth(420)
        dialog.canceled.connect(self.stop_link_fetch)
        dialog.show()
        self._link_progress = dialog
        fetch.start()

    def stop_link_fetch(self) -> None:
        """Stop waiting for a link: what it brings later is closed unread."""
        fetch, self._link_fetch = self._link_fetch, None
        if fetch is not None:
            fetch.stop()
            fetch.deleteLater()
        dialog, self._link_progress = self._link_progress, None
        if dialog is not None:
            # hide(), not close(): a QProgressDialog's close emits canceled.
            dialog.canceled.disconnect(self.stop_link_fetch)
            dialog.hide()
            dialog.deleteLater()

    def _on_link_failed(self, fetch: _LinkFetch, reason: str) -> None:
        if fetch is not self._link_fetch:
            return
        self.stop_link_fetch()
        self._warn_plain(LINK_FAILED, reason)

    def _on_link_large(self, fetch: _LinkFetch, size: int | None) -> None:
        """A card's file over ASK_BYTES, nothing of it read yet: asked
        about, and fetched again only on a yes."""
        if fetch is not self._link_fetch:
            return
        link, where = fetch.link, self._link_where
        self.stop_link_fetch()
        if self._download_large_card(size):
            self._fetch_link(link, where, large_ok=True)

    def _download_large_card(self, size: int | None) -> bool:
        megabytes = f"{size / (1024 * 1024):.0f} MB" if size else None
        box = QMessageBox(self._dialog_parent())
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("A large card")
        box.setText(
            f"This card's file is {megabytes}."
            if megabytes
            else f"This card's file is over {ASK_BYTES // (1024 * 1024)} MB."
        )
        box.setInformativeText(
            "A card is its text and a picture, a few megabytes at most: the rest is "
            "pictures for other apps, such as a set of expressions. SealedLore keeps only "
            "the card's text and its main picture, but has to download the whole file "
            "to get them."
        )
        download = box.addButton(
            f"Download {megabytes}" if megabytes else "Download it", QMessageBox.AcceptRole
        )
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        return box.clickedButton() is download

    def _on_link_fetched(self, fetch: _LinkFetch, result: Fetched) -> None:
        if fetch is not self._link_fetch:
            result.close()
            return
        where = self._link_where
        self.stop_link_fetch()
        try:
            found = tavern_from_file(
                result.file, result.name, result.picture, lorebook=result.lorebook
            )
        except CardError as exc:
            self._warn_plain(LINK_FAILED, str(exc))
            return
        finally:
            result.close()
        if self._busy:
            return
        if isinstance(found, TavernCard):
            self._card_from_link(found, where)
        else:
            self._lorebook_from_link(found, result.name)

    def _card_from_link(self, card: TavernCard, where: str) -> None:
        if where == "story":
            self._story_from_card(card)
        elif self.session is None or self.in_chat:
            return
        elif where == "cast":
            self._card_into_story(card)
        elif card.book is None:
            self._warn_plain("Import a lorebook", f"{card.name}'s card has no lorebook in it.")
        else:
            held = self._held()
            fill = macro_filler(card.name, held.name if held is not None else None)
            self._lore_into_story(lore_from_card(card, fill), f"{card.name}'s card")

    def _lorebook_from_link(self, book: Mapping[str, Any], name: str) -> None:
        if self.session is None or self.in_chat:
            self._warn_plain(
                NOT_FOR_A_CHAT,
                "That link is a SillyTavern lorebook. Open the story it is for, then import "
                "it again from File → Import from a link… or its Lore page.",
            )
            return
        held = self._held()
        # A lorebook of its own has no {{char}} to name: it stays.
        imported = lore_from_book(book, macro_filler("{{char}}", held.name if held else None))
        title = book.get("name")
        self._lore_into_story(imported, title if isinstance(title, str) and title else name)
