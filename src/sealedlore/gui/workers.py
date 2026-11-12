"""The threads jobs run on, and the garbage collector.

Python collects on whichever thread crosses its threshold, and Qt objects it
sweeps up off the GUI thread crash PySide (sealedlore.threads). A QThread is
no exception: Python runs on it before its first slot and after its last, as
Qt deletes what was queued for deletion on it while it ends. So every QThread
in the app is a `WorkerThread`, which holds the collector's pause from
`start()` until it has ended and been waited for, on the GUI thread
(tests/test_threads.py holds every QThread to it). Meanwhile the GUI thread
collects the young generations itself (gui/collector.py), so a long job
doesn't stop collection for the window.
"""

from __future__ import annotations

import weakref

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Slot
from shiboken6 import isValid

from sealedlore import threads


class _Life:
    """A thread's hold on the collector, given back once."""

    def __init__(self) -> None:
        self.over = False

    def end(self) -> None:
        if self.over:
            return
        self.over = True
        threads.release()


class _Ender(QObject):
    """Gives a thread's hold back on the GUI thread once it has finished.
    A QObject of the application's, not the thread's: a thread object
    deleted before its queued `finished` came (its parent deleted at once)
    would have taken that with it, and the collector stayed paused for good."""

    def __init__(self, thread: QThread, life: _Life) -> None:
        super().__init__(QCoreApplication.instance())
        self._thread = weakref.ref(thread)
        self._life = life

    @Slot()
    def ended(self) -> None:
        thread = self._thread()
        if thread is not None and isValid(thread):
            # `finished` is emitted just before the thread exits; what Qt
            # deletes as it exits runs on it still. (Deleted already, it has
            # ended: ~QThread waits for a thread that is finishing.)
            thread.wait()
        self._life.end()
        self.deleteLater()


class WorkerThread(QThread):
    """A QThread for a job (see the module's note)."""

    def start(self, priority: QThread.Priority = QThread.Priority.InheritPriority) -> None:
        life = getattr(self, "_life", None)
        if life is not None and not life.over:
            return  # running already: QThread.start does nothing then either
        life = _Life()
        # Before the thread exists: nothing of it runs unpaused.
        threads.hold()
        self._life = life
        self.finished.connect(_Ender(self, life).ended, Qt.QueuedConnection)
        super().start(priority)
