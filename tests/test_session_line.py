"""The line above the transcript that always says how the open story or chat
is set up (gui/session_line.py). The author: a chat kept in memory only
looked like any other."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.engine.session import StorySession  # noqa: E402
from sealedlore.gui.session_line import describe_session  # noqa: E402
from sealedlore.models.config import Config, ProviderConfig  # noqa: E402
from sealedlore.models.route import ModelRoute  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def config(**changes) -> Config:
    return Config(
        providers=[ProviderConfig(name="nano", base_url=NANO, model="z-ai/glm-5.3")],
        active_provider_name="nano",
        **changes,
    )


def session_for(story: Story, cfg: Config, tmp_path: Path) -> StorySession:
    return StorySession(StoryBundle(story=story), cfg, MockChatProvider(), root=tmp_path)


def test_a_memory_chat_says_it_is_not_saved(tmp_path):
    story = Story(title="Quick", mode="chat", chat_keep="memory")
    story.defaults.main_model = "z-ai/glm-5.3"
    story.defaults.main_route = ModelRoute(priority="latency")
    line, tip = describe_session(session_for(story, config(), tmp_path))
    assert line.startswith("Simple chat · In memory only, not saved")
    assert "z-ai/glm-5.3" in line and "via nano-gpt.com" in line
    assert "Fastest first word · FP8+ · paid" in line
    assert "closing it loses it" in tip


def test_a_story_lists_every_calls_model_and_route(tmp_path):
    cfg = config(scene_model="z-ai/glm-5.3", model_routes={"scene": ModelRoute(priority="price")})
    story = Story(title="Doomsville")
    story.defaults.main_model = "anthropic/claude-sonnet-4.6"
    line, tip = describe_session(session_for(story, cfg, tmp_path))
    assert line.startswith("Story · Saved on disk")
    assert "anthropic/claude-sonnet-4.6" in line
    assert "Scene read: z-ai/glm-5.3 (Cheapest · FP8+, paid)" in tip
    assert "Plot: z-ai/glm-5.3 (Cheapest · FP8+, paid)" in tip, "the plot falls back to the scene"


def test_an_open_private_scene_leads_the_line(tmp_path):
    story = Story(title="Doomsville")
    session = session_for(story, config(), tmp_path)
    session.enter_private(keep="memory", provider=MockChatProvider(), model="TEE/glm-5.3")
    line, tip = describe_session(session)
    assert line.startswith("🔒 Private scene open: TEE/glm-5.3, in memory only")
    assert "nothing from it reaches any other model" in tip


def test_an_encrypted_chat_says_so_with_its_attestation(tmp_path):
    story = Story(title="Sealed", mode="chat", chat_keep="memory")
    story.defaults.main_model = "private/glm-5-3"
    line, _tip = describe_session(session_for(story, config(), tmp_path), tee_state="attested")
    assert "private/glm-5-3 · 🔐 end-to-end encrypted (attested)" in line
    assert "paid" not in line and "routing" not in line, "an enclave has no route"


def test_the_window_always_shows_it(app, tmp_path, story, cast, monkeypatch):
    from sealedlore.gui.main_window import MainWindow
    from sealedlore.storage.repository import save_story_bundle
    from tests.test_chat_gui import make_chat

    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    assert window.session_line.isHidden(), "nothing open, nothing to say"
    window.open_story(story.id)
    assert not window.session_line.isHidden()
    assert window.session_line.label.text().startswith("Story · Saved on disk")
    make_chat(window, monkeypatch, model="z-ai/glm-5.3", keep="memory")
    assert window.session_line.label.text().startswith("Simple chat · In memory only")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.close_story()
    assert window.session_line.isHidden()
    window.close()
