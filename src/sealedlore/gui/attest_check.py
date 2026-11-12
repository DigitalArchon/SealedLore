"""A "Test attestation" button beside wherever a private model is chosen.

engine/attestation_check.py does the checking, on a daemon thread of one
app-wide `AttestationTests` (parented to the application), never the dialog
showing the button. An attestation can't be cancelled: nothing waits for it,
not even quitting, and what it brings back for a button that is gone is
dropped (gui/workers.run_detached).
"""

from __future__ import annotations

import inspect
import weakref
from collections.abc import Callable
from itertools import count

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sealedlore.engine.attestation_check import (
    AttestationCheck,
    check_attestation,
    testable,
)
from sealedlore.gui.workers import run_detached
from sealedlore.models.config import ProviderConfig

Settings = Callable[[], ProviderConfig | None]


class AttestationTests(QObject):
    """Runs attestation tests off the GUI thread, one per request."""

    finished = Signal(int, object)  # job id, AttestationCheck

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ids = count(1)
        self._running: set[int] = set()

    def start(self, settings: ProviderConfig) -> int:
        job_id = next(self._ids)

        def job() -> AttestationCheck:
            try:
                return check_attestation(settings)
            except Exception as exc:  # noqa: BLE001 - shown, never fatal
                return AttestationCheck("error", f"The check couldn't run: {exc}")

        # A daemon thread nobody waits for (gui/workers.py): an attestation
        # can't be cancelled, and quitting no longer waits for one.
        run_detached(self, job, lambda result, _error: self._done(job_id, result), name="attest")
        self._running.add(job_id)
        return job_id

    def is_running(self, job_id: int) -> bool:
        return job_id in self._running

    def _done(self, job_id: int, result: AttestationCheck | None) -> None:
        self._running.discard(job_id)
        self.finished.emit(job_id, result or AttestationCheck("error", "No result"))


_tests: AttestationTests | None = None


def attestation_tests() -> AttestationTests:
    """The app's one runner, parented to the application."""
    global _tests
    if _tests is None:
        _tests = AttestationTests(QCoreApplication.instance())
    return _tests


class AttestationButton(QWidget):
    """ "Test attestation" and what it found. Shown only for a TEE/ or private/
    model; `settings` gives the endpoint and model as they stand when pressed,
    or raises ValueError to refuse in words."""

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
        try:
            settings = get() if get is not None else None
        except ValueError as exc:
            # Refused before anything is sent, in words (Settings: a key that
            # would go unencrypted, and the author said No).
            self._show(AttestationCheck("error", str(exc)))
            return
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
        # A refusal or a check that couldn't run is an error, said as one; a
        # partial result a warning (they read as dim hints, easy to miss).
        self.result.setObjectName(
            "errorLabel"
            if check.outcome in ("refused", "retired", "error")
            else "warningLabel"
            if check.outcome == "partial"
            else "hintLabel"
        )
        # The details could only be hovered: selectable now, to copy.
        self.result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result.style().unpolish(self.result)
        self.result.style().polish(self.result)
        self.result.show()
        # A word-wrapped label grows the row only once the layouts above it
        # hear of it: without this it was drawn over the form's next row.
        width = max(self.width(), 300)
        self.result.setMinimumHeight(self.result.heightForWidth(width))
        self.updateGeometry()
