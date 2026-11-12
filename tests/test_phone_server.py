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
