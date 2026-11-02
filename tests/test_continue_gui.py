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
