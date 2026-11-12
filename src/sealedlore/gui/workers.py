"""The threads jobs run on: the garbage collector, the story they lend out,
and the fetches nobody waits for.

**The collector.** Python collects on whichever thread crosses its threshold,
and Qt objects it sweeps up off the GUI thread crash PySide
(sealedlore.threads). A QThread is no exception: Python runs on it before its
first slot and after its last, as Qt deletes what was queued for deletion on
it while it ends. So every QThread in the app is a `WorkerThread`, which holds
the collector's pause from `start()` until it has ended and been waited for,
on the GUI thread (tests/test_threads.py holds every QThread to it).
Meanwhile the GUI thread collects the young generations itself
(gui/collector.py), so a long job doesn't stop collection for the window.

**One writer.** A thread working on a story's session (writing an image
prompt from the story's cached prefix, under a dialog's `exec()`) must not
have the session changed under it, and the window's timers and job signals
still fire meanwhile: a rebuild or condensed cards adopted, a card's picture
taken onto it. `WorkerThread(session=)` lends the session for as long as the
thread lives; `lent(session)` says whether any thread has it, and
`lending().returned` is emitted on the GUI thread once the last has given it
back, after the event that did, so what waited for it can run then.

**Fetches nobody waits for** (a model listing, an attestation, a speed test,
a picture or video being made, a price) run on a counted daemon thread
(`run_detached`), never a QThread. Quitting waited up to 35 seconds for each
of them in turn on the GUI thread, most with no way to cancel, and one still
running when the application went was destroyed under it, which Qt treats as
fatal. A daemon thread is simply left: what it brings back is handed to the
GUI thread by `post`, through one relay that lives as long as the
application, and dropped if its owner has been deleted meanwhile or the app
has begun to quit (`close_gate`), so no thread touches a Qt object that may
be going. Cancel what can be cancelled; never wait for the rest.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal, Slot
from shiboken6 import isValid

from sealedlore import threads
from sealedlore.providers.base import error_detail
from sealedlore.threads import BackgroundThread

# id(session) → [session, how many threads have it]. GUI thread only: lent in
# `start()`, given back as the thread is waited for.
_lent: dict[int, list[Any]] = {}


class Lending(QObject):
    """`returned(session)`: no thread has the session any more."""

    returned = Signal(object)
    _back = Signal(object)

    def __init__(self, parent: QObject | None) -> None:
        super().__init__(parent)
        # Queued: given back while a thread's `finished` is being delivered,
        # what waited runs after that, never inside it.
        self._back.connect(self.returned, Qt.QueuedConnection)


_lending: Lending | None = None


def lending() -> Lending:
    """The app's one, parented to the application."""
    global _lending
    if _lending is None or not isValid(_lending):
        _lending = Lending(QCoreApplication.instance())
    return _lending


def lent(session: object | None) -> bool:
    """Whether a thread is working on `session` now."""
    return session is not None and id(session) in _lent


def _lend(session: object) -> None:
    _lent.setdefault(id(session), [session, 0])[1] += 1


def _give_back(session: object) -> None:
    entry = _lent.get(id(session))
    if entry is None:
        return
    entry[1] -= 1
    if entry[1] > 0:
        return
    del _lent[id(session)]
    try:
        lending()._back.emit(session)
    except RuntimeError:  # the application is gone
        pass


class _Life:
    """A thread's hold on the collector, and on its session, given back once."""

    def __init__(self, session: object | None) -> None:
        self._session = session
        self.over = False

    def end(self) -> None:
        if self.over:
            return
        self.over = True
        session, self._session = self._session, None
        try:
            if session is not None:
                _give_back(session)
        finally:
            threads.release()


def _end(thread: QThread | None, life: _Life) -> None:
    if life.over:
        return
    if thread is not None and isValid(thread):
        # `finished` is emitted just before the thread exits; what Qt deletes
        # as it exits runs on it still. (Deleted already, it has ended:
        # ~QThread waits for a thread that is finishing.)
        thread.wait()
    life.end()


