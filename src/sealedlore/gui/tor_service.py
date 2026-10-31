"""SealedLore's own tor, for phone access: started, watched, stopped.

A `QProcess` and a `QLocalSocket` to its control port, both on the GUI
thread (no Python threads beside the window). Tor runs on a torrc of ours
(phone/torrc.py) and is told, twice over, to exit when SealedLore does:
TAKEOWNERSHIP on the control connection and __OwningControllerProcess.

The steps, each said by `changed` for the window to show:

    starting      the process is up, its control socket not yet
    connecting    SAFECOOKIE login, ownership, events
    bootstrapping progress to 100%
    publishing    the onion service added; its descriptor on its way
    ready         a directory has the descriptor: phones can reach it
    restarting    it stopped; it is started again in a moment
    failed        given up on, with why
    stopped       stopped by the author

A tor that dies or drops its control connection is started again after 2,
10, then 30 seconds; after that it is given up on and the window says why,
with the last lines tor printed (`log`, kept in memory only).

Tor knows when the network no longer recommends its version, and says so here
(`outdated`, `warning`), asked once it has a consensus and told by event after
that. Being old doesn't stop a tor connecting, so this never changes the step:
the window says it and phone access goes on (the author, Oct 31 2026).
"""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QProcess, QProcessEnvironment, QTimer, Signal
from PySide6.QtNetwork import QLocalSocket

from sealedlore.phone import tor_control
from sealedlore.phone.tor_control import OUT_OF_DATE, ControlError, Reply, ReplyReader
from sealedlore.phone.torrc import (
    PhonePaths,
    bundled,
    make_folders,
    onion_target,
    render_torrc,
    tor_environment,
)
from sealedlore.storage.atomic import open_private

RESTART_SECONDS = (2, 10, 30)
CONTROL_WAIT_MS = 15_000
LOG_LINES = 200
STOP_WAIT_MS = 3_000


