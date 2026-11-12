"""Plain background threads pause the garbage collector (sealedlore.threads).

Collecting on a background thread swept up Qt objects and crashed in
PySide's hand-back of them to the GUI thread (gui/speed_test.py), so
every plain thread goes through one helper that pauses the collector while
any is alive and has the GUI collect once the last ends."""

from __future__ import annotations

import ast
import gc
import threading
import time
import weakref
from pathlib import Path

import pytest

from sealedlore import threads
from sealedlore.threads import BackgroundThread, thread_pool

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "sealedlore"


def _settle() -> None:
    """Earlier tests' threads may still be finishing. A QThread lets go of
    the pause on the GUI thread, so events are let through while waiting."""
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
    except ImportError:
        app = None
    deadline = time.monotonic() + 10
    while threads.alive() and time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    assert threads.alive() == 0, "a background thread outlived its test"
    threads.resume_collector()


@pytest.fixture
def woken(monkeypatch):
    """A wake-up that only records, in place of the GUI's."""
    _settle()
    calls: list[str] = []
    monkeypatch.setattr(threads, "_wake", lambda: calls.append(threading.current_thread().name))
    yield calls
    threads.resume_collector()
    assert gc.isenabled()


def test_the_collector_is_off_while_a_background_thread_lives(woken):
    assert gc.isenabled()
    go = threading.Event()
    seen: list[bool] = []

    def work() -> None:
        go.wait(5)
        seen.append(gc.isenabled())

    thread = BackgroundThread(target=work, name="test-work")
    thread.start()
    assert not gc.isenabled()
    assert threads.alive() == 1
    go.set()
    thread.join(5)
    assert seen == [False]
    assert woken == ["test-work"], "the last to end wakes the GUI"
    assert not gc.isenabled(), "only the GUI switches it back on"
    assert threads.resume_collector()
    assert gc.isenabled()


def test_it_stays_off_until_the_last_of_several_ends(woken):
    gates = [threading.Event(), threading.Event()]
    workers = [BackgroundThread(target=gate.wait, args=(5,)) for gate in gates]
    for worker in workers:
        worker.start()
    gates[0].set()
    workers[0].join(5)
    assert woken == []
    assert not threads.resume_collector(), "one is still alive"
    assert not gc.isenabled()
    gates[1].set()
    workers[1].join(5)
    assert len(woken) == 1
    assert threads.resume_collector()


def test_a_pool_is_counted_until_its_threads_are_joined(woken):
    with thread_pool(3) as pool:
        states = list(pool.map(lambda _: gc.isenabled(), range(6)))
        assert threads.alive() == 1
    assert states == [False] * 6
    assert threads.alive() == 0
    assert len(woken) == 1


def test_with_no_gui_the_collector_is_never_touched(monkeypatch):
    _settle()
    monkeypatch.setattr(threads, "_wake", None)
    seen: list[bool] = []
    thread = BackgroundThread(target=lambda: seen.append(gc.isenabled()))
    thread.start()
    thread.join(5)
    assert seen == [True]
    assert gc.isenabled()


def test_the_gui_collects_and_switches_it_back_on_once_the_last_ends():
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.collector import install_collector_pause

    _settle()
    app = QApplication.instance()
    install_collector_pause(app)
    thread = BackgroundThread(target=time.sleep, args=(0.05,))
    thread.start()
    assert not gc.isenabled()
    thread.join(5)
    deadline = time.monotonic() + 5
    while not gc.isenabled() and time.monotonic() < deadline:
        app.processEvents()
    assert gc.isenabled()


def _pump(app, condition, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.005)


def test_a_worker_thread_holds_the_pause_until_it_has_ended_and_been_waited_for():
    """Python runs on a QThread before its first slot and after its last, as
    it ends: the collector stays off for the thread's whole life, and the GUI
    thread switches it back on."""
    from PySide6.QtCore import QObject, Signal, Slot
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.collector import install_collector_pause
    from sealedlore.gui.workers import WorkerThread

    _settle()
    app = QApplication.instance()
    install_collector_pause(app)
    go = threading.Event()
    seen: list[bool] = []

    class Job(QObject):
        finished = Signal()

        @Slot()
        def run(self) -> None:
            go.wait(5)
            seen.append(gc.isenabled())
            self.finished.emit()

    job = Job()
    thread = WorkerThread()
    job.moveToThread(thread)
    thread.started.connect(job.run)
    job.finished.connect(thread.quit)
    assert gc.isenabled()
    thread.start()
    assert not gc.isenabled(), "paused before the thread runs anything"
    assert threads.alive() == 1
    go.set()
    _pump(app, lambda: threads.alive() == 0)
    assert seen == [False]
    assert thread.isFinished(), "let go only once it has ended"
    _pump(app, gc.isenabled)
    job.deleteLater()


