"""The review of 3.5.0b1, phone access (findings P1 to P6): each test fails
on the code as it was reviewed (master 0f289da).

Spoken to as a phone would, over the real unix socket with no tor; the
clients are QLocalSockets in the test's own event loop, as in
test_phone_server.py and test_phone_window.py."""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="phone access is Linux only for now"
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.gui import phone_server, window_phone  # noqa: E402
from sealedlore.models.config import PhoneDevice, ProviderConfig  # noqa: E402
from sealedlore.models.node import NARRATOR_SPEAKER_ID  # noqa: E402
from sealedlore.phone.api import Api  # noqa: E402
from sealedlore.phone.page import page_files  # noqa: E402
from sealedlore.phone.pairing import Pairing, token_hash  # noqa: E402
from sealedlore.providers.base import ProviderError  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from tests import test_phone_window as desk  # noqa: E402
from tests.test_phone_api import ONION, TOKEN, FakeBackend  # noqa: E402
from tests.test_phone_server import CONNECTED, Client, wait_for  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def server(app):
    # Short: a unix socket's path is at most 107 bytes.
    folder = Path(tempfile.mkdtemp(prefix="slr"))
    api = Api(FakeBackend(), pairing=Pairing(), onion=lambda: ONION)
    server = phone_server.PhoneServer(api)
    server.listen(folder / "s.sock")
    yield server
    server.close()
    app.processEvents()
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def sockets():
    # Short: a unix socket's path is at most 107 bytes.
    folder = Path(tempfile.mkdtemp(prefix="slw"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def desktop(app, tmp_path, sockets, story, cast):
    """The window with a story open and phone access on, and a phone
    (test_phone_window's `open_window`). Closed whatever the test did: a
    window left open by a failed assertion aborts the run as it is deleted."""
    opened = []

    def open_window(replies: list[str], **options):
        window, phone = desk.open_window(tmp_path, sockets, story, cast, replies, **options)
        opened.append(window)
        return window, phone

    yield open_window
    for window in opened:
        wait_for(lambda w=window: not w._busy)
        if window.session is not None and window.session.in_private:
            window.session.discard_private()  # or closing asks about it
        window.close()


@pytest.fixture
def short_deadlines(monkeypatch):
    monkeypatch.setattr(phone_server, "IDLE_MS", 400)
    monkeypatch.setattr(phone_server, "REQUEST_MS", 400)


# --- P1: answers nobody takes ----------------------------------------------------------

PAGE = b"x" * (512 * 1024)  # more than a unix socket holds for a peer that isn't reading
PICTURE = b"\xff\xd8" + b"p" * (600 * 1024)


def unsent(server: phone_server.PhoneServer) -> int:
    return max((c._socket.bytesToWrite() for c in server._connections), default=0)


def never_reading(server: phone_server.PhoneServer) -> Client:
    client = Client(server)
    client.socket.setReadBufferSize(4 * 1024)  # nothing is read: the answers wait on the server
    return client


def page_request() -> bytes:
    return f"GET /big.js HTTP/1.1\r\nHost: {ONION}\r\n\r\n".encode()


def picture_request() -> bytes:
    return (
        f"GET /api/pictures/img1 HTTP/1.1\r\nHost: {ONION}\r\nAuthorization: Bearer {TOKEN}\r\n\r\n"
    ).encode()


def test_a_stranger_who_never_reads_cant_make_the_server_hold_its_answers(server):
    """Pipelined requests for a page, with no token and the answers never
    read: every one was answered into the socket's buffer at once, a
    gigabyte in about a second."""
    server.api._static["/big.js"] = (PAGE, "application/javascript")
    client = never_reading(server)
    client.send(page_request() * 400)  # 200 MB of answers asked for in 20 KB
    most = 0
    until = time.monotonic() + 1.0
    while time.monotonic() < until:
        wait_for(lambda: False, timeout=0.02)
        most = max(most, unsent(server))
    assert most > 0, "nothing was answered"
    assert most <= phone_server.MAX_UNSENT + len(PAGE) + 1024, f"{most / 1e6:.0f} MB held unsent"


def test_asking_for_more_doesnt_keep_a_stranger_who_takes_no_answer(server, short_deadlines):
    """Each batch of whole requests restarted the deadline, so a few requests
    every few seconds held the connection, and what it was owed, for good."""
    server.api._static["/big.js"] = (PAGE, "application/javascript")
    client = never_reading(server)
    assert wait_for(lambda: server.connection_count() == 1)
    until = time.monotonic() + 2.0  # five deadlines
    while time.monotonic() < until and server.connection_count():
        client.send(page_request() * 3)
        wait_for(lambda: False, timeout=0.1)
    assert server.connection_count() == 0, "held open by asking again"


def test_a_paired_phone_that_takes_nothing_is_let_go_in_the_end(
    server, short_deadlines, monkeypatch
):
    """With anything unsent a paired phone's connection had no deadline at all."""
    monkeypatch.setattr(phone_server, "STALLED_DEADLINES", 3, raising=False)
    server.api.backend.picture = lambda image_id, thumb: (PICTURE, "image/jpeg")
    client = never_reading(server)
    client.send(picture_request())
    assert wait_for(lambda: unsent(server) > 0)
    assert client.socket.state() == CONNECTED
    assert wait_for(lambda: server.connection_count() == 0, timeout=4), "kept for good"


def test_a_paired_phone_gets_every_answer_it_asked_for_as_it_reads(server, short_deadlines):
    """What isn't answered while too much is unsent is answered once it has
    gone: nothing asked for is lost, and no more is held than the bound."""
    server.api.backend.picture = lambda image_id, thumb: (PICTURE, "image/jpeg")
    client = Client(server)
    client.socket.setReadBufferSize(64 * 1024)
    client.send(picture_request() * 6)
    most = 0
    whole = 6 * len(PICTURE)  # and six heads
    until = time.monotonic() + 15
    while client.received.count(b"200 OK") < 6 or len(client.received) <= whole:
        assert time.monotonic() < until, "not every answer came"
        client.received.extend(bytes(client.socket.read(48 * 1024).data()))
        wait_for(lambda: False, timeout=0.002)
        most = max(most, unsent(server))
    assert wait_for(lambda: client.read().endswith(PICTURE[-64:]))
    assert client.read().count(b"\xff\xd8") == 6
    assert most <= phone_server.MAX_UNSENT + len(PICTURE) + 1024, f"{most / 1e6:.1f} MB unsent"
    assert client.socket.state() == CONNECTED


# --- P2: an editor open on the desktop ---------------------------------------------------


def narration(text: str) -> dict:
    return {"text": text, "speaker": NARRATOR_SPEAKER_ID}


def played_one_turn(app, desktop, replies: list[str]):
    window, phone = desktop(replies)
    window.show()
    phone.ask("POST", "/api/turn", narration("Turn one."))
    desk.wait_for(app, lambda: not window._busy and len(window._phone.entries) == 2)
    return window, phone


def test_the_phones_view_follows_the_story_with_an_editor_open_on_the_desktop(app, desktop):
    """The rebuild a finished job asks for waits while a message's editor is
    open, and the phones' view was rebuilt only by it: with an editor left
    open (nothing even typed) the phone was never told the story had moved,
    and kept showing it as it was."""
    window, phone = played_one_turn(app, desktop, ["Passage one.", "Passage two."])
    widget = window.transcript.message_widget(window.session.path()[-1].id)
    widget.begin_edit()
    app.processEvents()
    stream = phone.stream()
    stream.wait_for("hello")
    start = len(stream.events())
    assert phone.ask("POST", "/api/turn", narration("Turn two."))[0] == 202
    desk.wait_for(app, lambda: not window._busy)
    desk.wait_for(app, lambda: "job_finished" in stream.names()[start:])
    for _ in range(50):
        app.processEvents()
    events = stream.events()[start:]
    names = [name for name, _ in events]
    assert "changed" in names[names.index("job_finished") :], "the phone wasn't told"
    assert [data for name, data in events if name == "state"][-1]["busy"] is False
    page = phone.ask("GET", "/api/transcript")[1]
    assert [m["text"] for m in page["messages"]] == [
        "Turn one.",
        "Passage one.",
        "Turn two.",
        "Passage two.",
    ]
    assert page["messages"][-1]["can_continue"] and not page["messages"][1]["can_continue"]
    assert window.transcript.editing(), "the editor on the desktop stays open"
    widget.cancel_edit()


def test_continue_from_a_phone_is_only_for_the_passage_that_is_latest_now(app, desktop):
    """Continue checked the passage named against the phones' view and then
    carried on whatever was latest: with the view behind, a passage the
    phone had never seen."""
    window, phone = played_one_turn(app, desktop, ["Passage one.", "Passage two.", " More."])
    one = window.session.path()[-1]
    behind = [dict(entry) for entry in window._phone.entries]
    phone.ask("POST", "/api/turn", narration("Turn two."))
    desk.wait_for(app, lambda: not window._busy and len(window._phone.entries) == 4)
    window._phone.entries = behind  # the view as a phone behind the story was given it
    status, answer = phone.ask("POST", "/api/continue", {"node_id": one.id})
    assert status == 409, answer
    assert not window._busy
    assert [n.content for n in window.session.path()][-1] == "Passage two."


# --- P3: what is private is said to be, as it comes ----------------------------------------


def begin_scene(window, monkeypatch, replies: list[str]) -> str:
    """A private scene begun with the composer's own button; its tag."""
    private = MockChatProvider(replies)
    settings = ProviderConfig(
        name="Local", base_url="http://127.0.0.1:11434/v1", api_key="", model="local/model"
    )
    monkeypatch.setattr(window, "_private_settings", lambda: settings)
    window.private_provider_factory = lambda _settings: private
    window.config.private_intro_seen = True
    window.begin_private()
    assert window.session.in_private
    return "memory" if window.session.open_span.keep == "memory" else "scene"


def test_a_private_scene_begun_on_the_desktop_is_told_to_the_phones_at_once(
    app, desktop, monkeypatch
):
    """Nothing told the phones a scene had begun: its first turn came to a
    page that still took the story for public, and was shown without
    "Before this is shown"."""
    window, phone = desktop(["A public passage."])
    window.show()
    stream = phone.stream()
    stream.wait_for("hello")
    start = len(stream.events())
    tag = begin_scene(window, monkeypatch, [f"A private reply about {desk.MARKER}."])
    for _ in range(50):
        app.processEvents()
    told = [data["private"] for name, data in stream.events()[start:] if name == "state"]
    assert told and told[-1] == tag, "the phone wasn't told the scene began"

    start = len(stream.events())
    window.composer.input.setPlainText(f"I whisper {desk.MARKER} to her.")
    window.send_turn()
    desk.wait_for(app, lambda: "turn" in stream.names()[start:])
    turn = next(data for name, data in stream.events()[start:] if name == "turn")
    assert turn["private"] == tag, "the scene's turn came untagged"
    desk.wait_for(app, lambda: not window._busy)

    start = len(stream.events())
    window.session.discard_private()
    window._sync_private_ui()  # as every way out of a scene ends
    for _ in range(50):
        app.processEvents()
    told = [data["private"] for name, data in stream.events()[start:] if name == "state"]
    assert told and told[-1] is None, "the phone wasn't told the scene ended"


def test_a_public_turn_says_it_is_public(app, desktop):
    window, phone = desktop(["The door gives."])
    stream = phone.stream()
    stream.wait_for("hello")
    phone.ask("POST", "/api/turn", narration("I push the door."))
    stream.wait_for("job_finished")
    turn = next(data for name, data in stream.events() if name == "turn")
    assert turn == {
        "label": "Narrator",
        "text": "I push the door.",
        "kind": "narration",
        "by": "phone",
        "private": None,
    }


def two_phones(window, phone):
    """A second phone paired, and a stream for each: the first phone's, the other's."""
    window.config.phone.devices.append(
        PhoneDevice(id="p2", name="Other", token_sha256=token_hash("the-other-token"))
    )
    mine, theirs = phone.stream(), phone.stream(token="the-other-token")
    mine.wait_for("hello")
    theirs.wait_for("hello")
    return mine, theirs


def test_a_queued_turn_goes_back_to_the_phone_that_queued_it_and_no_other(app, desktop):
    """`queued_returned` carries the turn's text, and went to every phone
    with a stream open: a turn written on one phone was put in the box on
    another."""
    slow_read = "{" + " " * 600 + "}"  # the background read the turn is queued behind
    window, phone = desktop(["The door gives.", slow_read], reads=True, delay=0.02)
    mine, theirs = two_phones(window, phone)
    window.composer.input.setPlainText("I push the door.")
    window.send_turn()
    desk.wait_for(app, lambda: window._background)
    status, answer = phone.ask("POST", "/api/turn", narration("I step inside."))
    assert (status, answer) == (202, {"queued": True})
    window.stop_generation()
    desk.wait_for(app, lambda: not window._busy)
    mine.wait_for("queued_returned")
    returned = next(data for name, data in mine.events() if name == "queued_returned")
    assert returned == {"text": "I step inside.", "private": None}
    for _ in range(50):
        app.processEvents()
    assert "queued_returned" not in theirs.names()
    assert b"I step inside." not in theirs.raw()


def test_a_turn_that_comes_back_in_a_private_scene_says_so(app, desktop, monkeypatch):
    """Untagged, the page couldn't tell a private scene's turn coming back
    from any other."""
    window, phone = desktop([])
    mine, theirs = two_phones(window, phone)
    tag = begin_scene(window, monkeypatch, [])
    whisper = f"I whisper {desk.MARKER}."
    # As the window hands back a phone's turn that never went (`_drop_queued`,
    # `_return_unanswered`): to the origin it was sent with.
    window.queued_turn_returned.emit(window_phone.phone_origin("p1"), whisper)
    mine.wait_for("queued_returned")
    returned = next(data for name, data in mine.events() if name == "queued_returned")
    assert returned == {"text": whisper, "private": tag}
    for _ in range(50):
        app.processEvents()
    assert desk.MARKER.encode() not in theirs.raw()


def page_function(script: str, name: str) -> str:
    start = script.index(f"function {name}(")
    return script[start : script.index("\n  }\n", start)]


def test_the_page_gates_a_turn_and_a_returned_turn_by_their_own_tags():
    """The page tagged a turn as it came with the state it last had, and put
    a returned turn's text in the box whatever it was. It reads each event's
    own tag, and holds a private turn back until its kind is acknowledged."""
    script = page_files("test")["/app.js"][0].decode("utf-8")
    events = page_function(script, "onEvent")
    turn = events[events.index('case "turn"') : events.index('case "job_started"')]
    assert re.search(r"private:\s*data\.private", turn), "the turn's own tag isn't read"
    assert "offerPrivate()" in turn
    returned = events[events.index('case "queued_returned"') : events.index('case "revoked"')]
    assert "data.private" in returned and ".value = " not in returned
    shown = page_function(script, "showReturned")
    assert shown.index("hidden(") < shown.index(".value = ")
    kinds = script[script.index("function privateKinds") : script.index("function offerPrivate")]
    assert "S.returned" in kinds
    # Acknowledged, what was held back is put where it belongs.
    assert "showReturned()" in page_function(script, "askToShow")


# --- P4: a phone's job that fails ------------------------------------------------------------


@pytest.fixture
def boxes(monkeypatch) -> list[tuple[str, str]]:
    """Every message box the desktop opens: its title and text, answered at once."""
    opened: list[tuple[str, str]] = []

    def exec_(box) -> int:
        opened.append((box.windowTitle(), box.text()))
        return 0

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    return opened


OVERLOADED = "HTTP 503: the endpoint is overloaded"


def notes_shown(window) -> list[str]:
    return [note for node in window.session.full_path() for note in node.meta.notes]


def test_a_phones_turn_that_fails_opens_no_dialog_on_the_desktop(app, desktop, boxes):
    """It opened "Generation failed" there, modal: with nobody at the desktop
    every phone was refused, as waiting for an answer, until someone was."""
    window, phone = desktop([])
    window.show()
    window._provider = MockChatProvider(error=ProviderError(OVERLOADED))
    stream = phone.stream()
    stream.wait_for("hello")
    assert phone.ask("POST", "/api/turn", narration("I open the door."))[0] == 202
    stream.wait_for("job_finished")
    desk.wait_for(app, lambda: not window._busy and window._failed is None)
    assert boxes == [], "a dialog stood on the desktop"
    assert "failed" in stream.names()
    # Said where the author will find it: under the turn, and in the status bar.
    assert [n.content for n in window.session.full_path()] == ["I open the door."]
    (note,) = notes_shown(window)
    assert "failed" in note and "phone" in note and OVERLOADED in note
    assert OVERLOADED in window.statusBar().currentMessage()
    widget = window.transcript.message_widget(window.session.full_path()[-1].id)
    assert any(OVERLOADED in label.text() for label in widget.findChildren(type(widget.body)))
    # And the phones go on.
    assert phone.ask("GET", "/api/state")[1]["desktop_waiting"] is False
    window._provider = MockChatProvider(["The door gives."])
    assert phone.ask("POST", "/api/regenerate", {})[0] == 409  # no passage to retake
    assert phone.ask("POST", "/api/turn", narration("Again."))[0] == 202
    desk.wait_for(app, lambda: not window._busy)


def test_a_phones_retake_that_fails_opens_no_dialog_either(app, desktop, boxes):
    window, phone = played_one_turn(app, desktop, ["Passage one."])
    window._provider = MockChatProvider(error=ProviderError(OVERLOADED))
    assert phone.ask("POST", "/api/regenerate", {})[0] == 202
    desk.wait_for(app, lambda: not window._busy and window._failed is None)
    assert boxes == []
    assert any(OVERLOADED in note for note in notes_shown(window))


def test_the_desktops_own_turn_that_fails_still_says_so_in_its_dialog(app, desktop, boxes):
    window, phone = desktop([])
    window._provider = MockChatProvider(error=ProviderError(OVERLOADED))
    window.composer.input.setPlainText("I open the door.")
    window.send_turn()
    desk.wait_for(app, lambda: not window._busy and window._failed is None)
    assert [title for title, _ in boxes] == ["Generation failed"]
    assert OVERLOADED in boxes[0][1]
    assert notes_shown(window) == []
