"""File → New simple chat…: a title, a model, a system prompt and a tail.

The tail was once only in Story → Setup, so a chat that needed one had to be
created and then set up again (the author, Sept 2026).

Every chat is also asked where it is kept: on disk, as any story, or in
memory only, a full incognito conversation (the author, Sept 2026; at first a
TEE chat's choice only). The choice is fixed with the chat. See engine/chat.py.

A chat may keep a map as a story does (the author, Oct 2026): once the
conversation sets a scene, the map read notes the places, and the map's
art draws the local area and grows it as the chat goes. A chat has no cast,
so it is asked the name the map tracks the user by ("You" when blank).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.attest_check import AttestationButton
from sealedlore.gui.model_picker import Browse, BrowseRoute, ModelField
from sealedlore.models.config import Config, ProviderConfig, split_model
from sealedlore.providers.tee import is_private_mode, is_tee

# Any other model: memory only is about this computer, not about the model.
PLAIN_HINTS = {
    "disk": "Saved like any story.",
    "memory": "Nothing is kept on this computer, not even the log: when the chat is closed, "
    "it is gone. The model's provider still receives every message, as with any chat (your "
    "NanoGPT account's data retention setting applies there). Pictures are kept in memory too.",
}
KEEP_HINTS = {
    "disk": "Saved like any story. The model is attested and every reply's signature "
    "checked; the conversation still passes NanoGPT's gateway.",
    "memory": "Nothing is written to disk, not even the log: when the chat is closed, it is "
    "gone. Attested and checked as on disk. Pictures are kept in memory too.",
}
# A private/ model: sealed to the attested enclave (providers/private_mode.py).
ENCRYPTED_HINTS = {
    "disk": "Saved like any story. End-to-end encrypted: every message is sealed to the "
    "attested enclave's key, and NanoGPT relays only ciphertext (it still sees the model, "
    "timing, sizes and cost).",
    "memory": "Nothing is written to disk, not even the log: when the chat is closed, it is "
    "gone. End-to-end encrypted as on disk. Pictures are kept in memory too.",
}


class NewChatDialog(QDialog):
    def __init__(
        self,
        *,
        model: str,
        browse: Browse | None = None,
        parent: QWidget | None = None,
        endpoint: ProviderConfig | None = None,
        config: Config | None = None,
        browse_route: BrowseRoute | None = None,
        hosts_of: Callable[[str], tuple[str, ...] | None] | None = None,
    ) -> None:
        """`endpoint`: the default endpoint; `config`, when given, resolves
        a model naming another endpoint ("Anthropic::claude-opus-5-5",
        models/config.py). `browse_route` and `hosts_of`: the chat's route,
        beside its model (on NanoGPT); it is the chat's own, never saved in
        the settings."""
        super().__init__(parent)
        self.setWindowTitle("New simple chat")
        self.setMinimumWidth(560)
        self._config = config
        intro = QLabel(
            "A plain conversation with the model: your system prompt and your messages, "
            "none of the storytelling around them. Branches, pictures and summaries of a "
            "long chat still work."
        )
        intro.setWordWrap(True)
        self.title = QLineEdit("New chat")
        self._endpoint = endpoint
        self.model = ModelField(
            model,
            browse,
            route_endpoint=self._route_endpoint,
            browse_route=browse_route,
            hosts_of=hosts_of,
        )
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("e.g. You are a helpful assistant. Answer concisely.")
        self.prompt.setMinimumHeight(140)
        prompt_hint = QLabel(
            "Sent at the top of every request and cached. You can change it later under "
            "Story → Setup; a change costs one cache miss."
        )
        prompt_hint.setObjectName("hintLabel")
        prompt_hint.setWordWrap(True)
        self.tail = QPlainTextEdit()
        self.tail.setPlaceholderText(
            "e.g. Reply in British English, in two short paragraphs at most."
        )
        self.tail.setMaximumHeight(110)
        tail_hint = QLabel(
            "Added after your message at the end of every request, where a model weighs it "
            "most. Never saved into the conversation, and changing it later (Story → Setup) "
            "costs no cache miss. Blank sends nothing."
        )
        tail_hint.setObjectName("hintLabel")
        tail_hint.setWordWrap(True)
        self.world_map = QCheckBox("Keep a map of the conversation")
        self.chat_character = QLineEdit()
        self.chat_character.setPlaceholderText("You")
        self.chat_character.setMaxLength(60)
        self.chat_character.setEnabled(False)
        map_hint = QLabel(
            "After each reply a small model (Settings → Models → Map model) notes the places "
            "the conversation goes to, how they connect, who is where and the things that "
            "matter, and the map grows with it (Map, above the chat); its art is drawn in the "
            "background. One more small call after every reply. The map tracks you by the "
            'name beside it: your character\'s name, or "You".'
        )
        map_hint.setObjectName("hintLabel")
        map_hint.setWordWrap(True)
        self.keep = QComboBox()
        self.keep.addItem("On disk", "disk")
        self.keep.addItem("In memory only", "memory")
        self.keep_hint = QLabel()
        self.keep_hint.setObjectName("hintLabel")
        self.keep_hint.setWordWrap(True)
        self.keep_label = QLabel("Keep")
        # Before starting it: does the model's enclave attest?
        self.attest = AttestationButton(self._attest_settings)

        form = QFormLayout()
        form.addRow("Title", self.title)
        form.addRow("Model", self.model)
        form.addRow("", self.attest)
        form.addRow("System prompt", self.prompt)
        form.addRow("", prompt_hint)
        form.addRow("Tail", self.tail)
        form.addRow("", tail_hint)
        form.addRow("Map", self.world_map)
        form.addRow("Your name on it", self.chat_character)
        form.addRow("", map_hint)
        form.addRow(self.keep_label, self.keep)
        form.addRow("", self.keep_hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Start chat")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.model.textChanged.connect(lambda _text: self._sync())
        self.model.textChanged.connect(self.attest.model_changed)
        self.attest.model_changed(self.model.text())
        self.keep.currentIndexChanged.connect(lambda _index: self._sync())
        self.title.textChanged.connect(lambda _text: self._sync())
        self.world_map.toggled.connect(self.chat_character.setEnabled)
        self._sync()

    def _endpoint_of(self, model: str) -> ProviderConfig | None:
        """The endpoint a model's reference resolves to: through the config
        when there is one, else the default for a bare id alone."""
        if self._config is not None:
            return self._config.endpoint_for(model)
        return self._endpoint if split_model(model)[0] is None else None

    def _route_endpoint(self, model: str) -> str | None:
        endpoint = self._endpoint_of(model)
        return endpoint.base_url if endpoint is not None else None

    def route(self):
        """The route chosen for the chat's model; None: the subscription's."""
        return self.model.effective_route()

    def _attest_settings(self) -> ProviderConfig | None:
        model = self.model.text().strip()
        endpoint = self._endpoint_of(model)
        if endpoint is None:
            return None
        return endpoint.model_copy(update={"model": split_model(model)[1]})

    @property
    def tee(self) -> bool:
        return is_tee(self.model.text().strip())

    def keep_choice(self) -> str:
        return self.keep.currentData()

    def _sync(self) -> None:
        model = self.model.text().strip()
        if is_private_mode(model):
            hints = ENCRYPTED_HINTS
        elif self.tee:
            hints = KEEP_HINTS
        else:
            hints = PLAIN_HINTS
        self.keep_hint.setText(hints[self.keep.currentData()])
        self.ok_button.setEnabled(bool(self.title.text().strip() and self.model.text().strip()))

    def _accept(self) -> None:
        if self.title.text().strip() and self.model.text().strip():
            self.accept()
