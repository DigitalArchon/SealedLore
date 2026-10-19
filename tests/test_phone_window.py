"""Phone access on the window's side (gui/window_phone.py, gui/phone_server.py),
spoken to as a phone would: real HTTP over the real unix socket, with no tor.

The client is a QLocalSocket in the test's own event loop, not a thread: the
server is on the GUI thread, and a Python thread beside the window is the
kind that segfaulted the suite (CLAUDE.md, "Qt rules")."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QByteArray, Qt  # noqa: E402
from PySide6.QtNetwork import QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from sealedlore.gui import window_phone  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.config import PhoneDevice  # noqa: E402
from sealedlore.models.node import NARRATOR_SPEAKER_ID  # noqa: E402
from sealedlore.phone import api as phone_api  # noqa: E402
from sealedlore.phone.pairing import token_hash  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402

ONION = "a" * 56 + ".onion"
TOKEN = "the-phone-s-token"
MARKER = "ZQX-NOT-FOR-PHONES"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def sockets():
    # Short: a unix socket's path is at most 107 bytes.
    folder = Path(tempfile.mkdtemp(prefix="slp"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def wait_for(app: QApplication, condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.002)


class Phone:
    """A phone's requests, over the socket tor would forward to."""

    def __init__(self, app: QApplication, path: Path) -> None:
        self.app = app
        self.path = path

    def _connect(self) -> QLocalSocket:
        socket = QLocalSocket()
        socket.connectToServer(str(self.path))
        wait_for(self.app, lambda: socket.state() == QLocalSocket.LocalSocketState.ConnectedState)
        return socket

    def raw(self, data: bytes) -> bytes:
        socket = self._connect()
        socket.write(QByteArray(data))
        socket.flush()
        buffer = bytearray()

        def complete() -> bool:
            buffer.extend(bytes(socket.readAll().data()))
            head, sep, body = bytes(buffer).partition(b"\r\n\r\n")
            if not sep:
                return socket.state() == QLocalSocket.LocalSocketState.UnconnectedState
            length = next(
                (
                    int(line.split(b":")[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                None,
            )
            return length is not None and len(body) >= length

        wait_for(self.app, complete)
        socket.disconnectFromServer()
        return bytes(buffer)

    def ask(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        token: str | None = TOKEN,
        host: str = ONION,
    ) -> tuple[int, object]:
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        data = b""
        if body is not None:
            data = json.dumps(body).encode()
            lines += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
        answer = self.raw(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
        head, _, payload = answer.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        if b"application/json" in head:
            return status, json.loads(payload) if payload else {}
        return status, payload

    def stream(self, token: str = TOKEN) -> Stream:
        socket = self._connect()
        request = (
            f"GET /api/events HTTP/1.1\r\nHost: {ONION}\r\nAuthorization: Bearer {token}\r\n\r\n"
        )
        socket.write(QByteArray(request.encode()))
        socket.flush()
        return Stream(self.app, socket)


class Stream:
    def __init__(self, app: QApplication, socket: QLocalSocket) -> None:
        self.app = app
        self.socket = socket
        self.buffer = bytearray()

    def events(self) -> list[tuple[str, dict]]:
        self.buffer.extend(bytes(self.socket.readAll().data()))
        out = []
        for block in bytes(self.buffer).partition(b"\r\n\r\n")[2].split(b"\n\n"):
            lines = block.decode().split("\n")
            if len(lines) == 2 and lines[0].startswith("event: "):
                out.append((lines[0][7:], json.loads(lines[1][6:])))
        return out

    def names(self) -> list[str]:
        return [name for name, _ in self.events()]

    def wait_for(self, name: str, timeout: float = 20.0) -> None:
        wait_for(self.app, lambda: name in self.names(), timeout)

    def raw(self) -> bytes:
        self.events()
        return bytes(self.buffer)

    def closed(self) -> bool:
        self.events()
        return self.socket.state() == QLocalSocket.LocalSocketState.UnconnectedState


def open_window(
    tmp_path: Path,
    sockets: Path,
    story,
    cast,
    replies: list[str],
    *,
    shared: bool = True,
    delay: float = 0.0,
    reads: bool = False,
) -> tuple[MainWindow, Phone]:
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "every_turn" if reads else "manual"
    window.open_story(story.id)
    window.session.story.held_character_id = "char-serrik"
    provider = MockChatProvider(replies)
    provider.delay = delay
    window._provider = provider
    window._refresh_composer()
    window.config.phone.devices = [
        PhoneDevice(id="p1", name="Pixel", token_sha256=token_hash(TOKEN))
    ]
    if shared:
        window.config.phone.shared_stories = [story.id]
    window.start_phone_server(sockets / "h.sock", ONION)
    return window, Phone(QApplication.instance(), sockets / "h.sock")


def user_texts(window: MainWindow) -> list[str]:
    return [n.content for n in window.session.path() if n.kind == "user"]


# --- who may ask, about what ---------------------------------------------------------


def test_the_state_and_who_may_ask(app, tmp_path, sockets, story, cast):
    window, phone = open_window(tmp_path, sockets, story, cast, [])
    assert phone.ask("GET", "/api/state", token=None)[0] == 401
    assert phone.ask("GET", "/api/state", host="localhost")[0] == 421
    status, state = phone.ask("GET", "/api/state")
    assert status == 200
    assert state["story"] == {"id": story.id, "title": story.title, "chat": False}
    assert state["busy"] is False and state["private"] is None

    window.phone_share(story.id, False)
    status, state = phone.ask("GET", "/api/state")
    assert state == {"story": None, "shared": False, "open_elsewhere": True}
    assert phone.ask("GET", "/api/transcript")[0] == 423
    window.close()


def test_a_story_that_keeps_a_map_stays_off_the_phone(app, tmp_path, sockets, story, cast):
    mapped = story.model_copy(
        update={"defaults": story.defaults.model_copy(update={"world_map": True})}
    )
    window, phone = open_window(tmp_path, sockets, mapped, cast, [])
    assert window.phone_shared(story.id)
    assert phone.ask("GET", "/api/transcript")[0] == 423
    assert phone.ask("GET", "/api/stories")[1] == {"stories": []}
    window.close()


# --- turns -----------------------------------------------------------------------------


def test_a_phone_turn_streams_to_the_phone_and_lands_in_the_story(
    app, tmp_path, sockets, story, cast
):
    window, phone = open_window(tmp_path, sockets, story, cast, ["The hall falls quiet."])
    window.composer.input.setPlainText("the author's own draft")
    events = phone.stream()
    events.wait_for("hello")
    status, options = phone.ask("GET", "/api/composer")
    assert status == 200
    assert NARRATOR_SPEAKER_ID in [s["value"] for s in options["speakers"]]

    status, answer = phone.ask(
        "POST", "/api/turn", {"text": "Night falls.", "speaker": NARRATOR_SPEAKER_ID}
    )
    assert (status, answer) == (202, {"queued": False})
    events.wait_for("changed")
    names = events.names()
    for name in ("turn", "job_started", "text", "passage_done", "job_finished", "changed"):
        assert name in names, name
    assert names.index("turn") < names.index("job_started") < names.index("job_finished")
    texts = "".join(data["text"] for name, data in events.events() if name == "text")
    assert texts == "The hall falls quiet."

    assert user_texts(window) == ["Night falls."]
    assert window.composer.text() == "the author's own draft"
    status, page = phone.ask("GET", "/api/transcript")
    assert [m["text"] for m in page["messages"]] == ["Night falls.", "The hall falls quiet."]
    assert page["messages"][1]["can_regenerate"] and page["earlier"] is False
    window.close()


def test_the_desktop_s_turn_reaches_the_phone_too(app, tmp_path, sockets, story, cast):
    window, phone = open_window(tmp_path, sockets, story, cast, ["The door gives."])
    events = phone.stream()
    events.wait_for("hello")
    window.composer.input.setPlainText("I push the door.")
    window.send_turn()
    events.wait_for("job_finished")
    turn = next(data for name, data in events.events() if name == "turn")
    # The composer still speaks as it opened, as the narrator.
    assert turn == {
        "label": "Narrator",
        "text": "I push the door.",
        "kind": "narration",
        "by": "desktop",
    }
    window.close()


def test_a_phone_s_question_is_asked(app, tmp_path, sockets, story, cast):
    window, phone = open_window(tmp_path, sockets, story, cast, ["Because of the rules."])
    status, options = phone.ask("GET", "/api/composer")
    phone.ask("POST", "/api/turn", {"text": "Why?", "speaker": options["question"]})
    wait_for(app, lambda: not window._busy and window.session.bundle.asides)
    assert window.session.bundle.asides[0].question == "Why?"
    assert user_texts(window) == []
    window.close()


def test_a_value_the_composer_doesnt_offer_is_refused(app, tmp_path, sockets, story, cast):
    window, phone = open_window(tmp_path, sockets, story, cast, [])
    for body in (
        {"text": "Hi", "speaker": "char-nobody"},
        {"text": "Hi", "speaker": NARRATOR_SPEAKER_ID, "length": "endless"},
    ):
        assert phone.ask("POST", "/api/turn", body)[0] == 400
    assert not window._busy and user_texts(window) == []
    window.close()


def test_while_a_passage_is_written_a_phone_turn_waits_or_is_refused(
    app, tmp_path, sockets, story, cast
):
    window, phone = open_window(
        tmp_path,
        sockets,
        story,
        cast,
        ["The door gives.", "{}", "The hall.", "{}"],
        delay=0.05,
        reads=True,
    )
    window.composer.input.setPlainText("I push the door.")
    window.send_turn()
    assert window._busy and not window._background
    status, answer = phone.ask(
        "POST", "/api/turn", {"text": "Wait.", "speaker": NARRATOR_SPEAKER_ID}
    )
    assert (status, answer) == (409, {"error": window_phone.BUSY})

    wait_for(app, lambda: window._background)
    status, answer = phone.ask(
        "POST", "/api/turn", {"text": "Later.", "speaker": NARRATOR_SPEAKER_ID}
    )
    assert (status, answer) == (202, {"queued": True})
    status, answer = phone.ask(
        "POST", "/api/turn", {"text": "More.", "speaker": NARRATOR_SPEAKER_ID}
    )
    assert (status, answer) == (409, {"error": window_phone.ALREADY_QUEUED})
    wait_for(app, lambda: not window._busy and len(user_texts(window)) == 2)
    assert user_texts(window) == ["I push the door.", "Later."]
    window.close()


def test_a_dialog_on_the_desktop_holds_the_phone_back(app, tmp_path, sockets, story, cast):
    window, phone = open_window(tmp_path, sockets, story, cast, ["x"])
    dialog = QDialog(window)
    dialog.setWindowModality(Qt.ApplicationModal)
    dialog.show()
    app.processEvents()
    try:
        status, answer = phone.ask(
            "POST", "/api/turn", {"text": "Hi", "speaker": NARRATOR_SPEAKER_ID}
        )
        assert (status, answer) == (409, {"error": window_phone.WAITING_ON_DESKTOP})
        assert phone.ask("GET", "/api/state")[1]["desktop_waiting"] is True
    finally:
        dialog.close()
    window.close()


def test_a_retake_and_a_take_switch_from_the_phone(app, tmp_path, sockets, story, cast):
    window, phone = open_window(
        tmp_path, sockets, story, cast, ["The door gives.", "The door holds."]
    )
    phone.ask("POST", "/api/turn", {"text": "I push.", "speaker": NARRATOR_SPEAKER_ID})
    wait_for(app, lambda: not window._busy and window._phone.entries)
    status, _ = phone.ask("POST", "/api/regenerate", {"length": "brief"})
    assert status == 202
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]
    assert passage.content == "The door holds."
    wait_for(app, lambda: window._phone.entries[-1]["take"] == [2, 2])
    status, _ = phone.ask("POST", "/api/take", {"node_id": passage.id, "step": -1})
    assert status == 200
    assert window.session.path()[-1].content == "The door gives."
    assert phone.ask("POST", "/api/take", {"node_id": passage.id, "step": 1})[0] == 409
    window.close()


# --- nothing that isn't shared ---------------------------------------------------------


def test_nothing_of_a_story_not_shared_reaches_the_phone(app, tmp_path, sockets, story, cast):
    secret = story.model_copy(update={"title": f"Diary {MARKER}"})
    window, phone = open_window(
        tmp_path, sockets, secret, cast, [f"A passage that says {MARKER}."], shared=False
    )
    events = phone.stream()
    events.wait_for("hello")
    window.composer.input.setPlainText(f"I write {MARKER} in the margin.")
    window.send_turn()
    wait_for(app, lambda: not window._busy)
    app.processEvents()
    answers = [
        phone.ask("GET", path)
        for path in (
            "/api/state",
            "/api/stories",
            "/api/transcript",
            "/api/composer",
            "/api/branches",
        )
    ]
    assert [status for status, _ in answers] == [200, 200, 423, 423, 423]
    assert MARKER not in json.dumps([body for _, body in answers])
    assert MARKER.encode() not in events.raw()
    assert {"text", "turn", "changed"}.isdisjoint(events.names())
    window.close()


# --- pairing, revoking, stopping --------------------------------------------------------


def test_a_phone_is_paired_through_the_window(app, tmp_path, sockets, story, cast):
    window, phone = open_window(tmp_path, sockets, story, cast, [])
    window.config.phone.devices = []
    claims = []
    window._phone.on_claim = claims.append
    code = window._phone.pairing.open()
    status, claim = phone.ask("POST", "/api/pair", {"code": code, "name": "Pixel 9"}, token=None)
    assert status == 202 and [c.id for c in claims] == [claim["claim"]]
    device = window.phone_answer_claim(claim["claim"], allow=True)
    assert device is not None and device.name == "Pixel 9"
    status, answer = phone.ask("GET", f"/api/pair/{claim['claim']}", token=None)
    assert answer["state"] == "approved"
    token = answer["token"]
    assert window.config.phone.devices[0].token_sha256 == token_hash(token)
    assert token not in (tmp_path / "config.json").read_text()
    assert phone.ask("GET", "/api/state", token=token)[0] == 200
    window.close()


def test_revoking_a_phone_and_stopping_close_their_streams(app, tmp_path, sockets, story, cast):
    window, phone = open_window(tmp_path, sockets, story, cast, [])
    first = phone.stream()
    first.wait_for("hello")
    window.phone_revoke("p1")
    first.wait_for("revoked")
    wait_for(app, first.closed)
    assert phone.ask("GET", "/api/state")[0] == 401

    window.config.phone.devices = [PhoneDevice(id="p2", token_sha256=token_hash("another"))]
    second = phone.stream("another")
    second.wait_for("hello")
    window.stop_phone_server()
    second.wait_for("stopped")
    wait_for(app, second.closed)
    assert not (sockets / "h.sock").exists()
    window.close()


# --- private: shown, tagged, and nothing written -------------------------------------------


def test_a_memory_scene_reaches_the_phone_tagged_and_nothing_is_written(
    app, tmp_path, sockets, story, cast
):
    window, phone = open_window(tmp_path, sockets, story, cast, [])
    private = MockChatProvider([f"A quiet reply that holds {MARKER} close."])
    window.session.enter_private(keep="memory", provider=private, model="local/model")
    window._submit_turn(
        f"I whisper {MARKER}.", window._current_turn(f"I whisper {MARKER}."), origin="phone"
    )
    wait_for(app, lambda: not window._busy)
    window.reload_transcript()

    state = phone.ask("GET", "/api/state")[1]
    assert state["private"] == "memory"
    messages = phone.ask("GET", "/api/transcript")[1]["messages"]
    assert [m["private"] for m in messages] == ["memory", "memory"]
    assert MARKER in messages[1]["text"]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert MARKER.encode() not in path.read_bytes(), path
    window.session.discard_private()
    window.close()


def test_phone_access_off_by_default_and_on_only_when_started(app, tmp_path, story, cast):
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    assert not window.phone_on
    window.open_story(story.id)
    window._phone_settled()  # a hook with nothing behind it
    assert not window.phone_on
    window.close()


def test_the_phone_refuses_a_value_list_it_doesnt_know():
    # The API's lists are the composer's: one it hasn't heard of is refused.
    assert set(phone_api._OPTION_LISTS) == {"speaker", "length", "outcome", "skill", "difficulty"}
