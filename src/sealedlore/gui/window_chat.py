"""The main window's side of a simple chat (engine/chat.py). A mixin for `MainWindow`.

A chat uses the window with the storytelling taken out: the composer is a text
box and the Private button, the inspector shows Story so far, Images and
Prompt, and Setup is the system prompt. A part of it can be held in private
as a story's scene can (window_private). A chat on a TEE model is attested
when it opens, as a private scene is, and has a private scene's look; one
kept in memory only is never written anywhere and asks before it is closed.
"""

from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.gui.new_chat_dialog import NewChatDialog
from sealedlore.models.node import CHAT_USER_ID
from sealedlore.models.story import Story
from sealedlore.providers.base import ChatProvider
from sealedlore.providers.private_mode import PrivateModeProvider
from sealedlore.providers.tee import is_private_mode, move_refusal
from sealedlore.storage.repository import StoryBundle, save_story_bundle

MEMORY_TITLE = "(in memory only)"


class ChatWindow:
    _sealed_provider: PrivateModeProvider | None = None

    def _chat_actions(self, file_menu) -> None:
        """Called from `_build_actions`: File → New simple chat…"""
        self.new_chat_action = QAction("New simple &chat…", self)
        self.new_chat_action.setStatusTip(
            "A plain conversation with the model: your system prompt and your messages"
        )
        self.new_chat_action.triggered.connect(self.new_chat)
        file_menu.addAction(self.new_chat_action)

    # --- state ---------------------------------------------------------------

    @property
    def in_chat(self) -> bool:
        return self.session is not None and self.session.story.chat

    @property
    def memory_chat(self) -> bool:
        return self.session is not None and self.session.memory_only

    # --- starting one ------------------------------------------------------------

    def new_chat(self) -> None:
        if self._busy or not self._private_allows_close():
            return
        provider = self.config.active_provider()
        dialog = NewChatDialog(
            model=provider.model if provider is not None else "",
            browse=self._browse_models,
            parent=self,
            endpoint=None if self.use_mock else provider,
            browse_route=self._browse_models_and_route,
            hosts_of=self._hosts_of,
        )
        if dialog.exec() != NewChatDialog.Accepted:
            return
        story = Story(
            title=dialog.title.text().strip(),
            mode="chat",
            chat_prompt=dialog.prompt.toPlainText().strip(),
            chat_tail=dialog.tail.toPlainText().strip(),
            chat_keep=dialog.keep_choice(),
        )
        story.defaults.main_model = dialog.model.text().strip()
        # The chat's own route: on the chat, never in the settings (a chat
        # kept in memory teaches them nothing).
        story.defaults.main_route = dialog.route()
        bundle = StoryBundle(story=story)
        if story.chat_keep == "memory":
            self.open_memory_chat(bundle)
            return
        save_story_bundle(bundle, root=self.root)
        self.stories.refresh(selected_id=story.id)
        self.open_story(story.id)

    def open_memory_chat(
        self,
        bundle: StoryBundle,
        *,
        files: dict[str, bytes] | None = None,
        log: list[dict] | None = None,
    ) -> None:
        """A chat that is never written: its session is made here, from the
        bundle in memory, where every other story is loaded from disk.
        `files` and `log`: its pictures and log, from a backup."""
        if self.session is not None:
            self.session.close(wait_seconds=0)
        self.session = StorySession(
            bundle,
            self.config,
            self._current_provider(),
            root=self.root,
            estimator=self.estimator,
            # What a chat's turns teach the token estimate goes in the config
            # file; an incognito chat leaves nothing, that included.
            learn_corrections=False,
        )
        if files or log:
            self.session.restore_memory(files or {}, log or [])
        self._after_open()

    # --- the window in a chat ----------------------------------------------------

    def _chat_turn(self, text: str) -> TurnRequest:
        return TurnRequest(speaker_id=CHAT_USER_ID, user_text=text)

    def _sync_chat_ui(self) -> None:
        """Show what a chat has and nothing else; restore it all for a story."""
        chat = self.in_chat
        self.composer.set_chat_mode(chat)
        self.view_toggle.set_chat(chat)
        for page in (self.scene_panel, self.cast_panel, self.lore_panel, self.style_panel):
            self.right_tabs.setTabVisible(self.right_tabs.indexOf(page), not chat)
        if chat and self.right_tabs.currentWidget() in (
            self.scene_panel,
            self.cast_panel,
            self.lore_panel,
            self.style_panel,
        ):
            self.right_tabs.setCurrentWidget(self.summaries_panel)
        tee = self.session is not None and self.session.tee_chat
        # A chat on a TEE model is private throughout: no part to set apart.
        # One kept in memory only keeps its private parts in memory too.
        self.composer.fix_private_keep("memory" if chat and self.memory_chat else None)
        self.composer.set_private_offered(not tee)
        self.composer.set_private_look(self._shows_private())
        self._sync_capture()
        title = self.session.story.title if self.session is not None else ""
        if self.session is not None:
            suffix = f"  {MEMORY_TITLE}" if self.memory_chat else ""
            self.setWindowTitle(f"SealedLore — {title}{suffix}")

    def _chat_allows_close(self) -> bool:
        """Leaving a chat kept in memory only loses it: ask first."""
        if not self.memory_chat:
            return True
        drawing = len(self.image_jobs.pending(self.session.story.id))
        lost = (
            f" The {drawing} picture{'s' if drawing != 1 else ''} still being drawn "
            f"{'are' if drawing != 1 else 'is'} lost with it."
            if drawing
            else ""
        )
        making = self._video_counts(self.session.story.id)
        if making:
            lost += (
                f" {making} video{'s' if making != 1 else ''} still being made, already paid "
                f"for, {'are' if making != 1 else 'is'} lost too: nothing on disk says where "
                "to fetch them."
            )
        answer = QMessageBox.question(
            self,
            "Chat kept in memory only",
            "This chat is kept in memory only: closing it loses it, and nothing of it was "
            f"ever written to disk.{lost}\n\nTo keep it, export a backup first (Story → "
            "Export → Story backup…); to keep only its pictures and videos, Media → Save all….\n\n"
            "Close it anyway?",
            QMessageBox.Yes | QMessageBox.Cancel,
        )
        return answer == QMessageBox.Yes

    def _chat_model_allowed(self, model: str) -> bool:
        """A TEE chat moves only to another TEE model; says why otherwise."""
        try:
            self.session.check_chat_model(model)
        except ValueError as exc:
            QMessageBox.information(self, "Kept to TEE models", str(exc))
            return False
        return True

    def _chat_model_limits(self) -> dict:
        """For the model picker: a TEE chat lists only the models it may move
        to (the author: its list showed every model), and says why."""
        if self.session is None or not self.session.tee_chat:
            return {}
        current = self.session.model
        note = (
            "this chat is end-to-end encrypted: private/ models only"
            if is_private_mode(current)
            else "this chat is kept to TEE and end-to-end encrypted models"
        )
        return {
            "allowed": lambda model: move_refusal(current, model) is None,
            "allowed_note": note,
        }

    def _attest_chat(self) -> None:
        """A TEE chat is attested each time it opens, and after its model
        changes, as a private scene is when it begins."""
        if self.session is None or not self.session.tee_chat or self.use_mock:
            return
        settings = self.config.active_provider()
        if settings is None:
            return
        self._attest_or_say_not(settings.model_copy(update={"model": self.session.model}))

    def _chat_model_changed(self) -> None:
        """A TEE chat's new model is attested before anything else is sent:
        the old enclave's key signs nothing the new one writes."""
        if self.session is None:
            return
        old, self.session.private_tee = self.session.private_tee, None
        if old is not None:
            old.close()
        self.session.tee_block = None  # the new model's own attestation decides
        self._tee_state = ""
        self._sync_chat_ui()
        self._attest_chat()

    def _chat_provider(self) -> ChatProvider | None:
        """An end-to-end encrypted chat's provider: sealed to its attested
        enclave, built once per endpoint like the window's own
        (MainWindow._current_provider). None for any other chat or a story."""
        if (
            self.session is None
            or not self.session.story.chat
            or not is_private_mode(self.session.model)
            or self.use_mock
        ):
            return None
        settings = self.config.active_provider()
        if settings is None:
            return None
        if self._sealed_provider is None or self._sealed_provider.config != settings:
            self._discard_sealed_provider()
            self._sealed_provider = PrivateModeProvider(settings)
        return self._sealed_provider

    def _discard_sealed_provider(self) -> None:
        provider, self._sealed_provider = self._sealed_provider, None
        if provider is not None:
            provider.close()

    def _toggle_keep_full(self, node_id: str, keep: bool) -> None:
        if self.session is None or self._busy:
            return
        self.session.set_keep_full(node_id, keep)
        place = self.transcript.place_of(node_id)
        self.reload_transcript()
        self.transcript.keep_place(place)
        self.refresh_summaries()
        self._reassemble_inspector()
