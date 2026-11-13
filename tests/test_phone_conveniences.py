"""The desktop's conveniences from a phone (gui/window_phone.py,
gui/window_retakes.py): a take steered by a note, and a passage written again
with a change. Spoken to as a phone would, over the real unix socket with no
tor (tests/test_phone_window.py has the client)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="phone access is Linux only for now"
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from sealedlore.gui import window_phone  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.node import NARRATOR_SPEAKER_ID  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from tests.test_phone_window import MARKER, open_window, wait_for  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def sockets():
    # Short: a unix socket's path is at most 107 bytes.
    folder = Path(tempfile.mkdtemp(prefix="slp"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


# --- Regenerate with a note ------------------------------------------------------------


def test_a_take_steered_by_a_note_from_the_phone(app, tmp_path, sockets, story, cast):
    window, phone = open_window(
        tmp_path, sockets, story, cast, ["The door gives.", "The door holds.", "The door groans."]
    )
    phone.ask("POST", "/api/turn", {"text": "I push.", "speaker": NARRATOR_SPEAKER_ID})
    wait_for(app, lambda: not window._busy and window._phone.entries)
    turn, first = window.session.path()[-2:]
    note = "The door should hold this time."

    status, answer = phone.ask(
        "POST", "/api/regenerate", {"node_id": first.id, "note": f"  {note}\n", "length": "brief"}
    )
    assert (status, answer) == (202, {"started": True})
    wait_for(app, lambda: not window._busy)
    take = window.session.path()[-1]
    assert take.content == "The door holds." and take.meta.retake_note == note
    assert window.session.take_position(take.id) == (2, 2)
    assert note in window._provider.requests[-1].messages[-1].text
    # The phone's Length shaped the take, as a plain Regenerate's does.
    assert turn.meta.response_style == "brief"

    # A blank note is a plain Regenerate: the next take is steered by nothing.
    assert phone.ask("POST", "/api/regenerate", {"node_id": take.id, "note": "  "})[0] == 202
    wait_for(app, lambda: not window._busy)
    plain = window.session.path()[-1]
    assert plain.content == "The door groans." and plain.meta.retake_note is None
    assert note not in window._provider.requests[-1].messages[-1].text
    window.close()


def test_a_note_is_refused_as_any_retake_is(app, tmp_path, sockets, story, cast, monkeypatch):
    """With a note or without, the desktop's reasons are the phone's: busy, a
    dialog open there, a passage that is no take of anything, one not in the
    story as it stands."""
    window, phone = open_window(tmp_path, sockets, story, cast, ["The door gives."], delay=0.05)
    phone.ask("POST", "/api/turn", {"text": "I push.", "speaker": NARRATOR_SPEAKER_ID})
    assert window._busy
    body = {"node_id": "n-unknown", "note": "She refuses."}
    assert phone.ask("POST", "/api/regenerate", body) == (409, {"error": window_phone.BUSY})
    wait_for(app, lambda: not window._busy and window._phone.entries)
    passage = window.session.path()[-1]
    sent = len(window._provider.requests)
    assert phone.ask("POST", "/api/regenerate", body) == (
        409,
        {"error": "That passage isn't in the story as it stands."},
    )
    body["node_id"] = passage.id
    dialog = QDialog(window)
    dialog.setWindowModality(Qt.ApplicationModal)
    dialog.show()
    app.processEvents()
    try:
        assert phone.ask("POST", "/api/regenerate", body) == (
            409,
            {"error": window_phone.WAITING_ON_DESKTOP},
        )
    finally:
        dialog.close()
    monkeypatch.setattr(window, "_retake_refusal", lambda _path: "Not a take of anything.")
    assert phone.ask("POST", "/api/regenerate", body) == (
        409,
        {"error": "Not a take of anything."},
    )
    assert not window._busy and len(window._provider.requests) == sent
    assert window.session.path()[-1] is passage
    window.close()


def memory_scene(window: MainWindow, replies: list[str]) -> MockChatProvider:
    """A memory-only private scene with one exchange played in it."""
    private = MockChatProvider(replies)
    window.session.enter_private(keep="memory", provider=private, model="local/model")
    window._submit_turn("I whisper.", window._current_turn("I whisper."), origin="phone")
    wait_for(QApplication.instance(), lambda: not window._busy)
    window.reload_transcript()
    return private


def nothing_on_disk(folder: Path, marker: str = MARKER) -> None:
    for path in folder.rglob("*"):
        if path.is_file():
            assert marker.encode() not in path.read_bytes(), path


def test_a_note_for_a_memory_scenes_passage_is_the_scenes_own(app, tmp_path, sockets, story, cast):
    """A note typed on the phone for a private scene's passage is that scene's
    text: it goes to the scene's model with the take and to no other, is kept
    on the take in memory, and reaches no file and no phone's stream."""
    window, phone = open_window(tmp_path, sockets, story, cast, [])
    private = memory_scene(window, ["A quiet reply.", "A quieter one."])
    passage = window.session.full_path()[-1]
    events = phone.stream()
    events.wait_for("hello")
    note = f"She says {MARKER} under her breath."

    status, _ = phone.ask("POST", "/api/regenerate", {"node_id": passage.id, "note": note})
    assert status == 202
    wait_for(app, lambda: not window._busy)
    events.wait_for("changed")
    take = window.session.full_path()[-1]
    assert take.content == "A quieter one." and take.meta.retake_note == note
    assert take.meta.private_span == passage.meta.private_span is not None
    assert MARKER in private.requests[-1].messages[-1].text
    assert window._provider.requests == []
    assert MARKER.encode() not in events.raw()
    assert [m["private"] for m in phone.ask("GET", "/api/transcript")[1]["messages"]] == [
        "memory",
        "memory",
    ]
    nothing_on_disk(tmp_path)
    window.session.discard_private()
    window.close()


