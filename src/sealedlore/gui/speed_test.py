"""Settings → Models → Test speed…: each model's wait and rate, in a table.

engine/speed_test.py does the measuring. The thread belongs to one app-wide
`SpeedTests` (parented to the application), never to the dialog showing the
table: a dialog closed mid-test would destroy a running QThread and take the
app down (the image catalog learned this). The window waits for it on close.

Models are tested at the same time (a beta tester's request: one after
another, a run took as long as all of them together). Each has its own
connection, and a handful is well inside an endpoint's burst limit. Timed
alone and together, three runs each, their first words came no later.

**The garbage collector is paused while a run's threads are alive.** Python
collects on whichever thread crosses its threshold. With several threads
allocating at once it ran on one of them, swept up Qt objects left by windows
closed earlier, and PySide's hand-back of those to the GUI thread for
deletion called a null pointer (`Shiboken::BindingManager::
runDeletionInMainThread`): a segfault, every time, in the full test run.
Nothing is lost by the pause: what would have been collected is collected
when the run ends, on the GUI thread.
"""

from __future__ import annotations

import gc
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import count

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.speed_test import (
    PROMPT,
    SpeedResult,
    SpeedTarget,
    attest_first,
    provider_for,
    run_speed_test,
)
from sealedlore.models.config import ModelPrice, ProviderConfig
from sealedlore.providers.base import ChatProvider

MakeProvider = Callable[[ProviderConfig], ChatProvider]
COLUMNS = ("Model", "Used for", "First word", "Tokens a second", "In all", "Notes")
NOTES = len(COLUMNS) - 1
# More models than this wait their turn: an endpoint allows only so many
# calls in a few seconds.
MAX_AT_ONCE = 8


class _Run(QObject):
    """One test run, on its thread: every target at once, each on a thread
    of its own; results are handed on from this one as they come."""

    began = Signal(int)
    done = Signal(int, object)
    finished = Signal()

    def __init__(
        self,
        targets: Sequence[SpeedTarget],
        make_provider: MakeProvider,
        prices: Mapping[str, ModelPrice],
    ) -> None:
        super().__init__()
        self._targets = list(targets)
        self._make = make_provider
        self._prices = dict(prices)
        self._lock = threading.Lock()
        self._providers: list[ChatProvider] = []
        self._stopping = False

    def _prepare(self, target: SpeedTarget):
        """Make a model's client and attest its enclave if it has one, before
        anything is timed: (client, seconds attesting), or the result when it
        already failed. On a pool thread; never raises."""
        if self._stopping:
            return SpeedResult(model=target.model, stopped=True)
        try:
            provider = self._make(target.settings)
        except Exception as exc:  # noqa: BLE001 - shown in the table
            return SpeedResult(model=target.model, error=str(exc))
        with self._lock:
            self._providers.append(provider)
            if self._stopping:  # Stop came while the client was being made
                provider.cancel()
        try:
            attested = attest_first(provider, target.model)
        except Exception as exc:  # noqa: BLE001 - shown, never fatal
            attested = SpeedResult(model=target.model, error=str(exc))
        if isinstance(attested, SpeedResult):
            self._release(provider)
            return attested
        return provider, attested

    def _test(self, target: SpeedTarget, provider: ChatProvider, attested) -> SpeedResult:
        """One model's timed request, on a pool thread. Never raises."""
        try:
            if self._stopping:
                return SpeedResult(model=target.model, stopped=True)
            return run_speed_test(
                provider,
                target.model,
                prices=self._prices,
                attest=False,
                attested=attested,
                route=target.route,
            )
        except Exception as exc:  # noqa: BLE001 - shown, never fatal
            return SpeedResult(model=target.model, error=str(exc))
        finally:
            self._release(provider)

    def _release(self, provider: ChatProvider) -> None:
        with self._lock:
            if provider in self._providers:
                self._providers.remove(provider)
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    @Slot()
    def run(self) -> None:
        try:
            if not self._targets:
                return
            workers = min(len(self._targets), MAX_AT_ONCE)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for index in range(len(self._targets)):
                    self.began.emit(index)
                # Enclaves are attested first, and only then is anything
                # timed: the checking is heavy, and made the others late.
                ready = list(pool.map(self._prepare, self._targets))
                rows = {}
                for index, (target, found) in enumerate(zip(self._targets, ready, strict=True)):
                    if isinstance(found, SpeedResult):
                        self.done.emit(index, found)
                    else:
                        rows[pool.submit(self._test, target, *found)] = index
                for future in as_completed(rows):
                    self.done.emit(rows[future], future.result())
        finally:
            self.finished.emit()

    def stop(self) -> None:
        """From the GUI thread: end every request in flight and skip the rest."""
        with self._lock:
            self._stopping = True
            for provider in self._providers:
                provider.cancel()


