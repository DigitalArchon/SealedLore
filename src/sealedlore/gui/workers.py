"""The threads jobs run on: the garbage collector, and the story they lend out.

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
"""

from __future__ import annotations

import weakref
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal, Slot
from shiboken6 import isValid

from sealedlore import threads

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
