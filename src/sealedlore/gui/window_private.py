"""The main window's side of private scenes. A mixin for `MainWindow`.

Beginning one is one click (the Keep choice beside the button is already
made); only a story too long for the private model's budget asks anything,
and a TEE model's attestation is checked in the background. Ending one asks
the private model for a summary, which the author approves, edits, asks for
again, or discards the scene instead.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QCheckBox, QMessageBox

from sealedlore.engine.session_plot import SessionNotice
from sealedlore.gui.private_dialogs import FitDialog, PrivateSummaryDialog
from sealedlore.gui.winprivacy import exclude_from_capture
from sealedlore.gui.worker import GenerationWorker
from sealedlore.models.config import ProviderConfig
from sealedlore.providers.base import ChatProvider
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.private_mode import PrivateModeProvider
from sealedlore.providers.tee import TeeClient, is_private_mode, is_tee
from sealedlore.storage.repository import save_config

# On the model button while a TEE/ model runs unchecked (Settings → Private).
UNCHECKED_TEE = "⚠ not checked (Check TEE models is off)"
# What a private scene pauses, and how to get it back (the author: people
# weren't told, and found Settings and the stories list simply greyed out).
PAUSED_IN_PRIVATE = (
    "While a private scene is open, some of the app is paused so nothing from the scene "
    "can reach another model or be saved where it shouldn't:\n\n"
    "• Settings\n"
    "• switching to another story or branch, the settings review and Restart\n"
    "• the scene card, the plot, the clock and who you are playing\n\n"
    "To use them again, end the scene with the End private button beside the message "
    "box. The private model then writes a summary for you to approve, or you can end "
    "the scene without one."
)
if sys.platform == "win32":
    PAUSED_IN_PRIVATE += (
        "\n\nOn Windows the window is also kept out of screen capture while the scene "
        "is open: Recall, screenshots and screen recordings leave it out."
    )


class PrivateWindow:
    def _init_private(self) -> None:
        self.composer.private_requested.connect(self._on_private_requested)
        # The provider a private scene is sent to; tests set a factory.
        self.private_provider_factory = None
        self._pending_private_summary: str | None = None
        self._summarising = False
        self._tee_thread: QThread | None = None
        self._tee_worker: GenerationWorker | None = None
        self._tee_state = ""
        self._capture_hidden = False
        self.composer.private_keep.setCurrentIndex(
            max(self.composer.private_keep.findData(self.config.private_keep), 0)
        )

    # --- state ---------------------------------------------------------------

    @property
    def in_private(self) -> bool:
        return self.session is not None and self.session.in_private

    def _private_settings(self) -> ProviderConfig | None:
        return self.config.private()

    def _make_private_provider(self, settings: ProviderConfig) -> ChatProvider:
        if self.private_provider_factory is not None:
            return self.private_provider_factory(settings)
        if is_private_mode(settings.model):
            return PrivateModeProvider(settings)
        return OpenAICompatibleProvider(settings)

    def _wants_attestation(self, model: str) -> bool:
        """An end-to-end encrypted model is always attested: its key is what
        everything is sealed to. A TEE/ model, when Settings asks for it."""
        return is_private_mode(model) or (is_tee(model) and self.config.private_verify_tee)

    def _attest_or_say_not(self, settings: ProviderConfig) -> None:
        """Attest a TEE model, or, with "Check TEE models" off in Settings,
        say so on the model button for as long as the scene or chat lasts:
        the switch is easy to turn off and forget, and a TEE model that isn't
        checked is just a model."""
        if self._wants_attestation(settings.model):
            self._start_attestation(settings)
        elif is_tee(settings.model):
            self._tee_state = UNCHECKED_TEE
            self._refresh_model_button()
            self.statusBar().showMessage(
                f'{settings.model} is not being checked: "Check TEE models" is off under '
                "Settings → Private, so its enclave isn't attested and replies aren't "
                "checked for its signature.",
                15000,
            )

    def _sync_private_ui(self) -> None:
        span = self.session.open_span if self.session is not None else None
        self.composer.set_private_state(span is not None, span.keep if span else None)
        self._refresh_model_button()
        self._sync_capture()

    def _shows_private(self) -> bool:
        """A private scene is open, or a TEE or end-to-end encrypted chat."""
        session = self.session
        return session is not None and (session.open_span is not None or session.tee_chat)

    def _sync_capture(self) -> None:
        """On Windows, keep the window out of screen capture (Recall,
        screenshots) while it shows something private (`winprivacy`)."""
        hidden = self._shows_private()
        if hidden != self._capture_hidden:
            exclude_from_capture(self, hidden)
            self._capture_hidden = hidden

    def _private_model_text(self) -> str | None:
        span = self.session.open_span if self.session is not None else None
        if span is None:
            if self.session is not None and self.session.tee_chat:
                # A TEE chat reads as a private scene does.
                where = "memory only" if self.session.memory_only else "kept on disk"
                return f"🔒 {self.session.model.rsplit('/', 1)[-1]} · {where}{self._tee_badge()}"
            return None
        where = "memory only" if span.keep == "memory" else "kept on disk"
        return f"🔒 {span.model.rsplit('/', 1)[-1]} · {where}{self._tee_badge()}"

    def _tee_badge(self) -> str:
        if not self._tee_state:
            return ""
        span = self.session.open_span if self.session is not None else None
        model = span.model if span is not None else (self.session.model if self.session else "")
        prefix = "" if is_private_mode(model) else "TEE "
        return f"  ·  {prefix}{self._tee_state}"

    def _update_private_controls(self) -> None:
        private = self.in_private
        # Pictures stay: the private model writes their prompts, and the image
        # dialog warns that the picture itself is neither private nor unsaved.
        for action in (self.review_action, self.restart_action):
            action.setEnabled(action.isEnabled() and not private)
        # Settings stays clickable and says why it's paused (_settings_paused).
        # Switching story or branch would leave the scene behind half-open.
        self.stories.setEnabled(self.stories.isEnabled() and not private)
        self.branch_bar.setEnabled(self.session is not None and not self._busy and not private)
        if private and self.map_shown():
            self.show_map(False)
        for widget in (self.stories, self.branch_bar):
            widget.setToolTip(
                "Paused during a private scene: end it with End private beside the message "
                "box to switch."
                if private
                else ""
            )
        # No scene tracking in a scene (the author's ruling): the scene card,
        # the plot and who is held are frozen until it ends. An edit would be
        # stamped on the last public passage, saved even in memory mode, and
        # shape every later prompt to the story's model.
        for widget in (self.scene_panel, self.plot_panel, self.clock_button, self.composer.held):
            widget.setEnabled(widget.isEnabled() and not private)

    def _refuse_outside_scene(self, node_id: str) -> bool:
        """During a scene only its own messages may be deleted or taken back:
        removing the passage it hangs from would leave it nowhere to return."""
        session = self.session
        if session is None or not session.in_private:
            return False
        if any(node.id == node_id for node in session.span_nodes(session.open_span)):
            return False
        self._warn_plain(
            "Not during a private scene",
            "End the private scene first: the messages before it are where it returns to.",
        )
        return True

    # --- beginning -------------------------------------------------------------

    def _on_private_requested(self, begin: bool) -> None:
        if begin:
            self.begin_private()
        else:
            self.end_private()

    def begin_private(self) -> None:
        if self.session is None or self._busy or self.session.in_private:
            return
        settings = self._private_settings()
        if settings is None:
            QMessageBox.information(
                self,
                "No private model",
                "Set up a private model first: Settings → Private (a local server such as "
                "Ollama, or a NanoGPT model; TEE models are checked for you).",
            )
            return
        keep = self.composer.keep_choice()
        if keep != self.config.private_keep:
            self.config.private_keep = keep
            save_config(self.config, root=self.root)
        provider = self._make_private_provider(settings)
        self.session.enter_private(
            keep=keep, provider=provider, model=settings.model, base_url=settings.base_url
        )
        self._tee_state = ""
        fit = self.session.private_fit(self._current_turn("(checking the fit)"))
        self.session.last_prompt = None
        self._sync_private_ui()
        self._update_controls()
        if not fit.fits:
            dialog = FitDialog(fit, self.session.model, self)
            dialog.exec()
            if dialog.choice == FitDialog.CANCEL:
                self.session.discard_private()
                self._sync_private_ui()
                self._update_controls()
                return
            if dialog.choice == FitDialog.SUMMARISE:
                self._start_handoff()
        self.statusBar().showMessage(
            f"Private scene: turns go to {settings.model} only; the scene card and the plot "
            "are frozen until it ends. End it with the same button.",
            10000,
        )
        self._attest_or_say_not(settings)
        self._explain_private_pause()

    def _explain_private_pause(self) -> None:
        """Once, until the author says not to: what a scene pauses, and how to
        get it back."""
        if self.config.private_intro_seen:
            return
        box = QMessageBox(
            QMessageBox.Information, "Private scene", PAUSED_IN_PRIVATE, QMessageBox.Ok, self
        )
        # Parented to the box: setCheckBox doesn't take ownership in PySide6,
        # and a checkbox Python frees under a live box crashes the app.
        again = QCheckBox("Don't show this again", box)
        box.setCheckBox(again)
        box.exec()
        seen = again.isChecked()
        box.deleteLater()
        if seen:
            self.config.private_intro_seen = True
            save_config(self.config, root=self.root)

    def _settings_paused(self) -> bool:
        """Settings during a scene: say why not, rather than grey it out unexplained."""
        if not self.in_private:
            return False
        QMessageBox.information(self, "Settings are paused", PAUSED_IN_PRIVATE)
        return True

    def _start_handoff(self) -> None:
        session = self.session

        def job():
            session.write_handoff()
            yield SessionNotice("The story so far is condensed for the private model.")

        self._start(
            job,
            streaming=False,
            busy_text=f"{session.model} is condensing the story so far for the private model…",
            provider=session.provider,
        )

    def _start_attestation(self, settings: ProviderConfig) -> None:
        """Attest a TEE model in the background: the open private scene's, or,
        with no scene open, a TEE chat's (window_chat). An end-to-end
        encrypted model is attested by the provider that seals to it."""
        span = self.session.open_span
        span_id = span.id if span is not None else None
        result: list = []
        failure: list[str] = []
        tee: TeeClient | None = None
        self._tee_settings = settings
        if is_private_mode(settings.model):
            # The sealing provider attests before it sends anything itself.
            self.session.tee_block = None
            sealed = self.session.private_provider if span is not None else self._current_provider()
            if not isinstance(sealed, PrivateModeProvider):
                return  # a test's stand-in
            attest = sealed.attest
        else:
            tee = TeeClient(settings.base_url, settings.api_key, settings.model)
            attest = tee.attest
            # Nothing reaches the model until this comes back and holds.
            self.session.tee_block = f"{settings.model}'s enclave is still being attested"

        def job():
            try:
                result.append(attest())
            except Exception as exc:  # shown, never fatal
                failure.append(str(exc))
            return
            yield

        worker = GenerationWorker(job)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda: self._on_attested(span_id, tee, result, failure))
        self._tee_worker, self._tee_thread = worker, thread
        self._tee_state = "checking…"
        self._refresh_model_button()
        thread.start()

    def _on_attested(
        self, span_id: str | None, tee: TeeClient | None, result: list, failure: list
    ) -> None:
        self._tee_worker, self._tee_thread = None, None
        span = self.session.open_span if self.session is not None else None
        for_chat = span_id is None
        if (
            (self.session is None or not self.session.tee_chat)
            if for_chat
            else (span is None or span.id != span_id)
        ):
            if tee is not None:
                tee.close()
            return
        attestation = result[0] if result else None
        if attestation is not None:
            if for_chat:
                self.session.story.chat_attestation = attestation
            else:
                span.attestation = attestation
            if tee is not None:
                # Checks each reply's signature; a provider that signs none
                # (Chutes) marks them "signature unavailable".
                self.session.private_tee = tee
            if not self._busy:  # otherwise the job in flight saves it
                self.session.save()
        verified = attestation is not None and attestation.verified
        if tee is None:
            self._on_encryption_attested(attestation if verified else None, failure, for_chat)
            return
        if not verified:
            # Refused (providers/tee.TeeClient.attest): forged, revoked, for
            # another nonce, a debug enclave, or no attestation at all.
            reason = "; ".join(failure) or "the enclave's attestation didn't hold"
            self.session.tee_block = f"The attestation was refused: {reason}"
            self._tee_state = "⚠ refused"
            self._refresh_model_button()
            self._offer_reattest(reason, for_chat)
            return
        self.session.tee_block = None
        shortfalls = ", ".join(attestation.shortfalls)
        self._tee_state = f"attested (partial: {shortfalls})" if shortfalls else "attested"
        if attestation.instances > 1:
            self._tee_state += f", {attestation.instances} instances"
        self._refresh_model_button()
        self.statusBar().showMessage(
            f"TEE attested{' (partial: ' + shortfalls + ')' if shortfalls else ''}: "
            f"{attestation.detail}. Replies are checked to be signed by the attested key; "
            "their content is not compared to the signature.",
            15000,
        )

    def _offer_reattest(self, reason: str, for_chat: bool) -> None:
        """A refused enclave: nothing is sent to it. Try again, leave, or stay."""
        box = QMessageBox(
            QMessageBox.Warning,
            "Enclave refused",
            f"The {'model' if for_chat else 'private model'}'s enclave can't be trusted: "
            f"{reason}.\n\nNothing will be sent to it.",
            QMessageBox.NoButton,
            self,
        )
        again = box.addButton("Try again", QMessageBox.AcceptRole)
        leave = box.addButton(
            "Close the chat" if for_chat else "End the scene", QMessageBox.DestructiveRole
        )
        box.addButton("Keep it open", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        box.deleteLater()
        if clicked is again and getattr(self, "_tee_settings", None) is not None:
            self._start_attestation(self._tee_settings)
        elif clicked is leave and not self._busy:
            if for_chat:
                # A memory-only chat is simply gone; one on disk is kept.
                self._close_story()
            else:
                self.end_private()

    def _tee_allows_send(self) -> bool:
        """Before anything is sent to a TEE/ model: wait out its attestation,
        and say so when it was refused (the session refuses regardless)."""
        block = self.session.tee_block if self.session is not None else None
        if not block:
            return True
        if self._tee_thread is not None:
            self.statusBar().showMessage(
                "Waiting for the enclave's attestation: send again in a moment.", 6000
            )
            return False
        self._offer_reattest(block, for_chat=self.session.open_span is None)
        return False

    def _on_encryption_attested(self, attestation, failure: list[str], for_chat: bool) -> None:
        """An end-to-end encrypted model's attestation: nothing is sealed to
        it until one succeeds, so a failure costs nothing but the next try."""
        if attestation is not None:
            older = "  ⚠ older release" if attestation.latest_release else ""
            self._tee_state = f"end-to-end encrypted{older}"
            self._refresh_model_button()
            self.statusBar().showMessage(
                f"End-to-end encrypted: {attestation.detail}. NanoGPT relays ciphertext; it "
                "sees the account, the model, timing, sizes and usage.",
                15000,
            )
            return
        self._tee_state = "⚠ not attested"
        self._refresh_model_button()
        detail = "; ".join(failure)
        QMessageBox.warning(
            self,
            "Enclave not attested",
            f"The {'model' if for_chat else 'private model'}'s enclave couldn't be attested"
            f"{': ' + detail if detail else ''}.\n\nNothing is sent to it until it is: each "
            "message tries again first, and says why if it still can't.",
        )

    # --- ending ----------------------------------------------------------------

    def end_private(self) -> None:
        session = self.session
        if session is None or self._busy or not session.in_private:
            return
        span = session.open_span
        if not session.span_nodes(span):
            session.discard_private()
            self._after_private_ended("The private scene ended; nothing was written in it.")
            return
        if not self._tee_allows_send():
            return  # the summary would go to a model not yet (or never) attested
        if session.private_provider is None:
            answer = QMessageBox.question(
                self,
                "No private model",
                "There's no private model to write the summary (Settings → Private). "
                "Discard the private scene instead? Nothing from it goes back into the story.",
            )
            if answer == QMessageBox.Yes:
                session.discard_private()
                self._after_private_ended("The private scene was discarded.")
            return
        self._pending_private_summary = None
        self._summarising = True

        def job():
            self._pending_private_summary = session.summarise_private()
            return
            yield

        self._start(
            job,
            streaming=False,
            busy_text=f"{span.model} is summarising the private scene… (Stop to end it "
            "without a summary or keep playing)",
        )

    def _offer_private_summary(self) -> None:
        """After the summary job: the author approves, edits, retries or discards."""
        summary, self._pending_private_summary = self._pending_private_summary, None
        asked, self._summarising = self._summarising, False
        if self.session is None or not self.session.in_private:
            return
        if summary is None:
            if asked:
                # Stopped, or failed (the reason was shown): never a dead end.
                self._offer_no_summary()
            return
        notes = {
            "failed": "This summary's signature record was not signed by the attested key.",
            "unchecked": "This summary's signature couldn't be checked.",
        }
        dialog = PrivateSummaryDialog(
            summary, self, note=notes.get(self.session.open_span.summary_tee or "")
        )
        exclude_from_capture(dialog, True)  # a window of its own
        dialog.exec()
        session = self.session
        if dialog.choice == PrivateSummaryDialog.APPROVE:
            text = dialog.text()
            self._start(
                lambda: session.close_private(text),
                streaming=False,
                busy_text="Adding the summary; the scene and the clock catch up from it…",
                provider=session.provider,
            )
        elif dialog.choice == PrivateSummaryDialog.AGAIN:
            self.end_private()
        elif dialog.choice == PrivateSummaryDialog.DISCARD:
            self._confirm_discard_private()

    def _confirm_discard_private(self) -> None:
        session = self.session
        answer = QMessageBox.question(
            self,
            "Discard the private scene",
            "End the scene with nothing carried over? The story goes on from where it "
            "began."
            + (
                " Its messages are kept on disk as a side branch no model is sent."
                if session.open_span.keep == "disk"
                else " Its messages are gone."
            ),
        )
        if answer == QMessageBox.Yes:
            session.discard_private()
            self._after_private_ended("The private scene was discarded.")

    def _offer_no_summary(self) -> None:
        """The summary was stopped or failed. Before, nothing was said and the
        only way out was another summary: stop that too and the author was
        stuck in the scene until they quit the app."""
        choice = self._ask_no_summary()
        if choice == "again":
            self.end_private()
        elif choice == "discard":
            self._confirm_discard_private()

    def _ask_no_summary(self) -> str:
        """ "again", "discard" or "keep"."""
        box = QMessageBox(
            QMessageBox.Question,
            "No summary",
            "The private scene wasn't summarised. Ask again, end the scene without a "
            "summary (nothing from it goes back into the story), or keep playing.",
            QMessageBox.NoButton,
            self,
        )
        again = box.addButton("Try again", QMessageBox.AcceptRole)
        discard = box.addButton("End without a summary…", QMessageBox.DestructiveRole)
        box.addButton("Keep playing", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is again:
            return "again"
        return "discard" if box.clickedButton() is discard else "keep"

    def _after_private_ended(self, message: str) -> None:
        self._tee_state = ""
        if self.session is not None:
            self.session.tee_block = None
        self._sync_private_ui()
        self.reload_transcript()
        self.refresh_summaries()
        self._refresh_scene_panel()
        self._refresh_composer()
        self._reassemble_inspector()
        self._update_controls()
        self.statusBar().showMessage(message, 8000)

    # --- opening and closing --------------------------------------------------------

    def _resume_private(self) -> None:
        """A story opened in the middle of a scene kept on disk."""
        session = self.session
        if session is None:
            return
        if session.private_notice:
            self.statusBar().showMessage(session.private_notice, 15000)
            session.private_notice = None
            session.save()
        span = session.open_span
        if span is None:
            self._sync_private_ui()
            return
        settings = self._private_settings()
        if settings is not None:
            session.resume_private(self._make_private_provider(settings))
            self._attest_or_say_not(settings)
        else:
            self.statusBar().showMessage(
                "This story is in a private scene, but no private model is set up "
                "(Settings → Private). End the scene to discard it.",
                15000,
            )
        self._sync_private_ui()

    def _private_allows_close(self) -> bool:
        """Closing, or leaving the story (New, Import, Duplicate), in the
        middle of a memory-only scene loses it: ask. Yes discards the scene.
        A chat kept in memory only asks the same (window_chat)."""
        session = self.session
        if session is not None and session.memory_only:
            return self._chat_allows_close()
        if session is None or not session.in_private or session.open_span.keep != "memory":
            return True
        answer = QMessageBox.question(
            self,
            "Private scene still open",
            "This private scene is held in memory only: closing now loses it, and nothing "
            "from it goes back into the story.\n\nTo keep a summary, press Cancel and end "
            "the scene first. Close anyway?",
            QMessageBox.Yes | QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return False
        session.discard_private()
        return True

    def _wait_for_attestation(self) -> None:
        if self._tee_thread is not None:
            self._tee_thread.wait(35_000)
