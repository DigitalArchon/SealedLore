"""The privacy promises looked over before 3.5.0b1, in the window. Offscreen,
mock clients only (tests/test_review_3_5_privacy.py has the engine's side)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.config import ProviderConfig  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402

MARKER = "ZQX-PRIVATE"
LOCAL = "http://localhost:11434/v1"
ELSEWHERE = "https://api.elsewhere.example/v1"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_for(app: QApplication, condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.005)


class At(MockChatProvider):
    """A scene's client as the window makes it: it knows its own endpoint."""

    def __init__(self, settings: ProviderConfig) -> None:
        super().__init__([f"A private reply with {MARKER}."])
        self.config = settings


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "manual"
    window.config.private_provider = ProviderConfig(
        name="private", base_url=LOCAL, model="local/tiny"
    )
    window.config.private_budget = 50_000
    window.config.private_intro_seen = True
    window.made = []

    def make(settings: ProviderConfig) -> At:
        window.made.append(At(settings))
        return window.made[-1]

    window.private_provider_factory = make
    window.open_story(story.id)
    window._provider = MockChatProvider(["The story model's reply."])
    yield window
    if window.session is not None and window.session.in_private:
        window.session.discard_private()
    window.close()


def send(app, window: MainWindow, text: str) -> None:
    window.composer.input.setPlainText(text)
    window.send_turn()
    wait_for(app, lambda: not window._busy)


def answers(monkeypatch, picks: list[str]) -> list[tuple[str, str, list[str], str]]:
    """Answer each box the window opens with the next of `picks` (a button's
    words); what each box said: its title, text, buttons and default button."""
    boxes: list[tuple[str, str, list[str], str]] = []

    def answer(box: QMessageBox) -> int:
        default = box.defaultButton()
        boxes.append(
            (
                box.windowTitle(),
                box.text(),
                [b.text() for b in box.buttons()],
                default.text() if default is not None else "",
            )
        )
        want = picks.pop(0)
        next(b for b in box.buttons() if b.text() == want).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", answer)
    return boxes


def disk_scene_left_open(app, window: MainWindow) -> str:
    """A scene kept on disk on this computer's server, a message in it, and
    the story closed in the middle of it. The story's id."""
    window.composer.private_keep.setCurrentIndex(window.composer.private_keep.findData("disk"))
    window.composer.private_toggle.click()
    span = window.session.open_span
    assert span.keep == "disk" and span.privacy.origin == "http://localhost:11434"
    send(app, window, f"I whisper {MARKER} to her.")
    assert window.made[0].config.base_url == LOCAL and len(window.made[0].payloads) == 1
    story_id = window.session.story.id
    window._close_story()
    return story_id


# --- V1: a scene stays on the server it began on, until the author says otherwise -------------


def test_a_disk_scene_reopened_after_settings_changed_asks_before_anything_goes(
    app, window: MainWindow, monkeypatch
):
    story_id = disk_scene_left_open(app, window)
    window.config.private_provider = ProviderConfig(
        name="private", base_url=ELSEWHERE, api_key="k", model="some/cloud-model"
    )
    boxes = answers(monkeypatch, ["Keep it open", "Keep it open", "Carry it on there"])
    window.open_story(story_id)
    session = window.session
    assert session.in_private and session.privacy_held()
    title, text, buttons, default = boxes[0]
    assert title == "Not sent: the private scene's server changed"
    assert "http://localhost:11434" in text and "https://api.elsewhere.example" in text
    assert set(buttons) == {"Carry it on there", "End without a summary…", "Keep it open"}
    assert default == "Keep it open", "Enter must not send the scene anywhere"
    assert window._tee_state == "⚠ held back"

    # Kept open: sending asks again, and nothing goes anywhere.
    window.composer.input.setPlainText("And then?")
    window.send_turn()
    assert len(boxes) == 2 and not window._busy
    assert all(client.payloads == [] for client in window.made[1:])
    assert len(session.span_nodes(session.open_span)) == 2

    # Yes: it goes on there, on the scene's own model, and is recorded there.
    window.send_turn()
    assert len(boxes) == 3
    span = session.open_span
    assert session.privacy_held() is None and window._tee_state == ""
    assert span.privacy.origin == "https://api.elsewhere.example"
    assert window.made[-1].config.base_url == ELSEWHERE
    assert window.made[-1].config.model == "local/tiny"
    send(app, window, "And then?")
    sent = window.made[-1].payloads[-1]
    assert sent["model"] == "local/tiny" and MARKER in json.dumps(sent)

    # Asked once: opened again, nothing is asked.
    window._close_story()
    window.open_story(story_id)
    assert len(boxes) == 3 and window.session.privacy_held() is None


