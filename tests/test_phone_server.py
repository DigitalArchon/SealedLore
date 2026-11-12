"""The phones' connections (gui/phone_server.py), spoken to over its real unix
socket with the API's own stand-in for the window: what a stranger's bytes can
and can't do to the server.

The client is a QLocalSocket in the test's own event loop, as in
test_phone_window.py: no thread beside the server."""

from __future__ import annotations

import os
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

from PySide6.QtCore import QByteArray  # noqa: E402
from PySide6.QtNetwork import QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui import phone_server  # noqa: E402
from sealedlore.phone.api import Api  # noqa: E402
from sealedlore.phone.pairing import Pairing  # noqa: E402
from tests.test_phone_api import ONION, TOKEN, FakeBackend  # noqa: E402

CONNECTED = QLocalSocket.LocalSocketState.ConnectedState
UNCONNECTED = QLocalSocket.LocalSocketState.UnconnectedState


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def server(app):
    # Short: a unix socket's path is at most 107 bytes.
    folder = Path(tempfile.mkdtemp(prefix="sls"))
    api = Api(FakeBackend(), pairing=Pairing(), onion=lambda: ONION)
    server = phone_server.PhoneServer(api)
    server.listen(folder / "s.sock")
    yield server
    server.close()
    app.processEvents()
    shutil.rmtree(folder, ignore_errors=True)


def wait_for(condition, timeout: float = 5.0) -> bool:
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        app.processEvents()
        time.sleep(0.002)
    return True


class Client:
    def __init__(self, server: phone_server.PhoneServer) -> None:
        self.socket = QLocalSocket()
        self.socket.connectToServer(str(server.path))
        assert wait_for(lambda: self.socket.state() == CONNECTED)
        self.received = bytearray()

    def send(self, data: bytes) -> None:
        self.socket.write(QByteArray(data))
        self.socket.flush()

    def read(self) -> bytes:
        self.received.extend(bytes(self.socket.readAll().data()))
        return bytes(self.received)

    def answered(self) -> bool:
        return b"\r\n\r\n" in self.read()

    def status(self) -> int:
        return int(self.read().split(b" ")[1])

    def closed(self) -> bool:
        self.read()
        return self.socket.state() == UNCONNECTED


def state_request(token: str | None = TOKEN, close: bool = False) -> bytes:
    lines = ["GET /api/state HTTP/1.1", f"Host: {ONION}"]
    if token:
        lines.append(f"Authorization: Bearer {token}")
    if close:
        lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def test_a_content_length_python_wont_read_is_answered_not_raised(server):
    """int() raises ValueError past about 4,300 digits: it went past the
    parser's HttpError, out of the server's Qt slot, and nothing answered."""
    client = Client(server)
    client.send(
        f"POST /api/turn HTTP/1.1\r\nHost: {ONION}\r\nContent-Length: {'9' * 5000}\r\n\r\n".encode()
    )
    assert wait_for(client.answered), "no answer"
    assert client.status() == 413
    assert wait_for(client.closed)


def test_whatever_the_parser_didnt_foresee_is_a_bad_request(server, monkeypatch):
    def broken(self, data):
        raise RuntimeError("a case no rule expected")

    monkeypatch.setattr(phone_server.RequestParser, "feed", broken)
    client = Client(server)
    client.send(state_request())
    assert wait_for(client.answered), "no answer"
    assert client.status() == 400
    assert wait_for(client.closed)
    assert server.connection_count() == 0


# --- slots a stranger can't hold ----------------------------------------------------


@pytest.fixture
def short_deadlines(monkeypatch):
    monkeypatch.setattr(phone_server, "IDLE_MS", 400)
    monkeypatch.setattr(phone_server, "REQUEST_MS", 400, raising=False)


def test_a_request_trickled_a_byte_at_a_time_is_dropped_at_its_deadline(server, short_deadlines):
    """Every byte restarted the idle timer, so a request sent a byte at a time
    held its slot for as long as the bytes kept coming."""
    client = Client(server)
    request = state_request()  # some 130 bytes: a trickle of over six seconds
    for byte in request[:-2]:
        if client.closed():
            break
        client.send(bytes([byte]))
        wait_for(lambda: False, timeout=0.05)
    assert client.closed(), "still open after the whole trickle"
    assert b"200 OK" not in client.read()
    assert server.connection_count() == 0


def test_a_kept_alive_connection_gets_a_new_deadline_for_each_request(server, short_deadlines):
    client = Client(server)
    for answers in range(1, 4):
        client.send(state_request())
        assert wait_for(lambda n=answers: client.read().count(b"200 OK") == n)
        wait_for(lambda: False, timeout=0.25)  # within IDLE_MS
    assert not client.closed()
    assert wait_for(client.closed), "an idle connection closes"


def test_with_every_slot_held_by_strangers_a_phone_still_gets_in(server, monkeypatch):
    """A full server refused the newcomer: 32 connections sending nothing
    kept every phone out."""
    monkeypatch.setattr(phone_server, "MAX_CONNECTIONS", 3)
    strangers = [Client(server) for _ in range(3)]
    for stranger in strangers:
        stranger.send(b"GET /api/state HTTP/1.1\r\nHo")
    assert wait_for(lambda: server.connection_count() == 3)
    phone = Client(server)
    phone.send(state_request(close=True))
    assert wait_for(phone.answered), "the phone was refused"
    assert phone.status() == 200
    assert wait_for(strangers[0].closed), "the oldest stranger made room"
    assert not strangers[1].closed() and not strangers[2].closed()


def test_a_connection_a_paired_phone_used_is_never_dropped_for_room(server, monkeypatch):
    monkeypatch.setattr(phone_server, "MAX_CONNECTIONS", 2)
    phone = Client(server)
    phone.send(state_request())
    assert wait_for(lambda: b"200 OK" in phone.read())
    stream = Client(server)
    stream.send(state_request().replace(b"/api/state", b"/api/events"))
    assert wait_for(lambda: b"event: hello" in stream.read())
    stranger = Client(server)
    stranger.send(state_request(token="wrong"))
    assert wait_for(stranger.closed), "no room, and none made"
    assert b"401" not in stranger.read()
    assert not phone.closed() and not stream.closed()


def test_a_paired_phone_slow_to_take_a_big_answer_keeps_its_connection(server, short_deadlines):
    big = b"\xff\xd8" + b"x" * (8 * 1024 * 1024)
    server.api.backend.picture = lambda image_id, thumb: (big, "image/jpeg")
    client = Client(server)
    client.socket.setReadBufferSize(64 * 1024)  # a slow reader: the rest waits on the server
    client.send(
        f"GET /api/pictures/img1 HTTP/1.1\r\nHost: {ONION}\r\n"
        f"Authorization: Bearer {TOKEN}\r\n\r\n".encode()
    )
    wait_for(lambda: False, timeout=1.0)  # past the deadlines, nothing read
    assert client.socket.state() == CONNECTED
    assert wait_for(lambda: len(client.read()) > len(big), timeout=10)
    assert client.read().endswith(big)
