"""The phones' connections: a local socket tor forwards the onion's port to.

`QLocalServer` on a unix socket in a folder only this user can open, so no
TCP port is open on the computer at all: tor's `ADD_ONION … unix:` reaches it
and nothing else can (checked on the author's phone: SealedLoreDocs
design/phone.md). Everything runs on the GUI thread, in the event loop, so
the API can read the story as the window does and nothing needs a lock.

A connection is kept alive between requests (a new stream over Tor costs a
round trip to the onion) and closed when idle. An event stream holds its
connection until the phone goes or falls too far behind.
"""

from __future__ import annotations

import os
import traceback
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from sealedlore.phone.api import Api
from sealedlore.phone.http import HttpError, RequestParser, error_response, render

MAX_CONNECTIONS = 32
IDLE_MS = 20_000
HEARTBEAT_MS = 20_000
# An event stream this far behind is dropped: the phone reconnects and
# reloads what it missed.
MAX_UNSENT = 1024 * 1024


class PhoneServer(QObject):
    def __init__(self, api: Api, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_new_connection)
        self._connections: set[_Connection] = set()
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
            if len(self._connections) >= MAX_CONNECTIONS:
                socket.abort()
                socket.deleteLater()
                continue
            self._connections.add(_Connection(socket, self))

    def _forget(self, connection: _Connection) -> None:
        self._connections.discard(connection)


class _Connection(QObject):
    def __init__(self, socket: QLocalSocket, server: PhoneServer) -> None:
        super().__init__(server)
        self._socket = socket
        self._server = server
        self._parser = RequestParser()
        self._handle: int | None = None  # the hub's, once this is an event stream
        self._closing = False
        self._done = False
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(IDLE_MS)
        self._idle.timeout.connect(self.close)
        socket.readyRead.connect(self._read)
        socket.disconnected.connect(self._gone)
        self._idle.start()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self._socket.disconnectFromServer()
            gone = self._socket.state() == QLocalSocket.LocalSocketState.UnconnectedState
        except RuntimeError:  # Qt deleted the socket with its server
            gone = True
        if gone:
            self._gone()

    def _write(self, data: bytes) -> None:
        self._socket.write(QByteArray(data))
        self._socket.flush()

    def _read(self) -> None:
        data = bytes(self._socket.readAll().data())
        if self._handle is not None or self._closing:
            return  # an event stream reads nothing more
        self._idle.start()
        try:
            requests = self._parser.feed(data)
        except HttpError as exc:
            self._write(render(error_response(exc.status, exc.detail), keep_alive=False))
            self.close()
            return
        for request in requests:
            try:
                response = self._server.api.handle(request)
            except Exception:  # noqa: BLE001 - a bug must not take the window down
                traceback.print_exc()
                response = error_response(500)
            if response.stream:
                self._write(render(response, keep_alive=False))
                hub = self._server.api.hub
                self._handle = hub.subscribe(response.device, self._send_event, self.close)
                self._idle.stop()
                hub.send_to(self._handle, "hello", self._server.api.state_payload())
                return
            self._write(render(response, keep_alive=request.keep_alive))
            if not request.keep_alive:
                self.close()
                return

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
        self._idle.stop()
        self._server._forget(self)
        try:
            self._socket.deleteLater()
        except RuntimeError:
            pass  # deleted with its server already
        self.deleteLater()
