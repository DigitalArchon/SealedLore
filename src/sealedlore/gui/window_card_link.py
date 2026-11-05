"""The window's side of a story's characters and the author's own
(engine/card_link.py): characters put into the open story from Your
characters (the Cast page, so also the guide's and Setup's Characters step,
and Your characters' page), the Cast page's line about a character's card,
and what the author does with what changed (gui/card_changes.py). A mixin for
`MainWindow`.

Pictures are compared by their bytes, each file hashed once (a picture's file
never changes under its name). Writing to Your characters goes through their
own session (gui/window_collection.py), the one writer of their folder.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

from sealedlore.engine import card_link
from sealedlore.engine.card_link import changes, picture_mark
from sealedlore.gui.card_changes import KEEP, SAVE, TAKE, VERSION, CardChangesDialog
from sealedlore.gui.collection_picker import CollectionPicker
from sealedlore.gui.ref_images import thumbnail_of
from sealedlore.ids import new_id
from sealedlore.models.character import Character
from sealedlore.models.collection import PictureMark
from sealedlore.models.image import ImageRef
from sealedlore.storage import collection
from sealedlore.storage.image_meta import picture_kind
from sealedlore.storage.images import REFS_DIR
from sealedlore.storage.paths import COLLECTION_ID
from sealedlore.storage.picture_store import PictureStore
from sealedlore.storage.tavern_cards import macro_filler
from sealedlore.storage.tavern_lore import lore_from_book

KEEP_LABEL = "Keep in Your characters…"
CHANGES_LABEL = "Changes…"
CARD_LABEL = "Their card…"
# The Cast page's line, again a moment after the last edit (it reads pictures).
LINK_DELAY_MS = 500


def copy_picture(ref: ImageRef, source: PictureStore, target: PictureStore) -> ImageRef | None:
    """A picture's file copied from one store to another under a new name,
    its labels and how it was drawn with it; None when it can't be read."""
    data = source.read(ref.file)
    suffix = picture_kind(data) if data else None
    if data is None or suffix is None:
        return None
    copy = ref.model_copy()
    copy.id = new_id()
    copy.file = f"{REFS_DIR}/{copy.id}{suffix}"
    # The drawing it was taken from is the other folder's.
    copy.drawn_as = ""
    target.write(copy.file, data)
    return copy


@dataclass
class Sides:
    """A linked character's pictures and their card's, by their bytes."""

    story_marks: list[PictureMark]
    story_refs: dict[str, ImageRef]
    card_marks: list[PictureMark]
    card_refs: dict[str, ImageRef]
    # A side with a picture whose file can't be read.
    story_unknown: bool = False
    card_unknown: bool = False

    def changes(self, character: Character, card: Character) -> list:
        return changes(
            character,
            card,
            self.story_marks,
            self.card_marks,
            story_unknown=self.story_unknown,
            card_unknown=self.card_unknown,
        )

    def in_step(self, character: Character, card: Character) -> None:
        card_link.in_step(
            character,
            card,
            self.story_marks,
            self.card_marks,
            story_unknown=self.story_unknown,
            card_unknown=self.card_unknown,
        )


