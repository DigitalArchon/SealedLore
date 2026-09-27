"""A "Test attestation" button beside wherever a private model is chosen.

engine/attestation_check.py does the checking. The thread belongs to one
app-wide `AttestationTests` (parented to the application), never to the
dialog showing the button: a dialog closed mid-fetch would destroy a running
QThread and take the app down (the image catalog learned this). The window
waits for it on close.
"""

from __future__ import annotations

import inspect
import weakref
from collections.abc import Callable
from itertools import count

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sealedlore.engine.attestation_check import (
    AttestationCheck,
    check_attestation,
    testable,
)
from sealedlore.gui.worker import GenerationWorker
from sealedlore.models.config import ProviderConfig

Settings = Callable[[], ProviderConfig | None]


class AttestationTests(QObject):
    """Runs attestation tests off the GUI thread, one per request."""

    finished = Signal(int, object)  # job id, AttestationCheck

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ids = count(1)
        self._running: dict[int, tuple[QThread, GenerationWorker]] = {}

    def start(self, settings: ProviderConfig) -> int:
        job_id = next(self._ids)
        result: list[AttestationCheck] = []

        def job():
            try:
                result.append(check_attestation(settings))
            except Exception as exc:  # noqa: BLE001 - shown, never fatal
                result.append(AttestationCheck("error", f"The check couldn't run: {exc}"))
            return
            yield

        worker = GenerationWorker(job)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda: self._done(job_id, result))
        self._running[job_id] = (thread, worker)
        thread.start()
        return job_id

    def _done(self, job_id: int, result: list[AttestationCheck]) -> None:
        self._running.pop(job_id, None)
        self.finished.emit(job_id, result[0] if result else AttestationCheck("error", "No result"))

    def wait(self, msecs: int = 35_000) -> None:
        """Before the app closes: a QThread destroyed while running takes it down."""
        for thread, _worker in list(self._running.values()):
            thread.wait(msecs)


_tests: AttestationTests | None = None


def attestation_tests() -> AttestationTests:
    """The app's one runner, parented to the application."""
    global _tests
    if _tests is None:
        _tests = AttestationTests(QCoreApplication.instance())
    return _tests


class AttestationButton(QWidget):
    """ "Test attestation" and what it found. Shown only for a TEE/ or private/
    model; `settings` gives the endpoint and model as they stand when pressed."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Weakly: `settings` is usually a method of the dialog this button sits
        # in, and a button holding its own parent makes a cycle whose
        # collection crashed PySide (the whole Settings dialog, garbage-collected
        # in a test run, took the process down).
        self._settings = (
            weakref.WeakMethod(settings) if inspect.ismethod(settings) else lambda: settings
        )
        self._job: int | None = None
        self._model = ""
        self.last: AttestationCheck | None = None
        self.button = QPushButton("Test attestation")
        self.button.setToolTip(
            "Check this model's enclave now, exactly as a private scene or TEE chat would "
            "on starting: whether it attests in full, partially (and why), or is refused."
        )
        self.button.clicked.connect(self.run)
        self.result = QLabel()
        self.result.setObjectName("hintLabel")
        self.result.setWordWrap(True)
        self.result.hide()
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.button)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(row)
        layout.addWidget(self.result)
        attestation_tests().finished.connect(self._on_finished)
        self.model_changed("")

    def model_changed(self, model: str) -> None:
        """Show for TEE/ and private/ models; a new model clears the old result."""
        self.setVisible(testable(model.strip()))
        if self._job is None:
            self.result.hide()
            self.result.clear()

    def run(self) -> None:
        get = self._settings()
        settings = get() if get is not None else None
        if settings is None or not testable(settings.model):
            self._show(AttestationCheck("error", "Choose a TEE/ or private/ model first."))
            return
        self._job = attestation_tests().start(settings)
        self._model = settings.model
        self.button.setEnabled(False)
        self._show(AttestationCheck("", f"Checking {settings.model}'s enclave…"))

    def _on_finished(self, job_id: int, check: AttestationCheck) -> None:
        if job_id != self._job:
            return
        self._job = None
        self.button.setEnabled(True)
        self._show(
            AttestationCheck(check.outcome, f"{self._model}: {check.headline}", check.detail)
        )

    def _show(self, check: AttestationCheck) -> None:
        self.last = check
        # The headline, in full; what was checked, in the tooltip (the notes
        # run to several lines, and would crowd the form).
        self.result.setText(check.headline)
        self.result.setToolTip(check.detail)
        self.result.setObjectName("warningLabel" if check.outcome == "refused" else "hintLabel")
        self.result.style().unpolish(self.result)
        self.result.style().polish(self.result)
        self.result.show()
        # A word-wrapped label grows the row only once the layouts above it
        # hear of it: without this it was drawn over the form's next row.
        width = max(self.width(), 300)
        self.result.setMinimumHeight(self.result.heightForWidth(width))
        self.updateGeometry()
