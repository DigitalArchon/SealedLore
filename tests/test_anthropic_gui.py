"""Settings → Endpoints → Speaks: choosing Anthropic's own API. Offscreen."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import Config, ProviderConfig  # noqa: E402
from sealedlore.providers.anthropic import AnthropicProvider  # noqa: E402
from sealedlore.providers.endpoints import EndpointsProvider  # noqa: E402
from sealedlore.providers.openai_compat import OpenAICompatibleProvider  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def config(**changes) -> Config:
    chat = ProviderConfig(name="default", base_url="https://nano-gpt.com/api/v1", api_key="k")
    for name, value in changes.items():
        setattr(chat, name, value)
    return Config(providers=[chat], active_provider_name="default")


def test_the_choice_round_trips_and_the_note_says_how_the_key_is_sent(app):
    cfg = config()
    dialog = SettingsDialog(cfg, None)
    assert dialog.chat_api.currentData() == "auto"
    assert "Bearer" in dialog.api_note.text()

    dialog.base_url.setText("https://api.anthropic.com")
    assert "x-api-key" in dialog.api_note.text(), "read from the address"
    dialog.chat_api.setCurrentIndex(dialog.chat_api.findData("anthropic"))
    dialog._save()
    assert cfg.active_provider().api == "anthropic"
    assert cfg.active_provider().speaks() == "anthropic"

    again = SettingsDialog(cfg, None)
    assert again.chat_api.currentData() == "anthropic"
    assert "x-api-key" in again.api_note.text()


def test_browse_lists_the_models_through_the_chosen_client(app):
    """Browse… goes through the routing provider (providers/endpoints.py),
    which makes each endpoint's client as the endpoint is typed."""
    dialog = SettingsDialog(config(), None)
    provider, _url, _owned = dialog._model_source()
    assert isinstance(provider, EndpointsProvider)
    assert isinstance(provider.provider_for("x"), OpenAICompatibleProvider)
    provider.close()

    dialog.base_url.setText("https://api.anthropic.com")
    provider, url, _owned = dialog._model_source()
    assert isinstance(provider.provider_for("x"), AnthropicProvider)
    assert "https://api.anthropic.com" in url, "the catalog is keyed by the endpoints"
    provider.close()

    # Chosen outright, for a proxy that speaks Anthropic's API on another host.
    dialog.base_url.setText("https://proxy.example/v1")
    dialog.chat_api.setCurrentIndex(dialog.chat_api.findData("anthropic"))
    provider, _url, _owned = dialog._model_source()
    assert isinstance(provider.provider_for("x"), AnthropicProvider)
    provider.close()
    assert all(t.settings.api == "anthropic" for t in dialog.speed_targets())