class SpeedTests(QObject):
    """Runs speed tests off the GUI thread, one run at a time."""

    began = Signal(int, int)  # run id, row
    done = Signal(int, int, object)  # run id, row, SpeedResult
    finished = Signal(int)  # run id

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ids = count(1)
        self._running: dict[int, tuple[QThread, _Run]] = {}
        # Whether it was this that paused the collector (see the module's note).
        self._paused_gc = False
        # Tests give a factory; the app reaches each model as play would.
        self.make_provider: MakeProvider = provider_for

    def start(
        self, targets: Sequence[SpeedTarget], prices: Mapping[str, ModelPrice] | None = None
    ) -> int:
        run_id = next(self._ids)
        if not self._running and gc.isenabled():
            # On the GUI thread, before any of the run's threads exist.
            gc.collect()
            gc.disable()
            self._paused_gc = True
        run = _Run(targets, self.make_provider, prices or {})
        thread = QThread(self)
        run.moveToThread(thread)
        thread.started.connect(run.run)
        run.began.connect(lambda row: self.began.emit(run_id, row))
        run.done.connect(lambda row, result: self.done.emit(run_id, row, result))
        run.finished.connect(thread.quit)
        # Cleanup hangs off the thread, never the worker's own signal.
        thread.finished.connect(lambda: self._ended(run_id))
        self._running[run_id] = (thread, run)
        thread.start()
        return run_id

    def stop(self, run_id: int) -> None:
        running = self._running.get(run_id)
        if running is not None:
            running[1].stop()

    def is_running(self, run_id: int) -> bool:
        return run_id in self._running

    def _ended(self, run_id: int) -> None:
        self._running.pop(run_id, None)
        if not self._running and self._paused_gc:
            self._paused_gc = False
            gc.enable()
        self.finished.emit(run_id)

    def wait(self, msecs: int = 35_000) -> None:
        """Before the app closes: a QThread destroyed while running takes it down."""
        for thread, run in list(self._running.values()):
            run.stop()
            thread.wait(msecs)


_tests: SpeedTests | None = None


def speed_tests() -> SpeedTests:
    """The app's one runner, parented to the application."""
    global _tests
    if _tests is None:
        _tests = SpeedTests(QCoreApplication.instance())
    return _tests


def seconds(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}s"


def describe(result: SpeedResult) -> tuple[str, str, str, str]:
    """First word, tokens a second, in all, notes: what the table shows.

    Each number says what it covers. A tester read "163 tokens in 6.6s"
    beside "360 tokens a second": the rate was the answer's own (it took
    under half a second, after six of reasoning) and the note had divided by
    the whole wait.
    """
    if result.stopped:
        return "", "", "", "Stopped"
    notes = []
    if result.error is not None:
        if result.first_reasoning is not None:
            notes.append(_reasoned(result, result.total))
        if result.attest_seconds is not None:
            notes.append(f"enclave attested in {seconds(result.attest_seconds)} first")
        return "", "", seconds(result.total), "; ".join([result.error.splitlines()[0], *notes])
    count_mark = "~" if result.tokens_estimated else ""
    took = seconds(result.answer_seconds)
    notes.append(f"the answer: {count_mark}{result.answer_tokens} tokens in {took}")
    if result.first_reasoning is not None:
        notes.append(_reasoned(result, result.first_text))
    if result.attest_seconds is not None:
        notes.append(f"enclave attested in {seconds(result.attest_seconds)} first")
    if result.finish_reason == "length":
        notes.append("cut off by the test's token limit")
    if result.cost is not None:
        notes.append(("~" if result.cost_estimated else "") + f"${result.cost:.4f}")
    if result.tokens_per_second is None:
        rate = "all at once"
    else:
        rate = f"{count_mark}{result.tokens_per_second:.0f}"
    return seconds(result.first_text), rate, seconds(result.total), "; ".join(notes)


