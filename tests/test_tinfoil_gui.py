"""Tinfoil's own API in the window (providers/tinfoil_direct.py): Settings'
endpoint and private-scene choices, the pickers' labels, the marks, Test
attestation and a sealed chat on it. Offscreen; nothing is sent."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.engine.attestation_check import check_attestation  # noqa: E402
from sealedlore.engine.catalog import ModelInfo  # noqa: E402
from sealedlore.gui.model_picker import COLUMNS, ModelCatalog, ModelPickerDialog  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog, private_service_of  # noqa: E402
from sealedlore.gui.transcript import tee_tip  # noqa: E402
from sealedlore.models.config import (  # noqa: E402
    DEFAULT_BASE_URL,
    TINFOIL_BASE_URL,
    Config,
    ProviderConfig,
)
from sealedlore.providers.privacy import TINFOIL, privacy_at, privacy_of  # noqa: E402
from sealedlore.providers.sealed_ehbp import attestation_record  # noqa: E402
from sealedlore.providers.tinfoil_direct import TinfoilDirect  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def tinfoil_record():
    return attestation_record(
        enclave="inference.tinfoil.sh",
        hardware="AMD SEV-SNP",
        measurement="07c3b78a",
        release_digest="cb88bff7" * 8,
        release_tag="v0.0.176",
        latest_release="",
        latest_checked=True,
        hpke_public_key="aa",
        service=TINFOIL,
    )


def config(**changes) -> Config:
    return Config(
        providers=[ProviderConfig(name="default", base_url=NANO, api_key="k")],
        active_provider_name="default",
        **changes,
    )


def test_settings_adds_tinfoils_endpoint_filled_in(app):
    cfg = config()
    dialog = SettingsDialog(cfg, None)
    dialog.add_tinfoil.click()
    assert dialog.provider_name.text() == "Tinfoil"
    assert dialog.base_url.text() == TINFOIL_BASE_URL
    assert dialog.chat_api.currentData() == "tinfoil"
    assert "pinned to its attested TLS key" in dialog.api_note.text()
    dialog.api_key.setText("tk-1")
    dialog._save()
    tinfoil = cfg.endpoint_named("Tinfoil")
    assert tinfoil is not None and tinfoil.api == "tinfoil" and tinfoil.speaks() == "tinfoil"
    assert privacy_of("Tinfoil::glm-5-3", cfg).service == TINFOIL


def test_private_scenes_choose_a_service_each_with_its_own_address_and_key(app):
    cfg = config()
    dialog = SettingsDialog(cfg, None)
    assert dialog.private_service.currentData() == "nanogpt"
    assert dialog.private_url.text() == DEFAULT_BASE_URL
    dialog.private_key.setText("nano-key")
    dialog.private_service.setCurrentIndex(dialog.private_service.findData("tinfoil"))
    assert dialog.private_url.text() == TINFOIL_BASE_URL and dialog.private_key.text() == ""
    assert "Choose lists Tinfoil's models" in dialog.private_model.placeholderText()
    dialog.private_key.setText("tinfoil-key")
    dialog.private_model.setText("glm-5-3")
    assert dialog.private_model.privacy.text() == "🔐 E2EE · Tinfoil"
    assert not dialog.private_attest.isHidden(), "Test attestation, for a Tinfoil model"
    dialog.private_service.setCurrentIndex(dialog.private_service.findData("nanogpt"))
    assert (dialog.private_url.text(), dialog.private_key.text()) == (DEFAULT_BASE_URL, "nano-key")
    dialog.private_service.setCurrentIndex(dialog.private_service.findData("tinfoil"))
    assert dialog.private_key.text() == "tinfoil-key", "each service keeps its own"
    dialog._save()
    private = cfg.private_provider
    assert (private.base_url, private.api, private.model) == (
        TINFOIL_BASE_URL,
        "tinfoil",
        "glm-5-3",
    )
    assert private_service_of(private) == "tinfoil"
    again = SettingsDialog(cfg, None)
    assert again.private_service.currentData() == "tinfoil"
    again.done(0)


def test_private_service_of_reads_a_saved_endpoint():
    assert private_service_of(None) == "nanogpt"
    assert private_service_of(ProviderConfig(name="p", base_url=NANO)) == "nanogpt"
    local = ProviderConfig(name="p", base_url="http://localhost:11434/v1")
    assert private_service_of(local) == "other"
    assert private_service_of(ProviderConfig(name="p", base_url=TINFOIL_BASE_URL)) == "tinfoil"


def test_the_picker_says_encrypted_by_tinfoil(app):
    catalog = ModelCatalog()
    catalog.models = {
        ref: ModelInfo(id=ref, name=ref, endpoint=ref.split("::")[0] if "::" in ref else "")
        for ref in ("Tinfoil::glm-5-3", "private/glm-5-3", "z-ai/glm-5.3")
    }
    catalog._endpoint, catalog._fetched_at = "e", datetime.now(UTC)
    cfg = config()
    cfg.providers.append(ProviderConfig(name="Tinfoil", base_url=TINFOIL_BASE_URL))
    dialog = ModelPickerDialog(
        catalog, lambda: (None, "e", False), privacy_of=lambda m: privacy_of(m, cfg)
    )
    column = COLUMNS.index("Privacy")
    shown = {
        dialog.table.topLevelItem(i).data(0, 0x0101): dialog.table.topLevelItem(i)
        for i in range(dialog.table.topLevelItemCount())
    }
    assert shown["Tinfoil::glm-5-3"].text(column) == "🔐 Encrypted (Tinfoil)"
    assert "Tinfoil's attested router" in shown["Tinfoil::glm-5-3"].toolTip(column)
    assert shown["private/glm-5-3"].text(column) == "🔐 Encrypted"
    assert shown["Tinfoil::glm-5-3"].text(COLUMNS.index("Hosts")) == "—"
    dialog.deleteLater()


def test_an_encrypted_passages_tooltip_names_who_sealed_it():
    assert "Tinfoil" in tee_tip("encrypted", None, "tinfoil")
    assert "NanoGPT relayed ciphertext" in tee_tip("encrypted", None)
    assert tee_tip("verified", None, "tinfoil") == tee_tip("verified", None)


def test_test_attestation_attests_tinfoil_without_the_key(monkeypatch):
    seen: list = []

    def attest(self):
        seen.append(self.config.api_key)
        return tinfoil_record()

    monkeypatch.setattr(TinfoilDirect, "attest", attest)
    monkeypatch.setattr(TinfoilDirect, "still_offered", lambda self, model: True)
    # A key saved for another address doesn't stop it: nothing is sent with one.
    settings = ProviderConfig(
        name="p",
        base_url=TINFOIL_BASE_URL,
        api_key="tk",
        key_saved_for="https://elsewhere.example",
        model="glm-5-3",
        api="tinfoil",
    )
    check = check_attestation(settings)
    assert check.outcome == "encrypted" and "Tinfoil's router attested" in check.headline
    assert "Tinfoil's router enclave at inference.tinfoil.sh" in check.detail
    monkeypatch.setattr(TinfoilDirect, "still_offered", lambda self, model: False)
    assert check_attestation(settings).outcome == "retired"


def test_a_chat_on_tinfoil_is_sealed_attested_and_says_so(app, window, monkeypatch):
    from tests.test_chat_gui import make_chat

    monkeypatch.setattr(window, "_attest_chat", lambda: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(
        window.config,
        "providers",
        [
            ProviderConfig(name="n", base_url=NANO, api_key="k"),
            ProviderConfig(name="Tinfoil", base_url=TINFOIL_BASE_URL, api_key="tk", api="tinfoil"),
        ],
    )
    monkeypatch.setattr(window.config, "active_provider_name", "n")
    make_chat(window, monkeypatch, model="Tinfoil::glm-5-3", keep="memory")
    session = window.session
    assert session.tee_chat and session.privacy(session.model).service == TINFOIL
    window.use_mock = False  # the real providers, built but never called
    window._discard_provider()
    sealed = window._sealed_chat_provider()
    assert isinstance(sealed, TinfoilDirect)
    limits = window._chat_model_limits()
    assert "end-to-end encrypted models only" in limits["allowed_note"]
    assert limits["allowed"]("private/glm-5-3") and not limits["allowed"]("z-ai/glm-5.3")

    window._chat_model_changed("private/glm-5-3")  # no message yet: nothing to note under
    window._on_attested(None, None, [tinfoil_record()], [])
    assert "🔐 E2EE · Tinfoil" in window._private_model_text()
    assert "TEE" not in window._private_model_text()
    window._refresh_model_button()
    assert "Tinfoil's router enclave" in window.model_button.toolTip()
    window.use_mock = True
    window._discard_provider()
    window.open_story(window.stories.list.item(0).data(0x0100))  # leave the memory chat


def test_a_private_model_on_tinfoil_is_judged_on_the_private_endpoint():
    assert privacy_at("glm-5-3", TINFOIL_BASE_URL, "tinfoil").service == TINFOIL


@pytest.fixture
def window(app, tmp_path, story, cast):
    """As tests/test_chat_gui.py's."""
    from sealedlore.gui.main_window import MainWindow
    from sealedlore.storage.repository import StoryBundle, save_story_bundle
    from tests.conftest import make_exchange

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    made = MainWindow(root=tmp_path, use_mock=True)
    made.resize(1366, 768)
    made.show()
    made.open_story(story.id)
    yield made
    made._chat_allows_close = lambda: True
    made.close()
