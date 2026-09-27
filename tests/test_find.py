"""Find in the story (Ctrl+F): matching, and the window showing a match,
including one in an older message the transcript hadn't built. Offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.app import load_stylesheet  # noqa: E402
from sealedlore.gui.find_bar import Match, find_matches  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.transcript import PAGE  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402

EXCHANGES = PAGE  # 2 × PAGE messages: the oldest half starts unbuilt


def test_matches_ignore_case_and_keep_offsets_into_the_text_as_it_is():
    texts = [("a", "The Guard arrived. the guard left."), ("b", "İstanbul, then the Guard.")]
    assert find_matches(texts, "GUARD") == [
        Match("a", 4, 5),
        Match("a", 23, 5),
        Match("b", 19, 5),
    ]
    # Characters that mean something in a pattern are just characters.
    assert find_matches([("c", "Is it (really) 3.5?")], "(really) 3.5") == [Match("c", 6, 12)]
    assert find_matches(texts, "   ") == []


@pytest.fixture
def app() -> QApplication:
    # Scroll positions need the real stylesheet: without it, offscreen, the
    # column lays its messages out zero high.
    app = QApplication.instance() or QApplication([])
    before = app.styleSheet()
    app.setStyleSheet(load_stylesheet())
    yield app
    app.setStyleSheet(before)


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    nodes = make_exchange(EXCHANGES)
    for node in nodes:  # long enough that a match off screen is far off it
        node.content = f"{node.content} " + "The station hummed through its night cycle. " * 12
    nodes[2].content = "Long ago, the Kestrel came down in the snow."  # u1: unbuilt at first
    nodes[-1].content = "Now the kestrel's wreck is a shelter."
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes)
    bundle.story.active_leaf_id = nodes[-1].id
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1366, 768)
    window.show()
    window.open_story(story.id)
    yield window
    window.close()


def settle(app: QApplication) -> None:
    for _ in range(10):
        app.processEvents()


def search(window: MainWindow, text: str) -> None:
    window.find_action.trigger()
    window.find_bar.field.setText(text)
    window.find_bar._timer.stop()
    window._on_find_changed(text)


def visible(window: MainWindow, label) -> bool:
    top = label.mapTo(window.transcript.widget(), label.rect().topLeft()).y()
    view = window.transcript.verticalScrollBar().value()
    return view <= top + label.height() and top <= view + window.transcript.viewport().height()


def test_find_starts_at_the_latest_match_and_enter_goes_back_to_an_unbuilt_one(
    app, window: MainWindow
):
    assert window.transcript.unbuilt > 0
    search(window, "kestrel")
    settle(app)
    assert window.find_bar.count.text() == "2 of 2"
    latest = window.transcript.message_widget(f"a{EXCHANGES - 1}").body
    assert latest.selectedText() == "kestrel"
    assert visible(window, latest)

    # Enter in the field: the earlier match, in a message not built until now.
    window.transcript.verticalScrollBar().setValue(window.transcript.verticalScrollBar().maximum())
    QApplication.sendEvent(
        window.find_bar.field, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier)
    )
    settle(app)
    assert window.find_bar.count.text() == "1 of 2"
    earlier = window.transcript.message_widget("u1").body
    assert earlier.selectedText() == "Kestrel"
    assert latest.selectedText() == "", "the last match is no longer selected"
    assert visible(window, earlier)


def test_a_rebuild_keeps_the_count_without_moving_the_reader(app, window: MainWindow):
    search(window, "kestrel")
    settle(app)
    window.transcript.verticalScrollBar().setValue(0)
    window.reload_transcript()
    assert window.find_bar.count.text() == "2 of 2"
    search(window, "no such words")
    assert window.find_bar.count.text() == "No matches"


def test_esc_closes_the_bar_and_ctrl_f_leaves_the_map(app, window: MainWindow, monkeypatch):
    window.show_map(True)
    window.find_action.trigger()
    assert not window.map_shown() and window.find_open()
    assert window.find_action.shortcut().matches(Qt.CTRL | Qt.Key_F)

    # Typing in the bar, Esc closes it, even while a passage is being written.
    stopped: list[bool] = []
    monkeypatch.setattr(window, "stop_generation", lambda: stopped.append(True))
    monkeypatch.setattr(window, "_busy", True)
    window.find_bar.field.setFocus()
    settle(app)
    window._on_escape()
    assert not window.find_open() and stopped == []
    window._on_escape()
    assert stopped == [True], "with the bar closed, Esc stops the passage as before"


@pytest.fixture
def long_window(app, tmp_path: Path, story, cast):
    nodes = make_exchange(3 * PAGE)  # 240 messages
    for node in nodes:
        node.content = f"{node.content} " + "The station hummed through its night cycle. " * 6
    nodes[2].content = "Long ago, the Kestrel came down in the snow."
    nodes[-1].content = "Now the kestrel's wreck is a shelter."
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes)
    bundle.story.active_leaf_id = nodes[-1].id
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1366, 768)
    window.show()
    window.open_story(story.id)
    yield window
    window.close()


def test_a_match_far_back_moves_the_window_there_and_back(app, long_window: MainWindow):
    """Only a window around the match is built, not every message after it."""
    window = long_window
    view = window.transcript
    search(window, "kestrel")
    settle(app)
    window._step_find(-1)  # the one in the story's first exchange
    settle(app)
    assert view.message_widget("u1").body.selectedText() == "Kestrel"
    assert view.unbuilt_later > PAGE, "every message after the match was built"
    assert visible(window, view.message_widget("u1").body)
    window._step_find(1)  # round to the newest again
    settle(app)
    assert view.at_latest
    assert view.message_widget(f"a{3 * PAGE - 1}").body.selectedText() == "kestrel"


def test_editing_an_old_message_keeps_the_window_there(app, long_window: MainWindow):
    window = long_window
    view = window.transcript
    view.scroll_to_node("u2")
    settle(app)
    assert view.unbuilt_later > PAGE
    window._edit_node("u2", "Author turn 2, put right.")
    settle(app)
    # Looked up without moving the window: the edit has to have kept it there.
    edited = view._built_widget("u2")
    assert edited is not None, "the reader was left at the newest page"
    assert edited.body.text() == "Author turn 2, put right."
    assert view.unbuilt_later > PAGE, "the edit rebuilt the whole story after the message"
    assert visible(window, edited.body)
