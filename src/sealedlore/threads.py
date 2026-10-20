"""Plain Python threads beside the window, and the garbage collector.

Python's collector runs on whichever thread crosses its threshold. Qt
objects it sweeps up off the GUI thread are handed back by PySide for
deletion there, which called a null pointer: a segfault every time in the
full test run once several plain threads ran at once (the speed test's,
gui/speed_test.py). So every plain thread the app starts goes through this module
(`BackgroundThread`, `thread_pool`, `holding`), which counts them; while
any is alive the collector is paused, and once the last ends the GUI is
woken to collect on its own thread and switch it back on.

No Qt here: the engine starts most of these threads. The GUI installs the
wake-up (`pause_collector_while_busy`, from `gui/collector.py`); with none
installed (the CLI, a headless test) threads are only counted and the
collector is never touched, since nothing Qt can be swept up.
"""

from __future__ import annotations

import gc
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

_lock = threading.Lock()
_alive = 0
# Set by the GUI: called, from whichever thread ended last, when none is
# alive any more. It must only post to the GUI thread (never collect).
_wake: Callable[[], None] | None = None
# Whether it was this module that switched the collector off.
_paused = False


def pause_collector_while_busy(wake: Callable[[], None] | None) -> None:
    """From the GUI, once: pause the collector while any thread counted
    here is alive, and call `wake` (from the thread that ended last) to
    have the GUI thread call `resume_collector`. None stops pausing."""
    global _wake
    with _lock:
        _wake = wake


def alive() -> int:
    """How many counted threads (and holds) are alive now."""
    with _lock:
        return _alive


def collector_paused() -> bool:
    with _lock:
        return _paused


def _began() -> None:
    global _alive, _paused
    with _lock:
        _alive += 1
        if _wake is not None and not _paused and gc.isenabled():
            # Switching it off is a flag, safe from any thread. Nothing is
            # collected here: this may be the worker's thread.
            gc.disable()
            _paused = True


def _ended() -> None:
    global _alive, _paused
    with _lock:
        _alive -= 1
        wake = _wake if _alive == 0 and _paused else None
        if _alive == 0 and _paused and wake is None:
            # The GUI stopped pausing meanwhile: nobody would wake it.
            _paused = False
            gc.enable()
    if wake is not None:
        try:
            wake()
        except Exception:  # noqa: BLE001 - the GUI is gone (a closing app)
            pass


def resume_collector() -> bool:
    """On the GUI thread: if nothing counted is alive and the collector was
    paused here, collect now, on this thread, and switch it back on.
    Whether it did.

    The young generations only: that is what the pause held back, and it
    resets the counts, so the collector doesn't start again on the next
    thread that allocates. A full collection also walks every long-lived
    object (0.2 s at the end of the test run, after every burst of threads,
    which is a hitch a turn on the GUI thread); the collector's own
    schedule does that, as it always has."""
    global _paused
    with _lock:
        if _alive or not _paused:
            return False
    # Still off for every other thread while this one collects.
    gc.collect(1)
    with _lock:
        if _alive or not _paused:
            return False  # a thread began meanwhile: its end wakes us again
        _paused = False
        gc.enable()
    return True


class BackgroundThread(threading.Thread):
    """A `threading.Thread` that is counted while it lives (see the module's
    note). Built and used exactly as one."""

    def __init__(
        self,
        target: Callable[..., object] | None = None,
        name: str | None = None,
        args: Iterable[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
        *,
        daemon: bool | None = True,
    ) -> None:
        super().__init__(target=target, name=name, args=args, kwargs=kwargs, daemon=daemon)
        self._counted = False

    def start(self) -> None:
        _began()
        self._counted = True
        try:
            super().start()
        except BaseException:
            self._counted = False
            _ended()
            raise

    def run(self) -> None:
        try:
            super().run()
        finally:
            if self._counted:
                self._counted = False
                _ended()


def hold() -> None:
    """Counted as one thread until `release`: for threads something else
    starts and ends (a pool's, a QThread's run of them)."""
    _began()


def release() -> None:
    _ended()


@contextmanager
def holding() -> Iterator[None]:
    """`hold` for as long as the block runs."""
    hold()
    try:
        yield
    finally:
        release()


@contextmanager
def thread_pool(max_workers: int) -> Iterator[ThreadPoolExecutor]:
    """A `ThreadPoolExecutor` whose threads are counted: the hold lasts
    until the pool has shut down and joined them."""
    with holding(), ThreadPoolExecutor(max_workers=max_workers) as pool:
        yield pool