class TorService(QObject):
    # (step, words): the step's name as above, and a line for the window.
    changed = Signal(str, str)
    # The onion's address ("….onion"), once tor has it.
    address = Signal(str)
    # (private key, service id): a key tor made, for the keychain.
    key_made = Signal(str, str)

    def __init__(
        self,
        tor: Path,
        paths: PhonePaths,
        *,
        key: str | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.tor = tor
        self.paths = paths
        self.key = key
        self.step = "stopped"
        self.words = ""
        self.progress = 0
        self.service_id = ""
        # Its version, and what the network says of it ("" until it has said).
        self.version = ""
        self.version_status = ""
        self.log: deque[str] = deque(maxlen=LOG_LINES)
        self._process: QProcess | None = None
        self._control: QLocalSocket | None = None
        self._reader = ReplyReader()
        self._waiting: deque = deque()  # a callback per command sent, in order
        self._nonce = b""
        self._cookie = b""
        self._stopping = False
        self._restarts = 0
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._try_control)
        self._poll_left = 0
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._launch)

    # --- the author's -------------------------------------------------------------

    def start(self) -> None:
        self._stopping = False
        self._restarts = 0
        self._launch()

    def stop(self) -> None:
        """Ends tor: its control connection closed, which ends tor as its
        owner; killed if it hasn't gone in three seconds."""
        self._stopping = True
        self._retry.stop()
        self._poll.stop()
        self._close_control()
        process, self._process = self._process, None
        if process is not None:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
                if not process.waitForFinished(STOP_WAIT_MS):
                    process.kill()
                    process.waitForFinished(STOP_WAIT_MS)
            process.deleteLater()
        self._set("stopped", "Phone access is off.")

    def running(self) -> bool:
        return self._process is not None

    @property
    def outdated(self) -> bool:
        return self.version_status in OUT_OF_DATE

    @property
    def warning(self) -> str:
        """What to tell the author of an out-of-date tor; "" for any other."""
        if not self.outdated:
            return ""
        which = f"its version ({self.version})" if self.version else "its version"
        remedy = (
            "A newer SealedLore brings a newer tor."
            if bundled(self.tor)
            else f"Update the tor at {self.tor}."
        )
        return (
            f"Tor says {which} is out of date, so it may be missing security fixes. "
            f"Phone access still works. {remedy}"
        )

    # --- starting ------------------------------------------------------------------

    def _launch(self) -> None:
        if self._stopping:
            return
        make_folders(self.paths)
        self.paths.control.unlink(missing_ok=True)  # a socket a dead tor left
        with os.fdopen(open_private(self.paths.torrc, os.O_WRONLY | os.O_TRUNC), "w") as torrc:
            torrc.write(render_torrc(self.paths, os.getpid()))
        with os.fdopen(open_private(self.paths.defaults, os.O_WRONLY | os.O_TRUNC), "w"):
            pass
        process = QProcess(self)
        environment = QProcessEnvironment()
        for name, value in tor_environment(self.tor).items():
            environment.insert(name, value)
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_log)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        self._process = process
        self._reader = ReplyReader()
        self._waiting.clear()
        self.progress = 0
        self.version_status = ""
        self._set("starting", "Starting tor…")
        process.start(
            str(self.tor),
            ["-f", str(self.paths.torrc), "--defaults-torrc", str(self.paths.defaults)],
        )
        self._poll_left = CONTROL_WAIT_MS // self._poll.interval()
        self._poll.start()

    def _try_control(self) -> None:
        self._poll_left -= 1
        if self._poll_left <= 0:
            self._poll.stop()
            self._failed("tor didn't open its control socket.")
            return
        if not self.paths.control.exists():
            return
        self._poll.stop()
        control = QLocalSocket(self)
        control.readyRead.connect(self._read_control)
        control.disconnected.connect(self._on_control_gone)
        control.connected.connect(self._login)
        self._control = control
        self._set("connecting", "Connecting to tor…")
        control.connectToServer(str(self.paths.control))

    # --- the control connection -------------------------------------------------------

    def _send(self, command: str, then) -> None:
        self._waiting.append(then)
        self._control.write(QByteArray((command + "\r\n").encode("utf-8")))
        self._control.flush()

    def _read_control(self) -> None:
        control = self._control
        if control is None:
            return
        try:
            replies = self._reader.feed(bytes(control.readAll().data()))
            for reply in replies:
                if reply.event:
                    self._on_event(reply)
                elif self._waiting:
                    self._waiting.popleft()(reply)
        except (ControlError, OSError, ValueError) as exc:
            self._failed(str(exc))

    def _login(self) -> None:
        self._send("PROTOCOLINFO 1", self._on_protocolinfo)

    def _on_protocolinfo(self, reply: Reply) -> None:
        self.version = tor_control.version_of(reply)
        cookie_path = tor_control.cookie_file(reply)
        with open(cookie_path, "rb") as cookie:
            self._cookie = cookie.read(tor_control.COOKIE_LENGTH + 1)
        if len(self._cookie) != tor_control.COOKIE_LENGTH:
            raise ControlError("tor's cookie isn't 32 bytes")
        self._nonce = tor_control.new_nonce()
        self._send(tor_control.authchallenge(self._nonce), self._on_challenge)

    def _on_challenge(self, reply: Reply) -> None:
        self._send(tor_control.authenticate(self._cookie, self._nonce, reply), self._on_login)

    def _on_login(self, reply: Reply) -> None:
        if not reply.ok:
            raise ControlError(f"tor refused the login: {reply.text()}")
        self._cookie = b""
        self._send("TAKEOWNERSHIP", self._expect_ok)
        self._send("SETEVENTS STATUS_CLIENT STATUS_GENERAL HS_DESC", self._expect_ok)
        self._send("GETINFO status/bootstrap-phase", self._on_phase)

    def _expect_ok(self, reply: Reply) -> None:
        if not reply.ok:
            raise ControlError(f"tor refused: {reply.text()}")

    def _on_phase(self, reply: Reply) -> None:
        self._expect_ok(reply)
        self._bootstrap(tor_control.bootstrap_of(reply.text()))

    def _bootstrap(self, phase: tor_control.Bootstrap | None) -> None:
        if phase is None or self.step not in ("connecting", "bootstrapping"):
            return
        self.progress = phase.progress
        if phase.progress < 100:
            self._set("bootstrapping", f"Connecting to Tor {phase.progress}%")
            return
        self._set("publishing", "Publishing the address…")
        # It has a consensus now, and so an answer. No answer is no warning:
        # it must never be what stops phone access.
        self._send(
            "GETINFO status/version/current",
            lambda reply: self._note_version(tor_control.version_status(reply)),
        )
        self._send(tor_control.add_onion(self.key, onion_target(self.paths)), self._on_onion)

    def _on_onion(self, reply: Reply) -> None:
        onion = tor_control.onion_of(reply)
        self.service_id = onion.service_id
        if onion.private_key:
            self.key = onion.private_key
            self.key_made.emit(onion.private_key, onion.service_id)
        self.address.emit(onion.service_id + ".onion")

    def _on_event(self, event: Reply) -> None:
        text = event.text()
        if text.startswith("STATUS_CLIENT"):
            self._bootstrap(tor_control.bootstrap_of(text))
        elif text.startswith("STATUS_GENERAL"):
            self._note_version(tor_control.dangerous_version(text))
        elif self.service_id and tor_control.descriptor_uploaded(event, self.service_id):
            if self.step == "publishing":
                self._restarts = 0
                self._set("ready", "Phone access is on.")

    def _note_version(self, status: str) -> None:
        if not status:
            return
        was = self.outdated
        self.version_status = status
        if self.outdated != was:
            self.changed.emit(self.step, self.words)

    # --- ending ------------------------------------------------------------------------

    def _read_log(self) -> None:
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardOutput().data()).decode("utf-8", "replace")
        self.log.extend(line for line in text.splitlines() if line.strip())

    def _on_finished(self, code: int, _status) -> None:
        if not self._stopping:
            self._failed(f"tor stopped (exit code {code}).")

    def _on_error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart and not self._stopping:
            self._failed(f"tor couldn't be started: {self.tor}")

    def _on_control_gone(self) -> None:
        if not self._stopping and self._control is not None:
            self._failed("tor closed its control connection.")

    def _close_control(self) -> None:
        control, self._control = self._control, None
        if control is not None:
            control.disconnected.disconnect(self._on_control_gone)
            control.abort()
            control.deleteLater()

    def _failed(self, why: str) -> None:
        """Started again after a wait, or given up on with the reason."""
        if self._stopping or self._retry.isActive():
            return
        self._poll.stop()
        self._close_control()
        process, self._process = self._process, None
        if process is not None:
            process.finished.disconnect(self._on_finished)
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(STOP_WAIT_MS)
            process.deleteLater()
        if self._restarts < len(RESTART_SECONDS):
            wait = RESTART_SECONDS[self._restarts]
            self._restarts += 1
            self._set("restarting", f"Tor stopped: {why} Starting it again in {wait} s.")
            self._retry.start(wait * 1000)
            return
        self._set("failed", f"Tor stopped: {why}")

    def _set(self, step: str, words: str) -> None:
        self.step, self.words = step, words
        self.changed.emit(step, words)
