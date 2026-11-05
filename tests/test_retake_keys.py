"""Regenerate and Continue from the keyboard (Ctrl+R, Ctrl+Shift+Enter) and
the Story menu: what the composer's buttons do, through the same code, on
exactly when the buttons are, and nothing at all typed in a message's
editor, where the keys are the editor's (Ctrl+Enter there saves the edit)."""

from __future__ import annotations

import gc
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QAction, QKeySequence, QShortcut  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.config import ProviderConfig  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from tests.test_background_queue import _send, _window, wait_for  # noqa: E402

REGENERATE = QKeySequence("Ctrl+R")
CONTINUE = [QKeySequence("Ctrl+Shift+Enter"), QKeySequence("Ctrl+Shift+Return")]


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def shown(app: QApplication, window: MainWindow) -> MainWindow:
    """On screen and active: a window's keys reach only the active window.
    A window an earlier test closed without showing is collected first,
    once its own timers have run: collected while this one was being
    activated, it took PySide down (offscreen, on the code before these
    keys too)."""
    app.processEvents()
    gc.collect()
    app.processEvents()
    window.show()
    window.activateWindow()
    QTest.qWaitForWindowActive(window, 2000)
    app.processEvents()
    return window


def press(app: QApplication, widget, key, modifiers) -> None:
    """A key typed where `widget` has the keyboard, as a person types it:
    through the window's shortcuts first. The window is made active again
    first: offscreen there is no window manager to give it back after a
    dialog (the chat's own, faked here, took it)."""
    window = widget.window()
    window.activateWindow()
    QTest.qWaitForWindowActive(window, 2000)
    widget.setFocus()
    app.processEvents()
    QTest.keyClick(widget, key, modifiers)
    app.processEvents()


def ctrl_r(app: QApplication, widget) -> None:
    press(app, widget, Qt.Key_R, Qt.ControlModifier)


def ctrl_shift_enter(app: QApplication, widget, *, keypad: bool = False) -> None:
    if keypad:
        press(app, widget, Qt.Key_Enter, Qt.ControlModifier | Qt.ShiftModifier | Qt.KeypadModifier)
    else:
        press(app, widget, Qt.Key_Return, Qt.ControlModifier | Qt.ShiftModifier)


def follows(window: MainWindow) -> bool:
    """The Story menu's entries on and off as the composer's buttons are."""
    composer = window.composer
    return (window.regenerate_action.isEnabled(), window.continue_action.isEnabled()) == (
        composer.regenerate_button.isEnabled(),
        composer.continue_button.isEnabled(),
    )


def played(app: QApplication, tmp_path: Path, story, cast, responses: list[str]) -> MainWindow:
    """A story with one exchange played, the window shown: a passage to
    retake and to carry on."""
    window = shown(app, _window(tmp_path, story, cast, ["The door gives.", "{}", *responses]))
    _send(window, "I push the door.")
    wait_for(app, lambda: not window._busy)
    return window


def test_the_keys_are_theirs_alone(app, tmp_path: Path, story, cast):
    """No other shortcut of the window's has them (on Windows the window
    binds every key the platform gives New, Open, Close, Save and Quit, so
    this runs there too), nor do any of Qt's standard keys, which text boxes
    take for themselves. Refresh is Ctrl+R on GNOME and Replace on KDE: no
    widget here acts on either."""
    window = _window(tmp_path, story, cast, ["{}"])
    keys = [REGENERATE, *CONTINUE]
    assert window.regenerate_action.shortcuts() == [REGENERATE]
    assert window.continue_action.shortcuts() == CONTINUE
    for action in window.findChildren(QAction):
        if action in (window.regenerate_action, window.continue_action):
            continue
        assert not [key for key in action.shortcuts() if key in keys], action.text()
    for shortcut in window.findChildren(QShortcut):
        assert shortcut.key() not in keys
    for standard in QKeySequence.StandardKey:
        if standard.name in ("Refresh", "Replace"):
            continue
        assert not [key for key in QKeySequence.keyBindings(standard) if key in keys], standard
    # The buttons say their keys; the menu shows them beside its entries.
    assert "(Ctrl+R)" in window.composer.regenerate_button.toolTip()
    assert "(Ctrl+Shift+Enter)" in window.composer.continue_button.toolTip()
    story_menu = [action.text().replace("&", "") for action in window.story_menu.actions()]
    assert "Regenerate the latest passage" in story_menu
    assert "Continue the latest passage" in story_menu
    window.close()


