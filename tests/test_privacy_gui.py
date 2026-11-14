"""Settings and the model fields, for an end-to-end encrypted endpoint whose
client this version hasn't (providers/privacy.py). Offscreen."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import Config, ProviderConfig  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def config(**changes) -> Config:
    chat = ProviderConfig(name="default", base_url=NANO, api_key="k")
    for name, value in changes.items():
        setattr(chat, name, value)
    return Config(providers=[chat], active_provider_name="default")


def test_speaks_offers_no_sealed_protocol_until_its_client_exists(app):
    dialog = SettingsDialog(config(), None)
    offered = {dialog.chat_api.itemData(i) for i in range(dialog.chat_api.count())}
    assert offered == {"auto", "openai", "anthropic"}
    dialog.base_url.setText("https://inference.tinfoil.sh/v1")
    assert "isn't built into this version" in dialog.api_note.text(), "read from the address"
    dialog.done(0)


def test_an_endpoint_set_to_a_sealed_protocol_keeps_it_through_a_save(app):
    """Saved as "From the address", a hand-written `api: tinfoil` on another
    host would have become a plain endpoint."""
    cfg = config(base_url="https://proxy.example/v1", api="tinfoil")
    dialog = SettingsDialog(cfg, None)
    assert dialog.chat_api.currentData() == "tinfoil"
    assert "not built yet" in dialog.chat_api.currentText()
    assert "isn't built into this version" in dialog.api_note.text()
    dialog._save()
    assert cfg.active_provider().api == "tinfoil"
