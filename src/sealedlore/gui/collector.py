"""The GUI's half of pausing the garbage collector (see `sealedlore.threads`).

The engine counts its plain threads and the window its QThreads
(gui/workers.py); the last to end wakes this, by a queued signal, and the
collecting is done here, on the GUI thread. While the pause lasts, a timer
here collects the young generations as the collector would have
(`threads.collect_young`): a job can run for minutes, and the window's own
garbage would otherwise wait for it."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QObject, Qt, QTimer, Signal, Slot

from sealedlore import threads

# How often the GUI thread looks, while paused, whether the young generations
# are due. A look is a lock and two numbers.
YOUNG_EVERY_MS = 1000


class _Waker(QObject):
    woken = Signal()

    def __init__(self, parent: QObject | None) -> None:
        super().__init__(parent)
        # Queued: it is emitted from the thread that ended last.
        self.woken.connect(self._resume, Qt.QueuedConnection)
        self._young = QTimer(self)
        self._young.setInterval(YOUNG_EVERY_MS)
        self._young.timeout.connect(self._collect_young)
        self._young.start()

    @Slot()
    def _resume(self) -> None:
        threads.resume_collector()

    @Slot()
    def _collect_young(self) -> None:
        threads.collect_young()

    def wake(self) -> None:
        try:
            self.woken.emit()
        except RuntimeError:  # the application is gone
            pass


_waker: _Waker | None = None


def install_collector_pause(app: QCoreApplication) -> None:
    """Once, as the application is made (`app.build_app`, the tests'
    conftest): pause the collector while background threads are alive."""
    global _waker
    if _waker is None:
        _waker = _Waker(app)
    threads.pause_collector_while_busy(_waker.wake)