def test_a_held_scene_ends_without_a_summary_with_nothing_sent(
    app, window: MainWindow, monkeypatch
):
    story_id = disk_scene_left_open(app, window)
    window.config.private_provider = ProviderConfig(
        name="private", base_url=ELSEWHERE, model="some/cloud-model"
    )
    boxes = answers(monkeypatch, ["Keep it open", "End without a summary…"])
    asked: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _p, title, text, *a: asked.append(text) or QMessageBox.Yes,
    )
    window.open_story(story_id)
    # End private scene: the summary would be written at the new place, so it asks first.
    window.end_private()
    assert "Its summary would be written there" in boxes[1][1]
    assert asked and "nothing carried over" in asked[0]
    assert not window.session.in_private and window.session.privacy_held() is None
    assert all(client.payloads == [] for client in window.made[1:])
    assert window._provider.payloads == []


def test_a_disk_scene_reopened_where_it_began_asks_nothing(app, window: MainWindow, monkeypatch):
    story_id = disk_scene_left_open(app, window)
    # Another model in Settings → Private's field, the same server.
    window.config.private_provider = ProviderConfig(
        name="private", base_url=LOCAL, model="local/another"
    )
    boxes = answers(monkeypatch, [])
    window.open_story(story_id)
    assert boxes == [] and window.session.privacy_held() is None
    send(app, window, "And then?")
    assert window.made[-1].payloads[-1]["model"] == "local/tiny"


# --- V3: a TEE or encrypted chat's portrait prompts are written by its own model -------------


def test_the_portraits_dialog_names_the_model_that_writes_a_private_chats_prompts(
    app, tmp_path: Path
):
    from PySide6.QtWidgets import QLabel

    from sealedlore.gui.portraits_dialog import PortraitsDialog
    from tests.test_review_3_5_privacy import private_chat

    session, main = private_chat(tmp_path, "private/glm-5-3")
    people, _lore = session.picture_cards()
    dialog = PortraitsDialog(session, people, catalog=None, browse=None)
    try:
        said = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "private/glm-5-3 writes a prompt for each" in said
        assert "zai-org/glm-5.3 writes" not in said
        wait_for(app, lambda: not dialog.writing())
        assert main.payloads and {p["model"] for p in main.payloads} == {"private/glm-5-3"}
    finally:
        wait_for(app, lambda: not dialog.writing())
        dialog.done(0)
        dialog.deleteLater()


# --- V4: nothing is embedded while a private scene is open -----------------------------------


def test_re_embed_lorebook_waits_for_the_private_scene(app, window: MainWindow):
    from sealedlore.providers.mock import MockEmbeddings

    embeddings = MockEmbeddings()
    window.session.embeddings = embeddings
    window.lore_panel.set_embeddings_available(True)
    button = window.lore_panel.reembed_button
    usual = button.toolTip()
    assert button.isEnabled()
    window.composer.private_toggle.click()
    assert window.session.in_private
    assert not button.isEnabled()
    assert "private scene" in button.toolTip()
    # However it is asked for: no job starts, and the endpoint isn't called.
    window.reembed_lore()
    assert not window._busy and embeddings.calls == []
    # A refresh of the pages mid-scene doesn't bring it back.
    window.lore_panel.set_embeddings_available(True)
    assert not button.isEnabled()
    window.session.discard_private()
    window._sync_private_ui()
    window._update_controls()
    assert button.isEnabled() and button.toolTip() == usual
