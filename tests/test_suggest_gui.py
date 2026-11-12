"""Suggest… in the window: the button under More, the job, the menu of
options, and an option taken into the box to send by hand."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.gui import composer as composer_module  # noqa: E402
from sealedlore.gui.composer import QUESTION, SUGGEST_LABEL  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.suggest_menu import CAPTION, CHAT_CAPTION, SuggestMenu  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.test_background_queue import _send, _window, wait_for  # noqa: E402

OPTIONS = [
    '"Who else knows about this door?" I ask.',
    "I lift the lantern and go down the first three steps.",
    "I say nothing, and listen.",
]
REPLY = json.dumps(OPTIONS)


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def played(tmp_path: Path, story, cast, replies: list[str]) -> MainWindow:
    """A story with one exchange played, the More row open."""
    window = _window(tmp_path, story, cast, replies)
    window.composer.set_more(True)
    _send(window, "I put my shoulder to the door.")
    wait_for(QApplication.instance(), lambda: not window._busy)
    return window


def menu_of(window: MainWindow) -> SuggestMenu:
    wait_for(QApplication.instance(), lambda: getattr(window, "_suggest_menu", None) is not None)
    return window._suggest_menu


def test_the_button_is_under_more_and_follows_who_is_played(app, tmp_path: Path, story, cast):
    window = _window(tmp_path, story, cast, ["{}"])
    composer = window.composer
    button = composer.suggest_button
    assert button.text() == SUGGEST_LABEL == "Suggest…"
    assert composer.more_row.isAncestorOf(button)
    assert composer.send_button.isEnabled() and button.isEnabled()
    assert button.toolTip() == composer_module.SUGGEST_TIPS[False]

    composer.held.setCurrentIndex(composer.held.findData(composer_module.NOBODY))

    assert composer.send_button.isEnabled() and not button.isEnabled()
    assert button.toolTip() == composer_module.SUGGEST_NOBODY_TIP
    composer.held.setCurrentIndex(composer.held.findData("char-serrik"))
    assert button.isEnabled()
    window.close()

    empty = MainWindow(root=tmp_path / "none", use_mock=True)
    assert not empty.composer.send_button.isEnabled()
    assert not empty.composer.suggest_button.isEnabled()
    empty.close()


def test_it_is_off_while_a_job_runs(app, tmp_path: Path, story, cast):
    window = _window(tmp_path, story, cast, ["The door gives.", "{}"])
    window._provider.delay = 0.02
    button = window.composer.suggest_button
    _send(window, "I put my shoulder to the door.")
    assert not button.isEnabled()
    wait_for(app, lambda: window._background)
    # The reads still have the story: a turn may be written, nothing asked.
    assert window.composer.send_button.isEnabled() and not button.isEnabled()
    assert button.toolTip() == composer_module.SUGGEST_BUSY_TIP
    wait_for(app, lambda: not window._busy)
    assert button.isEnabled()
    window.close()


def test_asking_shows_the_options_and_one_goes_into_the_box_unsent(
    app, tmp_path: Path, story, cast
):
    window = played(tmp_path, story, cast, ["The door gives.", "{}", REPLY])
    session, composer = window.session, window.composer
    nodes = len(session.nodes)
    # Left on Question: a suggestion is the character's turn, not a question.
    composer.speaker.setCurrentIndex(composer.speaker.findData(QUESTION))

    composer.suggest_button.click()
    assert window._busy and window._busy_dialog is not None
    assert "what your character might do next" in window._busy_dialog.labelText()
    menu = menu_of(window)
    wait_for(app, lambda: not window._busy)

    assert menu.options() == OPTIONS
    assert menu.actions()[0].defaultWidget().text() == CAPTION
    asked = len(window._provider.requests)
    menu.choose(1)
    app.processEvents()

    assert composer.input.toPlainText() == OPTIONS[1]
    assert composer.speaker.currentData() == "char-serrik"
    assert composer.send_button.text() == "Send"
    # Nothing was sent, and nothing joined the story: that is the author's to do.
    assert len(window._provider.requests) == asked and len(session.nodes) == nodes
    assert not window._busy and session.bundle.asides == []
    assert "change it as you like" in window.statusBar().currentMessage()
    window.close()


def test_leaving_the_menu_takes_nothing(app, tmp_path: Path, story, cast):
    window = played(tmp_path, story, cast, ["The door gives.", "{}", REPLY])
    window.composer.input.setPlainText("My own idea")

    window.composer.suggest_button.click()
    menu = menu_of(window)
    wait_for(app, lambda: not window._busy)
    menu.close()
    app.processEvents()

    assert window.composer.input.toPlainText() == "My own idea"
    window.close()


def test_text_already_in_the_box_is_asked_about_first(
    app, tmp_path: Path, story, cast, monkeypatch
):
    window = played(tmp_path, story, cast, ["The door gives.", "{}", REPLY])
    composer = window.composer
    composer.input.setPlainText("I was going to say")
    asked: list[tuple] = []
    answers = [QMessageBox.No, QMessageBox.Yes]

    def question(_parent, title, _text, buttons, default):
        asked.append((title, buttons, default))
        return answers[len(asked) - 1]

    monkeypatch.setattr(QMessageBox, "question", question)

    window._take_suggestion(OPTIONS[0])
    assert composer.input.toPlainText() == "I was going to say"
    window._take_suggestion(OPTIONS[0])

    assert composer.input.toPlainText() == OPTIONS[0]
    assert (
        asked == [("Replace what you wrote?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)] * 2
    )
    window.close()


def test_a_reply_that_cant_be_read_says_so_and_shows_no_menu(
    app, tmp_path: Path, story, cast, monkeypatch
):
    window = played(tmp_path, story, cast, ["The door gives.", "{}", "I'd rather write on."])
    warned: list[tuple] = []
    monkeypatch.setattr(window, "_warn_plain", lambda *words: warned.append(tuple(words)))

    window.composer.suggest_button.click()
    wait_for(app, lambda: not window._busy and bool(warned))
    app.processEvents()

    assert warned[0][0] == "Suggesting a turn failed" and "couldn't be read" in warned[0][1]
    assert getattr(window, "_suggest_menu", None) is None
    assert window.composer.input.toPlainText() == ""
    window.close()


def test_stop_ends_the_asking(app, tmp_path: Path, story, cast):
    window = played(tmp_path, story, cast, ["The door gives.", "{}", REPLY * 40])
    window._provider.delay = 0.02
    window._provider.chunk_size = 4

    window.composer.suggest_button.click()
    wait_for(app, lambda: window._busy_dialog is not None)
    window._busy_dialog.canceled.emit()
    wait_for(app, lambda: not window._busy)
    app.processEvents()

    assert getattr(window, "_suggest_menu", None) is None
    assert window.composer.suggest_button.isEnabled()
    window.close()


def test_its_cost_is_counted(app, tmp_path: Path, story, cast):
    window = played(tmp_path, story, cast, ["The door gives.", "{}", REPLY])
    session = window.session

    window.composer.suggest_button.click()
    menu_of(window)
    wait_for(app, lambda: not window._busy)

    assert session.unreported_usage == [], "taken into the session's cost"
    from sealedlore.storage.repository import read_api_log

    kinds = [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]
    assert kinds[-2:] == ["suggest_request", "suggest_response"]
    window.close()


def test_a_chat_has_it_for_the_user_s_next_message(app, tmp_path: Path):
    ideas = ["Can you show the step?", "What if x were negative?"]
    chat = Story(title="Tutor", mode="chat", chat_prompt="You are a patient maths tutor.")
    save_story_bundle(StoryBundle(story=chat), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(chat.id)
    window._provider = MockChatProvider(["x is 2.", json.dumps(ideas)])
    composer = window.composer
    button = composer.suggest_button
    # A chat has no More to open: its row shows what a chat has of it.
    assert not button.isHidden() and not composer.controls_host.isHidden()
    assert button.isEnabled() and button.toolTip() == composer_module.SUGGEST_TIPS[True]
    composer.input.setPlainText("Solve 2x = 4.")
    window.send_turn()
    wait_for(app, lambda: not window._busy)

    button.click()
    assert "what you might write next" in window._busy_dialog.labelText()
    menu = menu_of(window)
    wait_for(app, lambda: not window._busy)

    assert menu.options() == ideas
    assert menu.actions()[0].defaultWidget().text() == CHAT_CAPTION
    menu.choose(0)
    assert composer.input.toPlainText() == ideas[0]
    assert len(window.session.nodes) == 2
    window.close()


def test_a_tee_chat_keeps_the_row_for_it(app, tmp_path: Path):
    """Private isn't offered in a chat that is private throughout; the row
    used to go with it."""
    chat = Story(title="Tutor", mode="chat")
    save_story_bundle(StoryBundle(story=chat), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(chat.id)

    window.composer.set_private_offered(False)

    assert window.composer.private_toggle.isHidden()
    assert not window.composer.controls_host.isHidden()
    assert not window.composer.suggest_button.isHidden()
    window.close()


# --- the menu ---------------------------------------------------------------------


def test_the_menu_shows_each_option_whole_and_chooses_once(app):
    long = "I ask her, quietly, " + "and then again, " * 30 + "whether she means it."
    menu = SuggestMenu([long, "I wait."])
    chosen: list[str] = []
    menu.chosen.connect(chosen.append)
    rows = menu.list
    assert rows.count() == 2 and menu.options() == [long, "I wait."]
    # The long one's row is several lines tall, and the list shows every row.
    words = rows.itemWidget(rows.item(0))
    assert words.wordWrap() and words.text() == long
    assert words.textFormat() == Qt.PlainText, "a model's words, never markup"
    assert rows.item(0).sizeHint().height() > 2 * rows.item(1).sizeHint().height()
    total = sum(rows.item(row).sizeHint().height() for row in range(rows.count()))
    assert rows.height() >= total

    rows.itemClicked.emit(rows.item(0))
    rows.itemActivated.emit(rows.item(0))  # a double click, or Enter after it

    assert chosen == [long]
    menu.deleteLater()
