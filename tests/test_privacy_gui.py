"""Settings and the model fields, for an end-to-end encrypted endpoint whose
client this version hasn't (providers/privacy.py). Offscreen."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.model_picker import ModelField  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import Config, ProviderConfig  # noqa: E402
from sealedlore.providers.privacy import Privacy  # noqa: E402

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
    """Tinfoil's is offered now; Chutes E2EE's client isn't built yet."""
    dialog = SettingsDialog(config(), None)
    offered = {dialog.chat_api.itemData(i) for i in range(dialog.chat_api.count())}
    assert offered == {"auto", "openai", "anthropic", "tinfoil"}
    dialog.base_url.setText("https://api.chutes.ai")
    assert "isn't built into this version" in dialog.api_note.text(), "read from the address"
    dialog.base_url.setText("https://inference.tinfoil.sh/v1")
    assert "pinned to its attested TLS key" in dialog.api_note.text()
    dialog.done(0)


def test_an_endpoint_set_to_a_sealed_protocol_keeps_it_through_a_save(app):
    """Saved as "From the address", a hand-written `api: chutes-e2ee` on
    another host would have become a plain endpoint."""
    cfg = config(base_url="https://proxy.example/v1", api="chutes-e2ee")
    dialog = SettingsDialog(cfg, None)
    assert dialog.chat_api.currentData() == "chutes-e2ee"
    assert "not built yet" in dialog.chat_api.currentText()
    assert "isn't built into this version" in dialog.api_note.text()
    dialog._save()
    assert cfg.active_provider().api == "chutes-e2ee"


def test_a_model_field_labels_a_model_by_its_endpoint(app):
    """The field asks whoever holds the endpoints; without one, the route's
    address, else the id alone (NanoGPT's prefixes, as before)."""
    sealed = ModelField("glm-5-3", privacy_of=lambda model: Privacy("sealed", "chutes"))
    assert sealed.privacy.text() == "🔐 E2EE" and "Chutes" in sealed.privacy.toolTip()
    assert "isn't built into this version" in sealed.privacy.toolTip()
    tinfoil = ModelField("glm-5-3", privacy_of=lambda model: Privacy("sealed", "tinfoil"))
    assert tinfoil.privacy.text() == "🔐 E2EE · Tinfoil" and "Tinfoil" in tinfoil.privacy.toolTip()
    by_address = ModelField("glm-5-3", route_endpoint=lambda model: "https://api.chutes.ai")
    assert by_address.privacy.text() == "🔐 E2EE"
    plain = ModelField("TEE/glm-5.3")
    assert plain.privacy.text() == "TEE"
    assert ModelField("z-ai/glm-5.3").privacy.isHidden()