# --- Change this passage… ---------------------------------------------------------------


def play(window: MainWindow, phone, *turns: str) -> None:
    """Turns sent from the phone, each answered before the next."""
    app = QApplication.instance()
    shown = len(window.session.full_path())
    for text in turns:
        shown += 2
        phone.ask("POST", "/api/turn", {"text": text, "speaker": NARRATOR_SPEAKER_ID})
        wait_for(app, lambda n=shown: not window._busy and len(window._phone.entries) == n)


def said(events) -> list[str]:
    return [data["text"] for name, data in events.events() if name == "notice"]


def test_a_passage_changed_from_the_phone_is_written_in_its_place(
    app, tmp_path, sockets, story, cast
):
    window, phone = open_window(
        tmp_path, sockets, story, cast, ["One.", "Two.", "One, in the rain.", "Two, at night."]
    )
    play(window, phone, "First.", "Second.")
    first, latest = window.session.path()[1], window.session.path()[3]
    _, page = phone.ask("GET", "/api/transcript")
    assert [m["can_revise"] for m in page["messages"]] == [False, True, False, True]
    events = phone.stream()
    events.wait_for("hello")
    change = "It is raining."

    status, answer = phone.ask(
        "POST", "/api/revise", {"node_id": first.id, "change": f" {change}\n"}
    )
    assert (status, answer) == (202, {"started": True})
    wait_for(app, lambda: not window._busy)
    events.wait_for("job_finished")
    # The phone is told which passage is written again: it shows the words
    # in that passage's place, not in a new card at the end.
    started = [data for name, data in events.events() if name == "job_started"]
    assert started == [
        {"streaming": True, "question": False, "continues": None, "revises": first.id}
    ]
    texts = "".join(data["text"] for name, data in events.events() if name == "text")
    assert texts == "One, in the rain."

    # A new take beside the original, in its place under what followed.
    new = window.session.path()[1]
    assert new.id != first.id and new.meta.revised_from == first.id
    assert new.meta.revise_note == change
    assert [n.content for n in window.session.path()] == [
        "First.",
        "One, in the rain.",
        "Second.",
        "Two.",
    ]
    assert change in window._provider.requests[-1].messages[-1].text
    wait_for(app, lambda: window._phone.entries[1]["id"] == new.id)
    _, page = phone.ask("GET", "/api/transcript")
    assert page["messages"][1]["text"] == "One, in the rain."
    assert page["messages"][1]["take"] == [2, 2] and page["messages"][1]["can_revise"]

    # A change begun on the desktop shows on the phone the same way.
    window._start_change(latest.id, "It is night.")
    wait_for(app, lambda: not window._busy)
    wait_for(app, lambda: events.names().count("job_finished") == 2)
    started = [data for name, data in events.events() if name == "job_started"]
    assert started[-1]["revises"] == latest.id
    assert window.session.path()[-1].content == "Two, at night."
    window.close()


