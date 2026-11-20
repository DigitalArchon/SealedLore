"""A model call made while a dialog waits for it: on a worker thread, a
progress dialog with Stop meanwhile, its label following the job's notices
(Write with AI, Find the people…)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtWidgets import QProgressDialog, QWidget

from sealedlore.gui.worker import GenerationWorker
from sealedlore.gui.workers import WorkerThread


def run_with_progress(
    parent: QWidget,
    session,
    words: str,
    job: Callable,
    result: dict,
    *,
    title: str,
    follow_notices: bool = False,
) -> None:
    """Run `job` (a generator, as GenerationWorker takes one) for `session`,
    lent to the thread for its while; a failure is `result["error"]`, a Stop
    `result["stopped"]`. `follow_notices`: each notice the job yields is the
    progress dialog's label, under `words`."""
    worker = GenerationWorker(job, provider=session.provider)
    thread = WorkerThread(session=session)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.failed.connect(lambda message: result.__setitem__("error", message))
    worker.finished.connect(thread.quit)
    loop = QEventLoop()
    # Cleanup on the thread, never the worker's own finished (CONTRIBUTING.md).
    thread.finished.connect(loop.quit)
    progress = QProgressDialog(words, "Stop", 0, 0, parent)
    progress.setWindowTitle(title)
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.setMinimumWidth(460)

    def stop() -> None:
        result["stopped"] = True
        worker.stop()

    progress.canceled.connect(stop)
    if follow_notices:
        worker.notice.connect(lambda text: progress.setLabelText(f"{words}\n{text}"))
    progress.show()
    thread.start()
    loop.exec()
    # Closing a QProgressDialog emits canceled, which would mark every job
    # that finished as stopped: Find the people then offered nothing and
    # Write with AI showed no changes (the lab walk, Nov 20 2026).
    progress.canceled.disconnect(stop)
    progress.close()
    progress.deleteLater()
    thread.wait()
    worker.deleteLater()
    thread.deleteLater()
