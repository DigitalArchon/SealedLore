"""A new setup starts as far along as it safely can: the story model is
Sonnet 4.6, and the private endpoint is nano-gpt's with the main key. The
private model is left for the author to choose, so private scenes stay off
until they have looked the settings over."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sealedlore.cli import main as cli_main
from sealedlore.models.config import (
    DEFAULT_BASE_URL,
    DEFAULT_STORY_MODEL,
    Config,
    ProviderConfig,
)
from sealedlore.storage.repository import load_config

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_configure_without_a_model_sets_the_default(tmp_path: Path, capsys):
    cli_main(["--data-dir", str(tmp_path), "configure", "--api-key", "k"])
    provider = load_config(root=tmp_path).active_provider()
    assert provider is not None and provider.model == DEFAULT_STORY_MODEL
    # An existing provider's model is never replaced by the default.
    cli_main(["--data-dir", str(tmp_path), "configure", "--model", "z-ai/glm-5.3"])
    cli_main(["--data-dir", str(tmp_path), "configure", "--api-key", "k2"])
    assert load_config(root=tmp_path).active_provider().model == "z-ai/glm-5.3"


pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_a_fresh_setup_needs_only_the_key(app):
    config = Config()
    dialog = SettingsDialog(config, None)
    assert dialog.model.text() == DEFAULT_STORY_MODEL
    assert dialog.private_url.text() == DEFAULT_BASE_URL
    assert dialog.private_key.text() == "" and dialog.private_model.text() == ""
    dialog.api_key.setText("main-key")
    dialog._save()
    provider = config.active_provider()
    assert provider is not None and provider.base_url == DEFAULT_BASE_URL
    assert provider.model == DEFAULT_STORY_MODEL and provider.api_key == "main-key"
    # No private model chosen: nothing saved, so no private scene can start.
    assert config.private_provider is None and config.private() is None


def test_choosing_a_private_model_uses_the_main_key(app):
    config = Config()
    dialog = SettingsDialog(config, None)
    dialog.api_key.setText("main-key")
    dialog.private_model.setText("private/glm-5-3")
    dialog._save()
    assert config.private_provider is not None
    assert config.private_provider.api_key == "", "blank: it follows the main key"
    private = config.private()
    assert private is not None and private.api_key == "main-key"
    assert private.base_url == DEFAULT_BASE_URL and private.model == "private/glm-5-3"


def test_the_default_never_moves_an_open_story(app, story):
    """With no model saved the field shows the default; saving Settings for
    another reason must not put it on the open story."""
    config = Config(
        providers=[ProviderConfig(name="default", api_key="k", model="")],
        active_provider_name="default",
    )
    story.defaults.main_model = "z-ai/glm-5.3"
    dialog = SettingsDialog(config, story)
    assert dialog.model.text() == DEFAULT_STORY_MODEL
    dialog._save()
    assert story.defaults.main_model == "z-ai/glm-5.3"
    assert config.active_provider().model == DEFAULT_STORY_MODEL