class _Ender(QObject):
    """Gives a thread's hold back on the GUI thread once it has finished, if
    the thread object hasn't (`WorkerThread._ended`). A QObject of the
    application's, not the thread's: a thread object deleted before its
    queued `finished` came (its parent deleted at once) takes its own slot
    with it, and the collector stayed paused for good."""

    def __init__(self, thread: QThread, life: _Life) -> None:
        super().__init__(QCoreApplication.instance())
        self._thread = weakref.ref(thread)
        self._life = life

    @Slot()
    def ended(self) -> None:
        _end(self._thread(), self._life)
        self.deleteLater()


class WorkerThread(QThread):
    """A QThread for a job (see the module's note). `session`: the story's
    session the job works on, lent while the thread lives."""

    def __init__(self, parent: QObject | None = None, *, session: object | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._life: _Life | None = None
        # Connected before anyone else's, so given back before their own
        # `finished` handlers run (the window's finish takes up what waited
        # for the story); queued, so on the GUI thread.
        self.finished.connect(self._ended, Qt.QueuedConnection)

    @Slot()
    def _ended(self) -> None:
        if self._life is not None:
            _end(self, self._life)

    def start(self, priority: QThread.Priority = QThread.Priority.InheritPriority) -> None:
        life = self._life
        if life is not None and not life.over:
            return  # running already: QThread.start does nothing then either
        life = _Life(self._session)
        # Before the thread exists: nothing of it runs unpaused, and nothing
        # changes its story under it.
        threads.hold()
        if self._session is not None:
            _lend(self._session)
        self._life = life
        self.finished.connect(_Ender(self, life).ended, Qt.QueuedConnection)
        super().start(priority)


# --- fetches nobody waits for ---------------------------------------------------------------

# Held while a thread hands something on, and by `close_gate`: once the gate
# is closed, nothing is handed on any more.
_gate = threading.Lock()
_closed = False


class _Relay(QObject):
    arrived = Signal(object, object, object)  # owner, callback, args

    def __init__(self, parent: QObject | None) -> None:
        super().__init__(parent)
        self.arrived.connect(self._call, Qt.QueuedConnection)

    @Slot(object, object, object)
    def _call(self, owner: QObject, callback: Callable[..., object], args: tuple) -> None:
        if isValid(owner):
            callback(*args)


_relay: _Relay | None = None


def install(app: QCoreApplication) -> None:
    """Once, as the application is made (`app.build_app`, the tests'
    conftest): the relay, and the gate closed as the app begins to quit."""
    global _relay, _closed
    if _relay is None or not isValid(_relay):
        _relay = _Relay(app)
        app.aboutToQuit.connect(close_gate)
    with _gate:
        _closed = False


def close_gate() -> None:
    """As the app quits: nothing more is handed to the GUI thread, and the
    threads still going (daemons, left to the process's end) no longer wake
    the collector."""
    global _closed
    with _gate:
        _closed = True
    threads.stop_waking()


def post(owner: QObject, callback: Callable[..., object], *args: object) -> bool:
    """From any thread: `callback(*args)` on the GUI thread, if `owner` (a
    QObject of the GUI thread's) is still there then. False when dropped
    already (the app is quitting)."""
    with _gate:
        relay = _relay
        if _closed or relay is None:
            return False
        try:
            relay.arrived.emit(owner, callback, args)
        except RuntimeError:  # the application is gone
            return False
    return True


def run_detached(
    owner: QObject,
    job: Callable[[], Any],
    done: Callable[[Any, str | None], object],
    *,
    name: str = "detached",
) -> BackgroundThread:
    """On the GUI thread: `job()` on a counted daemon thread, then
    `done(result, error)` on the GUI thread as `post` says, `error` the
    failure in words or None."""
    if _relay is None or not isValid(_relay):
        install(QCoreApplication.instance())

    def run() -> None:
        try:
            result, error = job(), None
        except Exception as exc:  # noqa: BLE001 - said by whoever waits for it
            result, error = None, error_detail(exc)
        post(owner, done, result, error)

    thread = BackgroundThread(target=run, name=name)
    thread.start()
    return thread
