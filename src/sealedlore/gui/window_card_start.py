"""Starting from one of the author's own characters (Your characters): a
simple chat with them (gui/card_chat_dialog.py, engine/card_chat.py), or a
story drafted around them from a premise (gui/generate_dialog.py,
engine/drafting.py). A mixin for `MainWindow`."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog

from sealedlore.engine import card_chat
from sealedlore.engine.prompt_edits import texts_in_force
from sealedlore.gui.card_chat_dialog import CardChatDialog
from sealedlore.gui.collection_picker import CollectionPicker
from sealedlore.ids import new_id
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.story import Story
from sealedlore.storage.image_meta import picture_kind
from sealedlore.storage.images import REFS_DIR
from sealedlore.storage.repository import StoryBundle, save_story_bundle


class CardStartWindow:
    def chat_with_kept(self, card_id: str) -> None:
        """Chat with them…: a simple chat started from their card."""
        card = self._kept_card(card_id)
        if card is None or self._story_job_says_wait():
            return
        provider = self.config.active_provider()
        dialog = CardChatDialog(
            card,
            texts=texts_in_force(self.config.prompt_edits),
            model=provider.model if provider is not None else "",
            browse=self._browse_models,
            parent=self,
            endpoint=None if self.use_mock else provider,
            config=None if self.use_mock else self.config,
            browse_route=self._browse_models_and_route,
            hosts_of=self._hosts_of,
            saved=self.saved_prompts(),
        )
        accepted = dialog.exec() == QDialog.Accepted
        setup, keep = dialog.setup(), dialog.keep_choice()
        model, route = dialog.model.text().strip(), dialog.route()
        dialog.deleteLater()
        # Asked once the chat is wanted: asked first, a Cancel of the dialog
        # had already cost a memory-only scene or chat.
        if not accepted or not self._leave_for_new():
            return
        story = Story(
            title=setup.title or card.name,
            mode="chat",
            chat_prompt=setup.system,
            chat_tail=setup.tail,
            chat_keep=keep,
        )
        story.setup.description = f"A chat with {card.name}, from Your characters."
        story.defaults.main_model = model
        story.defaults.main_route = route
        nodes = card_chat.opening(setup.first, setup.greeting)
        if nodes:
            nodes[0].meta.scene = story.scene.model_copy(deep=True)
            story.active_leaf_id = nodes[-1].id
        bundle = StoryBundle(story=story, nodes=nodes)
        files = self._portrait_for_chat(card, story)
        self.show_collection(False)
        if keep == "memory":
            self.open_memory_chat(bundle, files=files)
        else:
            bundle.pending_files = files
            save_story_bundle(bundle, root=self.root)
            self.stories.refresh(selected_id=story.id)
            self.open_story(story.id)
        if self.session is None or self.session.story.id != story.id:
            return
        if setup.greeting is None and setup.first:
            if self.session.tee_block:
                # A TEE or encrypted model still being attested refuses
                # anything sent: their card waits in the box to type in.
                self.composer.input.setPlainText(setup.first)
                self.statusBar().showMessage(
                    "Their card is in the box to type in: send it once the model is attested.",
                    15000,
                )
                return
            # No greeting: their card goes as the first message, answered.
            self._submit_turn(setup.first, self._chat_turn(setup.first))

    def _leave_for_new(self) -> bool:
        """Before something new opens: a memory-only scene or chat asks, and
        on Yes is left at once (else it asked again as the new one opened)."""
        if not self._private_allows_close():
            return False
        if self.session is not None and self.session.memory_only:
            self._close_story()
        return True

    def _portrait_for_chat(self, card: Character, story: Story) -> dict[str, bytes]:
        """Their portrait as the chat's own reference picture, so a picture
        of the chat draws them as they look."""
        if not card.reference_images:
            return {}
        data = self._collection_pictures().read(card.reference_images[0].file)
        suffix = picture_kind(data) if data else None
        if data is None or suffix is None:
            return {}
        ref_id = new_id()
        ref = ImageRef(id=ref_id, file=f"{REFS_DIR}/{ref_id}{suffix}", caption=card.name)
        story.reference_images.append(ref)
        return {ref.file: data}

    def draft_with_kept(self, card_id: str) -> None:
        """Draft a story with them…: a story drafted from a premise around them."""
        card = self._kept_card(card_id)
        if card is not None and not self._story_job_says_wait():
            self.new_story_from_premise(characters=[card])

    def _story_job_says_wait(self) -> bool:
        """What leaves the open story waits for its job, and says so: the
        page stays usable while the story works."""
        if self._busy:
            self.statusBar().showMessage(
                "Once the story's current job is done: it is still writing.", 6000
            )
            return True
        return False

    def _choose_kept(self, chosen: list[Character]) -> list[Character] | None:
        """Characters from Your characters for a draft: those already chosen
        ticked; None for Cancel."""
        picker = CollectionPicker(
            self._kept(),
            self._collection_pictures,
            title="Build the story around",
            lead="Whom is the story about? Their cards stand as they are; the draft writes the "
            "world, the other characters and the opening around them.",
            action="Choose",
            placing=False,
            parent=self._dialog_parent(),
        )
        for card in chosen:
            picker.check(card.id)
        accepted = picker.exec() == QDialog.Accepted
        result = picker.chosen()
        picker.deleteLater()
        return result if accepted else None
