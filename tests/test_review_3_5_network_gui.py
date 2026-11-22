"""What the review of 3.5.0b1 found where the window asks a server something
(Nov 22 2026), one test a fault: each fails on 0f289da. Offscreen, and
every server is a stand-in made here."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui import settings_dialog as settings_module  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import Config, ProviderConfig, bind_unbound_keys  # noqa: E402
from sealedlore.providers.context_probe import ContextSize  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def config(**changes) -> Config:
    chat = ProviderConfig(name="NanoGPT", base_url=NANO, api_key="nano-key", model="glm")
    made = Config(providers=[chat], active_provider_name="NanoGPT", **changes)
    bind_unbound_keys(made)  # as a config is when loaded
    return made


# --- N6: Detect comes back, whatever the server answered ---------------------------


def _private_dialog() -> SettingsDialog:
    private = ProviderConfig(
        name="private", base_url="http://127.0.0.1:11434/v1", api_key="", model="m"
    )
    dialog = SettingsDialog(config(private_provider=private), None)
    dialog.private_url.setText("http://127.0.0.1:11434/v1")
    dialog.private_model.setText("m")
    return dialog


def test_detect_gives_its_button_back_when_the_asking_raises(app, monkeypatch):
    dialog = _private_dialog()

    def raises(url, key, model, **_):
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(settings_module, "detect_context", raises)
    dialog._detect_private_context()
    assert dialog.detect_button.isEnabled(), "left off, Detect could never be pressed again"
    assert dialog.detect_note.text().startswith("The server didn't say.")
    dialog.deleteLater()


@pytest.mark.parametrize(
    "tokens",
    [float("inf"), float("nan"), "8192", True, -5, None, pytest.param(10**30, id="30 digits")],
)
def test_detect_sets_no_budget_from_a_size_that_is_none(app, monkeypatch, tokens):
    dialog = _private_dialog()
    before = dialog.private_budget.value()
    monkeypatch.setattr(
        settings_module,
        "detect_context",
        lambda url, key, model, **_: ContextSize(tokens=tokens, source="a server"),
    )
    dialog._detect_private_context()
    assert dialog.detect_button.isEnabled()
    assert dialog.private_budget.value() == before
    assert dialog.detect_note.text().startswith("The server didn't say.")
    dialog.deleteLater()


def test_detect_still_sets_the_budget_from_a_real_size(app, monkeypatch):
    dialog = _private_dialog()
    monkeypatch.setattr(
        settings_module,
        "detect_context",
        lambda url, key, model, **_: ContextSize(tokens=32768, source="loaded in Ollama"),
    )
    dialog._detect_private_context()
    assert dialog.detect_button.isEnabled()
    assert dialog.private_budget.value() == int(32768 * 0.8) - 1_000
    assert "32,768 tokens (loaded in Ollama)" in dialog.detect_note.text()
    dialog.deleteLater()
