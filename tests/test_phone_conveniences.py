"""The desktop's conveniences from a phone (gui/window_phone.py,
gui/window_retakes.py): a take steered by a note. Spoken to as a phone would,
over the real unix socket with no tor (tests/test_phone_window.py has the
client)."""

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
