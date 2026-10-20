"""The GUI's half of pausing the garbage collector (see `sealedlore.threads`).

The engine counts its plain threads; the last to end wakes this, by a
queued signal, and the collecting is done here, on the GUI thread."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot

from sealedlore import threads


class _Waker(QObject):
    woken = Signal()

    def __init__(self, parent: QObject | None) -> None:
        super().__init__(parent)
        # Queued: it is emitted from the thread that ended last.
        self.woken.connect(self._resume, Qt.QueuedConnection)

    @Slot()
    def _resume(self) -> None:
        threads.resume_collector()

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