def test_a_change_that_brings_nothing_leaves_the_passage_and_says_so(
    app, tmp_path, sockets, story, cast
):
    from sealedlore.engine.revision import NOTHING_CAME
    from sealedlore.gui.window_retakes import CHANGE_STOPPED

    window, phone = open_window(tmp_path, sockets, story, cast, ["The door gives."])
    play(window, phone, "I push.")
    passage = window.session.path()[-1]
    body = {"node_id": passage.id, "change": "It is raining."}
    events = phone.stream()
    events.wait_for("hello")

    # A reply that came back empty.
    window._provider = MockChatProvider(["  \n"])
    assert phone.ask("POST", "/api/revise", body)[0] == 202
    wait_for(app, lambda: not window._busy)
    wait_for(app, lambda: NOTHING_CAME in said(events))
    assert window.session.path()[-1] is passage and len(window.session.nodes) == 2

    # One stopped partway, from the phone.
    slow = MockChatProvider(["The door gives, in the rain. " * 8], chunk_size=4)
    slow.delay = 0.02
    window._provider = slow
    words = events.names().count("text")
    assert phone.ask("POST", "/api/revise", body)[0] == 202
    wait_for(app, lambda: events.names().count("text") > words)  # some of it has come
    assert phone.ask("POST", "/api/stop", {})[0] == 200
    wait_for(app, lambda: not window._busy)
    wait_for(app, lambda: CHANGE_STOPPED[False] in said(events))
    assert CHANGE_STOPPED[False].endswith("the passage is as it was.")
    assert window.session.path()[-1] is passage and len(window.session.nodes) == 2
    shown = phone.ask("GET", "/api/transcript")[1]["messages"][-1]
    assert (shown["id"], shown["text"], shown["take"]) == (passage.id, "The door gives.", None)
    window.close()


def test_a_change_is_refused_with_the_desktops_reason(
    app, tmp_path, sockets, story, cast, monkeypatch
):
    """Every reason the desktop has is the phone's, and none of them opens a
    dialog there: busy, a passage not in the story as it stands, a dialog
    open, an edit that would be asked about, no endpoint, a private model
    not attested, a passage that can't be changed, a job that didn't start."""
    from PySide6.QtWidgets import QMessageBox

    window, phone = open_window(tmp_path, sockets, story, cast, ["One.", "Two."], delay=0.05)

    def refused(node_id: str) -> str:
        status, answer = phone.ask(
            "POST", "/api/revise", {"node_id": node_id, "change": "It is raining."}
        )
        assert status == 409, answer
        return answer["error"]

    phone.ask("POST", "/api/turn", {"text": "First.", "speaker": NARRATOR_SPEAKER_ID})
    assert window._busy and refused("n-any") == window_phone.BUSY
    wait_for(app, lambda: not window._busy and len(window._phone.entries) == 2)
    play(window, phone, "Second.")
    turn, first, _, latest = window.session.path()
    sent = len(window._provider.requests)

    assert refused("n-unknown") == window_phone.NOT_IN_STORY
    assert refused(turn.id) == window_phone.NOT_IN_STORY  # an author's turn is no passage

    dialog = QDialog(window)
    dialog.setWindowModality(Qt.ApplicationModal)
    dialog.show()
    app.processEvents()
    try:
        assert refused(first.id) == window_phone.WAITING_ON_DESKTOP
    finally:
        dialog.close()

    # An older passage's take shows the story as it stood before it: an edit
    # left open after it would be asked about on the desktop.
    asked: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda _p, title, *_a, **_k: asked.append(title) or 0
    )
    widget = window.transcript.message_widget(latest.id)
    widget.begin_edit()
    app.processEvents()
    widget._editor.setPlainText("An edit the author left open on the desktop.")
    assert refused(first.id) == window_phone.EDIT_OPEN
    assert asked == [] and widget._editor is not None
    widget.cancel_edit()

    monkeypatch.setattr(window, "use_mock", False)
    assert refused(first.id) == window_phone.NO_ENDPOINT
    monkeypatch.setattr(window, "use_mock", True)
    monkeypatch.setattr(window.session, "tee_block", "not attested")
    assert refused(first.id) == "The private model can't be used now: not attested"
    monkeypatch.setattr(window.session, "tee_block", None)

    monkeypatch.setattr(first, "content", "")
    assert refused(first.id) == "This passage has no words to change: Regenerate writes it again."
    monkeypatch.setattr(first, "content", "One.")
    monkeypatch.setattr(window, "_start_change", lambda _node_id, _change: None)
    assert refused(first.id) == window_phone.NOT_STARTED

    for _ in range(20):
        app.processEvents()
    assert not window._busy and len(window._provider.requests) == sent
    assert [n.content for n in window.session.path()] == ["First.", "One.", "Second.", "Two."]
    window.close()