def test_a_worker_thread_deleted_before_its_finish_came_lets_go_all_the_same():
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.workers import WorkerThread

    _settle()
    app = QApplication.instance()
    thread = WorkerThread()
    thread.start()
    thread.quit()  # its event loop ends as soon as it begins
    assert thread.wait(5000)
    assert threads.alive() == 1, "its queued finish hasn't been delivered yet"
    gone = weakref.ref(thread)
    del thread  # deleted, with the finish still queued
    assert gone() is None
    _pump(app, lambda: threads.alive() == 0)
    _pump(app, gc.isenabled)


def test_the_windows_job_runs_with_the_collector_paused(tmp_path):
    """MainWindow._start: the job's own code sees it off."""
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.collector import install_collector_pause
    from sealedlore.gui.main_window import MainWindow

    _settle()
    app = QApplication.instance()
    install_collector_pause(app)
    window = MainWindow(root=tmp_path, use_mock=True)
    seen: list[bool] = []

    def job():
        seen.append(gc.isenabled())
        return
        yield

    window._start(job, streaming=False)
    _pump(app, lambda: not window._busy)
    assert seen == [False]
    _pump(app, lambda: threads.alive() == 0 and gc.isenabled())
    window.close()


def test_while_paused_the_gui_thread_collects_the_young_generations(woken):
    """Every job's thread pauses the collector now, some for many minutes:
    the GUI thread's garbage is collected meanwhile, there."""
    first = gc.get_threshold()[0]
    threads.hold()
    try:
        assert not gc.isenabled()
        assert not threads.collect_young(), "nothing piled up yet"
        piled = []
        for _ in range(first + 50):
            pair: list = []
            pair.append(pair)  # a cycle: only the collector frees it
            piled.append(pair)
        del piled, pair
        assert gc.get_count()[0] > first
        assert threads.collect_young()
        assert gc.get_count()[0] < first
        assert not gc.isenabled(), "still paused for every other thread"
    finally:
        threads.release()


def test_with_the_collector_on_nothing_is_collected_young(woken):
    assert gc.isenabled()
    assert not threads.collect_young()


def _qthreads(path: Path) -> list[str]:
    """QThreads made, subclassed or pooled anywhere but gui/workers.py, as
    "file:line what"."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    banned = {"QThread", "QRunnable", "QThreadPool", "QtConcurrent"}

    def name_of(node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and name_of(node.func) in banned:
            found.append(f"{path.name}:{node.lineno} {name_of(node.func)}(")
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if name_of(base) in banned:
                    found.append(f"{path.name}:{node.lineno} class {node.name}({name_of(base)})")
        elif isinstance(node, ast.Attribute) and node.attr in ("QThreadPool", "QtConcurrent"):
            found.append(f"{path.name}:{node.lineno} {node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module and "QtConcurrent" in node.module:
            found.append(f"{path.name}:{node.lineno} import {node.module}")
    return found


def test_every_qthread_is_a_worker_thread():
    """A QThread made anywhere else (or a pool's runnable) would let the
    collector run on it beside the window, before its first slot or as it
    ends."""
    helper = SOURCE_ROOT / "gui" / "workers.py"
    offenders = [
        made
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if path != helper
        for made in _qthreads(path)
    ]
    assert offenders == []
    assert _qthreads(helper), "the scan finds the helper's own"


def _thread_starts(path: Path) -> list[str]:
    """Every way of starting a plain thread that doesn't go through the
    helper, as "file:line what"."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    banned_calls = {"Thread", "ThreadPoolExecutor", "Timer", "start_new_thread"}

    def name_of(node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and name_of(node.func) in banned_calls:
            found.append(f"{path.name}:{node.lineno} {name_of(node.func)}(")
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if name_of(base) == "Thread":
                    found.append(f"{path.name}:{node.lineno} class {node.name}(Thread)")
    return found


def test_every_background_thread_goes_through_the_helper():
    """A `threading.Thread(` (or a pool, a timer) started anywhere else would
    let the collector run on it beside the window."""
    helper = SOURCE_ROOT / "threads.py"
    offenders = [
        start
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if path != helper
        for start in _thread_starts(path)
    ]
    assert offenders == []
    assert _thread_starts(helper), "the scan finds the helper's own"
