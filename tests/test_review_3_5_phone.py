"""The review of 3.5.0b1, phone access (findings P1 to P6): each test fails
on the code as it was reviewed (master 0f289da).

Spoken to as a phone would, over the real unix socket with no tor; the
clients are QLocalSockets in the test's own event loop, as in
test_phone_server.py and test_phone_window.py."""

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

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui import phone_server  # noqa: E402
from sealedlore.phone.api import Api  # noqa: E402
from sealedlore.phone.pairing import Pairing  # noqa: E402
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
