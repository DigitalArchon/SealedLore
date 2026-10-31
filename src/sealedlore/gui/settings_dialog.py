"""Provider, generation and context settings.

Tabbed because the parts have different lifetimes: the provider and its key
live in the app config, generation parameters and the context budget belong to
the story, and the archival knobs sit across both.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.image_variants import family_id, picture_support
from sealedlore.engine.portraits import portrait_size
from sealedlore.engine.reasoning import (
    LEVEL_LABELS,
    CallKind,
    default_note,
    reasoning_for,
    reasons_unasked,
)
from sealedlore.engine.reasoning import describe as describe_reasoning
from sealedlore.engine.routing import describe, routable, route_body
from sealedlore.engine.speed_test import Role, SpeedTarget, plan_targets
from sealedlore.gui.attest_check import AttestationButton
from sealedlore.gui.fields import FormScroll
from sealedlore.gui.image_jobs import ImageCatalog
from sealedlore.gui.image_picker import pick_image_model
from sealedlore.gui.model_hosts import hosts_catalog
from sealedlore.gui.model_picker import (
    Browse,
    ModelCatalog,
    ModelField,
    pick_model,
    pick_model_and_route,
)
from sealedlore.gui.phone_dialog import PHONE_KEEPS
from sealedlore.gui.settings_pages import Fold, SettingsPages
from sealedlore.gui.sizing import size_to_screen
from sealedlore.gui.speed_test import SpeedTestDialog, speed_tests
from sealedlore.gui.video_jobs import VideoCatalog
from sealedlore.models.config import (
    CHAT_API_LABELS,
    DEFAULT_BASE_URL,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_STORY_MODEL,
    ENDPOINT_SEPARATOR,
    MEDIA_API_LABELS,
    RECOMMENDED_IMAGE_MODELS,
    RECOMMENDED_MODELS,
    RECOMMENDED_TEXT_MODELS,
    Config,
    EmbeddingProviderConfig,
    MediaEndpoint,
    ProviderConfig,
    _same_host,
    chat_api,
    on_nanogpt,
    qualify,
    recommended_model,
    recommended_models,
    require_https,
    split_model,
)
from sealedlore.models.generation import (
    REASONING_LEVELS,
    GenerationParams,
    ReasoningConfig,
    ReasoningLevel,
)
from sealedlore.models.route import ModelRoute
from sealedlore.models.story import Story
from sealedlore.phone.torrc import find_tor
from sealedlore.providers import make_chat_provider
from sealedlore.providers.base import ChatProvider
from sealedlore.providers.context_probe import detect_context
from sealedlore.providers.endpoints import EndpointsProvider, listing_key
from sealedlore.providers.images import parse_image_models
from sealedlore.providers.tee import move_refusal
from sealedlore.storage import keychain
from sealedlore.storage.paths import config_file

UNSET = -1.0
UNSET_INT = -1


def _optional_double(value: float) -> float | None:
    return None if value < 0 else value


def _optional_int(value: int) -> int | None:
    return None if value < 0 else value


# Settings → Lore: how a lorebook too big to send whole is chosen from.
LORE_SELECTORS = (
    ("Model picks after each passage", "picker"),
    ("Jev before each turn", "jev"),
    ("Similarity and keywords", "similarity"),
)


def _note_label() -> QLabel:
    label = QLabel()
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    label.setTextFormat(Qt.PlainText)
    return label


def _scrolled(page: QWidget) -> QScrollArea:
    """A tab page that scrolls when it is taller than the dialog."""
    return FormScroll(page)


# What each page is for, under its name.
PAGE_ABOUT = {
    "Endpoints": "Where your models are: each service's address, and your key for it.",
    "Models": "Which model does each job: the story itself, and the small calls around it.",
    "Generation": "How the story model writes: how much it reasons, and how long a reply may run.",
    "Context": "How much of the story the storyteller is sent each turn, and how older "
    "parts are summarised into chapters.",
    "Lore": "How lore entries are chosen for each turn.",
    "Images": "Pictures of the story, portraits of its characters, and the map's pictures.",
    "Video": "Short videos of the story. Off until video has a key of its own.",
    "Private": "Private scenes: played on a model of their own, and kept out of the story's model.",
    "Phone": "Playing shared stories from a phone's Tor Browser, through SealedLore's own tor.",
}

# Settings → Context → Recall earlier detail (engine/recall.py): what is
# recalled and where, as "Config.recall|Config.recall_in" (a combo's data is
# a string: PySide6 returns None from currentData() for a tuple).
RECALL_CHOICES = (
    ("Off", "off|questions"),
    ("Questions: merged chapters", "chapters|questions"),
    ("Questions: chapters and passages", "exchanges|questions"),
    ("Questions and turns: chapters and passages", "exchanges|everywhere"),
)


class SettingsDialog(QDialog):
    def __init__(
        self,
        config: Config,
        story: Story | None,
        parent: QWidget | None = None,
        *,
        catalog: ModelCatalog | None = None,
        image_catalog: ImageCatalog | None = None,
        video_catalog: VideoCatalog | None = None,
        fetch_models: bool = False,
        root: Path | None = None,
    ) -> None:
        """`fetch_models`: load the endpoint's model list on opening (the
        window, never a test), to know which models have a choice of host.
        `root`: the data folder, to say where config.json is."""
        super().__init__(parent)
        self._root = root
        # One under each key field: where that key will be kept.
        self._key_notes: list[QLabel] = []
        self.image_catalog = image_catalog
        self.video_catalog = video_catalog
        self._fetch_models = fetch_models
        self.setWindowTitle("Settings")
        # The page list beside a page as wide as the tabs had, and tall
        # enough for most pages without scrolling (they scroll, so the
        # dialog no longer grows to its tallest page).
        self.setMinimumWidth(860)
        size_to_screen(self, 0.55, 0.86, at_least=QSize(900, 680))
        self.config = config
        self.story = story
        self._catalog = catalog
        # Browse… lists the models of the endpoint typed here, saved or not.
        self._browse: Browse | None = (
            (lambda current: pick_model(catalog, self._model_source, current, self))
            if catalog is not None
            else None
        )

        # Every page scrolls: the Lore page was taller than the dialog, and
        # its last rows were drawn over. Every text model is on Models, by
        # what it does; the rest of each page is that feature's own options.
        # The pages are a list in groups, each headed by what it is for
        # (gui/settings_pages.py); eight tabs in a row said none of it.
        self.tabs = SettingsPages()
        pages = (
            ("Connection", (("Endpoints", self._endpoint_tab), ("Models", self._models_tab))),
            (
                "Writing",
                (
                    ("Generation", self._generation_tab),
                    # Always: most of it is app-wide, and it used to be
                    # reachable (and saved) only while a story was open.
                    ("Context", self._context_tab),
                    ("Lore", self._lore_tab),
                ),
            ),
            ("Pictures", (("Images", self._images_tab), ("Video", self._video_tab))),
            ("Privacy", (("Private", self._private_tab), ("Phone", self._phone_tab))),
        )
        for group, members in pages:
            self.tabs.add_group(group)
            for name, build in members:
                self.tabs.addTab(_scrolled(build()), name, PAGE_ABOUT[name])

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        self.error = QLabel()
        self.error.setObjectName("errorLabel")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        layout.addWidget(buttons)

    # --- tabs -------------------------------------------------------------

    def _model_source(self) -> tuple[ChatProvider, str, bool]:
        """Browse… lists every endpoint's models as the endpoints stand in
        the dialog, saved or not, each fetched with its own key, through
        the routing provider (providers/endpoints.py)."""
        endpoints = self._endpoints_as_typed()
        for settings in endpoints:
            if not settings.base_url.strip():
                raise ValueError(
                    f"Set the base URL of {settings.name or 'the new endpoint'} first."
                )
            require_https(settings.base_url.strip())
        config = Config(providers=endpoints, active_provider_name=self._default_endpoint().name)
        return EndpointsProvider(config), listing_key(config), True

    # --- endpoints -------------------------------------------------------------

    def _endpoints_as_typed(self) -> list[ProviderConfig]:
        """Every endpoint as it stands in the dialog: the selected one's
        fields written into its copy first."""
        self._store_endpoint_fields()
        return self._endpoints

    def _default_endpoint(self) -> ProviderConfig:
        return self._endpoints_as_typed()[self._default_index]

    def _resolve(self, ref: str) -> tuple[ProviderConfig | None, str]:
        """(the endpoint a model reference resolves to, as typed, or None
        when it names one the dialog hasn't; its bare id)."""
        name, bare = split_model(ref.strip())
        endpoints = self._endpoints_as_typed()
        if name is None:
            return endpoints[self._default_index], bare
        return next((p for p in endpoints if p.name == name), None), bare

    def _nanogpt_endpoint(self) -> ProviderConfig | None:
        """The NanoGPT endpoint the recommended models (NanoGPT's ids) are
        for: the default one when it is NanoGPT's, else the first that is."""
        endpoints = self._endpoints_as_typed()
        default = endpoints[self._default_index]
        ordered = [default, *(p for p in endpoints if p is not default)]
        return next((p for p in ordered if on_nanogpt(p.base_url)), None)

    def _qualify_recommended(self, model_id: str) -> str:
        """A recommended id as a field should hold it: named with the NanoGPT
        endpoint when that isn't the default (models/config.py)."""
        nano = self._nanogpt_endpoint()
        if nano is None:
            return model_id
        return qualify(nano.name, model_id, self._default_endpoint().name)

    def _store_endpoint_fields(self) -> None:
        """The fields into the selected endpoint's copy. Unvalidated (a
        half-typed address): Save checks every endpoint."""
        if self._selected is None or self._loading_endpoint:
            return
        copy = self._endpoints[self._selected]
        self._endpoints[self._selected] = copy.model_copy(
            update={
                "name": self.provider_name.text().strip(),
                "base_url": self.base_url.text().strip(),
                "api_key": self.api_key.text(),
                "api": self.chat_api.currentData(),
            }
        )

    def _load_endpoint_fields(self) -> None:
        copy = self._endpoints[self._selected]
        self._loading_endpoint = True
        try:
            self.provider_name.setText(copy.name)
            self.base_url.setText(copy.base_url)
            self.api_key.setText(copy.api_key)
            self.chat_api.setCurrentIndex(max(0, self.chat_api.findData(copy.api)))
        finally:
            self._loading_endpoint = False
        self._sync_api_note()

    def _endpoint_row_text(self, index: int) -> str:
        copy = self._endpoints[index]
        host = urlparse(copy.base_url.strip()).hostname or "no address yet"
        text = f"{copy.name or '(unnamed)'} — {host}"
        return text + ("  (default)" if index == self._default_index else "")

    def _refresh_endpoint_rows(self) -> None:
        self._loading_rows = True
        try:
            self.endpoint_list.clear()
            for index in range(len(self._endpoints)):
                self.endpoint_list.addItem(self._endpoint_row_text(index))
            if self._selected is not None:
                self.endpoint_list.setCurrentRow(self._selected)
        finally:
            self._loading_rows = False
        self.remove_endpoint.setEnabled(len(self._endpoints) > 1)
        self.make_default.setEnabled(self._selected != self._default_index)

    def _on_endpoint_row(self, row: int) -> None:
        if row < 0 or self._loading_rows:
            return
        self._store_endpoint_fields()
        self._selected = row
        self._load_endpoint_fields()
        self.make_default.setEnabled(self._selected != self._default_index)
        self._show_routes()

    def _endpoint_fields_changed(self, *_args) -> None:
        """A name or address typed: the list's row follows, and the model
        fields' routes (an address off NanoGPT has none)."""
        if self._selected is None or self._loading_endpoint:
            return
        self._store_endpoint_fields()
        item = self.endpoint_list.item(self._selected)
        if item is not None:
            item.setText(self._endpoint_row_text(self._selected))
        self._show_routes()

    def _unique_endpoint_name(self, base: str) -> str:
        taken = {p.name for p in self._endpoints}
        if base not in taken:
            return base
        number = 2
        while f"{base} {number}" in taken:
            number += 1
        return f"{base} {number}"

    def _add_endpoint(self) -> None:
        """A fresh endpoint, its address to be typed (https://api.anthropic.com
        for Anthropic's own API, with an Anthropic key)."""
        self._store_endpoint_fields()
        name = self._unique_endpoint_name("New endpoint")
        self._endpoints.append(ProviderConfig(name=name).model_copy(update={"base_url": ""}))
        self._origins.append(None)
        self._selected = len(self._endpoints) - 1
        self._load_endpoint_fields()
        self._refresh_endpoint_rows()
        self.endpoints_note.setText(
            f"Added {name}. Give it a name, its address and its key; Browse… on the Models "
            "page then lists its models beside the others', and a model chosen from it "
            'names it ("Name::model").'
        )
        self.provider_name.setFocus()
        self.provider_name.selectAll()

    def _remove_endpoint(self) -> None:
        if self._selected is None or len(self._endpoints) < 2:
            self.endpoints_note.setText("Keep at least one endpoint: the default one.")
            return
        self._store_endpoint_fields()
        index = self._selected
        name = self._endpoints[index].name
        naming = [
            label for label, field in self._model_fields() if split_model(field.text())[0] == name
        ]
        if self.story is not None and any(
            split_model(model)[0] == name for model in self.story.defaults.own_models().values()
        ):
            naming.append("this story's own model")
        del self._endpoints[index]
        del self._origins[index]
        if self._default_index == index:
            self._default_index = 0
        elif self._default_index > index:
            self._default_index -= 1
        self._selected = min(index, len(self._endpoints) - 1)
        self._load_endpoint_fields()
        self._refresh_endpoint_rows()
        note = f"Removed {name}; its key goes from the keychain when you Save."
        if naming:
            note += (
                f" {', '.join(naming)} still name it and are left as they are: they will "
                f"fail to send until you change them or add an endpoint called {name} again."
            )
        self.endpoints_note.setText(note)

    def _model_fields(self) -> list[tuple[str, ModelField]]:
        """Every model field on the Models page, with its label."""
        if not hasattr(self, "_route_fields"):
            return []
        labels = {
            "story": "Story model",
            "summarisation": "Summarisation model",
            "authoring": "Authoring model",
            "scene": "Scene model",
            "plot": "Plot model",
            "lore": "Lore model",
            "image_prompt": "Image prompt writer",
        }
        return [(labels.get(role, role), field) for role, field in self._route_fields.items()]

    def _make_default(self) -> None:
        """The selected endpoint becomes where a model named without an
        endpoint is sent. The model fields on the Models page are renamed
        to keep what they mean (a bare id now names the old default; one
        naming the new default goes bare); other stories' own models aren't."""
        if self._selected is None or self._selected == self._default_index:
            return
        self._store_endpoint_fields()
        old = self._endpoints[self._default_index].name
        new = self._endpoints[self._selected].name
        for _label, field in self._model_fields():
            text = field.text().strip()
            if not text:
                continue
            name, bare = split_model(text)
            field.setText(qualify(name or old, bare, new))
        self._default_index = self._selected
        self._refresh_endpoint_rows()
        self.endpoints_note.setText(
            f"{new} is the default: a model named without an endpoint goes there. The "
            f"models on the Models page keep their endpoints ({old} is named where it was "
            "meant); a story's own model named without one now means this endpoint."
        )

    def _check_endpoints(self, endpoints: list[ProviderConfig]) -> None:
        """Raises ValueError naming what is wrong: a blank or repeated name,
        an address that isn't https."""
        seen: set[str] = set()
        for settings in endpoints:
            if not settings.name:
                raise ValueError("Every endpoint needs a name (Endpoints).")
            if settings.name in seen:
                raise ValueError(f"Two endpoints are called {settings.name!r}: rename one.")
            seen.add(settings.name)
            if ENDPOINT_SEPARATOR in settings.name:
                raise ValueError(f"An endpoint's name can't hold '{ENDPOINT_SEPARATOR}'.")
            try:
                require_https(settings.base_url.strip())
            except ValueError as exc:
                raise ValueError(f"{settings.name}: {exc}") from exc

    def _carry_keys(self, endpoints: list[ProviderConfig]) -> None:
        """A renamed endpoint's key follows it: its keychain slot is its
        name's (storage/keychain.py), and a key the keychain holds but
        couldn't give back this session is read now, or the rename refused,
        rather than lost with the old slot."""
        for origin, settings in zip(self._origins, endpoints, strict=True):
            if origin is None or origin == settings.name or settings.api_key:
                continue
            slot = f"provider:{origin}"
            if slot not in self.config.keychain_slots:
                continue
            try:
                value = keychain.read(slot, self._root)
            except keychain.KeychainError:
                value = None
            if not value:
                raise ValueError(
                    f"The key for {origin!r} is in {keychain.where()} and couldn't be read, so "
                    f"renaming it to {settings.name!r} would lose it: enter the key again, or "
                    "keep the name."
                )
            settings.api_key = value

    def _endpoint_tab(self) -> QWidget:
        """Where the story's calls go: every endpoint with its key, one the
        default (a model named without an endpoint goes there), and how
        calls are cached."""
        provider = self.config.active_provider()
        widget = QWidget()
        form = QFormLayout(widget)

        # Working copies: the list is written back on Save. `_origins` keeps
        # each one's saved name, for its key's keychain slot on a rename.
        self._endpoints = [p.model_copy(deep=True) for p in self.config.providers]
        self._origins: list[str | None] = [p.name for p in self.config.providers]
        if not self._endpoints:
            self._endpoints = [ProviderConfig(name="default", base_url=DEFAULT_BASE_URL)]
            self._origins = [None]
        self._default_index = next(
            (
                i
                for i, p in enumerate(self._endpoints)
                if provider is not None and p.name == provider.name
            ),
            0,
        )
        self._selected: int | None = self._default_index
        self._loading_endpoint = False
        self._loading_rows = False
        self._provider_at_open = provider.name if provider else None

        intro = QLabel(
            "Each endpoint has an address and a key of its own; a model chosen from one "
            'names it ("Anthropic::claude-opus-5-5"), and a model named without an '
            "endpoint goes to the default one. NanoGPT and Anthropic both serve Claude: with "
            "a key for each, Browse… on the Models page shows which is which."
        )
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)
        form.addRow(intro)

        self.endpoint_list = QListWidget()
        self.endpoint_list.setObjectName("endpointList")
        self.endpoint_list.setMaximumHeight(96)
        self.endpoint_list.setToolTip(
            "The endpoints set up; the fields below are the selected one's."
        )
        self.endpoint_list.currentRowChanged.connect(self._on_endpoint_row)
        self.add_endpoint = QPushButton("Add")
        self.add_endpoint.setToolTip("Another endpoint, with its own address and key")
        self.add_endpoint.clicked.connect(self._add_endpoint)
        self.remove_endpoint = QPushButton("Remove")
        self.remove_endpoint.setToolTip(
            "Take the selected endpoint out. Model fields that name it are left as they are."
        )
        self.remove_endpoint.clicked.connect(self._remove_endpoint)
        self.make_default = QPushButton("Make default")
        self.make_default.setToolTip(
            "A model named without an endpoint is sent to the default one. The fields on the "
            "Models page are renamed to keep what they mean."
        )
        self.make_default.clicked.connect(self._make_default)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.add_endpoint)
        buttons.addWidget(self.remove_endpoint)
        buttons.addWidget(self.make_default)
        buttons.addStretch(1)
        self.endpoints_note = _note_label()
        form.addRow(self.endpoint_list)
        form.addRow(buttons)
        form.addRow(self.endpoints_note)

        self.provider_name = QLineEdit()
        self.provider_name.setToolTip(
            'How the endpoint is named in a model\'s reference ("Name::model") and in the '
            "picker's Provider column."
        )
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://api.anthropic.com")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.provider_name.textChanged.connect(self._endpoint_fields_changed)
        self.base_url.textChanged.connect(self._endpoint_fields_changed)
        self.api_key.textChanged.connect(self._endpoint_fields_changed)
        self.key_storage = QComboBox()
        self.key_storage.addItem("System keychain", "keychain")
        self.key_storage.addItem("config.json, in plain text", "file")
        self.key_storage.setCurrentIndex(max(0, self.key_storage.findData(self.config.key_storage)))
        self.key_storage.setToolTip(
            "Every API key on these pages: the system keychain when this computer has "
            "one, or SealedLore's config.json in plain text. Plain text is for the "
            "command line and test setups."
        )
        self.key_storage.currentIndexChanged.connect(self._sync_key_notes)
        # What the endpoint speaks: OpenAI's /chat/completions, or Anthropic's
        # own Messages API (providers/anthropic.py). From the address knows
        # Anthropic's host; anything else is OpenAI-compatible.
        self.chat_api = QComboBox()
        for value, label in CHAT_API_LABELS.items():
            self.chat_api.addItem(label, value)
        self.chat_api.setToolTip(
            "Which protocol the address speaks. From the address takes api.anthropic.com "
            "as Anthropic's own API and anything else as OpenAI-compatible."
        )
        self.api_note = _note_label()
        self.chat_api.currentIndexChanged.connect(self._sync_api_note)
        self.chat_api.currentIndexChanged.connect(self._endpoint_fields_changed)
        self.base_url.textChanged.connect(self._sync_api_note)

        self.cache_mode = QComboBox()
        self.cache_mode.addItems(["auto", "on", "off"])
        self.cache_mode.setCurrentText(self.config.cache_control_mode)
        self.cache_ttl = QComboBox()
        self.cache_ttl.addItem("1 hour", "1h")
        self.cache_ttl.addItem("5 minutes", "5m")
        self.cache_ttl.setCurrentIndex(max(0, self.cache_ttl.findData(self.config.cache_ttl)))
        self.cache_ttl.setToolTip(
            "How long a Claude model keeps the story's cached prompt between turns. A pause "
            "longer than this rewrites the whole prompt at full price. An hour costs a "
            "little more per turn and survives the pauses of real play."
        )

        form.addRow("Name", self.provider_name)
        form.addRow("Base URL", self.base_url)
        form.addRow("Speaks", self.chat_api)
        form.addRow("API key", self.api_key)
        form.addRow(self.api_note)
        form.addRow("Keep API keys in", self.key_storage)
        form.addRow(self._key_note())
        hint = QLabel(
            "Any OpenAI-compatible /chat/completions endpoint, or Anthropic's own API "
            "(https://api.anthropic.com with an Anthropic key). Which models run where is "
            "on the Models page."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        form.addRow(hint)
        caching = Fold(form, "Prompt caching (advanced)")
        caching.add_row("Prompt caching", self.cache_mode)
        caching.add_row("Cache lifetime", self.cache_ttl)
        caching_hint = QLabel(
            "On 'auto', cache_control blocks go only to Anthropic models, on every "
            "endpoint, so a long story's unchanging start is read from the cache each turn "
            "instead of paid for again."
        )
        caching_hint.setObjectName("hintLabel")
        caching_hint.setWordWrap(True)
        caching.add_row(caching_hint)
        self._load_endpoint_fields()
        self._refresh_endpoint_rows()
        return widget

    def _sync_api_note(self, *_args) -> None:
        """What the key is sent as, and what the choice of protocol means."""
        if chat_api(self.base_url.text(), self.chat_api.currentData()) == "anthropic":
            self.api_note.setText(
                "Anthropic's own Messages API: the key is sent as its x-api-key header. "
                "Models are Anthropic's own ids (claude-sonnet-4-6); every call is "
                "unpriced, since Anthropic lists no prices; thinking follows the reasoning "
                "levels under Generation; pictures, videos and embeddings need endpoints "
                "of their own."
            )
        else:
            self.api_note.setText(
                "An OpenAI-compatible endpoint: the key is sent as a Bearer token."
            )

    def _key_place(self) -> str:
        """Where a key typed here goes, as the choice stands."""
        path = config_file(self._root)
        if self.key_storage.currentData() == "file":
            return f"Saved in plain text in {path}."
        problem = keychain.problem()
        if problem is not None:
            return f"{problem.capitalize()}, so it is saved in plain text in {path}."
        if keychain.last_problem:
            return (
                f"Kept in {keychain.where()}, but {keychain.last_problem}, so for now it is in "
                f"plain text in {path}."
            )
        return f"Saved in {keychain.where()}, not in config.json."

    def _key_note(self) -> QLabel:
        label = _note_label()
        label.setText(self._key_place())
        self._key_notes.append(label)
        return label

    def _sync_key_notes(self) -> None:
        text = self._key_place()
        for label in self._key_notes:
            label.setText(text)

    def _route_endpoint(self, model: str) -> str | None:
        """The address of the endpoint a model's reference resolves to, as
        typed, for the field's route; None for one the dialog hasn't."""
        endpoint, _bare = self._resolve(model)
        return (endpoint.base_url.strip() or None) if endpoint is not None else None

    def _routed_field(self, text: str, role: str = "") -> ModelField:
        """A role's model field, with its route beside it (NanoGPT only) and
        the models measured for it under Recommended."""
        browse = self._browse_route if self._catalog is not None else None
        return ModelField(
            text,
            self._browse,
            route_endpoint=self._route_endpoint,
            browse_route=browse,
            hosts_of=self._hosts_of,
            recommended=RECOMMENDED_TEXT_MODELS.get(role, ()),
            qualify_recommended=self._qualify_recommended,
        )

    def _hosts_of(self, model: str) -> tuple[str, ...] | None:
        """A model's hosts from the endpoint's list; None until it's loaded."""
        models = self._catalog.models if self._catalog is not None else None
        if not models:
            return None
        info = models.get(model)
        return info.hosts if info is not None else ()

    def _browse_route(self, current: str, route: ModelRoute | None):
        return pick_model_and_route(
            self._catalog, self._model_source, current, route, self._route_endpoint, self
        )

    def _show_routes(self, *_args) -> None:
        for field in getattr(self, "_route_fields", {}).values():
            field._show_route()

    def _models_tab(self) -> QWidget:
        """Every text model the app calls, by what it does. They were spread
        over five tabs (Provider, Context, Lore, Images, Private)."""
        provider = self.config.active_provider()
        widget = QWidget()
        form = QFormLayout(widget)
        intro = QLabel(
            "Every model the app calls on this endpoint, by what it does. A blank one "
            "uses the model named in grey. The image model is under Images, and the "
            "private scene model under Private. On NanoGPT, Route… beside a model "
            "chooses which of its hosts serves it (billed pay-as-you-go)."
        )
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)
        form.addRow(intro)
        if self.story is not None and not self.story.chat and self.story.defaults.own_models():
            own = QLabel(
                "The open story has models of its own, which stay as they are: Story → Setup "
                "→ Models."
            )
            own.setObjectName("hintLabel")
            own.setWordWrap(True)
            form.addRow(own)

        # A new setup starts on Sonnet 4.6 rather than a blank field: one
        # thing fewer before the first story.
        self.model = self._routed_field(
            (provider.model if provider else "") or DEFAULT_STORY_MODEL, "story"
        )
        self.model.setPlaceholderText(DEFAULT_STORY_MODEL)
        self.model.setToolTip("The storyteller: writes every passage.")
        # A story goes with this unless it has a model of its own (Story →
        # Setup → Models); a chat has its own, and only a change made here
        # moves it. Compared with the field as it opened, so the default filled
        # into a blank field doesn't count as a change either.
        self._model_at_open = self.model.text().strip()
        form.addRow("Story model", self.model)

        self.summarization_model = self._routed_field(self.config.summarization_model or "")
        self.summarization_model.setPlaceholderText("same as the story model")
        self.summarization_model.setToolTip(
            "Writes the chapters as the story is archived, and merges them."
        )
        form.addRow("Summarisation model", self.summarization_model)

        self.authoring_model = self._routed_field(self.config.authoring_model or "")
        self.authoring_model.setPlaceholderText("same as the story's model")
        self.authoring_model.setToolTip(
            "Drafts stories from a premise and reviews settings. Occasional calls the "
            "whole story rests on, so a stronger model (e.g. anthropic/claude-opus-5) "
            "can be worth it here."
        )
        form.addRow("Authoring model", self.authoring_model)

        self.keep_scene = QCheckBox("Keep the scene up to date after every passage")
        self.keep_scene.setChecked(self.config.scene_reads == "every_turn")
        self.keep_scene.setToolTip(
            "After each passage, a small model reads who came in, who left, where the "
            "scene moved and whether it is private, and the Scene tab follows (with an "
            "Undo). One extra call per passage, on the scene model below, or on the "
            "story's summarisation model (then its story model) when that is blank. "
            "Off, the roster is kept by hand as before."
        )
        form.addRow(self.keep_scene)
        # A new setup starts with the recommended models for the small calls
        # (models/config.py); one already saved keeps what it has, blanks too.
        fresh = recommended_models(self._default_endpoint().base_url) if provider is None else {}
        self.scene_model = self._routed_field(
            self.config.scene_model or fresh.get("scene_model", ""), "scene_model"
        )
        self.scene_model.setPlaceholderText("same as the story's summarisation model")
        self.scene_model.setToolTip(
            "Reads the scene after every passage: about 2k tokens in and 150 out, on "
            "every turn, and in a story with a plot the plot's clock and facts beside "
            "it. It needs a model that reasons: fast ones that don't miss about half "
            "the arrivals and departures. Use recommended models fills in the one that "
            "measured best."
        )
        form.addRow("Scene model", self.scene_model)

        self.plot_reads = QCheckBox("Keep a plot's clock and facts after every passage")
        self.plot_reads.setChecked(self.config.plot_reads)
        self.plot_reads.setToolTip(
            "Only for stories with a plot. After each passage, the scene model reads how "
            "much story time passed and which of the plot's facts changed, and the Plot "
            "tab follows (with an Undo). Off, they change only when you set them; an event "
            "the storyteller was told to begin still counts as begun when the passage "
            "brings in who it brings, and one never shown after six passages is taken as "
            "happened (or waits again), so no event is sent over and over."
        )
        form.addRow(self.plot_reads)
        self.plot_model = self._routed_field(
            self.config.plot_model or fresh.get("plot_model", ""), "plot_model"
        )
        self.plot_model.setPlaceholderText("same as the scene model")
        self.plot_model.setToolTip(
            "Decides when a plot's events happen: a small call before the passage "
            "starts, on the turns an event could begin, so a quick, steady model is "
            "right here. Use recommended models fills in the one that measured best."
        )
        form.addRow("Plot model", self.plot_model)

        self.lore_model = self._routed_field(self.config.lore_model, "lore_model")
        self.lore_model.setPlaceholderText("blank: same as the scene model")
        self.lore_model.setToolTip(
            "Picks lore after each passage when a lorebook is too large to send whole and "
            'Lore → Large lorebooks is "Model picks". The default is a NanoGPT model id; '
            "on another endpoint pick one it lists (Browse…), or leave it blank to use "
            "the scene model."
        )
        form.addRow("Lore model", self.lore_model)

        self.image_prompt_model = self._routed_field(self.config.image_prompt_model or "")
        self.image_prompt_model.setPlaceholderText("same as the story's model")
        self.image_prompt_model.setToolTip(
            "Writes the image prompt from the story. The story's own model rides its cached "
            "prompt, so it is cheap and has read everything."
        )
        form.addRow("Image prompt writer", self.image_prompt_model)

        self.recommended = QPushButton("Use recommended models")
        self.recommended.setToolTip(
            "Fill in the models that measured well for the small calls made on every turn "
            "(the scene, the plot, picking lore). Left blank they run on the story model, "
            "which costs far more. Nothing is saved until you press Save."
        )
        self.recommended.clicked.connect(self._fill_recommended)
        self.recommended_note = QLabel()
        self.recommended_note.setObjectName("hintLabel")
        self.recommended_note.setWordWrap(True)
        self.recommended_note.hide()
        self.speed_test = QPushButton("Test speed…")
        self.speed_test.setToolTip(
            "Send each model chosen here (and the private model) one short request and "
            "show how long its first word takes and how fast the rest comes. A model used "
            "for several things is tested once. Uses the fields as they stand, saved or not."
        )
        self.speed_test.clicked.connect(self._test_speed)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.recommended)
        row.addWidget(self.speed_test)
        row.addStretch(1)
        form.addRow(row)
        form.addRow(self.recommended_note)

        # Each role's route: which of NanoGPT's hosts serves its model.
        self._route_fields = {
            "story": self.model,
            "summarisation": self.summarization_model,
            "authoring": self.authoring_model,
            "scene": self.scene_model,
            "plot": self.plot_model,
            "lore": self.lore_model,
            "image_prompt": self.image_prompt_model,
        }
        for role, field in self._route_fields.items():
            field.set_route(self.config.model_routes.get(role))
        # A chat open: its own route is the Story field's, as its model is.
        self._route_at_open = self.model.effective_route()
        self.base_url.textChanged.connect(self._show_routes)
        # Which models have a choice of host is in the endpoint's list: the
        # one Browse… uses, fetched now on NanoGPT (kept an hour).
        self._watching_catalog = self._catalog is not None
        if self._catalog is not None:
            self._catalog.changed.connect(self._show_routes)
            if self._fetch_models and any(on_nanogpt(p.base_url) for p in self._endpoints):
                self._catalog.ensure(self._model_source)
        return widget

    def speed_targets(self) -> list[SpeedTarget]:
        """Every distinct model the fields name, as they stand, each on the
        endpoint its reference resolves to (as typed). Raises ValueError
        when an endpoint can't be used as typed."""
        endpoints = self._endpoints_as_typed()
        for settings in endpoints:
            if not settings.base_url.strip():
                raise ValueError(
                    f"Set the base URL of {settings.name or 'the new endpoint'} first."
                )
            require_https(settings.base_url.strip())

        def role(
            name: str, field: ModelField, fallback: str | None = None, kind: CallKind = "side"
        ) -> Role:
            # Each field's route, as its role sends it (engine/routing.py);
            # a reference naming an endpoint the dialog hasn't is left out.
            model = field.text().strip()
            endpoint, _bare = self._resolve(model) if model else (None, "")
            route = field.effective_route()
            return Role(
                name,
                field.text(),
                endpoint,
                fallback,
                route_body(route, model),
                describe(route),
                kind=kind,
            )

        roles = [role("Story", self.model, kind="story")]
        roles += self._story_roles()
        roles += [
            role("Summarisation", self.summarization_model, "Story"),
            role("Authoring", self.authoring_model, "Story"),
            role("Scene", self.scene_model, "Summarisation"),
            role("Plot", self.plot_model, "Scene"),
            role("Lore", self.lore_model, "Scene"),
            role("Image prompt writer", self.image_prompt_model, "Story"),
        ]
        private = self._private_test_settings()
        if private is not None and private.model.strip():
            roles.append(Role("Private", private.model, private, kind="story"))
        # The levels as they stand in the Generation tab, saved or not.
        return plan_targets(roles, reasoning_for=self._sent_to)

    def _story_roles(self) -> list[Role]:
        """The open story's own models, as its calls send them."""
        if self.story is None:
            return []
        defaults = self.story.defaults
        roles = []
        for role, own in defaults.own_models().items():
            if role == "story" and own == self.model.text().strip():
                continue
            route = (
                defaults.main_route
                if role == "story" and self.story.chat
                else defaults.model_routes.get(role)
            )
            endpoint, _bare = self._resolve(own)
            body = (
                route_body(route, own)
                if endpoint is not None and routable(endpoint.base_url, own)
                else {}
            )
            name = "This story" if role == "story" else f"This story: {role.replace('_', ' ')}"
            kind = "story" if role == "story" else "side"
            roles.append(Role(name, own, endpoint, None, body, describe(route), kind=kind))
        return roles

    def _test_speed(self) -> None:
        try:
            targets = self.speed_targets()
        except ValueError as exc:
            self.recommended_note.setText(str(exc))
            self.recommended_note.show()
            return
        dialog = SpeedTestDialog(targets, self, prices=self.config.model_prices)
        dialog.exec()
        dialog.deleteLater()

    def _fill_recommended(self) -> None:
        """Put the recommended model in each field it has one for. The ids are
        NanoGPT's: named with the NanoGPT endpoint when that isn't the default
        one, and with no NanoGPT endpoint the author is told where to look."""
        nano = self._nanogpt_endpoint()
        found = recommended_models(nano.base_url) if nano is not None else {}
        filled = []
        for name, model in found.items():
            ref = self._qualify_recommended(model)
            getattr(self, name).setText(ref)
            filled.append(ref)
        self.recommended_note.setText(
            "Filled in: " + ", ".join(filled) + ". Save to keep them."
            if found
            else "The recommendations are NanoGPT model ids. On these endpoints, choose a "
            "small, fast model for the scene and plot with Browse…"
        )
        self.recommended_note.show()

    def _role_model(self, field: ModelField) -> str:
        """A role's model as typed. A recommended id left in the field of a
        new setup that turned out not to be on NanoGPT is a blank: the id
        would fail on every turn there."""
        text = field.text().strip()
        if (
            self._provider_at_open is None
            and text in RECOMMENDED_MODELS.values()
            and not recommended_models(self._default_endpoint().base_url)
        ):
            return ""
        return text

    def _generation_tab(self) -> QWidget:
        """For every story: it was each story's own, and so could not be set
        with no story open (the author, Sept 30 2026)."""
        params = self.config.generation
        widget = QWidget()
        form = QFormLayout(widget)

        self.temperature = self._double_field(params.temperature, maximum=2.0)
        self.top_p = self._double_field(params.top_p, maximum=1.0)
        self.max_tokens = self._int_field(params.max_tokens, maximum=200_000)
        self.presence_penalty = self._double_field(params.presence_penalty, maximum=2.0)
        self.frequency_penalty = self._double_field(params.frequency_penalty, maximum=2.0)
        self.seed = self._int_field(params.seed, maximum=2_147_483_647)
        self.stop = QLineEdit(", ".join(params.stop))
        self.stop.setPlaceholderText("comma-separated")

        self.reasoning_story = self._level_combo(self.config.reasoning_story)
        self.reasoning_side = self._level_combo(self.config.reasoning_side)
        self.reasoning_story_note = _note_label()
        self.reasoning_side_note = _note_label()
        self.reasoning_caught = _note_label()
        self.reasoning_story.currentIndexChanged.connect(self._show_reasoning)
        self.reasoning_side.currentIndexChanged.connect(self._show_reasoning)
        self.model.textChanged.connect(self._show_reasoning)

        # How much the models reason first: it is what decides how long a
        # turn waits. The sampling numbers most people never touch.
        form.addRow("Story model reasoning", self.reasoning_story)
        form.addRow(self.reasoning_story_note)
        form.addRow("Other calls' reasoning", self.reasoning_side)
        form.addRow(self.reasoning_side_note)
        form.addRow(self.reasoning_caught)
        self.max_tokens.setToolTip(
            "The longest a reply may run, reasoning included. The passage's length is "
            "set by Length in the composer and the Style page, not this."
        )
        form.addRow("Max tokens", self.max_tokens)

        sampling = Fold(form, "Sampling (advanced)")
        intro = QLabel("For the story model's passages and questions, in every story:")
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)
        sampling.add_row(intro)
        sampling.add_row("Temperature", self.temperature)
        sampling.add_row("Top p", self.top_p)
        sampling.add_row("Presence penalty", self.presence_penalty)
        sampling.add_row("Frequency penalty", self.frequency_penalty)
        sampling.add_row("Seed", self.seed)
        sampling.add_row("Stop sequences", self.stop)
        hint = QLabel("Negative values mean 'leave unset' — the field is then omitted.")
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        sampling.add_row(hint)
        self._show_reasoning()
        return widget

    def _level_combo(self, level: str) -> QComboBox:
        combo = QComboBox()
        for value in REASONING_LEVELS:
            combo.addItem(LEVEL_LABELS[value], value)
        combo.setCurrentIndex(max(0, combo.findData(level)))
        combo.setToolTip(
            "As low as possible sends nothing to a model that reasons only when asked, "
            "and its lowest level to one that reasons anyway (NanoGPT says which models "
            "do; any other is noticed from its replies). The other levels map to the "
            "nearest one the model lists."
        )
        return combo

    def _reasoning_levels(self) -> dict[str, ReasoningLevel]:
        return {
            "story": self.reasoning_story.currentData(),
            "side": self.reasoning_side.currentData(),
        }

    def _sent_to(self, kind: CallKind, model: str) -> ReasoningConfig:
        """What the level as it stands would send `model`, from what the
        config knows of it (nothing is fetched here)."""
        facts = self.config.model_reasoning.get(model)
        return reasoning_for(self._reasoning_levels()[kind], model, facts)

    def _show_reasoning(self) -> None:
        story_model = self.model.text().strip()
        if story_model:
            sent = describe_reasoning(self._sent_to("story", story_model))
            own = default_note(self.config.model_reasoning.get(story_model))
            self.reasoning_story_note.setText(f"Sends {story_model}: {sent}. {own}".rstrip())
        else:
            self.reasoning_story_note.setText("")
        self.reasoning_side_note.setText(
            "For the scene and plot reads, summaries, the review, drafts and the picture "
            "prompt writer."
        )
        caught = sorted(
            model for model, facts in self.config.model_reasoning.items() if reasons_unasked(facts)
        )
        self.reasoning_caught.setText(
            "Caught reasoning without being asked, so asked for their lowest level: "
            + ", ".join(caught)
            + "."
            if caught
            else ""
        )
        self.reasoning_caught.setVisible(bool(caught))

    def _context_tab(self) -> QWidget:
        """Budget and archival (§5). Shown with or without a story: only the
        budget belongs to one."""
        widget = QWidget()
        form = QFormLayout(widget)

        self.budget = QSpinBox()
        self.budget.setRange(1_000, 1_000_000)
        self.budget.setSingleStep(1_000)
        self.budget.setValue(
            self.story.defaults.context_token_budget if self.story is not None else 60_000
        )
        self.budget.setToolTip(
            "How many tokens the storyteller's whole prompt may use each turn: the rules, "
            "world, cast and chapter summaries as well as the recent story word for word. "
            "More keeps more of the story verbatim, at a higher cost per turn. Claude "
            "Sonnet's storytelling degrades past about 60,000."
        )
        self.budget.setEnabled(self.story is not None)

        self.auto_archive = QCheckBox("Archive automatically when the budget is reached")
        self.auto_archive.setChecked(self.config.auto_archive)
        self.auto_archive.setToolTip(
            "Chapters are written in the background as the story nears its budget, so no "
            "turn waits for them. Off, a banner offers to archive when the prompt is over "
            "budget, and the oldest turns are left out until you do."
        )

        self.story_ledger = QCheckBox("Keep a story ledger beside the chapters")
        self.story_ledger.setChecked(self.config.story_ledger)
        self.story_ledger.setToolTip(
            "A point-form record, updated with each chapter, of how people stand with each "
            "other, who knows what, open promises and where things stand. It keeps quiet "
            "facts that summaries lose, for about 1,200 more prompt tokens and one more "
            "call per chapter."
        )

        self.plot_chapter_days = QCheckBox("Head a plot story's chapters with their days")
        self.plot_chapter_days.setChecked(self.config.plot_chapter_days)
        self.plot_chapter_days.setToolTip(
            "In a story with a plot, each chapter summary is headed with the story days it "
            'covers, from the plot\'s clock ("Chapter 4 · days 12–15"), so the storyteller '
            "can tell how long ago things happened. Never in a story without a plot."
        )

        self.chunk_turns = QSpinBox()
        self.chunk_turns.setRange(2, 100)
        self.chunk_turns.setValue(self.config.archive_chunk_turns)

        self.archive_target = QSpinBox()
        self.archive_target.setRange(20, 95)
        self.archive_target.setSingleStep(5)
        self.archive_target.setSuffix("%")
        self.archive_target.setValue(round(self.config.archive_target_ratio * 100))
        self.archive_target.setToolTip(
            "Once archival starts it keeps taking whole chapters until the prompt is "
            "this share of the budget. Lower means fewer, larger archival runs and "
            "many more turns between them; higher means it stops sooner and returns "
            "to the budget within a few turns."
        )

        self.summary_words = QSpinBox()
        self.summary_words.setRange(50, 2_000)
        self.summary_words.setSingleStep(25)
        self.summary_words.setValue(self.config.summary_target_words)

        self.recall = QComboBox()
        for label, value in RECALL_CHOICES:
            self.recall.addItem(label, value)
        current = f"{self.config.recall}|{self.config.recall_in}"
        self.recall.setCurrentIndex(
            next(
                (i for i, (_, value) in enumerate(RECALL_CHOICES) if value == current),
                next(
                    i
                    for i, (_, value) in enumerate(RECALL_CHOICES)
                    if value.split("|")[0] == self.config.recall
                ),
            )
        )
        self.recall.setToolTip(
            "Merging condenses old chapters into a part, and detail goes. With recall, "
            "when you ask the storyteller a question (Speaking as → Question) the chapters a "
            "part stands for, or the archived passages themselves, come back into its prompt "
            "if they are about it: two at most, found by the words they share with your "
            "question and by your embeddings model. Passages are embedded the first time "
            "(about 25 seconds for a 250-exchange story), chapters in a few. In story turns "
            "too, recall brought old states back as if they were current in testing, and "
            "turns rarely needed it: use it there with care."
        )

        self.recall_carry = QSpinBox()
        self.recall_carry.setRange(0, 20)
        self.recall_carry.setSpecialValueText("Off")
        self.recall_carry.setSuffix(" turns")
        self.recall_carry.setValue(self.config.recall_carry_turns)
        self.recall_carry.setToolTip(
            "What a Question recalled stays in the storyteller's prompt for this many of "
            "your turns after it, on that branch, so the story keeps to what you were just "
            "told. Off: only the Question sees it."
        )

        scope = QLabel(
            "The context budget belongs to the open story; the rest applies to every story."
            if self.story is not None
            else "The context budget is set per story (open one); the rest applies to every story."
        )
        scope.setObjectName("hintLabel")
        scope.setWordWrap(True)
        form.addRow(scope)
        form.addRow("Context budget", self.budget)
        form.addRow(self.auto_archive)
        chapters = Fold(form, "Chapters and recall (advanced)")
        chapters.add_row(self.story_ledger)
        chapters.add_row(self.plot_chapter_days)
        chapters.add_row("Turns per chapter", self.chunk_turns)
        chapters.add_row("Archive down to", self.archive_target)
        chapters.add_row("Summary length (words)", self.summary_words)
        chapters.add_row("Recall earlier detail", self.recall)
        chapters.add_row("Keep a Question's recall for", self.recall_carry)

        hint = QLabel(
            "Archival summarises the oldest whole chunk of the story rather than "
            "one turn at a time, so the summary block stays stable and the cached "
            "prompt prefix survives. The most recent turns always stay verbatim. "
            "When it runs it keeps going until the prompt is back down to the share "
            "of the budget above, so it happens rarely rather than every few turns. "
            "The model that writes the chapters is on the Models tab."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        form.addRow(hint)
        return widget

    def done(self, result: int) -> None:  # noqa: N802 - Qt naming
        # The private tab's own catalog fetches on a thread parented to this
        # dialog; closing under it would destroy a running QThread.
        self._private_catalog.wait()
        speed_tests().wait()
        hosts_catalog().wait()
        if self._watching_catalog:
            # The window's catalog outlives the dialog; once only (a second
            # disconnect warns).
            self._watching_catalog = False
            self._catalog.changed.disconnect(self._show_routes)
        super().done(result)

    def _private_source(self) -> tuple[ChatProvider, str, bool]:
        url = self.private_url.text().strip()
        if not url:
            raise ValueError("Set the private endpoint's base URL first.")
        require_https(url)
        # A blank key is the default endpoint's, on its own host (Config.private).
        default = self._default_endpoint()
        key = self.private_key.text() or (
            default.api_key if _same_host(url, default.base_url.strip()) else ""
        )
        provider = make_chat_provider(
            ProviderConfig(name="private-browse", base_url=url, api_key=key)
        )
        return provider, url, True

    def _private_test_settings(self) -> ProviderConfig | None:
        """The private endpoint and model as typed, for Test attestation."""
        try:
            provider, _url, _owned = self._private_source()
        except ValueError:
            return None
        provider.close()
        return provider.config.model_copy(update={"model": self.private_model.text().strip()})

    def _phone_tab(self) -> QWidget:
        """Phone access (gui/window_phone.py): only whether it starts with the
        app, and whether a phone connecting is said. Turning it on and off,
        pairing and the stories shared are File → Phone access and Phone devices…."""
        widget = QWidget()
        form = QFormLayout(widget)
        intro = QLabel(
            "Phone access lets a phone play the stories you share, from Tor Browser: "
            "SealedLore runs its own tor and gives this computer an onion address, so "
            "nothing has to be set up on your router and no port is opened. Turn it on for "
            "this run with File → Phone access; pair phones and choose the stories they may "
            "see in File → Phone devices…. Stories that keep a map stay on the desktop for now."
        )
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)
        form.addRow(intro)
        self.phone_on_launch = QCheckBox("Turn phone access on when SealedLore starts")
        self.phone_on_launch.setChecked(self.config.phone.start_on_launch)
        form.addRow(self.phone_on_launch)
        self.phone_notify = QCheckBox("Say when a phone connects")
        self.phone_notify.setChecked(self.config.phone.notify_connect)
        form.addRow(self.phone_notify)
        tor = find_tor()
        if tor is None:
            where = (
                "Phone access is for Linux for now."
                if sys.platform == "win32"
                else "No tor was found. The AppImage brings its own; running from source, "
                "install it (for example: sudo apt install tor). Only the program is used, "
                "never a tor service the system runs."
            )
        elif os.environ.get("SEALEDLORE_TOR") == str(tor):
            where = "The tor that comes with SealedLore."
        else:
            where = str(tor)
        tor_label = QLabel(where)
        tor_label.setWordWrap(True)
        tor_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Tor:", tor_label)
        keeps = QLabel(PHONE_KEEPS)
        keeps.setObjectName("hintLabel")
        keeps.setWordWrap(True)
        form.addRow(keeps)
        return widget

    def _private_tab(self) -> QWidget:
        """The private model: where private scenes go, and nothing else does."""
        existing = self.config.private_provider
        widget = QWidget()
        form = QFormLayout(widget)
        intro = QLabel(
            "Private scenes (the button beside the composer) are played on this model "
            "alone: nothing from them reaches the story's model or any other, and only the "
            "summary you approve goes back into the story. A local server (Ollama "
            "http://localhost:11434/v1, LM Studio http://localhost:1234/v1, llama.cpp "
            "http://localhost:8080/v1) keeps it on this machine. On NanoGPT, a private/ "
            "model is end-to-end encrypted to an attested enclave; a TEE/ model runs in "
            "one, but its text still passes NanoGPT's gateway."
        )
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)
        form.addRow(intro)
        # NanoGPT's endpoint and, with the key blank, the main key: only the
        # model is left to choose. Nothing is saved until one is, so private
        # scenes stay off until the author has looked this over.
        self.private_url = QLineEdit(existing.base_url if existing else DEFAULT_BASE_URL)
        self.private_url.setPlaceholderText("http://localhost:11434/v1")
        self.private_key = QLineEdit(existing.api_key if existing else "")
        self.private_key.setEchoMode(QLineEdit.Password)
        self.private_key.setPlaceholderText(
            "blank: the main endpoint's key on the same host, else none"
        )
        self._private_catalog = ModelCatalog(self)

        def browse(current: str) -> str | None:
            return pick_model(self._private_catalog, self._private_source, current, self)

        self.private_model = ModelField(existing.model if existing else "", browse)
        self.private_model.setPlaceholderText("e.g. llama3.1:8b, or TEE/glm-5.3-flash")
        self.private_budget = QSpinBox()
        self.private_budget.setRange(2_000, 1_000_000)
        self.private_budget.setSingleStep(1_000)
        self.private_budget.setValue(self.config.private_budget)
        self.private_budget.setSuffix(" tokens")
        self.detect_button = QPushButton("Detect")
        self.detect_button.setToolTip("Ask the server how much context the model is running with")
        self.detect_button.clicked.connect(self._detect_private_context)
        budget_row = QHBoxLayout()
        budget_row.addWidget(self.private_budget, 1)
        budget_row.addWidget(self.detect_button)
        self.detect_note = QLabel(
            "Ollama often runs a model with only 4-8k of context whatever it could take: "
            "raise it with OLLAMA_CONTEXT_LENGTH or a Modelfile's num_ctx."
        )
        self.detect_note.setObjectName("hintLabel")
        self.detect_note.setWordWrap(True)
        self.private_tee = QCheckBox("Check TEE models")
        self.private_tee.setToolTip(
            "What is checked: the enclave's Intel TDX quote, in its own signed bytes, binds "
            "its signing key to a fresh nonce of ours; the quote verifies up to Intel's "
            "root, isn't revoked and isn't a debug enclave, and Intel rates its platform; "
            "NVIDIA's signed verdict on the GPUs is for our nonce and passes; each reply's "
            "signature record recovers the attested key. Anything forged, revoked or for "
            "another nonce is refused and nothing is sent; what couldn't be checked is "
            "shown as partial. Not checked: which software the enclave runs, and the "
            "reply's content against its record. The text still passes NanoGPT's gateway. "
            "A private/ model is always attested, whatever this says: it is end-to-end "
            "encrypted to the attested enclave's key."
        )
        self.private_tee.setChecked(self.config.private_verify_tee)
        form.addRow("Base URL", self.private_url)
        form.addRow("API key", self.private_key)
        form.addRow(self._key_note())
        form.addRow("Model", self.private_model)
        # Before choosing it: does this model's enclave attest? (The author:
        # rather than start a scene and have it refused.)
        self.private_attest = AttestationButton(self._private_test_settings)
        self.private_model.textChanged.connect(self.private_attest.model_changed)
        self.private_attest.model_changed(self.private_model.text())
        form.addRow("", self.private_attest)
        form.addRow("Context budget", budget_row)
        form.addRow("", self.detect_note)
        form.addRow(self.private_tee)
        # Its own line: as the checkbox's text it ran off the dialog.
        tee_note = QLabel(
            "For a NanoGPT TEE/ model: the enclave's attestation (Intel's and NVIDIA's "
            "signatures, for our nonce) when the scene begins, nothing sent if it's refused, "
            "and that each reply is signed by the attested key (the tooltip says what that "
            "does and doesn't prove). A private/ model is always attested."
        )
        tee_note.setObjectName("hintLabel")
        tee_note.setWordWrap(True)
        form.addRow(tee_note)
        return widget

    def _detect_private_context(self) -> None:
        url, model = self.private_url.text().strip(), self.private_model.text().strip()
        if not url or not model:
            self.detect_note.setText("Set the base URL and model first.")
            return
        try:
            require_https(url)
        except ValueError as exc:
            self.detect_note.setText(str(exc))
            return
        key = self.private_key.text() or (
            self.api_key.text() if _same_host(url, self.base_url.text().strip()) else ""
        )
        self.detect_button.setEnabled(False)
        self.detect_note.setText("Asking the server…")
        QApplication.processEvents()
        found = detect_context(url, key, model)
        self.detect_button.setEnabled(True)
        if found is None:
            self.detect_note.setText(
                "The server didn't say. Set the budget by hand, a little under the model's context."
            )
            return
        # Leave room for the reply and a margin for counting differences.
        budget = max(2_000, int(found.tokens * 0.8) - 1_000)
        self.private_budget.setValue(min(budget, self.private_budget.maximum()))
        note = f"{found.tokens:,} tokens ({found.source}); budget set to {budget:,}."
        if not found.loaded:
            note += " That's the most it can take: check what it's loaded with."
        self.detect_note.setText(note)

    def _images_tab(self) -> QWidget:
        """Pictures: which model draws them, at what size, and who writes the prompt."""
        widget = QWidget()
        form = QFormLayout(widget)
        endpoint = self.config.image_endpoint
        self.image_url, self.image_key, self.image_api = self._media_fields(endpoint)
        self.image_url.setPlaceholderText("same as the chat endpoint")
        self.image_key.setPlaceholderText("same as the chat key (same host only)")
        form.addRow(QLabel("<b>Pictures of the story</b>"))
        self._image_models = parse_image_models({"data": self.config.image_models})
        # Typed, or chosen with Browse… from a searchable table, as the text
        # models are (gui/image_picker.py).
        self.image_model = ModelField(
            family_id(self.config.image_model, self._image_models),
            self._browse_image_models if self.image_catalog is not None else None,
            pictures=self._picture_support,
        )
        self.image_model.setToolTip(
            "The model that draws. Seedream 5.0 Pro takes up to ten reference pictures "
            "and keeps characters and ships as drawn."
        )
        self.image_size = QComboBox()
        self.image_size.setEditable(True)
        self.image_model.textChanged.connect(self._fill_image_sizes)
        self._fill_image_sizes()
        self.image_size.setCurrentText(self.config.image_size)
        self.image_count = QSpinBox()
        self.image_count.setRange(1, 4)
        self.image_count.setValue(self.config.image_count)
        form.addRow("Image model", self.image_model)
        form.addRow("Size", self.image_size)
        form.addRow("Pictures per request", self.image_count)
        hint = QLabel(
            "Every prompt is shown to you, and can be changed, before anything is sent "
            "(its writer is on the Models tab). The model list and prices come from the "
            "endpoint the first time you generate an image."
            if not self._image_models
            else "Every prompt is shown to you, and can be changed, before anything is sent "
            "(its writer is on the Models tab)."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        form.addRow(hint)
        self._portrait_rows(form)
        self._place_rows(form)
        # Where pictures are drawn: the chat endpoint unless set. Most never
        # set it, so it is put away, open when it has been.
        where = Fold(form, "Picture endpoint (if not the chat endpoint's)")
        where.add_row("Address", self.image_url)
        where.add_row("API key", self.image_key)
        where.add_row(self._key_note())
        where.add_row("Service", self.image_api)
        where.set_open(bool(endpoint.base_url.strip() or endpoint.api_key))
        return widget

    def _portrait_rows(self, form: QFormLayout) -> None:
        """Portraits of the characters (Story → Draw portraits…, the Cast
        page, and the offer when a story starts)."""
        form.addRow(QLabel("<b>Portraits</b>"))
        self.portrait_model = ModelField(
            family_id(self.config.portrait_model, self._image_models),
            self._browse_image_models if self.image_catalog is not None else None,
            pictures=self._picture_support,
            recommended=RECOMMENDED_IMAGE_MODELS["portrait_model"],
        )
        form.addRow("Portrait model", self.portrait_model)
        self.portrait_note = QLabel()
        self.portrait_note.setObjectName("hintLabel")
        self.portrait_note.setWordWrap(True)
        form.addRow(self.portrait_note)
        self.portrait_model.textChanged.connect(self._sync_portrait_note)
        self._sync_portrait_note()

    def _place_rows(self, form: QFormLayout) -> None:
        """Pictures of the places the story goes to (the author, Oct 24
        2026): kept on the place's lore entry, and behind the story's text
        while the story is there."""
        form.addRow(QLabel("<b>Places</b>"))
        self.location_backgrounds = QCheckBox("Show the current place's picture behind the story")
        self.location_backgrounds.setChecked(self.config.location_backgrounds)
        self.location_backgrounds.setToolTip(
            "A picture of the place the scene is at (Picture of this place…, above the "
            "story, or a picture on the place's lore entry) stands behind the story's text "
            "while the story is there, in place of the story's own background. Off, the "
            "story's own background stays."
        )
        form.addRow("Place pictures", self.location_backgrounds)

    def _sync_portrait_note(self, *_args) -> None:
        model = self.portrait_model.text().strip()
        info = next((m for m in self._image_models if m.id == model), None)
        each = info.price(portrait_size(info)) if info else None
        price = f"${each:.3f} a portrait listed" if each is not None else "price not listed"
        known = recommended_model("portrait_model", model)
        self.portrait_note.setText(
            "A portrait of each character from their card, offered when a story starts and "
            "on the Cast page; every prompt is shown to you first. "
            f"{model or 'No model'}: {price}." + (f" {known.note}" if known is not None else "")
        )

    def _media_fields(self, endpoint: MediaEndpoint) -> tuple[QLineEdit, QLineEdit, QComboBox]:
        """An address, a key and the service it speaks, for pictures or video."""
        url = QLineEdit(endpoint.base_url)
        key = QLineEdit(endpoint.api_key)
        key.setEchoMode(QLineEdit.Password)
        api = QComboBox()
        for value, label in MEDIA_API_LABELS.items():
            api.addItem(label, value)
        api.setCurrentIndex(max(0, api.findData(endpoint.api)))
        api.setToolTip(
            "Which service the address is. From the address knows NanoGPT, OpenRouter "
            "and WaveSpeed; anything else is taken as OpenAI-compatible."
        )
        return url, key, api

    def _video_tab(self) -> QWidget:
        """Video: its own endpoint and key, off until a key is entered."""
        widget = QWidget()
        form = QFormLayout(widget)
        warning = QLabel(
            "Video costs far more than pictures: tens of cents to over $10 for a few "
            "seconds, where a picture is a few cents. Every video shows its price and "
            "asks before anything is sent.\n\nUse an API key made for video alone, with a "
            "daily spending limit set on it in the service's dashboard (on NanoGPT, when "
            "you create or edit the key). That limit is the only one that holds whatever "
            "happens here."
        )
        warning.setObjectName("warningLabel")
        warning.setWordWrap(True)
        form.addRow(warning)
        endpoint = self.config.video_endpoint
        self.video_url, self.video_key, self.video_api = self._media_fields(endpoint)
        self.video_url.setPlaceholderText(DEFAULT_BASE_URL)
        self.video_key.setPlaceholderText("a key of its own; video is off until one is entered")
        # Browse… lists the endpoint's video models once a video key is saved.
        self.video_model = ModelField(
            self.config.video_model,
            self._browse_video_models if self.video_catalog is not None else None,
        )
        form.addRow("Address", self.video_url)
        form.addRow("API key", self.video_key)
        form.addRow(self._key_note())
        form.addRow("Service", self.video_api)
        form.addRow("Video model", self.video_model)
        self.video_key_note = QLabel()
        self.video_key_note.setObjectName("hintLabel")
        self.video_key_note.setWordWrap(True)
        form.addRow(self.video_key_note)
        for field in (self.video_key, self.api_key, self.image_key):
            field.textChanged.connect(self._sync_video_key_note)
        self._sync_video_key_note()
        return widget

    def _browse_video_models(self, current: str) -> str | None:
        from sealedlore.gui.video_dialog import pick_video_model

        return pick_video_model(self.video_catalog, current, self)

    def _sync_video_key_note(self, *_args) -> None:
        key = self.video_key.text().strip()
        if not key:
            self.video_key_note.setText(
                "Video is off: there is no video key. None is taken from the chat's, so "
                "video is never paid for by a key that wasn't meant for it."
            )
        elif key in {self.api_key.text().strip(), self.image_key.text().strip()} - {""}:
            self.video_key_note.setText(
                "⚠ This is the same key as chat or pictures. That works, but its spending "
                "can't be capped for video alone, so every video will warn you again "
                "before it is sent."
            )
        else:
            self.video_key_note.setText("Video is on, with a key of its own.")

    def _browse_image_models(self, current: str) -> str | None:
        chosen = pick_image_model(self.image_catalog, current, self)
        # A Refresh may have brought new models (and their sizes).
        self._image_models = self.image_catalog.models
        for field in (
            self.image_model,
            self.portrait_model,
        ):
            field.show_pictures()
        return chosen

    def _picture_support(self, model: str) -> tuple[str, str]:
        """Beside an image model: whether it can be given a picture."""
        return picture_support(model, self._image_models)

    def _fill_image_sizes(self, *_args) -> None:
        wanted = self.image_model.text().strip()
        info = next((i for i in self._image_models if i.id == wanted), None)
        current = self.image_size.currentText()
        self.image_size.clear()
        for size in info.resolutions if info is not None else ():
            price = info.price(size)
            self.image_size.addItem(size)
            if price is not None:
                self.image_size.setItemData(
                    self.image_size.count() - 1, f"${price:.3f} a picture", Qt.ToolTipRole
                )
        if current:
            self.image_size.setCurrentText(current)

    def _lore_tab(self) -> QWidget:
        """The embeddings endpoint and retrieval knobs (§7)."""
        existing = self.config.embedding_provider
        widget = QWidget()
        form = QFormLayout(widget)

        self.embeddings_enabled = QCheckBox("Retrieve lore semantically")
        self.embeddings_enabled.setChecked(existing is not None)
        self.embeddings_enabled.toggled.connect(self._sync_lore_fields)

        self.embed_base_url = QLineEdit(existing.base_url if existing else "")
        self.embed_base_url.setPlaceholderText("same as the chat endpoint")
        self.embed_api_key = QLineEdit(existing.api_key if existing else "")
        self.embed_api_key.setEchoMode(QLineEdit.Password)
        self.embed_api_key.setPlaceholderText("same as the chat key (same host only)")
        self.embed_model = QLineEdit(existing.model if existing else DEFAULT_EMBEDDING_MODEL)
        self.embed_model.setPlaceholderText(DEFAULT_EMBEDDING_MODEL)
        self.embed_dimensions = QSpinBox()
        self.embed_dimensions.setRange(0, 8192)
        self.embed_dimensions.setSpecialValueText("model default")
        self.embed_dimensions.setValue(existing.dimensions if existing else 0)

        self.query_prefix = QLineEdit(existing.query_prefix if existing else "")
        self.query_prefix.setPlaceholderText("e.g. 'search_query: ' — bge-m3 needs none")
        self.document_prefix = QLineEdit(existing.document_prefix if existing else "")
        self.document_prefix.setPlaceholderText("e.g. 'search_document: '")

        self.retrieval_k = QSpinBox()
        self.retrieval_k.setRange(0, 50)
        self.retrieval_k.setValue(self.config.lore_retrieval_k)
        self.similarity = QDoubleSpinBox()
        self.similarity.setRange(0.0, 1.0)
        self.similarity.setSingleStep(0.05)
        self.similarity.setDecimals(2)
        self.similarity.setValue(self.config.lore_similarity_threshold)
        self.query_turns = QSpinBox()
        self.query_turns.setRange(0, 20)
        self.query_turns.setValue(self.config.lore_query_turns)
        self.lore_cap = QSpinBox()
        self.lore_cap.setRange(100, 100_000)
        self.lore_cap.setSingleStep(250)
        self.lore_cap.setValue(self.config.lore_token_cap)

        self.lore_selector = QComboBox()
        for label, value in LORE_SELECTORS:
            self.lore_selector.addItem(label, value)
        self.lore_selector.setCurrentIndex(
            max(0, self.lore_selector.findData(self.config.lore_selector))
        )
        self.lore_selector.setToolTip(
            "A lorebook up to a tenth of the context budget is sent whole every turn, "
            "cached, and none of this applies. Past that, entries are chosen per turn.\n\n"
            "Model picks: the lore model picks after each passage, in the background, and "
            "your turn adds any entry it names. No wait; one small call per passage.\n"
            "Jev: TypeSafe's Jev scores every entry just before your turn is sent, plus "
            "what your turn names. About 0.8s more per turn and ~$0.001 (NanoGPT only).\n"
            "Similarity and keywords: no extra calls. At 100 entries it left several times "
            "as many lore mistakes as either model.\n\n"
            "The lore model is on the Models tab."
        )
        form.addRow("Large lorebooks", self.lore_selector)
        form.addRow(self.embeddings_enabled)
        hint = QLabel(
            "Keyword matching always runs, and is the whole story when this is off "
            "or the endpoint fails. A small lorebook is sent whole and needs neither."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        form.addRow(hint)
        # The embeddings endpoint, opened when semantic retrieval is ticked.
        self.embeddings_fold = Fold(form, "Embeddings endpoint")
        self.embeddings_fold.add_row("Base URL", self.embed_base_url)
        self.embeddings_fold.add_row("API key", self.embed_api_key)
        self.embeddings_fold.add_row(self._key_note())
        self.embeddings_fold.add_row("Model", self.embed_model)
        self.embeddings_fold.add_row("Dimensions", self.embed_dimensions)
        self.embeddings_fold.add_row("Query prefix", self.query_prefix)
        self.embeddings_fold.add_row("Document prefix", self.document_prefix)
        prefixes = QLabel(
            "Prefixes are only for models that want the query and the document marked differently."
        )
        prefixes.setObjectName("hintLabel")
        prefixes.setWordWrap(True)
        self.embeddings_fold.add_row(prefixes)
        self.embeddings_fold.set_open(self.embeddings_enabled.isChecked())
        self.embeddings_enabled.toggled.connect(
            lambda on: on and self.embeddings_fold.set_open(True)
        )
        tuning = Fold(form, "Retrieval (advanced)")
        tuning.add_row("Entries per turn", self.retrieval_k)
        tuning.add_row("Similarity threshold", self.similarity)
        tuning.add_row("Turns in the query", self.query_turns)
        tuning.add_row("Lore token cap", self.lore_cap)

        self._lore_fields = [
            self.embed_base_url,
            self.embed_api_key,
            self.embed_model,
            self.embed_dimensions,
            self.query_prefix,
            self.document_prefix,
        ]
        self._sync_lore_fields()
        return widget

    def _sync_lore_fields(self) -> None:
        for field in self._lore_fields:
            field.setEnabled(self.embeddings_enabled.isChecked())

    def _double_field(self, value: float | None, *, maximum: float) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(UNSET, maximum)
        box.setSingleStep(0.05)
        box.setDecimals(2)
        box.setSpecialValueText("unset")
        box.setValue(UNSET if value is None else value)
        return box

    def _int_field(self, value: int | None, *, maximum: int) -> QSpinBox:
        box = QSpinBox()
        box.setRange(UNSET_INT, maximum)
        box.setSpecialValueText("unset")
        box.setValue(UNSET_INT if value is None else value)
        return box

    # --- saving -----------------------------------------------------------

    def _save(self) -> None:
        endpoints = self._endpoints_as_typed()
        embed_url = self.embed_base_url.text().strip()
        try:
            self._check_endpoints(endpoints)
            if self.embeddings_enabled.isChecked() and embed_url:
                require_https(embed_url)
            for media_url in (self.image_url.text().strip(), self.video_url.text().strip()):
                if media_url:
                    require_https(media_url)
            if self.private_url.text().strip():
                require_https(self.private_url.text().strip())
            self._carry_keys(endpoints)
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return

        # The list as it stands replaces the saved one: a renamed endpoint is
        # the same entry under its new name (its key with it, `_carry_keys`),
        # a removed one is gone, key and all (storage/repository.py drops its
        # keychain slot). Every copy was checked above; the fields it doesn't
        # show (timeout, extra_body) rode along in the copy.
        self.config.providers[:] = [
            p.model_copy(update={"base_url": p.base_url.strip()}) for p in endpoints
        ]
        provider = self.config.providers[self._default_index]
        self.config.key_storage = self.key_storage.currentData()
        provider.model = self.model.text().strip()
        self.config.active_provider_name = provider.name
        self.config.summarization_model = self.summarization_model.text().strip() or None
        self.config.authoring_model = self.authoring_model.text().strip() or None
        self.config.image_model = self.image_model.text().strip() or self.config.image_model
        self.config.image_size = self.image_size.currentText().strip() or self.config.image_size
        self.config.image_count = self.image_count.value()
        self.config.image_prompt_model = self.image_prompt_model.text().strip() or None
        self.config.location_backgrounds = self.location_backgrounds.isChecked()
        self.config.portrait_model = (
            self.portrait_model.text().strip() or self.config.portrait_model
        )
        images = MediaEndpoint(
            base_url=self.image_url.text().strip(),
            api_key=self.image_key.text().strip(),
            api=self.image_api.currentData(),
        )
        if images != self.config.image_endpoint:
            # Another endpoint's models: fetched afresh when next needed.
            self.config.image_models_fetched_at = None
        self.config.image_endpoint = images
        video = MediaEndpoint(
            base_url=self.video_url.text().strip() or DEFAULT_BASE_URL,
            api_key=self.video_key.text().strip(),
            api=self.video_api.currentData(),
        )
        if video != self.config.video_endpoint:
            self.config.video_models_fetched_at = None
        self.config.video_endpoint = video
        self.config.video_model = self.video_model.text().strip() or self.config.video_model
        # Each role's route, as it applies to the model in its field.
        for role, field in self._route_fields.items():
            route = field.effective_route()
            if route is None:
                self.config.model_routes.pop(role, None)
            else:
                self.config.model_routes[role] = route
        if self.story is not None and self.story.chat:
            chosen = self.model.effective_route()
            if chosen != self._route_at_open:
                self.story.defaults.main_route = chosen
        private_url = self.private_url.text().strip()
        private_model = self.private_model.text().strip()
        if private_url and private_model:
            # Keep what the form doesn't show (timeout, extra_body); the URL
            # was checked above.
            existing = self.config.private_provider or ProviderConfig(name="private")
            self.config.private_provider = existing.model_copy(
                update={
                    "name": "private",
                    "base_url": private_url,
                    "api_key": self.private_key.text(),
                    "model": private_model,
                }
            )
        else:
            self.config.private_provider = None
        self.config.private_budget = self.private_budget.value()
        self.config.private_verify_tee = self.private_tee.isChecked()
        self.config.phone.start_on_launch = self.phone_on_launch.isChecked()
        self.config.phone.notify_connect = self.phone_notify.isChecked()
        self.config.scene_reads = "every_turn" if self.keep_scene.isChecked() else "manual"
        self.config.scene_model = self._role_model(self.scene_model) or None
        self.config.plot_reads = self.plot_reads.isChecked()
        self.config.plot_model = self._role_model(self.plot_model) or None
        self.config.cache_control_mode = self.cache_mode.currentText()  # type: ignore[assignment]
        self.config.cache_ttl = self.cache_ttl.currentData()

        if self.embeddings_enabled.isChecked():
            self.config.embedding_provider = EmbeddingProviderConfig(
                base_url=embed_url,
                api_key=self.embed_api_key.text(),
                model=self.embed_model.text().strip() or DEFAULT_EMBEDDING_MODEL,
                dimensions=self.embed_dimensions.value(),
                query_prefix=self.query_prefix.text(),
                document_prefix=self.document_prefix.text(),
            )
        else:
            self.config.embedding_provider = None
        self.config.lore_retrieval_k = self.retrieval_k.value()
        self.config.lore_similarity_threshold = self.similarity.value()
        self.config.lore_query_turns = self.query_turns.value()
        self.config.lore_token_cap = self.lore_cap.value()
        self.config.lore_selector = self.lore_selector.currentData()
        self.config.lore_model = self._role_model(self.lore_model)

        stop = [item.strip() for item in self.stop.text().split(",") if item.strip()]
        self.config.generation = GenerationParams(
            temperature=_optional_double(self.temperature.value()),
            top_p=_optional_double(self.top_p.value()),
            max_tokens=_optional_int(self.max_tokens.value()),
            presence_penalty=_optional_double(self.presence_penalty.value()),
            frequency_penalty=_optional_double(self.frequency_penalty.value()),
            seed=_optional_int(self.seed.value()),
            stop=stop,
        )
        levels = self._reasoning_levels()
        self.config.reasoning_story = levels["story"]
        self.config.reasoning_side = levels["side"]

        if self.story is not None:
            self.story.defaults.context_token_budget = self.budget.value()
            if self.story.chat and provider.model and provider.model != self._model_at_open:
                current = self.story.defaults.main_model or ""
                refusal = move_refusal(current, provider.model)
                if refusal:
                    # A TEE chat never moves to a model that can't be attested
                    # (StorySession.check_chat_model); the endpoint's default
                    # still changes, for everything else.
                    QMessageBox.information(
                        self,
                        "Kept to TEE models",
                        f"This chat stays on {current}: {refusal} {provider.model} is the "
                        "default for other stories.",
                    )
                else:
                    self.story.defaults.main_model = provider.model
        # App-wide, whether or not a story is open.
        self.config.auto_archive = self.auto_archive.isChecked()
        self.config.story_ledger = self.story_ledger.isChecked()
        self.config.plot_chapter_days = self.plot_chapter_days.isChecked()
        self.config.archive_chunk_turns = self.chunk_turns.value()
        self.config.archive_target_ratio = self.archive_target.value() / 100
        self.config.summary_target_words = self.summary_words.value()
        self.config.recall, self.config.recall_in = self.recall.currentData().split("|")
        self.config.recall_carry_turns = self.recall_carry.value()

        self.accept()
