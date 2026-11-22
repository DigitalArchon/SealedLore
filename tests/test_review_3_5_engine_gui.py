"""The engine's story state, looked over before 3.5.0b1: what the window
does with it (tests/test_review_3_5_engine.py has the engine's). Offscreen,
mock clients only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from tests.test_background_queue import _window  # noqa: E402
from tests.test_phase7 import turn  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _press(label: str):
    def press(box) -> int:
        next(button for button in box.buttons() if button.text() == label).click()
        return 0

    return press


# --- E10: Take back on a branch's first turn keeps the branch for the turn ---------


def test_take_back_on_a_branchs_first_turn_leaves_its_name_for_the_next_turn(
    app, tmp_path: Path, story, cast, monkeypatch
) -> None:
    """The turn came back to the box, and sent again it was "Branch from
    message 3": the name had gone with the turn, and with one branch left
    the bar that says where the next turn goes was put away."""
    window = _window(tmp_path, story, cast, [])
    window.config.scene_reads = "manual"
    session = window.session
    for text in ("one", "two"):
        session.provider = MockChatProvider([f"Reply to {text}"])
        list(session.send(turn(text)))
    session.provider = MockChatProvider(["Another reply."])
    list(session.rewrite_from(session.path()[2].id, "a different two", name="What if"))
    window.reload_transcript()
    fork, first = session.full_path()[1:3]
    assert first.meta.branch_name == "What if"

    monkeypatch.setattr(QMessageBox, "exec", _press("Take back"))
    window._take_back(first.id)

    assert window.composer.text() == "a different two"
    assert session.story.active_leaf_id == fork.id
    assert session.pending_branch == (fork.id, "What if")
    assert not window.branch_bar.isHidden()
    assert "What if" in window.branch_bar.hint.text()
    window.close()


def test_a_first_message_taken_back_says_so_over_the_page_and_is_sent_as_the_first(
    app, tmp_path: Path, story, cast, monkeypatch
) -> None:
    """A branch from the story's very first message, its first message taken
    back: the story stands on no message until it is sent again. The page
    said "This story hasn't begun" with Start the story…, over a story with
    messages; then the fix for that sent the story to the end of Main, where
    the turn sent again was added after Main's last passage (the review of
    the fixes)."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QLabel, QPushButton

    from sealedlore.gui.main_window import TAKEN_BACK_FIRST

    window = _window(tmp_path, story, cast, [])
    window.config.scene_reads = "manual"
    session = window.session
    for text in ("one", "two"):
        session.provider = MockChatProvider([f"Reply to {text}"])
        list(session.send(turn(text)))
    session.provider = MockChatProvider(["Another reply."])
    list(session.rewrite_from(session.path()[0].id, "a different one", name="Other start"))
    window.reload_transcript()
    first = session.full_path()[0]
    assert first.meta.branch_name == "Other start"

    monkeypatch.setattr(QMessageBox, "exec", _press("Take back"))
    window._take_back(first.id)

    assert window.composer.text() == "a different one"
    assert session.story.active_leaf_id is None and len(session.nodes) == 4
    assert session.pending_branch == (None, "Other start")
    # Widgets a rebuild replaced are deleted later: gone before looking.
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    shown = [
        label.text()
        for label in window.transcript.findChildren(QLabel)
        if label.isVisibleTo(window)
    ]
    assert TAKEN_BACK_FIRST in shown
    buttons = [
        b.text() for b in window.transcript.findChildren(QPushButton) if b.isVisibleTo(window)
    ]
    assert not any("Start the story" in text for text in buttons), buttons
    assert not window.branch_bar.isHidden()  # Main is a click away
    window.close()