class CardLinkWindow:
    def _init_card_link(self) -> None:
        panel = self.cast_panel
        panel.collection_add_requested.connect(self.add_from_collection)
        panel.link_requested.connect(self._card_link_action)
        panel.current_changed.connect(lambda _card_id: self._sync_card_link())
        self._link_timer = QTimer(self)
        self._link_timer.setSingleShot(True)
        self._link_timer.setInterval(LINK_DELAY_MS)
        self._link_timer.timeout.connect(self._sync_card_link)
        panel.changed.connect(self._link_timer.start)
        panel.pictures.changed.connect(self._link_timer.start)
        # (store's owner, file) → the sha256 of its bytes.
        self._shas: dict[tuple[str, str], str] = {}

    # --- pictures by their bytes ------------------------------------------------------------

    def _marks(
        self, refs: list[ImageRef], store: PictureStore, owner: str
    ) -> tuple[list[PictureMark], dict[str, ImageRef], bool]:
        """Each picture's mark, in order, the pictures by sha256, and whether
        any picture's file couldn't be read (which is then never said to
        have been taken off: engine/card_link.py)."""
        marks: list[PictureMark] = []
        by_sha: dict[str, ImageRef] = {}
        unknown = False
        for ref in refs:
            sha = self._shas.get((owner, ref.file))
            if sha is None:
                data = store.read(ref.file)
                if not data:
                    unknown = True
                    continue
                sha = hashlib.sha256(data).hexdigest()
                self._shas[(owner, ref.file)] = sha
            mark = PictureMark(
                sha256=sha,
                look=ref.look,
                expression=ref.expression,
                caption=ref.caption,
                framing=ref.framing,
            )
            if sha not in by_sha:
                marks.append(mark)
                by_sha[sha] = ref
        return marks, by_sha, unknown

    def _sides(self, character: Character, card: Character) -> Sides:
        session = self.session
        story = self._marks(character.reference_images, session.pictures, session.story.id)
        theirs = self._marks(card.reference_images, self._collection_store(), COLLECTION_ID)
        return Sides(*story[:2], *theirs[:2], story[2], theirs[2])

    # --- putting them into the story ------------------------------------------------------

    def _link_refused(self) -> bool:
        session = self.session
        if session is None or session.story.chat:
            return True
        if self._busy:
            self.statusBar().showMessage(
                "Characters can be changed once the story's current job is done.", 6000
            )
            return True
        if session.in_private:
            # A memory-only scene's changes must never reach the disk.
            self.statusBar().showMessage("Not in a private scene.", 6000)
            return True
        return False

    def add_from_collection(self, ticked: Sequence[str] = ()) -> None:
        """Characters from Your characters into the open story (the Cast
        page's From Your characters…; `ticked`, those chosen on Your
        characters' page, ticked to begin with)."""
        if self._link_refused():
            return
        picker = CollectionPicker(
            self._kept(),
            self._collection_pictures,
            title="From Your characters",
            lead="Who joins this story? Each comes with their pictures, and what the story "
            "changes of them can go back to their card.",
            action="Add to the story",
            ticked=ticked,
            parent=self._dialog_parent(),
        )
        accepted = picker.exec() == QDialog.Accepted
        chosen, (to_cast, with_lore) = picker.chosen(), picker.options()
        picker.deleteLater()
        chosen = self._not_twice(chosen) if accepted else []
        if not chosen:
            return
        notes: list[str] = []
        added = [self._kept_into_story(card, to_cast, with_lore, notes) for card in chosen]
        self._after_cards_added(added, to_cast, notes)

    def _not_twice(self, cards: list[Character]) -> list[Character]:
        """`cards`, less any the story already has from the same card unless the
        author wants them again (they used to come in twice without a word)."""
        session = self.session
        linked = {
            character.linked.card_id
            for character in (*session.cast, *session.supporting)
            if character.linked is not None
        }
        again = [card for card in cards if card.id in linked]
        if not again:
            return cards
        names = ", ".join(card.name for card in again)
        answer = QMessageBox.question(
            self._dialog_parent(),
            "Already in this story",
            f"This story already has {names} from the same card. Add them again as well?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            return cards
        return [card for card in cards if card.id not in linked]

    def _kept_into_story(
        self, card: Character, to_cast: bool, with_lore: bool, notes: list[str] | None = None
    ) -> Character:
        """One of the author's characters as the open story's: a linked copy,
        their pictures copied in, their own lorebook as lore if wanted (what
        it held that a story doesn't use is added to `notes`)."""
        session = self.session
        user = self._story_user()
        copy = card_link.story_copy(card, self._kept(), user)
        source = self._collection_store()
        marks = []
        for ref in card.reference_images:
            new = copy_picture(ref, source, session.pictures)
            if new is not None:
                copy.reference_images.append(new)
                data = session.pictures.read(new.file) or b""
                marks.append(picture_mark(ref, data))
        copy.linked.pictures = marks
        lore = []
        book = card.tavern.character_book if card.tavern else None
        if with_lore and book:
            imported = lore_from_book(book, macro_filler(card.name, user))
            lore = imported.entries
            if notes is not None:
                notes += [f"{card.name}'s lorebook: left out {note}." for note in imported.notes()]
        session.add_card(copy, to_cast=to_cast, lore=lore)
        return copy

    def _story_user(self) -> str | None:
        """Whom a card's `{{user}}` becomes in the open story: the character
        played, else the one the story suggests (the guide and Setup come
        before anyone is played), else nobody yet: then it is written out
        when the story begins (`name_kept_users`)."""
        held = self._held()
        if held is not None:
            return held.name
        session = self.session
        suggested = session.story.setup.suggested_character_id if session else None
        card = session.card(suggested) if suggested else None
        return card.name if isinstance(card, Character) else None

    def name_kept_users(self) -> None:
        """The story's characters from Your characters put in before anyone
        was played: `{{user}}` written out as whom the author plays now."""
        session, held = self.session, self._held()
        if session is None or held is None:
            return
        named = [
            character
            for character in (*session.cast, *session.supporting)
            if card_link.name_the_user(character, held.name)
        ]
        # Their lore too, and any lore a lorebook brought in before anyone was
        # played: left as {{user}}, it read so to the storyteller, and a
        # keyword written so could never match.
        fill = macro_filler("{{char}}", held.name)
        for entry in session.bundle.lore:
            title, content = fill(entry.title), fill(entry.content)
            keywords = [fill(keyword) for keyword in entry.keywords]
            if (title, content, keywords) != (entry.title, entry.content, entry.keywords):
                entry.title, entry.content, entry.keywords = title, content, keywords
                named.append(entry)
        if named:
            session.save()

    def _after_cards_added(
        self, added: list[Character], to_cast: bool, notes: list[str] | None = None
    ) -> None:
        session = self.session
        session.forget_review_undo()
        self.refresh_panels()
        self._refresh_people()
        self._refresh_scene_panel()
        self._refresh_composer()
        self._reassemble_inspector()
        if not self.collection_shown():
            self.show_cast_page()
        self.cast_panel.select_character(added[0].id)
        names = ", ".join(character.name for character in added)
        where = "the cast" if to_cast else "the supporting characters"
        later = (
            " Their card's {{user}} becomes the name of whoever you play when the story begins."
            if any(c.linked is not None and not c.linked.user for c in added)
            else ""
        )
        if notes:
            self._say_left_out("Into the story", f"{names} joined {where}.{later}", notes)
        else:
            self.statusBar().showMessage(f"{names} joined {where}.{later}", 12000)

    def _add_shown_to_story(self, card_id: str) -> None:
        """Your characters' page: the character shown into the open story."""
        card = self._kept_card(card_id)
        if card is None or self._link_refused() or not self._not_twice([card]):
            return
        where = self._ask_where_card_goes(card, card.tavern.book_entries if card.tavern else 0)
        if where is None:
            return
        to_cast, with_lore = where
        notes: list[str] = []
        added = self._kept_into_story(card, to_cast, with_lore, notes)
        self._after_cards_added([added], to_cast, notes)

    # --- the Cast page's line ----------------------------------------------------------------

    def _sync_card_link(self) -> None:
        panel = self.cast_panel
        character = panel.current
        if character is None or self.session is None or self.in_chat:
            panel.set_link("", "")
            return
        link = character.linked
        if link is None:
            panel.set_link("", KEEP_LABEL)
            return
        card = self._kept_card(link.card_id)
        if card is None:
            panel.set_link(
                "Their card is no longer in Your characters (or came from another computer).",
                KEEP_LABEL,
            )
            return
        found = self._sides(character, card).changes(character, card)
        which = collection.version_name(card, self._kept()).lower()
        words = f"From Your characters: {card.name} ({which})."
        line = card_link.summary(found, link)
        panel.set_link(
            f"{words} {line}" if line else f"{words} In step with their card.",
            CHANGES_LABEL if found else CARD_LABEL,
        )

    def _card_link_action(self, character_id: str) -> None:
        session = self.session
        character = session.card(character_id) if session is not None else None
        if not isinstance(character, Character) or self._link_refused():
            return
        # What the button offered may be out of date: the line is worked out
        # again and acted on, but a card deleted meanwhile is never made
        # again by a click that meant to open it.
        offered = self.cast_panel.link_button.text()
        self._sync_card_link()
        now = self.cast_panel.link_button.text()
        if now == KEEP_LABEL and offered != KEEP_LABEL:
            self.statusBar().showMessage(
                "Their card is no longer in Your characters: see the line above.", 8000
            )
            return
        link = character.linked
        card = self._kept_card(link.card_id) if link is not None else None
        if card is None:
            self.keep_in_collection(character)
            return
        if now == CARD_LABEL:
            if self._dialog_parent() is not self:
                # The Cast page lent to Setup or the guide: the page would
                # open behind the dialog.
                self.statusBar().showMessage(
                    "Their card is in File → Your characters…, once this is closed.", 8000
                )
                return
            self.show_collection(True, card.id)
            return
        self._review_card_changes(character, card)

    # --- what changed ------------------------------------------------------------------------

    def _review_card_changes(self, character: Character, card: Character) -> None:
        session = self.session
        sides = self._sides(character, card)
        sides.in_step(character, card)
        found = sides.changes(character, card)
        story_marks, story_refs = sides.story_marks, sides.story_refs
        card_refs = sides.card_refs
        sources = [(story_refs, session.pictures), (card_refs, self._collection_store())]

        def thumb(sha: str, side: str) -> QPixmap | None:
            """A picture seen from `side` ("story" or "card"), read from
            whichever side has it."""
            for refs, store in sources if side == "story" else sources[::-1]:
                ref = refs.get(sha)
                if ref is not None:
                    return thumbnail_of(store.read(ref.file), 140)
            return None

        which = collection.version_name(card, self._kept()).lower()
        dialog = CardChangesDialog(
            character.name, f"their card ({which})", found, thumb, self._dialog_parent()
        )
        accepted = dialog.exec() == QDialog.Accepted
        choice, chosen = dialog.choice, dialog.chosen()
        dialog.deleteLater()
        if not accepted:
            return
        collection_store = self._collection_store()
        if choice == SAVE and chosen:
            card_link.save_to_card(
                character,
                card,
                chosen,
                story_refs=story_refs,
                card_refs=card_refs,
                copy=lambda ref: copy_picture(ref, session.pictures, collection_store),
            )
            collection.touch(card)
            self._save_collection()
            session.save()
            # Both sides changed: the story's character too, when a summary it
            # never touched follows the description saved (`_follow_derived`).
            self.refresh_panels()
            self.cast_panel.select_character(character.id)
            if self.collection_shown():
                self.collection_page.card_changed(card.id)
            self.statusBar().showMessage(f"Saved to {card.name}'s card.", 8000)
        elif choice == VERSION:
            self._version_from_story(character, card, story_marks)
        elif choice == KEEP:
            character.linked.kept_here = card_link.fingerprint(found)
            session.save()
        elif choice == TAKE and chosen:
            old_name = character.name
            card_link.take_from_card(
                character,
                card,
                chosen,
                story_refs=story_refs,
                card_refs=card_refs,
                copy=lambda ref: copy_picture(ref, collection_store, session.pictures),
            )
            # A name taken in keeps their place on the rosters, as a rename
            # on the Cast page does.
            session.rename_on_rosters(character, old_name)
            session.forget_review_undo()
            session.save()
            self.refresh_panels()
            self._refresh_people()
            self._reassemble_inspector()
            self.cast_panel.select_character(character.id)
            self.statusBar().showMessage(
                f"{character.name} took their card's changes into this story.", 8000
            )
        self._refresh_collection()
        self._sync_card_link()

    def _version_from_story(
        self, character: Character, card: Character, story_marks: list[PictureMark]
    ) -> None:
        session = self.session
        kept = self._kept()
        main = collection.main_of(card, kept)
        name, ok = QInputDialog.getText(
            self._dialog_parent(),
            "Save as a new version",
            f"{character.name} as this story has them becomes a new version of {main.name}'s "
            "card, kept beside it. What is this version called?",
            text=session.story.title,
        )
        if not ok or not name.strip():
            return
        version = card_link.version_from_story(character, card, kept, name=name.strip())
        version.collection.came_from = f"the story {session.story.title}"
        store = self._collection_store()
        collection.copy_files(version, store)
        for ref in character.reference_images:
            new = copy_picture(ref, session.pictures, store)
            if new is not None:
                version.reference_images.append(new)
        self._collection_session().cast.append(version)
        card_link.relink(character, version, story_marks, main_id=main.id)
        self._save_collection()
        session.save()
        self.statusBar().showMessage(
            f"A new version of {main.name}: {name.strip()}. This story's {character.name} is "
            "that version now.",
            10000,
        )

    def keep_in_collection(self, character: Character) -> None:
        """A story's character kept among the author's own, and linked to the
        card made (the Cast page's Keep in Your characters…)."""
        session = self.session
        if session is None or self._link_refused() or self._collection_unwritable():
            return
        kept = card_link.kept_from_story(character, f"the story {session.story.title}")
        kept.name = character.name
        store = self._collection_store()
        for ref in character.reference_images:
            new = copy_picture(ref, session.pictures, store)
            if new is not None:
                kept.reference_images.append(new)
        if any(c.name.casefold() == kept.name.casefold() for c in self._kept()):
            self.statusBar().showMessage(
                f"You already had a character named {kept.name}: both are kept.", 8000
            )
        self._collection_session().cast.append(kept)
        story_marks, _refs, _unknown = self._marks(
            character.reference_images, session.pictures, session.story.id
        )
        card_link.relink(character, kept, story_marks, main_id=kept.id)
        self._save_collection()
        session.save()
        self._sync_card_link()
        # Listed at once, if the page is showing (Setup's Characters tab).
        self._refresh_collection()
        self.statusBar().showMessage(
            f"{character.name} is kept in Your characters, and linked to this story's.", 8000
        )
