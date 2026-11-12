"""One writer: nothing changes a story while a thread works on it (gui/workers.py).

A dialog's thread writes a prompt from the story's cached prefix under the
dialog's `exec()`, and the window's timers and job signals still fire then.
They used to check only the window's own job (`_busy`): a finished rebuild,
condensed cards or a card's new picture were adopted, and the story saved,
on the GUI thread while the dialog's thread assembled a prompt from it. Now
the thread lends the story, and what would change it waits for it back.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal, Slot  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.image_dialog import ImageDialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.workers import WorkerThread, lending, lent  # noqa: E402
from sealedlore.models.image import GeneratedImage  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_for(app: QApplication, condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    yield window
    window.close()


class _Job(QObject):
    """A thread's job that waits for the test to let it end."""

    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.go = threading.Event()

    @Slot()
    def run(self) -> None:
        self.go.wait(10)
        self.finished.emit()


@pytest.fixture
def lend(app):
    """Lends a session to a thread whose job waits for the test; every one
    is let end and waited for afterwards, passed or failed (a QThread
    destroyed while running aborts the run)."""
    running: list[tuple[WorkerThread, _Job]] = []

    def start(session) -> tuple[WorkerThread, _Job]:
        job = _Job()
        thread = WorkerThread(session=session)
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.finished.connect(thread.quit)
        thread.start()
        running.append((thread, job))
        return thread, job

    yield start
    for thread, job in running:
        job.go.set()
        thread.wait(10_000)
    app.processEvents()


def test_a_thread_lends_its_session_until_it_has_ended(app, lend):
    session = object()
    back: list[object] = []
    lending().returned.connect(back.append)
    try:
        thread, job = lend(session)
        assert lent(session)
        job.go.set()
        wait_for(app, lambda: back == [session])
        assert not lent(session)
        assert thread.isFinished()
    finally:
        lending().returned.disconnect(back.append)


def test_a_finished_rebuild_waits_for_the_dialogs_thread(app, window: MainWindow, lend):
    session = window.session
    adopted: list[int] = []
    session.rebuilding_ids = lambda: [] if adopted else ["chapter-1"]
    session.rebuild_ready = lambda: True
    session.adopt_rebuild = lambda: adopted.append(1) or []
    thread, job = lend(session)
    window._rebuild_timer.start()
    window._poll_rebuild()
    assert adopted == [], "adopted while a thread was writing from the story"
    job.go.set()
    wait_for(app, lambda: adopted == [1])
    assert not window._rebuild_timer.isActive()


def test_condensed_cards_wait_for_the_dialogs_thread(app, window: MainWindow, lend):
    session = window.session
    adopted: list[int] = []
    started: list[int] = []
    session.condensing_ids = lambda: [] if adopted else ["char-serrik"]
    session.condensing_ready = lambda: True
    session.adopt_condensed = lambda: adopted.append(1) or []
    session.start_condensing = lambda *_args: started.append(1) or False
    thread, job = lend(session)
    window._condense_timer.start()
    window._poll_condensing()
    # The edit's delayed start finds the story lent too; it isn't asked again.
    window._condense_if_needed()
    assert adopted == [] and started == []
    job.go.set()
    wait_for(app, lambda: adopted == [1] and started == [1])


def test_a_cards_picture_waits_for_the_dialogs_thread_and_is_never_lost(
    app, window: MainWindow, lend
):
    session = window.session
    taken: list[int] = []
    session.take_card_pictures = lambda: taken.append(1) or []
    record = GeneratedImage(
        anchor_node_id=None,
        file="images/portrait.png",
        prompt="A portrait.",
        model="hidream",
        size="1024x1024",
        card_id="char-serrik",
    )
    thread, job = lend(session)
    window._on_image_finished("job-1", session.story.id, [record], session.pictures)
    assert taken == [], "taken onto the card while a thread was writing from the story"
    job.go.set()
    wait_for(app, lambda: taken == [1])


def test_the_image_dialogs_prompt_writer_lends_the_story(app, window: MainWindow):
    """The dialog's own thread, as Write prompt runs it."""
    go = threading.Event()

    class Waiting(MockChatProvider):
        def stream(self, request):
            go.wait(10)
            yield from super().stream(request)

    window.session.provider = Waiting(['{"references": [], "prompt": "A door."}'])
    dialog = ImageDialog(window.session, parent=window)
    try:
        dialog.direction.setPlainText("The door")
        dialog._write()
        assert lent(window.session)
        assert window._story_in_use()
        go.set()
        wait_for(app, lambda: dialog._worker is None)
        wait_for(app, lambda: not lent(window.session))
    finally:
        go.set()
        thread = dialog._thread
        if thread is not None:
            thread.wait(10_000)
        app.processEvents()
        dialog.deleteLater()
