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
from pathlib import Path

import pytest

from sealedlore import threads
from sealedlore.threads import BackgroundThread, thread_pool

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "sealedlore"


def _settle() -> None:
    """Earlier tests' threads may still be finishing."""
    deadline = time.monotonic() + 10
    while threads.alive() and time.monotonic() < deadline:
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
