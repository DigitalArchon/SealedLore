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


def test_speaks_offers_every_sealed_protocol_now_each_has_its_client(app, monkeypatch):
    """Tinfoil's is the one sealed protocol: Chutes is an ordinary endpoint
    (since Nov 15 2026), its -TEE models sealed whatever it speaks."""
    dialog = SettingsDialog(config(), None)
    offered = {dialog.chat_api.itemData(i) for i in range(dialog.chat_api.count())}
    assert offered == {"auto", "openai", "anthropic", "tinfoil"}
    dialog.base_url.setText("https://llm.chutes.ai/v1")
    assert "-TEE models are always sent end-to-end encrypted" in dialog.api_note.text()
    dialog.base_url.setText("https://inference.tinfoil.sh/v1")
    assert "pinned to its attested TLS key" in dialog.api_note.text()
    # A sealed service with no client (none today) is said to be one.
    import sealedlore.providers.privacy as privacy_module

    monkeypatch.setattr(privacy_module, "BUILT_SERVICES", frozenset())
    dialog.base_url.setText("https://router-0.tinfoil.sh/v1")
    assert "isn't built into this version" in dialog.api_note.text()
    dialog.done(0)


def test_an_endpoint_set_to_a_sealed_protocol_keeps_it_through_a_save(app):
    """A hand-written `api: tinfoil` on another host is kept: saved as
    "From the address" it would have become a plain endpoint."""
    cfg = config(base_url="https://proxy.example/v1", api="tinfoil")
    dialog = SettingsDialog(cfg, None)
    assert dialog.chat_api.currentData() == "tinfoil"
    assert "pinned to its attested TLS key" in dialog.api_note.text()
    dialog._save()
    assert cfg.active_provider().api == "tinfoil"


def test_a_model_field_labels_a_model_by_its_endpoint(app):
    """The field asks whoever holds the endpoints; without one, the route's
    address, else the id alone (NanoGPT's prefixes, as before)."""
    sealed = ModelField("m-TEE", privacy_of=lambda model: Privacy("sealed", "chutes"))
    assert sealed.privacy.text() == "🔐 E2EE · Chutes ⚠ partial"
    assert sealed.privacy.toolTip().startswith("Partial privacy")
    tinfoil = ModelField("glm-5-3", privacy_of=lambda model: Privacy("sealed", "tinfoil"))
    assert tinfoil.privacy.text() == "🔐 E2EE · Tinfoil" and "Tinfoil" in tinfoil.privacy.toolTip()
    by_address = ModelField("m-TEE", route_endpoint=lambda model: "https://llm.chutes.ai/v1")
    assert by_address.privacy.text() == "🔐 E2EE · Chutes ⚠ partial"
    assert ModelField(
        "glm-5-3", route_endpoint=lambda model: "https://llm.chutes.ai/v1"
    ).privacy.isHidden(), "one of Chutes' ordinary models: no label"
    plain = ModelField("TEE/glm-5.3")
    assert plain.privacy.text() == "TEE"
    assert ModelField("z-ai/glm-5.3").privacy.isHidden()
