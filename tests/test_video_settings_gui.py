"""Settings → Images and Video: their own endpoints and keys. Offscreen."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import Config, MediaEndpoint, ProviderConfig  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def config() -> Config:
    chat = ProviderConfig(name="default", base_url="https://nano-gpt.com/api/v1", api_key="chat")
    return Config(providers=[chat], active_provider_name="default")


def test_video_says_it_is_off_until_it_has_a_key(app):
    dialog = SettingsDialog(config(), None)
    assert "Video is off" in dialog.video_key_note.text()
    dialog.video_key.setText("video-key")
    assert "key of its own" in dialog.video_key_note.text()
    dialog.video_key.setText("chat")
    assert "same key as another one here" in dialog.video_key_note.text()
    dialog.video_key.setText("")
    dialog._save()
    assert dialog.config.videos() is None


def test_the_note_knows_every_other_key_typed_in_settings(app):
    """Not the endpoint shown and the pictures' alone, as `Config.videos`
    compares them: another chat endpoint's, the embeddings' and the private
    model's too."""
    cfg = config()
    cfg.providers.append(
        ProviderConfig(name="Anthropic", base_url="https://api.anthropic.com", api_key="anth")
    )
    dialog = SettingsDialog(cfg, None)
    dialog.embed_api_key.setText("embed")
    dialog.private_key.setText("priv")
    for key in ("anth", "embed", "priv"):
        dialog.video_key.setText(key)
        assert "same key as another one here" in dialog.video_key_note.text(), key
    dialog.private_key.setText("other")
    assert "key of its own" in dialog.video_key_note.text(), "followed as it is typed"


def test_saving_keeps_each_endpoint_and_key_apart(app):
    cfg = config()
    cfg.image_models_fetched_at = "2026-09-29T00:00:00Z"
    dialog = SettingsDialog(cfg, None)
    dialog.image_url.setText("https://openrouter.ai/api/v1")
    dialog.image_key.setText("or-key")
    dialog.video_url.setText("https://api.wavespeed.ai/api/v3")
    dialog.video_key.setText("ws-key")
    dialog._save()
    assert cfg.image_endpoint == MediaEndpoint(
        base_url="https://openrouter.ai/api/v1", api_key="or-key"
    )
    assert cfg.images().api == "openrouter"
    assert cfg.image_models_fetched_at is None, "another endpoint's listing is fetched afresh"
    videos = cfg.videos()
    assert videos.api == "wavespeed" and videos.api_key == "ws-key" and not videos.shared_key
    assert cfg.active_provider().api_key == "chat"


def test_an_insecure_media_address_is_refused(app):
    dialog = SettingsDialog(config(), None)
    dialog.video_url.setText("http://example.com")
    dialog._save()
    assert not dialog.error.isHidden() and dialog.result() == 0
