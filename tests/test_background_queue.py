"""The composer opens once the passage is in; a turn sent during the reads waits."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402


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


def test_a_turn_sent_during_the_reads_waits_for_them(app, tmp_path: Path, story, cast):
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "every_turn"
    window.open_story(story.id)
    session = window.session
    session.story.held_character_id = "char-serrik"
    provider = MockChatProvider(["The door gives.", "{}", "The hall is dark.", "{}"])
    # Slow enough that the scene read is still running when we look.
    provider.delay = 0.02
    window._provider = provider
    window._refresh_composer()

    window.composer.input.setPlainText("I push the door.")
    window.send_turn()
    wait_for(app, lambda: window._background)

    # The passage is in and the scene read is running: the author can write.
    assert window._busy
    assert window.composer.input.isEnabled()
    assert window.composer.send_button.isEnabled()
    assert not window.composer.regenerate_button.isEnabled()
    assert not window.composer.held.isEnabled()

    window.composer.input.setPlainText("I step inside.")
    window.send_turn()
    assert window._queued is not None
    assert not window.composer.input.isEnabled()
    assert window.composer.text() == ""

    wait_for(app, lambda: not window._busy and window._queued is None and len(session.path()) == 4)
    wait_for(app, lambda: not window._busy)
    assert [node.content for node in session.path() if node.kind == "user"] == [
        "I push the door.",
        "I step inside.",
    ]
    window.close()


def test_stopping_the_reads_gives_the_queued_turn_back(app, tmp_path: Path, story, cast):
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "every_turn"
    window.open_story(story.id)
    window.session.story.held_character_id = "char-serrik"
    provider = MockChatProvider(["The door gives.", "{}"])
    provider.delay = 0.05
    window._provider = provider
    window._refresh_composer()

    window.composer.input.setPlainText("I push the door.")
    window.send_turn()
    wait_for(app, lambda: window._background)
    window.composer.input.setPlainText("I step inside.")
    window.send_turn()
    window.stop_generation()
    wait_for(app, lambda: not window._busy)

    assert window._queued is None
    assert window.composer.text() == "I step inside."
    assert [n.content for n in window.session.path() if n.kind == "user"] == ["I push the door."]
    window.close()


def test_a_notice_stays_clear_of_the_status_widgets(app, tmp_path: Path, story, cast):
    """QStatusBar re-showed its widgets over a message on any relayout."""
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    window.show()
    app.processEvents()
    bar, label = window.statusBar(), window.status_strip.budget_label

    bar.showMessage("Reading the scene…", 12000)
    label.setText("a much longer budget label than before " * 3)
    window.model_button.setVisible(True)
    app.processEvents()
    assert bar.currentMessage() == "Reading the scene…"
    assert not label.isVisible() and not window.model_button.isVisible()

    bar.clearMessage()
    app.processEvents()
    assert bar.currentMessage() == ""
    assert label.isVisible() and window.model_button.isVisible()
    window.close()


def test_a_click_on_a_notice_dismisses_it(app, tmp_path: Path, story, cast):
    """The reminder after changing the storyteller's model covered the model
    and route buttons for seconds; a click gives them back at once."""
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    window.show()
    app.processEvents()
    bar = window.statusBar()

    bar.showMessage("The next turn sends the whole prompt again.", 12000)
    app.processEvents()
    assert not window.model_button.isVisible()
    assert "Click to dismiss" in bar._notice.toolTip()

    QTest.mouseClick(bar._notice, Qt.LeftButton)
    app.processEvents()
    assert bar.currentMessage() == ""
    assert window.model_button.isVisible()
    assert not bar._timer.isActive()
    window.close()


# --- the turn loop in the window (the review of Oct 20 2026) ----------------------


def _window(tmp_path: Path, story, cast, responses: list[str]) -> MainWindow:
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "every_turn"
    window.config.suggest_characters = False
    window.open_story(story.id)
    window.session.story.held_character_id = "char-serrik"
    window._provider = MockChatProvider(responses)
    window._refresh_composer()
    return window


def _send(window: MainWindow, text: str) -> None:
    window.composer.select_speaker("char-serrik")
    window.composer.input.setPlainText(text)
    window.send_turn()


def test_a_turn_whose_passage_never_came_goes_back_to_the_composer(
    app, tmp_path: Path, story, cast, monkeypatch
):
    """Live (Oct 20 2026): the story model's id didn't exist, the request
    failed, and the author's turn stayed in the transcript over an empty
    composer; typed again, it was there twice. It comes back with its
    settings, as Take back does, and the failure says so; a passage with
    any text is kept, and a Stop before the first token returns the turn
    too."""
    from sealedlore.providers.base import ProviderError

    window = _window(tmp_path, story, cast, ["The door gives.", "{}"])
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(window, "_warn_plain", lambda *words: warned.append(tuple(words)))
    session = window.session
    provider = window._provider
    provider.error = ProviderError("https://x/v1 returned HTTP 404: model not found")
    window.composer.ooc_toggle.setChecked(True)
    window.composer.ooc.setPlainText("Keep it short.")
    _send(window, "I push the door.")
    wait_for(app, lambda: not window._busy)
    provider.error = None

    assert session.nodes == [] and session.story.active_leaf_id is None
    assert window.composer.text() == "I push the door."
    assert window.composer.ooc_text() == "Keep it short."
    assert window.composer.speaker_id() == "char-serrik"
    assert warned == [
        (
            "Generation failed",
            "https://x/v1 returned HTTP 404: model not found\n\n"
            "Your turn is back in the composer, ready to send again.",
        )
    ]
    assert "Generation failed" in window.statusBar().currentMessage()

    # Stopped before the first token: the turn comes back too, and says so.
    provider.delay = 5.0
    window.send_turn()
    wait_for(app, lambda: len(provider.requests) == 2)
    window.stop_generation()
    wait_for(app, lambda: not window._busy)
    provider.delay = 0.0
    assert session.nodes == [] and window.composer.text() == "I push the door."
    assert "back in the composer" in window.statusBar().currentMessage()

    # Text had arrived when Stop came: the partial passage is kept, the composer empty.
    provider.delay = 0.05
    window.send_turn()
    view = window.transcript
    wait_for(app, lambda: view._streaming is not None and bool(view._streaming.body.source))
    window.stop_generation()
    wait_for(app, lambda: not window._busy)
    assert [node.kind for node in session.path()] == ["user", "assistant"]
    assert window.composer.text() == ""
    window.close()


def test_a_stop_before_the_job_began_puts_the_turn_back(app, tmp_path: Path, story, cast):
    """Enter then Esc within the thread's start: the worker saw the stop
    before it ran the job, and the turn was gone from the composer with
    nothing written (the review, Oct 20 2026)."""
    window = _window(tmp_path, story, cast, ["Never written.", "{}"])
    session = window.session
    _send(window, "A turn I typed carefully.")
    window.stop_generation()  # synchronously: the thread has not run yet
    wait_for(app, lambda: not window._busy)
    assert window._worker is not None and window._worker.never_ran
    assert session.nodes == [] and window._provider.requests == []
    assert window.composer.text() == "A turn I typed carefully."
    window.close()


def test_closing_with_a_queued_turn_asks_first(app, tmp_path: Path, story, cast, monkeypatch):
    """A turn queued during the reads was neither sent nor saved nor back in
    the composer when the window closed meanwhile (the review, Oct 20
    2026). Closing asks, Keep playing first; nothing more can be queued
    once the window is closing."""
    from PySide6.QtWidgets import QMessageBox

    window = _window(tmp_path, story, cast, ["The yard is quiet.", "{}", "Up.", "{}"])
    window.show()
    provider = window._provider
    provider.delay = 0.05
    session = window.session
    _send(window, "I go up.")
    wait_for(app, lambda: window._background)
    _send(window, "MY QUEUED TURN, typed while the scene read ran.")
    assert window._queued is not None
    # The box dismissed with no choice made: Keep playing.
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    window.close()
    assert window.isVisible() and window._queued is not None and not window._close_when_idle
    wait_for(app, lambda: not window._busy and window._queued is None and len(session.path()) == 4)
    wait_for(app, lambda: not window._busy)
    assert [n.content for n in session.path() if n.kind == "user"][-1].startswith("MY QUEUED")

    # Chosen to lose it: the queue is cleared knowingly and the window closes.
    provider.responses = ["Again.", "{}"]
    _send(window, "I wait.")
    wait_for(app, lambda: window._background)
    _send(window, "Lost on purpose.")
    monkeypatch.setattr(window, "_may_lose_queued", lambda: True)
    nodes_before = len(session.nodes)
    window.close()
    assert window._close_when_idle and window._queued is None
    assert not window._can_queue()
    wait_for(app, lambda: not window.isVisible(), timeout=30)
    assert len(session.nodes) == nodes_before


def test_take_back_cancels_by_default_and_keeps_the_starting_scene(
    app, tmp_path: Path, story, cast, monkeypatch
):
    """Enter on the Take back box, which says "This can't be undone", took
    the turn back (live, Oct 20 2026): Cancel is the default now. And a take
    back of the first turn after switching who is held lost the starting
    scene: the composer's hold() cut the scene before the delete, and that
    cut scene was all that was left (the review)."""
    import json

    from PySide6.QtWidgets import QMessageBox

    def delta(**fields) -> str:
        return json.dumps(fields)

    story.scene.tracked = True
    window = _window(
        tmp_path,
        story,
        cast,
        [
            "Maela picks the lock while Serrik watches.",
            delta(),
            "Serrik climbs to the yard alone; Maela stays below.",
            delta(left=[{"name": "Maela Orr", "how": "stayed below"}], location="The yard"),
        ],
    )
    session = window.session
    start = (list(session.story.scene.present_character_ids), session.story.scene.location)
    window.composer.held.setCurrentIndex(window.composer.held.findData("char-maela"))
    app.processEvents()
    window.composer.select_speaker("char-maela")
    window.composer.input.setPlainText("I pick the lock.")
    window.send_turn()
    wait_for(app, lambda: not window._busy)
    maela_turn = session.path()[0]
    window.composer.held.setCurrentIndex(window.composer.held.findData("char-serrik"))
    app.processEvents()
    _send(window, "I climb to the yard.")
    wait_for(app, lambda: not window._busy)
    assert session.story.scene.present_character_ids == ["char-serrik"]

    defaults: list[str] = []

    def press(box, label: str):
        defaults.append(box.defaultButton().text())
        next(b for b in box.buttons() if b.text() == label).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", lambda box: press(box, "Cancel"))
    window._take_back(maela_turn.id)
    assert defaults == ["Cancel"] and len(session.nodes) == 4

    monkeypatch.setattr(QMessageBox, "exec", lambda box: press(box, "Take back"))
    window._take_back(maela_turn.id)
    assert session.nodes == [] and window.composer.text() == "I pick the lock."
    assert window.composer.held_character_id() == "char-maela"
    assert (list(session.story.scene.present_character_ids), session.story.scene.location) == start
    window.close()


def test_regenerate_is_not_offered_on_an_opening_written_as_given(app, tmp_path: Path, story, cast):
    """The Regenerate button and ⋯ → Regenerate this passage were live on an
    as-written opening, and pressing them gave "the assistant node's parent
    is not an author turn" (the review, Oct 20 2026)."""
    from sealedlore.gui.main_window import NOT_A_TAKE

    story.setup.opening_text = "The keep stood silent."
    story.setup.opening_mode = "as_written"
    window = _window(tmp_path, story, cast, ["{}"])
    session = window.session
    session.provider = window._provider
    list(session.begin("char-serrik"))
    window.reload_transcript()
    window._update_controls()
    opening = session.path()[-1]
    assert opening.kind == "assistant" and opening.parent_id is None
    assert not window.composer.regenerate_button.isEnabled()
    assert "regenerate" not in window.transcript.message_widget(opening.id).actions_
    window.regenerate()
    window._regenerate_node(opening.id)
    assert not window._busy and window.statusBar().currentMessage() == NOT_A_TAKE
    # Written on from, the passage after the author's turn is a take again.
    window._provider.responses = ["A wind rose.", "{}"]
    _send(window, "I step in.")
    wait_for(app, lambda: not window._busy)
    assert window.composer.regenerate_button.isEnabled()
    assert "regenerate" in window.transcript.message_widget(session.path()[-1].id).actions_
    window.close()


