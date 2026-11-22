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
