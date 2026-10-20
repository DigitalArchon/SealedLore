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
from PySide6.QtWidgets import QCheckBox, QInputDialog, QMessageBox

from sealedlore.engine.session_plot import SessionNotice
from sealedlore.gui.private_dialogs import FitDialog, PrivateSummaryDialog, chat_words
from sealedlore.gui.winprivacy import exclude_from_capture
from sealedlore.gui.worker import GenerationWorker
from sealedlore.models.config import ProviderConfig
from sealedlore.providers import make_chat_provider
from sealedlore.providers.base import ChatProvider
from sealedlore.providers.private_mode import ModelRetired, PrivateModeProvider
from sealedlore.providers.tee import TeeClient, is_private_mode, is_tee, move_refusal
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
# A chat has no scene card, plot or character to pause.
PAUSED_IN_PRIVATE_CHAT = (
    "While a private part of the chat is open, some of the app is paused so nothing from "
    "it can reach another model or be saved where it shouldn't:\n\n"
    "• Settings\n"
    "• switching to another chat, story or branch\n"
    "• changing this chat's model\n\n"
    "To use them again, end the private part with the End private button beside the "
    "message box. The private model then writes a summary for you to approve, or you can "
    "end it without one."
)
if sys.platform == "win32":
    _CAPTURE = (
        "\n\nOn Windows the window is also kept out of screen capture while it is open: "
        "Recall, screenshots and screen recordings leave it out."
    )
    PAUSED_IN_PRIVATE += _CAPTURE.replace("while it is open", "while the scene is open")
    PAUSED_IN_PRIVATE_CHAT += _CAPTURE


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
        # The private/ model nano-gpt no longer offers, found when it was
        # attested: sends then say so, not "enclave refused".
        self._retired_model: str | None = None
        # What nano-gpt offers instead (the list the retired check read), and
        # whether the scene's summary waits on the chosen model's attestation.
        self._offered_instead: list[str] = []
        self._summarise_after_attest = False
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

    def _say(self, text: str) -> str:
        """A message about a private scene, as it is said of a chat's private
        part when the open story is a chat (private_dialogs.chat_words)."""
        return chat_words(text, self.in_chat)

    def _paused_text(self) -> str:
        return PAUSED_IN_PRIVATE_CHAT if self.in_chat else PAUSED_IN_PRIVATE

    def _make_private_provider(self, settings: ProviderConfig) -> ChatProvider:
        if self.private_provider_factory is not None:
            return self.private_provider_factory(settings)
        return make_chat_provider(settings)

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
        # A TEE chat keeps its look with no span open.
        self.composer.set_private_look(self._shows_private())
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
                short = self.config.bare_model(self.session.model).rsplit("/", 1)[-1]
                return f"🔒 {short} · {where}{self._tee_badge()}"
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
        for action in (self.review_action, self.restart_action, self.without_map_action):
            action.setEnabled(action.isEnabled() and not private)
        # Settings stays clickable and says why it's paused (_settings_paused).
        # Switching story or branch would leave the scene behind half-open.
        self.stories.setEnabled(self.stories.isEnabled() and not private)
        self.branch_bar.setEnabled(self.session is not None and not self._busy and not private)
        if private and self.map_shown():
            self.show_map(False)
        # The story's map stays where it is, paused (the author, Oct 15 2026):
        # nothing from the scene reaches it, and it catches up from the
        # approved summary (`_sync_world`).
        self._sync_world()
        for widget in (self.stories, self.branch_bar):
            widget.setToolTip(
                self._say(
                    "Paused during a private scene: end it with End private beside the "
                    "message box to switch."
                )
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
            self._say("Not during a private scene"),
            self._say(
                "End the private scene first: the messages before it are where it returns to."
            ),
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
        # A choice the author made, remembered; not one a memory chat made for them.
        if keep != self.config.private_keep and not self.composer.keep_fixed:
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
            dialog = FitDialog(fit, self.session.model, self, chat=self.in_chat)
            dialog.exec()
            if dialog.choice == FitDialog.CANCEL:
                self.session.discard_private()
                self._sync_private_ui()
                self._update_controls()
                return
            if dialog.choice == FitDialog.SUMMARISE:
                self._start_handoff()
        self.statusBar().showMessage(
            f"Private: your messages go to {settings.model} only, and the chat's own model "
            "sees none of them. End it with the same button."
            if self.in_chat
            else f"Private scene: turns go to {settings.model} only; the scene card and the "
            "plot are frozen until it ends. End it with the same button.",
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
            QMessageBox.Information,
            self._say("Private scene"),
            self._paused_text(),
            QMessageBox.Ok,
            self,
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
        QMessageBox.information(self, "Settings are paused", self._paused_text())
        return True

    def _start_handoff(self) -> None:
        session = self.session
        said = self._say

        def job():
            session.write_handoff()
            yield SessionNotice(said("The story so far is condensed for the private model."))

        self._start(
            job,
            streaming=False,
            busy_text=said(
                f"{session.model} is condensing the story so far for the private model…"
            ),
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
        retired: list[ModelRetired] = []
        tee: TeeClient | None = None
        self._tee_settings = settings
        self._retired_model = None
        if is_private_mode(settings.model):
            # The sealing provider attests before it sends anything itself.
            self.session.tee_block = None
            sealed = (
                self.session.private_provider if span is not None else self._sealed_chat_provider()
            )
            if not isinstance(sealed, PrivateModeProvider):
                return  # a test's stand-in

            def attest():
                # The enclave is Tinfoil's router whatever the model, so a
                # model nano-gpt has retired would attest: ask its list first.
                sealed.check_offered(settings.model)
                return sealed.attest()
        else:
            tee = TeeClient(settings.base_url, settings.api_key, settings.model)
            attest = tee.attest
            # Nothing reaches the model until this comes back and holds.
            self.session.tee_block = f"{settings.model}'s enclave is still being attested"

        def job():
            try:
                result.append(attest())
            except ModelRetired as exc:
                retired.append(exc)
            except Exception as exc:  # shown, never fatal
                failure.append(str(exc))
            return
            yield

        worker = GenerationWorker(job)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda: self._on_attested(span_id, tee, result, failure, retired))
        self._tee_worker, self._tee_thread = worker, thread
        self._tee_state = "checking…"
        self._refresh_model_button()
        thread.start()

    def _on_attested(
        self,
        span_id: str | None,
        tee: TeeClient | None,
        result: list,
        failure: list,
        retired: list | None = None,
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
        if retired:
            self._on_model_retired(retired[0].model, for_chat, retired[0].offered)
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
                # As File → Close story: a memory-only chat asks first, since
                # closing loses it (it used to go without the question here).
                self.close_story()
            else:
                self.end_private()

    def _tee_allows_send(self, *, ending: bool = False) -> bool:
        """Before anything is sent to a TEE/ model: wait out its attestation,
        and say so when it was refused (the session refuses regardless).
        `ending`: the scene's summary is what would be sent."""
        block = self.session.tee_block if self.session is not None else None
        if not block:
            return True
        if self._tee_thread is not None:
            self.statusBar().showMessage(
                "Waiting for the enclave's attestation: send again in a moment.", 6000
            )
            return False
        for_chat = self.session.open_span is None
        if self._retired_model:
            self._say_model_retired(self._retired_model, for_chat, ending=ending)
        else:
            self._offer_reattest(block, for_chat=for_chat)
        return False

    def _on_model_retired(self, model: str, for_chat: bool, offered: list[str]) -> None:
        """nano-gpt has retired the private/ model: nothing can be sent to it
        (its preflight would refuse it), so say so and where to choose another."""
        self._retired_model = model
        self._offered_instead = list(offered)
        self._summarise_after_attest = False
        self.session.tee_block = f"{model} is no longer offered by NanoGPT's Private Mode"
        self._tee_state = "⚠ no longer offered"
        self._refresh_model_button()
        self._say_model_retired(model, for_chat)

    def _say_model_retired(self, model: str, for_chat: bool, *, ending: bool = False) -> None:
        if for_chat:
            QMessageBox.warning(
                self,
                "Model no longer available",
                f"{model} is no longer offered by NanoGPT's Private Mode, so nothing can "
                "be sent to it.\n\nChoose another end-to-end encrypted model with the model "
                "button below the message box.",
            )
            return
        session = self.session
        written = bool(session.span_nodes(session.open_span))
        summarise = ending and written
        choices = self._scene_model_choices(model)
        if not choices:
            then = "End the private scene" + (" without a summary" if written else "")
            text = (
                f"{then}; NanoGPT offers no other end-to-end encrypted model to carry it "
                "on. Choose another for new scenes under Settings → Private."
            )
        elif summarise:
            text = (
                "Choose another end-to-end encrypted model to write the scene's summary, "
                "or end it without one. New scenes use the model under Settings → Private."
            )
        else:
            text = (
                "Carry the scene on with another end-to-end encrypted model (it also writes "
                "the summary when the scene ends), or end it. New scenes use the model "
                "under Settings → Private."
            )
        box = QMessageBox(
            QMessageBox.Warning,
            "Model no longer available",
            self._say(
                f"{model} is no longer offered by NanoGPT's Private Mode, so nothing can be "
                f"sent to it.\n\n{text}"
            ),
            QMessageBox.NoButton,
            self,
        )
        another = (
            box.addButton(
                "Summarise with another model…" if summarise else "Use another model…",
                QMessageBox.AcceptRole,
            )
            if choices
            else None
        )
        leave = box.addButton(
            "End without a summary…" if written else self._say("End the scene"),
            QMessageBox.DestructiveRole,
        )
        box.addButton("Keep it open", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        box.deleteLater()
        if self._busy:
            return
        if another is not None and clicked is another:
            self._choose_scene_model(model, choices, summarise=summarise)
        elif clicked is leave:
            self._end_without_model()

    def _scene_model_choices(self, retired: str) -> list[str]:
        """The models nano-gpt still offers that the open scene may move to:
        as private as its own (`move_refusal`), from the list the retired
        check read (public, no key; nothing more is fetched in a scene)."""
        span = self.session.open_span if self.session is not None else None
        if span is None:
            return []
        return [
            model
            for model in self._offered_instead
            if model != retired and move_refusal(span.model, model) is None
        ]

    def _choose_scene_model(self, retired: str, choices: list[str], *, summarise: bool) -> None:
        """Move the scene to a model the author picks, attest it, and, when
        the author was ending the scene, have it write the summary."""
        session = self.session
        model, ok = QInputDialog.getItem(
            self,
            "Another private model",
            self._say(
                f"Instead of {retired}, the private scene's messages go to:"
                + ("\n(it writes the scene's summary)" if summarise else "")
            ),
            choices,
            0,
            False,
        )
        if not ok or not model or self._busy or session is None or not session.in_private:
            return
        # The scene's own endpoint and key, not whatever Settings says now.
        base = getattr(session.private_provider, "config", None) or self._private_settings()
        if base is None:
            return
        settings = base.model_copy(update={"model": model})
        session.switch_private_model(provider=self._make_private_provider(settings), model=model)
        self._retired_model = None
        self._tee_state = ""
        self._summarise_after_attest = summarise
        self._sync_private_ui()
        self.statusBar().showMessage(
            self._say(
                f"The private scene is now on {model}"
                + (": its summary follows once it is attested." if summarise else ".")
            ),
            10000,
        )
        self._attest_or_say_not(settings)

    def _end_without_model(self) -> None:
        """End a scene whose model is gone: there's no one to write its
        summary, so a scene with messages ends without one (asked first)."""
        session = self.session
        if session is None or not session.in_private:
            return
        if session.span_nodes(session.open_span):
            self._confirm_discard_private()
            return
        session.discard_private()
        self._after_private_ended(self._say("The private scene ended; nothing was written in it."))

    def _on_encryption_attested(self, attestation, failure: list[str], for_chat: bool) -> None:
        """An end-to-end encrypted model's attestation: nothing is sealed to
        it until one succeeds, so a failure costs nothing but the next try."""
        summarise, self._summarise_after_attest = self._summarise_after_attest, False
        if attestation is not None:
            older = "  ⚠ older release" if attestation.latest_release else ""
            self._tee_state = f"end-to-end encrypted{older}"
            self._refresh_model_button()
            self.statusBar().showMessage(
                f"End-to-end encrypted: {attestation.detail}. NanoGPT relays ciphertext; it "
                "sees the account, the model, timing, sizes and usage.",
                15000,
            )
            if summarise and not for_chat and not self._busy:
                self.end_private()  # the author was ending the scene
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
            self._after_private_ended(
                self._say("The private scene ended; nothing was written in it.")
            )
            return
        if not self._tee_allows_send(ending=True):
            return  # the summary would go to a model not yet (or never) attested
        if session.private_provider is None:
            answer = QMessageBox.question(
                self,
                "No private model",
                self._say(
                    "There's no private model to write the summary (Settings → Private). "
                    "Discard the private scene instead? Nothing from it goes back into the "
                    "story."
                ),
            )
            if answer == QMessageBox.Yes:
                session.discard_private()
                self._after_private_ended(self._say("The private scene was discarded."))
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
            busy_text=self._say(
                f"{span.model} is summarising the private scene… (Stop to end it "
                "without a summary or keep playing)"
            ),
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
            summary,
            self,
            note=notes.get(self.session.open_span.summary_tee or ""),
            chat=self.in_chat,
        )
        exclude_from_capture(dialog, True)  # a window of its own
        dialog.exec()
        session = self.session
        if dialog.choice == PrivateSummaryDialog.APPROVE:
            text = dialog.text()
            self._start(
                lambda: session.close_private(text),
                streaming=False,
                busy_text="Adding the summary to the chat…"
                if self.in_chat
                else "Adding the summary; the scene and the clock catch up from it…",
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
            self._say("Discard the private scene"),
            self._say(
                "End the scene with nothing carried over? The story goes on from where it began."
            )
            + (
                " Its messages are kept on disk as a side branch no model is sent."
                if session.open_span.keep == "disk"
                else " Its messages are gone."
            ),
        )
        if answer == QMessageBox.Yes:
            session.discard_private()
            self._after_private_ended(self._say("The private scene was discarded."))

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
            self._say(
                "The private scene wasn't summarised. Ask again, end the scene without a "
                "summary (nothing from it goes back into the story), or keep playing."
            ),
            QMessageBox.NoButton,
            self,
        )
        again = box.addButton("Try again", QMessageBox.AcceptRole)
        discard = box.addButton("End without a summary…", QMessageBox.DestructiveRole)
        box.addButton(self._say("Keep playing"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is again:
            return "again"
        return "discard" if box.clickedButton() is discard else "keep"

    def _after_private_ended(self, message: str) -> None:
        self._tee_state = ""
        self._retired_model = None
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
            # The scene's own model: every request names it (span.model), so it
            # is what must be attested and still offered, whatever Settings →
            # Private names for new scenes now.
            settings = settings.model_copy(update={"model": span.model})
            session.resume_private(self._make_private_provider(settings))
            self._attest_or_say_not(settings)
        else:
            self.statusBar().showMessage(
                self._say(
                    "This story is in a private scene, but no private model is set up "
                    "(Settings → Private). End the scene to discard it."
                ),
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
            self._say("Private scene still open"),
            self._say(
                "This private scene is held in memory only: closing now loses it, and "
                "nothing from it goes back into the story.\n\nTo keep a summary, press "
                "Cancel and end the scene first. Close anyway?"
            ),
            QMessageBox.Yes | QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return False
        session.discard_private()
        return True

    def _wait_for_attestation(self) -> None:
        if self._tee_thread is not None:
            self._tee_thread.wait(35_000)