def test_a_failed_job_is_named_for_what_it_was(app, tmp_path: Path, story, cast, monkeypatch):
    """Every failed job was reported as "Generation failed" (the review, Oct
    20 2026): the title names the job."""
    from sealedlore.providers.base import ProviderError
    from tests.conftest import make_exchange

    nodes = make_exchange(12)
    story.active_leaf_id = nodes[-1].id
    save_story_bundle(StoryBundle(story=story, cast=cast, nodes=nodes), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.suggest_characters = False
    window.config.auto_archive = False
    window.open_story(story.id)
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(window, "_warn_plain", lambda *words: warned.append(tuple(words)))
    window._provider = MockChatProvider(["never"], error=ProviderError("the summariser is down"))
    window.archive_now()
    wait_for(app, lambda: not window._busy)
    assert warned == [("Archiving failed", "the summariser is down")]
    assert window.statusBar().currentMessage().startswith("Archiving failed:")
    window.close()


def test_an_editor_open_while_a_job_runs_keeps_its_text_and_outlives_the_job(
    app, tmp_path: Path, story, cast
):
    """The transcript stayed fully clickable while the model wrote: Save on
    an editor was thrown away without a word, and an editor open when the
    job ended was destroyed with the rebuild, mid-sentence (the review, Oct
    20 2026). Now Edit and ⋯ are off while a job runs, as every control that
    changes the story is; an editor already open stays, its Save waits for
    the job, and the rebuild waits for the editor to close."""
    from shiboken6 import isValid

    window = _window(
        tmp_path, story, cast, ["The door gives.", "{}", "The hall is dark.", "{}", "Quiet.", "{}"]
    )
    session = window.session
    _send(window, "I push the door.")
    wait_for(app, lambda: not window._busy)
    turn = session.path()[0]
    widget = window.transcript.message_widget(turn.id)
    assert widget.edit_button.isEnabled() and widget.more_button.isEnabled()
    widget.begin_edit()
    widget._editor.setPlainText("I push the door, hard.")

    window._provider.delay = 0.03
    _send(window, "I step inside.")
    wait_for(app, lambda: window._busy)
    other = window.transcript.message_widget(session.path()[1].id)
    assert not other.edit_button.isEnabled() and not other.more_button.isEnabled()
    assert widget._editor is not None and not widget._save_button.isEnabled()
    widget._commit_edit()  # Save pressed anyway: the editor and its text stay
    assert widget._editor is not None and session.nodes[0].content == "I push the door."
    wait_for(app, lambda: not window._busy)

    # The job ended: the rebuild waited, the editor is whole, Save works again.
    assert isValid(widget) and widget._editor is not None and widget._save_button.isEnabled()
    assert window._reload_deferred and other.edit_button.isEnabled()
    assert widget._editor.toPlainText() == "I push the door, hard."
    widget._commit_edit()
    assert session.nodes[0].content == "I push the door, hard."
    assert not window._reload_deferred
    assert window.transcript.message_widget(session.path()[-1].id) is not None

    # Cancelled instead, the waiting rebuild runs then too.
    window._provider.delay = 0.0
    widget = window.transcript.message_widget(turn.id)
    widget.begin_edit()
    _send(window, "I listen.")
    wait_for(app, lambda: not window._busy)
    assert window._reload_deferred and isValid(widget)
    widget.cancel_edit()
    assert not window._reload_deferred
    assert window.transcript.message_widget(session.path()[-1].id) is not None
    window.close()
