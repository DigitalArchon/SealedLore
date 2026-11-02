"""The main window's side of SillyTavern's character cards and lorebooks
(storage/tavern_cards.py, storage/tavern_lore.py): a new story from a card
(File → Import…), a card into the open story or out of it (the Cast page,
Story → Export), and lorebooks both ways (the Lore page, Story → Export).
A mixin for `MainWindow`."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QFileDialog, QInputDialog, QMessageBox

from sealedlore.gui.image_codec import encoded
from sealedlore.gui.ref_images import clean_incoming, image_of, store_reference
from sealedlore.gui.tavern_dialog import CardImportDialog
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
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
    write_card,
)
from sealedlore.storage.tavern_lore import (
    LorebookError,
    lore_from_book,
    read_lorebook,
    write_lorebook,
)

CARD_FILTER = "Character cards (*.png *.json *.charx);;All files (*)"


class TavernWindow:
    def _init_tavern(self) -> None:
        self.cast_panel.card_import_requested.connect(lambda: self.import_card_to_cast())
        self.cast_panel.card_export_requested.connect(self.export_card)
        self.lore_panel.lorebook_import_requested.connect(lambda: self.import_lorebook())
        self.lore_panel.lorebook_export_requested.connect(self.export_lorebook)

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
        if card is None:
            return
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
        if card is None:
            return
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
        imported, problem = None, f"{path.name} has no lorebook in it."
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
        if not imported.entries:
            self._warn_plain("Import a lorebook", f"{path.name} has no entries with content.")
            return
        session.add_lore(imported.entries)
        session.forget_review_undo()
        self.refresh_panels()
        self._reassemble_inspector()
        self.right_tabs.setCurrentWidget(self.lore_panel)
        count = len(imported.entries)
        lead = f"Added {count} lore {'entry' if count == 1 else 'entries'} from {path.name}."
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
