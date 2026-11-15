"""Chutes as an ordinary endpoint in the window (tests/test_chutes_mixed.py for
the rest): Settings → Endpoints → Add Chutes, Speaks moving it between Chutes'
two addresses, and Settings → Private's Chutes service saying which of its
models are private. Offscreen."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.engine.catalog import ModelInfo  # noqa: E402
from sealedlore.gui.model_picker import (  # noqa: E402
    COLUMNS,
    ModelCatalog,
    ModelField,
    ModelPickerDialog,
)
from sealedlore.gui.settings_dialog import SettingsDialog, private_service_of  # noqa: E402
from sealedlore.models.config import (  # noqa: E402
    CHUTES_ANTHROPIC_URL,
    CHUTES_BASE_URL,
    Config,
    ProviderConfig,
)
from sealedlore.providers.privacy import (  # noqa: E402
    CHUTES_PLAIN_NOTE,
    PRIVACY_LEVELS,
    privacy_of,
)

NANO = "https://nano-gpt.com/api/v1"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def config(*extra: ProviderConfig) -> Config:
    nano = ProviderConfig(name="Nano", base_url=NANO, api_key="k")
    return Config(providers=[nano, *extra], active_provider_name="Nano")


def test_add_chutes_is_an_openai_endpoint_and_speaks_moves_it_to_the_anthropic_proxy(
    app, monkeypatch
):
    cfg = config()
    dialog = SettingsDialog(cfg, None)
    dialog.add_chutes.click()
    assert (dialog.base_url.text(), dialog.chat_api.currentData()) == (CHUTES_BASE_URL, "openai")
    note = dialog.api_note.text()
    assert "OpenAI-compatible" in note and "https://api.chutes.ai" in note
    assert "Its other models aren't private" in note
    dialog.chat_api.setCurrentIndex(dialog.chat_api.findData("anthropic"))
    assert dialog.base_url.text() == CHUTES_ANTHROPIC_URL
    assert "Chutes' own proxy" in dialog.api_note.text()
    dialog.chat_api.setCurrentIndex(dialog.chat_api.findData("openai"))
    assert dialog.base_url.text() == CHUTES_BASE_URL
    # An address typed by hand stays where it is.
    dialog.base_url.setText("https://llm.chutes.ai/v1/")
    dialog.chat_api.setCurrentIndex(dialog.chat_api.findData("anthropic"))
    assert dialog.base_url.text() == "https://llm.chutes.ai/v1/"
    dialog.chat_api.setCurrentIndex(dialog.chat_api.findData("openai"))
    dialog.base_url.setText(CHUTES_BASE_URL)
    dialog.api_key.setText("cpk_1")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    dialog._save()
    saved = cfg.endpoint_named("Chutes")
    assert (saved.base_url, saved.api, saved.key_saved_for) == (
        CHUTES_BASE_URL,
        "openai",
        "https://llm.chutes.ai",
    )
    dialog.deleteLater()


def test_moving_to_the_anthropic_proxy_asks_about_the_saved_key(app, monkeypatch):
    """Everything else about a key's place holds: the address edited by
    Speaks asks at Save, No the default, and No keeps it at its own."""
    saved = ProviderConfig(
        name="Chutes",
        base_url=CHUTES_BASE_URL,
        api_key="cpk_1",
        key_saved_for="https://llm.chutes.ai",
    )
    cfg = config(saved)
    asked: list[tuple[str, object]] = []

    def question(_parent, title, _text, _buttons, default):
        asked.append((title, default))
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "question", question)
    dialog = SettingsDialog(cfg, None)
    dialog.endpoint_list.setCurrentRow(1)
    dialog.chat_api.setCurrentIndex(dialog.chat_api.findData("anthropic"))
    assert dialog.base_url.text() == CHUTES_ANTHROPIC_URL
    dialog._save()
    assert asked == [("Use the saved key with claude.chutes.ai?", QMessageBox.No)]
    endpoint = cfg.endpoint_named("Chutes")
    assert endpoint.base_url == CHUTES_ANTHROPIC_URL and endpoint.api_key == "cpk_1"
    assert endpoint.sendable().api_key == "", "held back at claude.chutes.ai until Yes"
    dialog.deleteLater()


def test_the_private_page_offers_chutes_and_says_which_of_its_models_are_private(app):
    cfg = config()
    dialog = SettingsDialog(cfg, None)
    assert dialog.private_service_note.isHidden()
    dialog.private_service.setCurrentIndex(dialog.private_service.findData("chutes"))
    assert dialog.private_service.currentText().startswith("Chutes")
    assert dialog.private_url.text() == CHUTES_BASE_URL
    assert not dialog.private_service_note.isHidden()
    note = dialog.private_service_note.text()
    assert "-TEE model is end-to-end encrypted" in note and "isn't private" in note
    dialog.private_model.setText("Qwen/Qwen3.8-27B-TEE")
    assert dialog.private_model.privacy.text() == "🔐 E2EE · Chutes ⚠ partial"
    dialog.private_model.setText("zai-org/GLM-5.3")
    assert dialog.private_model.privacy.isHidden()
    dialog.private_service.setCurrentIndex(dialog.private_service.findData("nanogpt"))
    assert dialog.private_service_note.isHidden()
    dialog.deleteLater()
    assert private_service_of(ProviderConfig(name="p", base_url=CHUTES_ANTHROPIC_URL)) == "chutes"


# --- each level's words, wherever a model is chosen or shown ----------------------------


def test_a_model_fields_label_says_its_level_first_and_the_three_after(app):
    cfg = config(ProviderConfig(name="Chutes", base_url=CHUTES_BASE_URL, api_key="cpk"))

    def judge(model: str):
        return privacy_of(model, cfg)

    for model, label, lead in (
        ("private/glm-5-3", "🔐 E2EE", "Highest privacy."),
        ("TEE/glm-5.3", "TEE", "Partial privacy."),
        ("Chutes::Qwen/Qwen3.8-27B-TEE", "🔐 E2EE · Chutes ⚠ partial", "Partial privacy."),
    ):
        field = ModelField(model, privacy_of=judge)
        assert field.privacy.text() == label
        tip = field.privacy.toolTip()
        assert tip.startswith(lead) and tip.endswith(PRIVACY_LEVELS), model
    ordinary = ModelField("Chutes::zai-org/GLM-5.3", privacy_of=judge)
    assert ordinary.privacy.isHidden(), "not private: no label beside it"


def test_the_picker_says_each_chutes_models_level_and_what_chutes_reads(app):
    cfg = config(ProviderConfig(name="Chutes", base_url=CHUTES_BASE_URL, api_key="cpk"))
    tee, ordinary = "Chutes::Qwen/Qwen3.8-27B-TEE", "Chutes::zai-org/GLM-5.3"
    catalog = ModelCatalog()
    catalog.models = {m: ModelInfo(id=m, name=m, endpoint="Chutes") for m in (tee, ordinary)}
    from datetime import UTC, datetime

    catalog._endpoint, catalog._fetched_at = "e", datetime.now(UTC)
    dialog = ModelPickerDialog(
        catalog, lambda: (None, "e", False), privacy_of=lambda m: privacy_of(m, cfg)
    )
    rows = {
        dialog.table.topLevelItem(i).data(0, 0x0100 + 1): dialog.table.topLevelItem(i)
        for i in range(dialog.table.topLevelItemCount())
    }
    column = COLUMNS.index("Privacy")
    assert rows[tee].text(column) == "🔐 Encrypted (Chutes, partial)"
    assert rows[tee].toolTip(column).startswith("Partial privacy.")
    assert rows[ordinary].text(column) == ""
    assert rows[ordinary].toolTip(column) == CHUTES_PLAIN_NOTE
    dialog.table.setCurrentItem(rows[ordinary])
    note = dialog.privacy_note.text()
    assert not dialog.privacy_note.isHidden()
    assert note.startswith("Not private. Chutes reads this model's text")
    assert "-TEE models are the ones sent end-to-end encrypted" in note
    assert note.endswith(PRIVACY_LEVELS)
    dialog.table.setCurrentItem(rows[tee])
    assert dialog.privacy_note.text().startswith("Partial privacy.")
    dialog.deleteLater()
