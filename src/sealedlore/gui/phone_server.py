"""The phones' connections: a local socket tor forwards the onion's port to.

`QLocalServer` on a unix socket in a folder only this user can open, so no
TCP port is open on the computer at all: tor's `ADD_ONION … unix:` reaches it
and nothing else can (checked on the author's phone: SealedLoreDocs
design/phone.md). Everything runs on the GUI thread, in the event loop, so
the API can read the story as the window does and nothing needs a lock.

A connection is kept alive between requests (a new stream over Tor costs a
round trip to the onion) and closed when idle. An event stream holds its
connection until the phone goes or falls too far behind.

Anyone with the onion address can open connections, so none may be held by
sending slowly: a request has REQUEST_MS from its first byte to its last,
however the bytes come, and with every slot taken a new connection takes the
place of the oldest no paired phone has used.
"""

from __future__ import annotations

import itertools
import os
import traceback
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from sealedlore.phone.api import Api
from sealedlore.phone.http import HttpError, RequestParser, error_response, render

MAX_CONNECTIONS = 32
IDLE_MS = 20_000  # a kept-alive connection waits this long for its next request
REQUEST_MS = 20_000  # a request, head and body, arrives whole within this
HEARTBEAT_MS = 20_000
# An event stream this far behind is dropped: the phone reconnects and
# reloads what it missed.
MAX_UNSENT = 1024 * 1024


class PhoneServer(QObject):
    # A phone's event stream opened (its device id), or one closed: how many
    # phones are connected is what the status bar shows.
    stream_opened = Signal(str)
    streams_changed = Signal()

    def __init__(self, api: Api, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_new_connection)
        self._connections: set[_Connection] = set()
        self._order = itertools.count()
        self._path: Path | None = None
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(HEARTBEAT_MS)
        self._heartbeat.timeout.connect(self.api.hub.heartbeat)

    @property
    def path(self) -> Path | None:
        return self._path

    def listen(self, path: Path) -> None:
        """Listen on `path`. A socket left there by a run that died is
        removed first, and only then: removed after listening, tor would be
        handed a socket no one is behind."""
        QLocalServer.removeServer(str(path))
        if not self._server.listen(str(path)):
            raise OSError(f"can't listen on {path}: {self._server.errorString()}")
        os.chmod(path, 0o600)
        self._path = path
        self._heartbeat.start()

    def close(self) -> None:
        """Every phone is told it stopped, every connection closed, the socket gone."""
        self._heartbeat.stop()
        self.api.hub.drop_all("stopped")
        for connection in list(self._connections):
            connection.close()
        self._server.close()
        if self._path is not None:
            QLocalServer.removeServer(str(self._path))
            self._path = None

    def connection_count(self) -> int:
        return len(self._connections)

    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if len(self._connections) >= MAX_CONNECTIONS and not self._make_room():
                socket.abort()
                socket.deleteLater()
                continue
            self._connections.add(_Connection(socket, self, next(self._order)))

    def _make_room(self) -> bool:
        """Drop the oldest connection no paired phone has used. Refusing the
        newcomer instead let whoever opened the slots first keep them; this
        way a stranger's connections are the ones that go, and a phone's
        request (a second or so over Tor) is through long before its turn."""
        strangers = [c for c in self._connections if not c.trusted]
        if not strangers:
            return False
        min(strangers, key=lambda c: c.order).drop()
        return True

    def _forget(self, connection: _Connection) -> None:
        self._connections.discard(connection)


