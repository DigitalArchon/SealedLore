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
