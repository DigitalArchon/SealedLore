"""Change this passage… in the window: the ⋯ entry, its dialog, and the take
the change makes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from sealedlore.gui import note_dialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.note_dialog import NoteDialog  # noqa: E402
from sealedlore.gui.transcript import CHANGE_LABELS  # noqa: E402
from sealedlore.models.node import Node  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.test_background_queue import _send, _window, wait_for  # noqa: E402

FIRST = "She opens the gate. The yard beyond is empty."
CHANGED = "She opens the gate. The yard beyond is full of soldiers."
CHANGE = "The yard should be full of soldiers."


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def answer(monkeypatch, text: str | None, seen: list | None = None) -> None:
    """The dialog answered with `text` (None: cancelled)."""

    def run(dialog: NoteDialog) -> int:
        if seen is not None:
            seen.append(dialog)
        if text is None:
            return QDialog.Rejected
        dialog.text.setPlainText(text)
        return QDialog.Accepted

    monkeypatch.setattr(NoteDialog, "exec", run)


def test_the_menu_asks_what_should_change_and_makes_a_take_of_it(
    app, tmp_path: Path, story, cast, monkeypatch
):
    window = _window(tmp_path, story, cast, [FIRST, "{}", CHANGED, "{}"])
    session = window.session
    _send(window, "I ask her to open the gate.")
    wait_for(app, lambda: not window._busy)
    turn, original = session.path()
    action = window.transcript.message_widget(original.id).actions_["revise"]
    assert action.text() == CHANGE_LABELS[False] == "Change this passage…"
    # Only on a passage.
    assert "revise" not in window.transcript.message_widget(turn.id).actions_
    seen: list = []
    answer(monkeypatch, CHANGE, seen)

    action.trigger()
    wait_for(app, lambda: not window._busy)

    dialog = seen[0]
    assert dialog.windowTitle() == "Change this passage"
    assert dialog.go.text() == "Change" and dialog.hint.text() == note_dialog.CHANGE_HINTS[False]
    new = session.path()[-1]
    assert new.content == CHANGED and new.meta.revised_from == original.id
    assert session.take_position(new.id) == (2, 2)
    shown = window.transcript.message_widget(new.id)
    assert shown.body.source == CHANGED
    assert shown.take_note.text() == f"The change you asked for — {CHANGE}"
    # The reads ran over the new take, as after Regenerate.
    assert new.meta.scene_read
    window.close()


def test_cancelling_sends_nothing(app, tmp_path: Path, story, cast, monkeypatch):
    window = _window(tmp_path, story, cast, [FIRST, "{}"])
    _send(window, "I ask her to open the gate.")
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]
    sent = len(window._provider.requests)
    answer(monkeypatch, None)

    window.transcript.message_widget(passage.id).actions_["revise"].trigger()

    assert not window._busy and len(window._provider.requests) == sent
    assert window.session.path()[-1] is passage
    window.close()


def test_an_older_passage_is_changed_in_its_place(app, tmp_path: Path, story, cast, monkeypatch):
    window = _window(tmp_path, story, cast, ["One.", "{}", "Two.", "{}", "One, in the rain.", "{}"])
    session = window.session
    _send(window, "First.")
    wait_for(app, lambda: not window._busy)
    _send(window, "Second.")
    wait_for(app, lambda: not window._busy)
    older, later = session.path()[1], session.path()[2:]
    sent = len(window._provider.requests)
    answer(monkeypatch, "It is raining.")

    window.transcript.message_widget(older.id).actions_["revise"].trigger()
    wait_for(app, lambda: not window._busy)

    path = session.path()
    assert path[1].content == "One, in the rain." and path[1].meta.revised_from == older.id
    assert path[2:] == later
    # No reads: they look at where the story ends.
    assert len(window._provider.requests) == sent + 1
    # The whole story is shown again, the new take in its place.
    assert window.transcript.message_widget(later[-1].id) is not None
    assert window.transcript.message_widget(path[1].id).body.source == "One, in the rain."
    window.close()


def test_a_stopped_change_keeps_nothing_and_says_so(app, tmp_path: Path, story, cast, monkeypatch):
    window = _window(tmp_path, story, cast, [FIRST, "{}"])
    session = window.session
    _send(window, "I ask her to open the gate.")
    wait_for(app, lambda: not window._busy)
    original = session.path()[-1]
    provider = MockChatProvider([CHANGED * 4], chunk_size=4)
    provider.delay = 0.02
    window._provider = provider
    answer(monkeypatch, CHANGE)

    window.transcript.message_widget(original.id).actions_["revise"].trigger()
    wait_for(
        app,
        lambda: (
            window.transcript._streaming is not None
            and bool(window.transcript._streaming.body.source)
        ),
    )
    window.stop_generation()
    wait_for(app, lambda: not window._busy)
    app.processEvents()

    assert session.path()[-1] is original and len(session.nodes) == 2
    assert window.transcript.message_widget(original.id).body.source == FIRST
    assert "the passage is as it was" in window.statusBar().currentMessage()
    window.close()


def test_what_a_passage_offers(app, tmp_path: Path, story, cast):
    """Not an opening written as given, a private scene's summary, or a
    passage with no words; and a private scene withdraws it from passages
    written before the scene."""
    opening = Node(kind="assistant", speaker_id="__narrator__", content="The keep stood empty.")
    turn = Node(kind="user", speaker_id="char-serrik", content="I wait.", parent_id=opening.id)
    opening.children = [turn.id]
    empty = Node(kind="assistant", speaker_id="__narrator__", content="", parent_id=turn.id)
    turn.children = [empty.id]
    story.active_leaf_id = empty.id
    save_story_bundle(StoryBundle(story=story, cast=cast, nodes=[opening, turn, empty]), tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)

    assert "revise" not in window.transcript.message_widget(opening.id).actions_
    assert "revise" not in window.transcript.message_widget(empty.id).actions_
    assert "regenerate" in window.transcript.message_widget(empty.id).actions_
    window.close()


def test_a_private_scene_withdraws_it_from_passages_before_the_scene(
    app, tmp_path: Path, story, cast
):
    window = _window(tmp_path, story, cast, [FIRST, "{}"])
    _send(window, "I ask her to open the gate.")
    wait_for(app, lambda: not window._busy)
    action = window.transcript.message_widget(window.session.path()[-1].id).actions_["revise"]
    assert action.isVisible()

    window.transcript.offer_for_scene("span-1")

    assert not action.isVisible()
    window.close()


def test_a_chat_s_reply_has_it_in_a_chat_s_words(app, tmp_path: Path, monkeypatch):
    chat = Story(title="Tutor", mode="chat", chat_prompt="You are a patient maths tutor.")
    save_story_bundle(StoryBundle(story=chat), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(chat.id)
    window._provider = MockChatProvider(["x is 2.", "x is two."])
    window.composer.input.setPlainText("Solve 2x = 4.")
    window.send_turn()
    wait_for(app, lambda: not window._busy)
    reply = window.session.path()[-1]
    action = window.transcript.message_widget(reply.id).actions_["revise"]
    assert action.text() == "Change this reply…"
    seen: list = []
    answer(monkeypatch, "Write the number as a word.", seen)

    action.trigger()
    wait_for(app, lambda: not window._busy)

    assert seen[0].windowTitle() == "Change this reply"
    assert seen[0].hint.text() == note_dialog.CHANGE_HINTS[True]
    assert window.session.path()[-1].content == "x is two."
    assert window.session.take_position(window.session.path()[-1].id) == (2, 2)
    window.close()
