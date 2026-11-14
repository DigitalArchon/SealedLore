"""Continue in the window: the button, the ⋯ menu, and the text streaming
onto the end of the passage it carries on."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.transcript import MessageWidget  # noqa: E402
from tests.test_background_queue import _send, _window, wait_for  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_the_button_and_the_menu_carry_on_the_latest_passage(app, tmp_path: Path, story, cast):
    window = _window(
        tmp_path, story, cast, ["Beyond the door the", "{}", "stairs went down.", "{}"]
    )
    session = window.session
    assert not window.composer.continue_button.isEnabled()

    _send(window, "I push the door.")
    wait_for(app, lambda: not window._busy)
    passage = session.path()[-1]
    assert window.composer.continue_button.isEnabled()
    widget = window.transcript.message_widget(passage.id)
    assert "continue" in widget.actions_
    # Only the latest passage is offered it.
    turn = window.transcript.message_widget(session.path()[-2].id)
    assert "continue" not in turn.actions_

    window.composer.continue_button.click()
    wait_for(app, lambda: not window._busy)

    assert [node.id for node in session.path()] == [session.path()[0].id, passage.id]
    assert passage.content == "Beyond the door the stairs went down."
    shown = [
        w
        for w in window.transcript.findChildren(MessageWidget)
        if window.transcript._layout.indexOf(w) >= 0 and w.node_id == passage.id
    ]
    assert len(shown) == 1
    assert shown[0].body.source == "Beyond the door the stairs went down."
    window.close()


def test_the_menu_item_on_the_passage_does_the_same(app, tmp_path: Path, story, cast):
    window = _window(tmp_path, story, cast, ["She turned.", "{}", "Then she ran.", "{}"])
    _send(window, "I call her name.")
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]

    window.transcript.message_widget(passage.id).actions_["continue"].trigger()
    wait_for(app, lambda: not window._busy)

    assert passage.content == "She turned.\n\nThen she ran."
    window.close()


def test_nothing_to_carry_on_before_a_passage(app, tmp_path: Path, story, cast):
    window = _window(tmp_path, story, cast, ["{}"])
    window.continue_passage()
    assert not window._busy
    window.close()


def test_a_reader_scrolled_up_keeps_their_place_when_the_reply_is_in(
    app, tmp_path: Path, story, cast
):
    """The rebuild once the job is done went to the end: scrolled up to read
    while the reply came in, the reader was taken down when it finished."""
    import time

    from sealedlore.gui.app import load_stylesheet
    from sealedlore.providers.mock import MockChatProvider

    long = "The station hummed through its night cycle. " * 40
    window = _window(tmp_path, story, cast, [long, "{}"] * 6)
    window.transcript.setStyleSheet(load_stylesheet())
    window.resize(1600, 900)
    window.show()
    for turn in range(4):
        _send(window, f"I wait, turn {turn}.")
        wait_for(app, lambda: not window._busy)
    slow = MockChatProvider([long, "{}"], chunk_size=8)
    slow.delay = 0.01
    window._provider = slow
    _send(window, "I wait again.")
    wait_for(app, lambda: window.transcript._streaming is not None)
    bar = window.transcript.verticalScrollBar()
    settle = time.monotonic() + 0.3
    wait_for(app, lambda: time.monotonic() > settle)
    bar.setValue(bar.maximum() // 3)
    app.processEvents()
    place = window.transcript.place_of()
    wait_for(app, lambda: not window._busy)
    for _ in range(20):
        app.processEvents()

    # The same message at the top of the view, give or take the few pixels
    # the rebuilt messages settle by, and nowhere near the end.
    now = window.transcript.place_of()
    assert now[0] == place[0] and abs(now[1] - place[1]) <= 12
    assert bar.value() < bar.maximum() - 200
    window.close()


def test_a_stop_leaves_the_cache_readout_as_it_was(app, tmp_path: Path, story, cast):
    """A reply stopped before it ended reports no usage, and the readout
    went blank, which read as the cache lost."""
    from sealedlore.models.node import Usage

    more = "Beyond it the stairs went down, " * 30
    window = _window(tmp_path, story, cast, ["The door gives.", "{}", more, "{}"])
    provider = window._provider
    provider.usage = Usage(prompt_tokens=5000, cache_read_tokens=4000)
    _send(window, "I push the door.")
    wait_for(app, lambda: not window._busy)
    shown = window.status_strip.cache_label.text()
    assert "reused" in shown and "none" not in shown

    provider.chunk_size, provider.delay = 4, 0.02
    window.composer.continue_button.click()
    wait_for(app, lambda: window.transcript._streaming is not None and provider._call_count > 2)
    window.stop_generation()
    wait_for(app, lambda: not window._busy)

    assert window.session.last_result.stopped_early
    assert window.status_strip.cache_label.text() == shown
    window.close()