def test_each_key_does_what_its_button_does(app, tmp_path: Path, story, cast):
    window = shown(
        app,
        _window(
            tmp_path,
            story,
            cast,
            ["The door gives.", "{}", "It holds.", "{}", "Then it gives.", "{}", "Dust.", "{}"],
        ),
    )
    session = window.session
    assert follows(window) and not window.regenerate_action.isEnabled()
    assert not window.continue_action.isEnabled()
    _send(window, "I push the door.")
    wait_for(app, lambda: not window._busy)
    assert follows(window) and window.regenerate_action.isEnabled()
    assert window.continue_action.isEnabled()
    turn, first = session.path()

    # Another take, in the shape the composer has now, as Regenerate sends it.
    window.composer.length.setCurrentIndex(window.composer.length.findData("brief"))
    ctrl_r(app, window.composer.input)
    wait_for(app, lambda: not window._busy)
    take = session.path()[-1]
    assert take.id != first.id and take.content == "It holds."
    assert session.take_position(take.id) == (2, 2)
    assert turn.meta.response_style == "brief"

    # More of it, written into it, as Continue does.
    ctrl_shift_enter(app, window.composer.input)
    wait_for(app, lambda: not window._busy)
    assert [node.id for node in session.path()] == [turn.id, take.id]
    assert take.content == "It holds.\n\nThen it gives."
    # The keypad's Enter is the same key.
    ctrl_shift_enter(app, window.composer.input, keypad=True)
    wait_for(app, lambda: not window._busy)
    assert take.content.endswith("Then it gives.\n\nDust.")
    window.close()


def test_the_keys_are_off_whenever_the_buttons_are(app, tmp_path: Path, story, cast):
    """An opening written as given is no take and has nothing to carry on;
    while a job runs nothing may be retaken."""
    story.setup.opening_text = "The keep stood silent."
    story.setup.opening_mode = "as_written"
    window = shown(app, _window(tmp_path, story, cast, ["{}"]))
    session = window.session
    session.provider = window._provider
    list(session.begin("char-serrik"))
    window.reload_transcript()
    window._update_controls()
    assert follows(window)
    assert not window.regenerate_action.isEnabled() and not window.continue_action.isEnabled()
    ctrl_r(app, window.composer.input)
    ctrl_shift_enter(app, window.composer.input)
    assert not window._busy and session.path()[-1].content == "The keep stood silent."

    window._provider = MockChatProvider(["A wind rose.", "{}"])
    window._provider.delay = 0.03
    _send(window, "I step in.")
    wait_for(app, lambda: window._busy)
    assert follows(window) and not window.regenerate_action.isEnabled()
    assert not window.continue_action.isEnabled()
    window.regenerate_action.trigger()
    window.continue_action.trigger()
    wait_for(app, lambda: not window._busy)
    passage = session.path()[-1]
    assert passage.content == "A wind rose." and session.take_position(passage.id) == (1, 1)
    assert follows(window) and window.regenerate_action.isEnabled()
    window.close()


def test_in_a_private_scene_they_follow_the_scene_s_rules(app, tmp_path: Path, story, cast):
    """Right after a scene begins the latest passage is the story model's:
    neither button is on, so neither key is; the scene's own passage is
    retaken by the private model."""
    window = played(app, tmp_path, story, cast, [])
    window.config.private_provider = ProviderConfig(
        name="private", base_url="http://localhost:11434/v1", model="local/tiny"
    )
    window.config.private_budget = 50_000
    window.config.private_intro_seen = True
    private = MockChatProvider(["A private reply.", "Another private reply."])
    window.private_provider_factory = lambda _settings: private
    window.composer.private_toggle.click()
    assert window.session.in_private
    assert follows(window) and not window.regenerate_action.isEnabled()
    assert not window.continue_action.isEnabled()
    window.composer.input.setPlainText("I lower my voice.")
    window.send_turn()
    wait_for(app, lambda: not window._busy)
    assert follows(window) and window.regenerate_action.isEnabled()
    ctrl_r(app, window.composer.input)
    wait_for(app, lambda: not window._busy)
    # The scene's messages are left out of `path()`, which the models read.
    assert window.session.full_path()[-1].content == "Another private reply."
    window.session.discard_private()
    window.close()