def test_with_a_turn_waiting_to_go_a_change_is_refused(app, tmp_path, sockets, story, cast):
    window, phone = open_window(
        tmp_path,
        sockets,
        story,
        cast,
        ["The door gives.", "{}", "The hall.", "{}"],
        delay=0.05,
        reads=True,
    )
    body = {"node_id": "n-any", "change": "It is raining."}
    phone.ask("POST", "/api/turn", {"text": "I push.", "speaker": NARRATOR_SPEAKER_ID})
    wait_for(app, lambda: window._background)
    # The reads after a passage have the story: a turn may wait, a change may not.
    assert phone.ask("POST", "/api/revise", body) == (409, {"error": window_phone.BUSY})
    queued = phone.ask("POST", "/api/turn", {"text": "On.", "speaker": NARRATOR_SPEAKER_ID})
    assert queued == (202, {"queued": True})
    assert phone.ask("POST", "/api/revise", body) == (
        409,
        {"error": window_phone.ALREADY_QUEUED},
    )
    wait_for(app, lambda: not window._busy and len(window.session.path()) == 4)
    window.close()


def test_a_change_for_a_memory_scenes_passage_is_the_scenes_own(
    app, tmp_path, sockets, story, cast
):
    """What a private scene refuses is refused the phone, and a change typed
    there for the scene's own passage is the scene's text: to the scene's
    model only, on the take in memory, in no file and no phone's stream."""
    window, phone = open_window(tmp_path, sockets, story, cast, ["The door gives."])
    play(window, phone, "I push.")
    public = window.session.path()[-1]
    private = memory_scene(window, ["A quiet reply.", "A quiet reply, in the rain."])
    passage = window.session.full_path()[-1]
    entries = phone.ask("GET", "/api/transcript")[1]["messages"]
    # Only the scene's own passage, while it is open: as the desktop offers it.
    assert [m["can_revise"] for m in entries] == [False, False, False, True]
    assert all(m["can_revise"] == window.session.can_revise(m["id"]) for m in entries)
    status, answer = phone.ask(
        "POST", "/api/revise", {"node_id": public.id, "change": "It is raining."}
    )
    assert status == 409 and answer["error"].startswith("Leave the private scene")
    events = phone.stream()
    events.wait_for("hello")
    told = len(window._provider.requests)
    change = f"She says {MARKER} as the rain starts."

    assert phone.ask("POST", "/api/revise", {"node_id": passage.id, "change": change})[0] == 202
    wait_for(app, lambda: not window._busy)
    events.wait_for("job_finished")
    take = window.session.full_path()[-1]
    assert take.content == "A quiet reply, in the rain." and take.meta.revise_note == change
    assert take.meta.revised_from == passage.id
    assert take.meta.private_span == passage.meta.private_span is not None
    assert MARKER in private.requests[-1].messages[-1].text
    assert len(window._provider.requests) == told  # the story's model heard nothing
    assert MARKER.encode() not in events.raw()
    nothing_on_disk(tmp_path)
    window.session.discard_private()
    window.close()
