"""Private scenes in the window: one click in, the controls that would leak
locked, the summary approved on the way out. Offscreen, mocks only."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import sealedlore.gui.main_window as main_window_module  # noqa: E402
import sealedlore.gui.window_private as window_private  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.config import ProviderConfig  # noqa: E402
from sealedlore.providers.base import ProviderError  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)
from tests.conftest import make_exchange  # noqa: E402

MARKER = "ZQX-PRIVATE"


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


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "manual"
    window.config.private_provider = ProviderConfig(
        name="private", base_url="http://localhost:11434/v1", model="local/tiny"
    )
    window.config.private_budget = 50_000
    window.config.private_intro_seen = True  # its own test below
    private = MockChatProvider([f"A private reply with {MARKER}."])
    window.private_provider_factory = lambda _settings: private
    window.open_story(story.id)
    window._provider = MockChatProvider(["The story model's reply."])
    window.private_mock = private
    yield window
    # Closing mid-scene asks first (a modal question): end the scene before.
    if window.session is not None and window.session.in_private:
        window.session.discard_private()
    window.close()


def send(app, window: MainWindow, text: str) -> None:
    window.composer.input.setPlainText(text)
    window.send_turn()
    wait_for(app, lambda: not window._busy)


def test_one_click_in_locks_what_would_leak(app, window: MainWindow):
    window.composer.private_keep.setCurrentIndex(window.composer.private_keep.findData("memory"))
    window.composer.private_toggle.click()
    assert window.session.in_private
    assert window.composer.private_toggle.isChecked()
    assert not window.composer.private_keep.isEnabled()
    assert "🔒" in window.model_button.text() and "memory only" in window.model_button.text()
    # Pictures stay available: the private model writes the prompt, and the
    # dialog warns that the picture itself is neither private nor unsaved.
    assert window.composer.image_button.isEnabled()
    assert window.image_action.isEnabled()
    assert not window.stories.isEnabled()
    assert "End private" in window.stories.toolTip()
    assert window.composer.input.property("private") == "true"
    assert window.minimumSizeHint().width() <= 1280


def test_a_private_scene_from_first_turn_to_approved_summary(app, window: MainWindow, monkeypatch):
    window.composer.private_toggle.click()
    send(app, window, f"I whisper {MARKER}.")
    assert window.private_mock.payloads
    headers = [
        w.header.text()
        for w in window.transcript.findChildren(type(window.transcript.add_message("x", "y", "z")))
    ]
    assert any("private" in header for header in headers)

    class Approve:
        APPROVE, AGAIN, BACK, DISCARD = "approve", "again", "back", "discard"

        def __init__(self, summary, parent=None, *, note=None, chat=False):
            self.choice = self.APPROVE
            self._summary = summary

        def exec(self):
            return 1

        def text(self):
            return "They spoke in private and agreed to meet at dawn."

    monkeypatch.setattr(window_private, "PrivateSummaryDialog", Approve)
    window.private_mock.responses = ["They spoke in private."]
    window.composer.private_toggle.click()  # end the scene: the summary job runs
    wait_for(app, lambda: not window._busy and not window.session.in_private)
    leaf = window.session.full_path()[-1]
    assert leaf.meta.private_summary_of is not None
    assert "agreed to meet at dawn" in leaf.content
    assert not window.composer.private_toggle.isChecked()
    assert window.composer.private_keep.isEnabled()
    assert "🔒" not in window.model_button.text()
    assert not window.composer.regenerate_button.isEnabled()

    saved = load_story_bundle(window.session.story.id, root=window.root)
    assert MARKER not in "".join(node.content for node in saved.nodes)


def test_closing_mid_scene_in_memory_asks_first(app, window: MainWindow, monkeypatch):
    window.composer.private_toggle.click()
    send(app, window, f"{MARKER} a")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    assert not window._private_allows_close()
    assert window.session.in_private
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    assert window._private_allows_close()
    assert not window.session.in_private


def test_no_private_model_says_where_to_set_one(app, window: MainWindow, monkeypatch):
    window.config.private_provider = None
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    window.composer.private_toggle.click()
    assert not window.session.in_private
    assert shown and "Settings → Private" in shown[0]


def test_the_image_dialog_warns_in_a_private_scene(app, window: MainWindow):
    from sealedlore.gui.image_dialog import ImageDialog

    window.config.image_models_fetched_at = "2099-01-01T00:00:00+00:00"
    window.composer.private_toggle.click()
    dialog = ImageDialog(window.session, parent=window)
    assert not dialog.private_warning.isHidden()
    assert "not private" in dialog.private_warning.text()
    assert "saved with the story on disk" in dialog.private_warning.text()
    dialog.prompt.setPlainText("A picture.")
    assert dialog.generate_button.text().startswith("Generate anyway")
    # The private model writes the prompt; the writer field can't send the
    # scene to another one.
    assert dialog.writer.text() == window.session.open_span.model
    assert dialog.writer.edit.isReadOnly() and dialog.writer_model() is None


def test_a_private_scene_freezes_the_scene_and_plot_controls(app, window: MainWindow):
    window.begin_private()
    assert window.in_private
    for widget in (window.scene_panel, window.plot_panel, window.composer.held):
        assert not widget.isEnabled()
    assert not window.branch_bar.isEnabled()
    # Nothing was written in it: ending discards at once, and the controls return.
    window.end_private()
    assert not window.in_private
    for widget in (window.scene_panel, window.composer.held):
        assert widget.isEnabled()


def test_without_an_endpoint_the_window_never_falls_back_to_the_mock(app, tmp_path, story, cast):
    from sealedlore.providers.base import UnconfiguredProvider

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(1))
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path)
    try:
        assert isinstance(window._build_provider(), UnconfiguredProvider)
        window.open_story(story.id)
        assert window.session is not None, "a story opens without an endpoint"
    finally:
        window.close()


def test_leaving_the_story_mid_memory_scene_asks_first(app, window: MainWindow, monkeypatch):
    window.composer.private_toggle.click()
    send(app, window, f"{MARKER} whispered")
    asked = []

    def cancel(*args, **kwargs):
        asked.append(args[1])
        return QMessageBox.Cancel

    monkeypatch.setattr(QMessageBox, "question", cancel)
    window.new_story()
    window.import_file()
    window.duplicate_entire_story(window.session.story.id)
    assert len(asked) == 3 and all("Private scene" in title for title in asked)
    assert window.session.in_private


def test_a_story_opens_speaking_as_the_character_played(app, window: MainWindow):
    # Playing Serrik: the speaker defaults to him, not to the narrator (seen
    # live: "Playing John / Speaking as Narration", and dice couldn't roll).
    assert window.composer.held_character_id() == "char-serrik"
    assert window.composer.speaker.currentData() == "char-serrik"


def test_a_tee_reply_is_marked_signed_never_verified(app, window: MainWindow):
    window.composer.private_toggle.click()
    send(app, window, f"I whisper {MARKER}.")
    leaf = window.session.full_path()[-1]
    leaf.meta.tee = "verified"
    window.reload_transcript()
    headers = [
        w.header.text()
        for w in window.transcript.findChildren(type(window.transcript.add_message("x", "y", "z")))
    ]
    assert any("TEE signed" in header for header in headers)
    assert not any("verified" in header.lower() for header in headers)


def test_a_scene_says_what_it_pauses_until_told_not_to(app, window: MainWindow, monkeypatch):
    window.config.private_intro_seen = False
    shown: list[str] = []

    def fake_exec(box):
        shown.append(box.text())
        box.checkBox().setChecked(True)  # "Don't show this again"
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    window.composer.private_toggle.click()
    assert shown and "Settings" in shown[0] and "End private" in shown[0]
    assert window.config.private_intro_seen
    window.session.discard_private()
    window._after_private_ended("x")
    window.composer.private_toggle.click()
    assert len(shown) == 1, "not shown again once ticked"


def test_settings_during_a_scene_says_why_instead_of_opening(app, window, monkeypatch):
    window.composer.private_toggle.click()
    told: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    opened: list = []
    monkeypatch.setattr(main_window_module, "SettingsDialog", lambda *a, **k: opened.append(1))
    assert window.settings_action.isEnabled()
    window.settings_action.trigger()
    assert told and "End private" in told[0] and opened == []


def test_a_failed_summary_never_traps_the_author_in_the_scene(app, window, monkeypatch):
    window.composer.private_toggle.click()
    send(app, window, f"{MARKER} a")
    window.private_mock.error = ProviderError("the model took too long")
    monkeypatch.setattr(window, "_warn_plain", lambda *a: None)
    asked: list[str] = []
    monkeypatch.setattr(window, "_ask_no_summary", lambda: asked.append("asked") or "keep")
    window.composer.private_toggle.click()  # end: the summary fails
    wait_for(app, lambda: not window._busy)
    assert asked and window.session.in_private, "asked, and keep playing keeps the scene"

    monkeypatch.setattr(window, "_ask_no_summary", lambda: "discard")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.composer.private_toggle.click()
    wait_for(app, lambda: not window._busy)
    assert not window.session.in_private
    assert not window.composer.private_toggle.isChecked()
    assert window.stories.isEnabled()


def test_on_windows_a_private_scene_is_kept_out_of_screen_capture(app, window, monkeypatch):
    """Recall and screenshots see black while the scene is open, and the
    window again once it ends (winprivacy; a no-op off Windows)."""
    calls: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        window_private, "exclude_from_capture", lambda widget, on: calls.append((widget, on))
    )
    window._sync_private_ui()
    assert calls == [], "nothing private yet"
    window.composer.private_toggle.click()
    assert calls == [(window, True)]
    window._sync_private_ui()
    assert calls == [(window, True)], "set once, not on every refresh"
    window.session.discard_private()
    window._sync_private_ui()
    assert calls == [(window, True), (window, False)]
