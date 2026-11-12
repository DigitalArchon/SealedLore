"""Regenerate with a note… in the window: the ⋯ entry, its dialog, and the
take it makes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from sealedlore.gui import note_dialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.note_dialog import RETAKE_ASKS, RETAKE_BUTTON, NoteDialog  # noqa: E402
from sealedlore.gui.transcript import REGENERATE_NOTE_LABEL  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.test_background_queue import _send, _window, wait_for  # noqa: E402

NOTE = "She should refuse this time."


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def answer(monkeypatch, text: str | None, seen: list | None = None) -> None:
    """The note dialog answered with `text` (None: cancelled)."""

    def run(dialog: NoteDialog) -> int:
        if seen is not None:
            seen.append(dialog)
        if text is None:
            return QDialog.Rejected
        dialog.text.setPlainText(text)
        return QDialog.Accepted

    monkeypatch.setattr(NoteDialog, "exec", run)


def test_the_menu_asks_for_a_note_and_makes_a_take_with_it(
    app, tmp_path: Path, story, cast, monkeypatch
):
    window = _window(tmp_path, story, cast, ["She nods.", "{}", "She shakes her head.", "{}"])
    session = window.session
    _send(window, "I ask her to open the gate.")
    wait_for(app, lambda: not window._busy)
    first = session.path()[-1]
    widget = window.transcript.message_widget(first.id)
    action = widget.actions_["regenerate_note"]
    assert action.text() == REGENERATE_NOTE_LABEL == "Regenerate with a note…"
    # Only on a passage, beside Regenerate.
    assert "regenerate_note" not in window.transcript.message_widget(session.path()[0].id).actions_
    seen: list = []
    answer(monkeypatch, NOTE, seen)

    action.trigger()
    wait_for(app, lambda: not window._busy)

    assert seen[0].windowTitle() == "Regenerate with a note"
    new = session.path()[-1]
    assert new.id != first.id and new.content == "She shakes her head."
    assert new.meta.retake_note == NOTE
    assert session.take_position(new.id) == (2, 2)
    # The take says what was asked of it; the one before says nothing.
    shown = window.transcript.message_widget(new.id)
    assert shown.take_note.text() == f"Your note for this take — {NOTE}"
    window._switch_variant(new.id, -1)
    assert window.transcript.message_widget(first.id).take_note is None
    window.close()


def test_cancelling_the_note_sends_nothing(app, tmp_path: Path, story, cast, monkeypatch):
    window = _window(tmp_path, story, cast, ["She nods.", "{}"])
    _send(window, "I ask her to open the gate.")
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]
    sent = len(window._provider.requests)
    answer(monkeypatch, None)

    window.transcript.message_widget(passage.id).actions_["regenerate_note"].trigger()

    assert not window._busy and len(window._provider.requests) == sent
    assert window.session.path()[-1] is passage
    window.close()


def test_an_older_passage_takes_its_note_in_place(app, tmp_path: Path, story, cast, monkeypatch):
    window = _window(tmp_path, story, cast, ["One.", "{}", "Two.", "{}", "One, but colder.", "{}"])
    session = window.session
    _send(window, "First.")
    wait_for(app, lambda: not window._busy)
    _send(window, "Second.")
    wait_for(app, lambda: not window._busy)
    older, later = session.path()[1], session.path()[2:]
    answer(monkeypatch, "Colder.")

    window.transcript.message_widget(older.id).actions_["regenerate_note"].trigger()
    wait_for(app, lambda: not window._busy)

    path = session.path()
    assert path[1].content == "One, but colder." and path[1].meta.retake_note == "Colder."
    assert path[2:] == later
    window.close()


def test_a_private_scene_withdraws_it_from_passages_before_the_scene(
    app, tmp_path: Path, story, cast
):
    window = _window(tmp_path, story, cast, ["She nods.", "{}"])
    _send(window, "I ask her to open the gate.")
    wait_for(app, lambda: not window._busy)
    action = window.transcript.message_widget(window.session.path()[-1].id).actions_[
        "regenerate_note"
    ]
    assert action.isVisible()

    window.transcript.offer_for_scene("span-1")

    assert not action.isVisible()
    window.close()


# --- the dialog -------------------------------------------------------------------


def dialog_for_a_take() -> NoteDialog:
    return NoteDialog(title="Regenerate with a note", asks=RETAKE_ASKS, button=RETAKE_BUTTON)


def test_the_dialog_waits_for_words_and_gives_them_trimmed(app):
    dialog = dialog_for_a_take()
    assert dialog.go.text() == "Regenerate" and not dialog.go.isEnabled()
    dialog.accept()
    assert dialog.result() != QDialog.Accepted

    dialog.text.setPlainText(f"  {NOTE}\n")

    assert dialog.go.isEnabled() and dialog.note() == NOTE
    dialog.accept()
    assert dialog.result() == QDialog.Accepted


def test_enter_in_the_box_never_sends_the_note(app):
    dialog = dialog_for_a_take()
    dialog.text.setPlainText(NOTE)
    dialog.show()
    dialog.text.setFocus()

    dialog.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))

    assert dialog.isVisible() and dialog.result() != QDialog.Accepted
    dialog.text.clear()
    dialog.close()


def test_closing_with_something_typed_asks_first(app, monkeypatch):
    asked: list[str] = []

    def question(_parent, title, *_args, **_kwargs):
        asked.append(title)
        return QMessageBox.Cancel if len(asked) == 1 else QMessageBox.Discard

    monkeypatch.setattr(QMessageBox, "question", question)
    dialog = dialog_for_a_take()
    dialog.show()

    dialog.reject()  # nothing typed: no question
    assert asked == [] and not dialog.isVisible()

    dialog.show()
    dialog.text.setPlainText(NOTE)
    dialog.reject()
    assert asked == ["Discard what you wrote?"] and dialog.isVisible()
    dialog.reject()
    assert len(asked) == 2 and not dialog.isVisible()


def test_a_chat_s_reply_has_it_too_in_a_chat_s_words(app, tmp_path: Path, monkeypatch):
    chat = Story(title="Tutor", mode="chat", chat_prompt="You are a patient maths tutor.")
    save_story_bundle(StoryBundle(story=chat), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(chat.id)
    window._provider = MockChatProvider(["x is 2.", "Divide both sides by two: x is 2."])
    window.composer.input.setPlainText("Solve 2x = 4.")
    window.send_turn()
    wait_for(app, lambda: not window._busy)
    reply = window.session.path()[-1]
    seen: list = []
    answer(monkeypatch, "Show the step.", seen)

    window.transcript.message_widget(reply.id).actions_["regenerate_note"].trigger()
    wait_for(app, lambda: not window._busy)

    assert seen[0].hint.text() == note_dialog.CHAT_RETAKE_HINT
    new = window.session.path()[-1]
    assert new.content == "Divide both sides by two: x is 2."
    assert new.meta.retake_note == "Show the step."
    window.close()
