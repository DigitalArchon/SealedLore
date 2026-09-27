"""Test attestation, before a private model is chosen (engine/attestation_check.py,
gui/attest_check.py). Offscreen; the checks themselves are stood in for."""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import sealedlore.gui.attest_check as gui_module  # noqa: E402
from sealedlore.engine.attestation_check import AttestationCheck, check_attestation  # noqa: E402
from sealedlore.models.config import Config, ProviderConfig  # noqa: E402
from sealedlore.models.private import Attestation  # noqa: E402
from sealedlore.providers.tee import TeeRefused  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def settings(model: str) -> ProviderConfig:
    return ProviderConfig(name="n", base_url=NANO, api_key="k", model=model)


class FakeTee:
    outcome: object = None
    seen: list = []

    def __init__(self, base_url, api_key, model):
        FakeTee.seen.append((base_url, api_key, model))

    def attest(self):
        if isinstance(FakeTee.outcome, BaseException):
            raise FakeTee.outcome
        return FakeTee.outcome

    def close(self):
        pass


@pytest.fixture
def fake_tee(monkeypatch):
    import sealedlore.providers.tee as tee_module

    FakeTee.seen = []
    monkeypatch.setattr(tee_module, "TeeClient", FakeTee)
    return FakeTee


def test_a_tee_models_check_says_full_partial_or_refused(fake_tee):
    fake_tee.outcome = Attestation(
        verified=True, level="full", gpus=8, tcb_status="UpToDate", detail="all held"
    )
    full = check_attestation(settings("TEE/glm-5.3"))
    assert full.outcome == "full" and full.usable
    assert full.headline == "✓ TEE attested (8 GPUs verified, Intel UpToDate)"
    assert fake_tee.seen == [(NANO, "k", "TEE/glm-5.3")]

    fake_tee.outcome = Attestation(
        verified=True, level="partial", shortfalls=["no GPU evidence"], tcb_status="UpToDate"
    )
    partial = check_attestation(settings("TEE/gpt-oss-120b"))
    assert partial.outcome == "partial" and partial.usable
    assert "partially: no GPU evidence" in partial.headline

    fake_tee.outcome = TeeRefused("TEE/gemma4-31b offers no attestation")
    refused = check_attestation(settings("TEE/gemma4-31b"))
    assert refused.outcome == "refused" and not refused.usable
    assert "offers no attestation" in refused.headline
    assert "Nothing would be sent" in refused.detail


def test_an_encrypted_models_check_is_its_own_attestation(monkeypatch):
    from sealedlore.providers.private_mode import PrivateModeProvider, attestation_record

    record = attestation_record(
        enclave="router-0.tinfoil.sh",
        hardware="AMD SEV-SNP",
        measurement="m",
        release_digest="d" * 64,
        release_tag="v1",
        latest_release="v2",
        latest_checked=True,
        hpke_public_key="aa",
    )
    monkeypatch.setattr(PrivateModeProvider, "attest", lambda self: record)
    result = check_attestation(settings("private/glm-5-3"))
    assert result.outcome == "encrypted" and "older enclave release" in result.headline

    from sealedlore.providers.base import ProviderError

    def refuse(self):
        raise ProviderError("the enclave's attestation was incomplete")

    monkeypatch.setattr(PrivateModeProvider, "attest", refuse)
    assert check_attestation(settings("private/glm-5-3")).outcome == "refused"


def wait_for(app, condition, seconds=10):
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.processEvents()
        time.sleep(0.01)


def test_the_button_shows_only_for_tee_models_and_reports_what_it_found(app, monkeypatch):
    asked: list[ProviderConfig] = []

    def fake_check(config):
        asked.append(config)
        return AttestationCheck("refused", "✗ Refused: offers no attestation", "Nothing sent.")

    monkeypatch.setattr(gui_module, "check_attestation", fake_check)
    button = gui_module.AttestationButton(lambda: settings("TEE/gemma4-31b"))
    for model, shown in (("anthropic/claude", False), ("TEE/x", True), ("private/y", True)):
        button.model_changed(model)
        assert button.isHidden() is not shown, model
    button.run()
    assert not button.button.isEnabled() and "Checking" in button.result.text()
    wait_for(app, lambda: button.button.isEnabled())
    assert asked[0].model == "TEE/gemma4-31b"
    assert button.result.text().startswith("TEE/gemma4-31b: ✗ Refused")
    assert button.result.objectName() == "warningLabel"
    button.model_changed("TEE/other")
    assert button.result.isHidden(), "a new model clears the old result"
    button.deleteLater()


def test_settings_tests_the_private_endpoint_as_typed(app, monkeypatch):
    from sealedlore.gui.settings_dialog import SettingsDialog

    config = Config(
        providers=[ProviderConfig(name="n", base_url=NANO, api_key="chat-key")],
        active_provider_name="n",
    )
    dialog = SettingsDialog(config, None)
    dialog.private_url.setText(NANO)
    dialog.private_model.setText("TEE/glm-5.3")
    assert not dialog.private_attest.isHidden()
    typed = dialog._private_test_settings()
    assert (typed.base_url, typed.model) == (NANO, "TEE/glm-5.3")
    assert typed.api_key == "chat-key", "a blank key is the chat's, on the same host"
    dialog.private_model.setText("llama3.1:8b")
    assert dialog.private_attest.isHidden()
    dialog.deleteLater()


def test_new_chat_and_the_picker_test_the_model_being_chosen(app):
    from datetime import UTC, datetime

    from sealedlore.engine.catalog import ModelInfo
    from sealedlore.gui.model_picker import ModelCatalog, ModelPickerDialog
    from sealedlore.gui.new_chat_dialog import NewChatDialog

    endpoint = ProviderConfig(name="n", base_url=NANO, api_key="k", model="x")
    chat = NewChatDialog(model="TEE/kimi-k3", endpoint=endpoint)
    assert not chat.attest.isHidden()
    assert chat._attest_settings().model == "TEE/kimi-k3"
    chat.model.setText("anthropic/claude")
    assert chat.attest.isHidden()
    chat.deleteLater()

    class Source:
        config = endpoint

        def close(self):
            pass

    catalog = ModelCatalog()
    catalog.models = {m: ModelInfo(id=m, name=m) for m in ("TEE/glm-5.3", "anthropic/x")}
    catalog._endpoint, catalog._fetched_at = "e", datetime.now(UTC)
    picker = ModelPickerDialog(catalog, lambda: (Source(), "e", True), current="TEE/glm-5.3")
    assert not picker.attest.isHidden()
    assert picker._attest_settings().model == "TEE/glm-5.3"
    picker.search.setText("anthropic")
    assert picker.attest.isHidden()
    picker.deleteLater()
