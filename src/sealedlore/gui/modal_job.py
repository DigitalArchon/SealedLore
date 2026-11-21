"""A model call made while a dialog waits for it: on a worker thread, a
progress dialog with Stop meanwhile, its label following the job's notices
(Write with AI, Find the people…)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEventLoop, QObject, Qt, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QProgressDialog, QWidget

from sealedlore.gui.worker import GenerationWorker
from sealedlore.gui.workers import WorkerThread


class _Label(QObject):
    """The progress dialog's label, set on the GUI thread, where the dialog
    lives. A lambda connected to the worker's signal has no object to say
    which thread it belongs to, so Qt ran it on the worker's, which touched
    the dialog from there: Find the people hung on Wayland and on Windows
    with the worker stuck in it (the lab walk, Nov 20 2026)."""

    def __init__(self, progress: QProgressDialog, words: str) -> None:
        super().__init__(progress)
        self._progress = progress
        self._words = words

    @Slot(str)
    def say(self, text: str) -> None:
        self._progress.setLabelText(f"{self._words}\n{text}")


class _Progress(QProgressDialog):
    """A progress dialog that stays until its job has ended: once Stop was
    pressed it has no button, and Esc or the window's close box hid it while
    the job ran on (the state "Stopping…" was there to end)."""

    stopping = False

    def reject(self) -> None:
        if not self.stopping:
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.stopping:
            self.canceled.emit()  # as Stop: the job is told, and this stays
        event.ignore()


def run_with_progress(
    parent: QWidget,
    session,
    words: str,
    job: Callable,
    result: dict,
    *,
    title: str,
    follow_notices: bool = False,
    on_stop: Callable[[], None] | None = None,
) -> None:
    """Run `job` (a generator, as GenerationWorker takes one) for `session`,
    lent to the thread for its while; a failure is `result["error"]`, a Stop
    `result["stopped"]`. `follow_notices`: each notice the job yields is the
    progress dialog's label, under `words`. `on_stop` tells a job that waits
    on something other than the story model's stream (the embeddings) that
    Stop was pressed.

    After Stop the dialog stays, saying "Stopping…", until the job has
    ended. Hidden at once (QProgressDialog hides itself on cancel), it left
    the window free while the job ran on, and quitting then waited for the
    whole of it behind a closed window (the author, Nov 21 2026)."""
    worker = GenerationWorker(job, provider=session.provider)
    thread = WorkerThread(session=session)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.failed.connect(lambda message: result.__setitem__("error", message))
    worker.finished.connect(thread.quit)
    # Whether the job has ended, set on the worker's thread (a list, touched
    # by nothing else there): the thread may still say it runs as it ends.
    ended: list[bool] = []
    worker.finished.connect(lambda: ended.append(True), Qt.DirectConnection)
    loop = QEventLoop()
    # Cleanup on the thread, never the worker's own finished (CONTRIBUTING.md).
    thread.finished.connect(loop.quit)
    progress = _Progress(words, "Stop", 0, 0, parent)
    progress.setWindowTitle(title)
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.setMinimumWidth(460)

    def stop() -> None:
        result["stopped"] = True
        worker.stop()
        if on_stop is not None:
            on_stop()
        if not ended:
            progress.stopping = True
            progress.setLabelText(f"{words}\nStopping…")
            progress.setCancelButton(None)
            progress.show()

    progress.canceled.connect(stop)
    if follow_notices:
        worker.notice.connect(_Label(progress, words).say)
    progress.show()
    thread.start()
    loop.exec()
    if not ended:
        # The loop ended before the job did: the application is quitting.
        # Stop it rather than wait out the whole of it.
        stop()
    # Closing a QProgressDialog emits canceled, which would mark every job
    # that finished as stopped: Find the people then offered nothing and
    # Write with AI showed no changes (the lab walk, Nov 20 2026).
    progress.canceled.disconnect(stop)
    progress.hide()  # not close(): closing is taken for Stop while it runs
    progress.deleteLater()
    thread.wait()
    worker.deleteLater()
    thread.deleteLater()
