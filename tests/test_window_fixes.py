"""What a look over the play window after 3.1.0b8 found, held: the keyboard
stays where the author is typing, a story that can't be read leaves the
window as it was, nothing typed is lost, nothing empty is saved, and a chat
speaks of a chat. Offscreen, mocks only."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402

LONG = "A long passage of the story, written slowly. " * 10


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_for(app: QApplication, condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.003)


def _window(
    tmp_path: Path, story, cast, responses: list[str] | None = None, exchanges: int = 2
) -> MainWindow:
    nodes = make_exchange(exchanges)
    if nodes:
        story.active_leaf_id = nodes[-1].id
    story.held_character_id = "char-serrik"
    save_story_bundle(StoryBundle(story=story, cast=cast, nodes=nodes), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "manual"
    window.config.suggest_characters = False
    window.resize(1600, 900)
    window.show()
    window.open_story(story.id)
    window._provider = MockChatProvider(responses or [LONG])
    return window


def _idle(app: QApplication, window: MainWindow) -> None:
    wait_for(app, lambda: not window._busy, timeout=40)
    app.processEvents()


# --- 1. the composer takes the keyboard back only from where it lost it ------------


def _send_slowly(app, window: MainWindow, text: str = "I go up.") -> None:
    window._provider.delay = 0.03
    window.composer.input.setPlainText(text)
    window.composer.input.setFocus()
    app.processEvents()
    window.send_turn()
    wait_for(app, lambda: window._busy)


def test_the_composer_has_the_keyboard_again_once_the_passage_is_in(app, tmp_path, story, cast):
    window = _window(tmp_path, story, cast)
    _send_slowly(app, window)
    assert window.focusWidget() is not window.composer.input, "turned off while it writes"
    wait_for(app, lambda: window._background or not window._busy)
    focus = window.focusWidget()
    _idle(app, window)
    assert focus is window.composer.input
    window.close()


def test_the_passage_landing_leaves_the_find_bar_its_keyboard(app, tmp_path, story, cast):
    """Ctrl+F while the passage streamed: it landed, the keyboard jumped to
    the composer, and the rest of the search and its Enter went out as a
    turn (the review after 3.1.0b8)."""
    window = _window(tmp_path, story, cast)
    _send_slowly(app, window)
    window.open_find()
    field = window.find_bar.field
    field.setFocus()
    app.processEvents()
    wait_for(app, lambda: window._background or not window._busy)
    focus = window.focusWidget()
    _idle(app, window)
    assert focus is field
    assert window.composer.text() == "" and window._queued is None
    window.close_find()
    window.close()


def test_the_passage_landing_leaves_a_message_being_edited_its_keyboard(app, tmp_path, story, cast):
    window = _window(tmp_path, story, cast)
    widget = window.transcript.message_widget("u0")
    widget.begin_edit()
    _send_slowly(app, window)
    editor = window.transcript.open_editor()._editor
    editor.setFocus()
    app.processEvents()
    wait_for(app, lambda: window._background or not window._busy)
    focus = window.focusWidget()
    _idle(app, window)
    assert focus is editor
    window.transcript.open_editor().cancel_edit()
    window.close()


def test_the_passage_landing_leaves_a_full_window_page_its_keyboard(app, tmp_path, story, cast):
    """On Your characters the rest of a sentence typed in a field went into
    the hidden composer's draft."""
    window = _window(tmp_path, story, cast)
    _send_slowly(app, window)
    window.show_collection(True)
    search = window.collection_page.search
    search.setFocus()
    app.processEvents()
    wait_for(app, lambda: window._background or not window._busy)
    focus = window.focusWidget()
    _idle(app, window)
    assert focus is search
    window.show_collection(False)
    window.close()