def _reasoned(result: SpeedResult, until: float | None) -> str:
    """ "reasoned for 3.4s (784 tokens)": the part of the wait that was thinking."""
    spent = (until or result.total) - (result.first_reasoning or 0.0)
    tokens = f" ({result.reasoning_tokens} tokens)" if result.reasoning_tokens else ""
    return f"reasoned for {seconds(max(spent, 0.0))}{tokens}"


class SpeedTestDialog(QDialog):
    def __init__(
        self,
        targets: Sequence[SpeedTarget],
        parent: QWidget | None = None,
        *,
        prices: Mapping[str, ModelPrice] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Model speed test")
        self.setMinimumSize(860, 360)
        self.targets = list(targets)
        self.results: dict[int, SpeedResult] = {}

        intro = QLabel(
            f"Each model is sent one short request (“{PROMPT}”), all at the same time. "
            "First word is the wait before the answer begins, reasoning included. Tokens "
            "a second is how fast the answer itself is then written, first word to last, "
            "so a model that thinks long and writes fast is slow in the first and quick "
            "in the second. In all is the whole request. A ~ marks a count made here "
            "because the endpoint gave none. Speeds change with the hour and the load, so "
            "one run is a rough guide."
        )
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)

        self.table = QTableWidget(len(self.targets), len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().hide()
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(NOTES, QHeaderView.Stretch)
        for row, target in enumerate(self.targets):
            self._set(row, 0, target.label)
            self._set(row, 1, ", ".join(target.roles))
            self._set(row, NOTES, "Waiting")

        self.status = QLabel()
        self.status.setObjectName("hintLabel")
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.addButton(self.stop_button, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        runner = speed_tests()
        runner.began.connect(self._on_began)
        runner.done.connect(self._on_done)
        runner.finished.connect(self._on_finished)
        self._run = runner.start(self.targets, prices) if self.targets else None
        if self._run is None:
            self.status.setText("No model is set to test.")
            self.stop_button.setEnabled(False)

    def _set(self, row: int, column: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setToolTip(text)
        if column in (2, 3, 4):
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(row, column, item)

    def _on_began(self, run_id: int, row: int) -> None:
        if run_id != self._run:
            return
        self._set(row, NOTES, "Testing…")
        self.status.setText("Testing…")

    def _on_done(self, run_id: int, row: int, result: SpeedResult) -> None:
        if run_id != self._run:
            return
        self.results[row] = result
        first, rate, in_all, notes = describe(result)
        self._set(row, 2, first)
        self._set(row, 3, rate)
        self._set(row, 4, in_all)
        self._set(row, NOTES, notes)
        if result.error is not None:
            self.table.item(row, NOTES).setToolTip(result.error)
        self.table.resizeRowsToContents()
        left = len(self.targets) - len(self.results)
        if left:
            self.status.setText(f"Testing… {left} still to answer.")

    def _on_finished(self, run_id: int) -> None:
        if run_id != self._run:
            return
        self.stop_button.setEnabled(False)
        tested = sum(1 for result in self.results.values() if result.ok)
        self.status.setText(f"Done: {tested} of {len(self.targets)} answered.")

    def stop(self) -> None:
        if self._run is not None:
            speed_tests().stop(self._run)
            self.stop_button.setEnabled(False)
            self.status.setText("Stopping…")

    def done(self, result: int) -> None:  # noqa: N802 - Qt naming
        # The run outlives the dialog (it is the app's), but there is nobody
        # left to read it: end it rather than spend on answers unseen.
        if self._run is not None and speed_tests().is_running(self._run):
            speed_tests().stop(self._run)
        super().done(result)