class _Connection(QObject):
    def __init__(self, socket: QLocalSocket, server: PhoneServer, order: int) -> None:
        super().__init__(server)
        self._socket = socket
        self._server = server
        self.order = order  # oldest first, when room has to be made
        self.trusted = False  # a paired phone has used it: never dropped for room
        self._parser = RequestParser()
        self._handle: int | None = None  # the hub's, once this is an event stream
        self._closing = False
        self._done = False
        self._between_requests = False  # waiting for the next on a kept-alive connection
        # One deadline at a time, never put off by bytes arriving: a request
        # trickled a byte at a time would otherwise hold its slot for ever.
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._out_of_time)
        socket.readyRead.connect(self._read)
        socket.disconnected.connect(self._gone)
        self._deadline.start(REQUEST_MS)  # opened to send a request: it has this long

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        # What is left to write has this long to go: a peer that never reads
        # its answer doesn't keep the connection.
        self._deadline.start(REQUEST_MS)
        try:
            self._socket.disconnectFromServer()
            gone = self._socket.state() == QLocalSocket.LocalSocketState.UnconnectedState
        except RuntimeError:  # Qt deleted the socket with its server
            gone = True
        if gone:
            self._gone()

    def _out_of_time(self) -> None:
        try:
            unsent = self._socket.bytesToWrite()
        except RuntimeError:  # Qt deleted the socket with its server
            unsent = 0
        if self.trusted and unsent:
            # A paired phone still taking a picture over a slow circuit keeps
            # its connection while some of its answer is still to go, until
            # tor closes the stream. It isn't held to showing progress by each
            # deadline: the socket takes more only once the other end has
            # drained most of what it holds, which on a slow circuit can take
            # longer than one.
            self._deadline.start(IDLE_MS)
            return
        self.drop()

    def drop(self) -> None:
        """At once, with nothing more written: out of time, or room needed."""
        try:
            self._socket.abort()
        except RuntimeError:  # Qt deleted the socket with its server
            pass
        self._gone()

    def _write(self, data: bytes) -> None:
        self._socket.write(QByteArray(data))
        self._socket.flush()

    def _read(self) -> None:
        data = bytes(self._socket.readAll().data())
        if self._handle is not None or self._closing:
            return  # an event stream reads nothing more
        if data and self._between_requests:
            self._between_requests = False
            self._deadline.start(REQUEST_MS)  # a request begins: this long for all of it
        try:
            requests = self._parser.feed(data)
        except HttpError as exc:
            self._write(render(error_response(exc.status, exc.detail), keep_alive=False))
            self.close()
            return
        except Exception:  # noqa: BLE001 - bytes from the network must not raise in a Qt slot
            # Raised out of a slot, an exception can take the app down (PySide
            # on Python 3.11). Whatever the parser didn't foresee is the
            # client's bad request.
            traceback.print_exc()
            self._write(render(error_response(400), keep_alive=False))
            self.close()
            return
        for request in requests:
            try:
                response = self._server.api.handle(request)
            except Exception:  # noqa: BLE001 - a bug must not take the window down
                traceback.print_exc()
                response = error_response(500)
            if response.device:
                self.trusted = True
            if response.stream:
                self._write(render(response, keep_alive=False))
                hub = self._server.api.hub
                self._handle = hub.subscribe(response.device, self._send_event, self.close)
                self._deadline.stop()  # a paired phone's stream is held as long as it lasts
                hub.send_to(self._handle, "hello", self._server.api.state_payload())
                self._server.stream_opened.emit(response.device)
                self._server.streams_changed.emit()
                return
            self._write(render(response, keep_alive=request.keep_alive))
            if not request.keep_alive:
                self.close()
                return
        if not requests:
            return  # the request so far keeps the deadline it had
        if self._parser.pending:
            self._deadline.start(REQUEST_MS)  # the next one has begun
        else:
            self._between_requests = True
            self._deadline.start(IDLE_MS)

    def _send_event(self, data: bytes) -> bool:
        if self._closing or self._socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
            return False
        if self._socket.bytesToWrite() > MAX_UNSENT:
            return False
        self._write(data)
        return True

    def _gone(self) -> None:
        """Once, however the connection ended: `close` and the socket's
        `disconnected` both lead here."""
        if self._done:
            return
        self._done = True
        if self._handle is not None:
            self._server.api.hub.unsubscribe(self._handle)
            self._handle = None
            self._server.streams_changed.emit()
        self._deadline.stop()
        self._server._forget(self)
        try:
            self._socket.deleteLater()
        except RuntimeError:
            pass  # deleted with its server already
        self.deleteLater()
