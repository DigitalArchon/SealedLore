"""Setup → About's {{user}} is and {{char}} is (engine/placeholders.py)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_setup_chooses_whom_user_and_char_name(app, story, cast):
    from sealedlore.gui.setup_dialog import PLACEHOLDER_FIRST_PLAYED, SetupDialog

    story.setup.placeholders.char_name = "The Sundering"
    dialog = SetupDialog(story, list(cast))
    assert dialog.user_is.currentText() == PLACEHOLDER_FIRST_PLAYED
    assert dialog.char_is.currentText() == "The Sundering"  # a world card's name
    dialog.user_is.setCurrentIndex(dialog.user_is.findData("char-maela"))
    dialog.char_is.setCurrentIndex(dialog.char_is.findData("char-serrik"))
    dialog._save()
    assert story.setup.placeholders.user_id == "char-maela"
    assert story.setup.placeholders.char_id == "char-serrik"
    dialog = SetupDialog(story, list(cast))
    assert dialog.user_is.currentText() == "Maela Orr"
    assert dialog.char_is.currentText() == "Serrik Vaun"
    dialog.user_is.setCurrentIndex(0)
    dialog._save()
    assert story.setup.placeholders.user_id is None
    dialog.deleteLater()
