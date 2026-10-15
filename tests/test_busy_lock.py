"""Looking through the Inspector while a model writes (gui/busy_lock.py):
the author can pick another character and see their portrait, and nothing
on the page can change the story until the job is done. Offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    save_story_bundle(StoryBundle(story=story, cast=cast, nodes=make_exchange(2)), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    yield window
    window.close()


def settle(app) -> None:
    for _ in range(3):
        app.processEvents()


def test_the_pages_can_be_looked_through_but_not_changed_during_a_job(app, window, cast):
    panel = window.cast_panel
    panel.select_character(cast[0].id)
    assert panel.name.isEnabled() and not panel.name.isReadOnly()
    assert panel.remove_button.isEnabled()
    window._busy = True
    window._update_controls()
    assert panel.isEnabled() and panel.list.isEnabled()  # still browsable
    assert panel.name.isReadOnly() and not panel.remove_button.isEnabled()
    assert not panel.portrait.draw_button.isEnabled()
    # Another character: their card shows, its controls built for it stay off.
    panel.select_character(cast[1].id)
    settle(app)
    assert panel.name.text() == cast[1].name and panel.name.isReadOnly()
    assert not panel.remove_button.isEnabled()
    combos = [panel.skills.cellWidget(row, 1) for row in range(panel.skills.rowCount())]
    assert not any(combo.isEnabled() for combo in combos)
    # Done: each control as the page last left it.
    window._busy = False
    window._update_controls()
    assert not panel.name.isReadOnly() and panel.remove_button.isEnabled()
    assert panel.portrait.draw_button.isEnabled()


def test_what_the_page_turns_off_meanwhile_stays_off(app, window):
    """The review: a story's first passage fixes Person and Tense while the
    page is locked; the page's setEnabled(False) on a control the lock had
    already turned off sent Qt no event, and they came back editable."""
    style = window.style_panel
    style.set_started(False)
    assert style.person.isEnabled()
    window._busy = True
    window._update_controls()
    assert not style.person.isEnabled()
    style.set_started(True)  # the passage is in: fixed now
    window._busy = False
    window._update_controls()
    assert not style.person.isEnabled() and not style.tense.isEnabled()
    # And text the page made editable meanwhile stays read-only until the end.
    panel = window.cast_panel
    window._busy = True
    window._update_controls()
    panel.name.setReadOnly(False)
    assert panel.name.isReadOnly()
    window._busy = False
    window._update_controls()
    assert not panel.name.isReadOnly()
    # Unlocked, the widgets' own methods are back.
    assert "setEnabled" not in vars(style.person) and "setReadOnly" not in vars(panel.name)


def test_a_control_qt_itself_turns_on_is_kept_off_and_remembered(app, window):
    """The event filter turns a control enabled by a path the shadow can't
    see (Qt's own method, a signal connected straight to the slot) off again
    by Qt's own method: called through the shadow, it stayed on for the job
    and was remembered off."""
    from PySide6.QtWidgets import QWidget

    panel = window.cast_panel
    window._busy = True
    window._update_controls()
    QWidget.setEnabled(panel.remove_button, True)
    assert not panel.remove_button.isEnabled()
    window._busy = False
    window._update_controls()
    assert panel.remove_button.isEnabled()
