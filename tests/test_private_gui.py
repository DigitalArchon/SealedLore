"""Private scenes in the window: one click in, the controls that would leak
locked, the summary approved on the way out. Offscreen, mocks only."""

from __future__ import annotations

import json
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


def test_close_story_mid_memory_scene_asks_and_discards(app, window: MainWindow, monkeypatch):
    window.composer.private_toggle.click()
    send(app, window, f"{MARKER} a")
    session = window.session
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    window.close_story()
    assert window.session is session and session.in_private
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.close_story()
    assert window.session is None
    assert not session.in_private


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


def test_a_private_scene_pauses_the_map_where_it_stands(app, window: MainWindow, monkeypatch):
    """The author (Oct 15 2026): the map stays in the window, paused, and in
    no way touches the scene; it catches up once the scene has ended."""
    window.session.story.defaults.world_map = True
    window._sync_world()
    assert window.map_layout and window.world_view.pause_note.isHidden()
    started: list[bool] = []
    monkeypatch.setattr(window.session, "start_map_art", lambda: started.append(True))
    window.begin_private()
    assert window.in_private and window.map_layout  # still there, not taken away
    assert not window.world_view.pause_note.isHidden()
    window._sync_world()
    assert started == []  # nothing drawn while the scene is open
    window.end_private()
    assert window.world_view.pause_note.isHidden() and started


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


def test_a_scene_on_a_retired_model_says_to_choose_another(app, window, monkeypatch):
    """nano-gpt retired private/glm-5-3-flash (Oct 2026). Its enclave is
    Tinfoil's router, so it would attest; the relay's list says it's gone."""
    from tests.test_private_mode import retired_setup

    provider, attested, _ = retired_setup(["private/glm-5-3"])
    window.private_provider_factory = lambda _settings: provider
    window.config.private_provider = ProviderConfig(
        name="private",
        base_url="https://nano-gpt.com/api/v1",
        api_key="k",
        model="private/glm-5-3-flash",
    )
    said: list[tuple[str, bool]] = []
    monkeypatch.setattr(window, "_say_model_retired", lambda m, chat, **_: said.append((m, chat)))
    reattest: list[str] = []
    monkeypatch.setattr(window, "_offer_reattest", lambda reason, chat: reattest.append(reason))

    window.composer.private_toggle.click()
    wait_for(app, lambda: window._tee_thread is None and said)
    assert said == [("private/glm-5-3-flash", False)] and attested == []
    assert "no longer offered" in window._private_model_text()
    assert "no longer offered" in window.session.tee_block

    # Sending says it again, never "enclave refused", and nothing is sent.
    window.composer.input.setPlainText(f"{MARKER} hello")
    window.send_turn()
    assert len(said) == 2 and reattest == []
    assert not window.session.span_nodes(window.session.open_span)

    # Nothing was written in it: ending it simply ends it.
    window._end_without_model()
    assert not window.session.in_private
    assert window._retired_model is None and window.session.tee_block is None


def test_a_scene_whose_model_was_retired_is_summarised_by_another(app, window, monkeypatch):
    """A scene with messages on a model nano-gpt has since retired: the
    author picks another as private, it is attested, and it writes the
    summary. Before, the only way out was to end with nothing carried over."""
    from sealedlore.providers.private_mode import ModelRetired
    from tests.test_private_mode import FakeEnclave, sealed_setup, sse

    window.composer.private_toggle.click()
    send(app, window, f"I whisper {MARKER}.")
    span = window.session.open_span
    span.model = "private/glm-5-3-flash"  # as begun, before nano-gpt retired it

    enclave = FakeEnclave()
    enclave.reply = sse("They spoke in private.")
    made: list[str] = []
    sealed = sealed_setup(enclave, ["private/glm-5-3", "private/kimi-k3"])
    window.private_provider_factory = lambda settings: made.append(settings.model) or sealed

    buttons: list[list[str]] = []
    picks = ["Keep it open", "Summarise with another model…"]

    def answer(box):
        buttons.append([b.text() for b in box.buttons()])
        want = picks.pop(0)
        next(b for b in box.buttons() if b.text() == want).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", answer)
    offered: list[list[str]] = []
    monkeypatch.setattr(
        window_private.QInputDialog,
        "getItem",
        staticmethod(lambda *a: (offered.append(list(a[3])) or "private/glm-5-3", True)),
    )

    class Approve:
        APPROVE, AGAIN, BACK, DISCARD = "approve", "again", "back", "discard"

        def __init__(self, summary, parent=None, *, note=None, chat=False):
            self.choice = self.APPROVE
            self._summary = summary

        def exec(self):
            return 1

        def text(self):
            return self._summary

    monkeypatch.setattr(window_private, "PrivateSummaryDialog", Approve)

    # Found retired when the scene was resumed: keep it open for now.
    retired = ModelRetired("private/glm-5-3-flash", ["private/glm-5-3", "private/kimi-k3"])
    window._on_attested(span.id, None, [], [], [retired])
    assert "Use another model…" in buttons[0] and window.session.in_private

    # Ending it: another model writes the summary.
    window.composer.private_toggle.click()
    wait_for(app, lambda: not window._busy and not window.session.in_private)
    assert "Summarise with another model…" in buttons[1]
    assert "End without a summary…" in buttons[1]
    assert offered == [["private/glm-5-3", "private/kimi-k3"]], "only as private, still offered"
    assert made == ["private/glm-5-3"]
    assert span.model == "private/glm-5-3" and span.summary_tee == "encrypted"
    leaf = window.session.full_path()[-1]
    assert leaf.meta.private_summary_of == span.id
    assert "They spoke in private." in leaf.content
    # The scene's text went sealed to the new model, and nowhere else.
    assert MARKER in json.dumps(enclave.requests[-1])
    assert all(MARKER.encode() not in body for body in enclave.wire)
    assert window._retired_model is None


def test_a_scene_moves_only_to_a_model_as_private(app, window, monkeypatch):
    window.composer.private_toggle.click()
    window.session.open_span.model = "private/glm-5-3-flash"
    with pytest.raises(ValueError, match="isn't as private"):
        window.session.switch_private_model(provider=window.private_mock, model="TEE/glm-5.3")
    assert window.session.open_span.model == "private/glm-5-3-flash"


def test_a_resumed_scene_is_checked_on_its_own_model(app, window, monkeypatch):
    """Every request in a scene names span.model: resuming one must attest
    and check that model, not the one Settings → Private names now."""
    window.composer.private_toggle.click()
    window.session.open_span.model = "private/glm-5-3-flash"
    window.config.private_provider = ProviderConfig(
        name="private", base_url="https://nano-gpt.com/api/v1", api_key="k", model="private/kimi-k3"
    )
    made: list[str] = []
    window.private_provider_factory = lambda settings: (
        made.append(settings.model) or window.private_mock
    )
    attested: list[str] = []
    monkeypatch.setattr(
        window, "_attest_or_say_not", lambda settings: attested.append(settings.model)
    )
    window._resume_private()
    assert made == attested == ["private/glm-5-3-flash"]
