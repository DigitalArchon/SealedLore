"""Setup's Starting scene tab and the Start dialog: who is there at the start."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from sealedlore.gui.main_window import MainWindow
from sealedlore.gui.setup_dialog import SetupDialog
from sealedlore.gui.start_dialog import StartDialog
from sealedlore.gui.start_presence import PLAYING, SET_ASIDE, StartPresence
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle

SERRIK, MAELA, IDRIS = "char-serrik", "char-maela", "char-idris"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_each_row_is_the_opening_s_unless_pinned(app, cast):
    presence = StartPresence(cast, present=[MAELA], absent=[IDRIS])
    assert presence.pinned() == ([MAELA], [IDRIS])
    assert presence.choice(SERRIK) is None

    presence.show_roster([SERRIK])
    assert presence.pinned() == ([SERRIK], [])


def test_the_character_played_is_locked_without_pinning_them(app, cast):
    presence = StartPresence(cast, present=[], absent=[IDRIS])
    presence.set_locked(SERRIK, set_aside=[MAELA])
    combo = presence._choices[SERRIK]
    assert combo.currentText() == PLAYING and not combo.isEnabled()
    assert presence._choices[MAELA].currentText() == SET_ASIDE
    # Played this time is not pinned present for a Restart.
    assert presence.pinned() == ([], [IDRIS])

    presence.set_locked(MAELA)
    assert presence._choices[SERRIK].isEnabled()
    assert presence._choices[SERRIK].count() == 3
    assert presence._choices[MAELA].currentText() == PLAYING


def test_setup_with_no_cast_says_where_to_choose(app):
    story = Story(title="Blank")
    story.setup.absent_at_start = ["someone"]
    dialog = SetupDialog(story, [])
    assert dialog.presence is None and dialog.suggested is None
    hints = " ".join(label.text() for label in dialog.findChildren(type(dialog.voice_hint)))
    assert "This story has no cast yet" in hints

    dialog.location.setText("The gate")
    dialog._save()
    assert story.setup.starting_scene.location == "The gate"
    assert story.setup.absent_at_start == ["someone"]  # nothing shown, nothing lost
    dialog.deleteLater()


def test_setup_saves_who_is_pinned(app, cast):
    story = Story(title="Keep")
    story.setup.starting_scene = SceneState(present_character_ids=[MAELA])
    dialog = SetupDialog(story, cast)
    assert dialog.presence.pinned() == ([MAELA], [])
    dialog.presence._choices[IDRIS].setCurrentIndex(2)
    dialog.presence._choices[MAELA].setCurrentIndex(0)
    dialog._save()
    assert story.setup.starting_scene.present_character_ids == []
    assert story.setup.absent_at_start == [IDRIS]
    dialog.deleteLater()


def test_the_start_dialog_sets_aside_the_ones_not_chosen(app, cast):
    story = Story(title="One of them")
    story.setup.choose_one_of = [SERRIK, MAELA]
    story.setup.suggested_character_id = MAELA
    dialog = StartDialog(story, cast)
    assert dialog.presence._choices[MAELA].currentText() == PLAYING
    assert dialog.presence._choices[SERRIK].currentText() == SET_ASIDE
    assert dialog.presence._choices[IDRIS].isEnabled()
    assert StartDialog(story, []).pinned() is None
    dialog.deleteLater()


def _wait_idle(app, window: MainWindow, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while window._busy:
        assert time.monotonic() < deadline, "the job never finished"
        app.processEvents()
        time.sleep(0.005)


def test_starting_pins_then_reads_an_opening_used_as_written(
    app, tmp_path: Path, cast, monkeypatch
):
    story = Story(title="As written")
    story.setup.opening_text = "Serrik and Maela waited at the gate."
    story.setup.opening_mode = "as_written"
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    read = json.dumps({"already_here": ["Serrik Vaun", "Maela Orr"]})
    monkeypatch.setattr(window, "_current_provider", lambda: MockChatProvider([read]))

    def accept(dialog) -> int:
        dialog.play_as.setCurrentIndex(dialog.play_as.findData(SERRIK))
        dialog.presence._choices[IDRIS].setCurrentIndex(2)
        return StartDialog.Accepted

    monkeypatch.setattr(StartDialog, "exec", accept)
    window.start_story()
    _wait_idle(app, window)

    session = window.session
    assert session.story.setup.absent_at_start == [IDRIS]
    assert session.nodes[0].meta.scene_read
    assert session.story.scene.present_character_ids == [SERRIK, MAELA]
    window.close()