def test_typed_in_a_message_s_editor_they_do_nothing(app, tmp_path: Path, story, cast):
    """The keys there are the editor's: nothing is retaken or carried on, and
    the edit stays as it was typed. Chosen from the Story menu, with the
    keyboard still in the editor, the entry does what it says."""
    window = played(app, tmp_path, story, cast, ["It holds.", "{}"])
    session = window.session
    turn, passage = session.path()
    widget = window.transcript.message_widget(turn.id)
    widget.begin_edit()
    app.processEvents()
    widget._editor.setPlainText("I push the door, hard.")
    for key in (ctrl_r, ctrl_shift_enter):
        key(app, widget._editor)
        assert not window._busy
    assert window.transcript.open_editor(focused=True) is widget
    assert widget._editor.toPlainText() == "I push the door, hard."
    assert session.path()[-1].id == passage.id and passage.content == "The door gives."

    window.regenerate_action.trigger()
    wait_for(app, lambda: not window._busy)
    assert session.path()[-1].content == "It holds."
    for editing in window.transcript.open_editors():
        editing.cancel_edit()
    window.close()


def test_from_the_branch_map_the_story_comes_back_first(app, tmp_path: Path, story, cast):
    """The take is written where it can be watched, and stopped."""
    window = played(app, tmp_path, story, cast, ["It holds.", "{}"])
    window.show_map(True)
    assert window.map_shown()
    ctrl_r(app, window.story_map.view)
    assert not window.map_shown()
    wait_for(app, lambda: not window._busy)
    assert window.session.path()[-1].content == "It holds."
    window.close()


def test_under_the_cast_board_only_the_menu_acts(app, tmp_path: Path, story, cast):
    """The board has the keyboard (as Your characters does): its keys retake
    nothing, as Ctrl+Enter sends nothing there. The Story menu's entry
    brings the story back and carries the passage on."""
    window = played(app, tmp_path, story, cast, [" It swings wide.", "{}"])
    window.show_cast_board(True)
    assert window.board_shown()
    ctrl_r(app, window.cast_board)
    ctrl_shift_enter(app, window.cast_board)
    assert not window._busy and window.board_shown()
    window.continue_action.trigger()
    assert not window.board_shown()
    wait_for(app, lambda: not window._busy)
    assert window.session.path()[-1].content == "The door gives. It swings wide."
    window.close()


def test_a_chat_has_them_too(app, tmp_path: Path, monkeypatch):
    from tests.test_chat_gui import make_chat

    window = shown(app, MainWindow(root=tmp_path, use_mock=True))
    make_chat(window, monkeypatch)
    window._provider = MockChatProvider(["Hello there.", "Hello again.", "How can I help?"])
    words = [action.text().replace("&", "") for action in window.story_menu.actions()]
    assert "Regenerate the latest reply" in words and "Continue the latest reply" in words
    assert "latest reply" in window.composer.regenerate_button.toolTip()
    assert not window.regenerate_action.isEnabled() and follows(window)
    window.composer.input.setPlainText("Hi.")
    window.send_turn()
    wait_for(app, lambda: not window._busy)
    session = window.session
    assert follows(window) and window.regenerate_action.isEnabled()
    ctrl_r(app, window.composer.input)
    wait_for(app, lambda: not window._busy)
    reply = session.path()[-1]
    assert reply.content == "Hello again." and session.take_position(reply.id) == (2, 2)
    ctrl_shift_enter(app, window.composer.input)
    wait_for(app, lambda: not window._busy)
    assert reply.content == "Hello again.\n\nHow can I help?"
    window.close()
