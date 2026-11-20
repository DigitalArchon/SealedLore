"""A model call made while a progress dialog waits (gui/modal_job.py), run
for real: every test of Find the people and Write with AI replaced the call,
so none saw that closing the dialog at the end counted as Stop, and both
threw away what the model had sent (found by the lab walk, Nov 20 2026)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QDialog, QProgressDialog

from sealedlore.engine.session import SessionNotice
from sealedlore.gui import lore_people_dialog
from sealedlore.gui.main_window import MainWindow
from sealedlore.gui.modal_job import run_with_progress
from sealedlore.models.lore import LoreEntry
from sealedlore.models.story import Story
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def window_with_lore(tmp_path: Path) -> MainWindow:
    story = Story(title="Hellsville")
    lore = [
        LoreEntry(title="Hellsville", content="A walled farming town two days north."),
        LoreEntry(
            title="Sheriff Ada Hale",
            content="Ada Hale has been the sheriff since the first winter: tall, slow to "
            "speak and quicker with a rifle than anyone in town.",
        ),
    ]
    save_story_bundle(StoryBundle(story=story, lore=lore), root=tmp_path / "data")
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    window.open_story(story.id)
    return window


def test_a_job_that_finishes_is_not_taken_for_stopped(app, tmp_path: Path):
    window = window_with_lore(tmp_path)
    ran = []

    def job():
        ran.append(True)
        return
        yield  # a generator, as GenerationWorker takes one

    result: dict = {}
    run_with_progress(window, window.session, "Working…", job, result, title="A job")
    assert ran == [True]
    assert "stopped" not in result and "error" not in result
    window.close()


def test_find_the_people_offers_whom_the_model_found(app, tmp_path: Path, monkeypatch):
    window = window_with_lore(tmp_path)
    reply = json.dumps(
        {"entries": [{"n": 2, "kind": "person", "name": "Ada Hale", "summary": "The sheriff."}]}
    )
    monkeypatch.setattr(window, "_current_provider", lambda: MockChatProvider([reply]))
    offered = []

    def exec_(dialog) -> int:
        offered.append(dialog.table.rowCount())
        return QDialog.Rejected

    monkeypatch.setattr(lore_people_dialog.LorePeopleDialog, "exec", exec_)
    window.find_people_in_story()
    assert offered == [1]
    window.close()


def test_the_jobs_notices_reach_the_dialog_on_the_gui_thread(app, tmp_path: Path, monkeypatch):
    """Set from the worker's thread, the label hung Find the people on Wayland
    and Windows: only the GUI thread may touch the dialog."""
    window = window_with_lore(tmp_path)
    seen: list[tuple[str, bool]] = []
    gui = app.thread()

    def set_label(dialog, text: str) -> None:
        seen.append((text, QThread.currentThread() is gui))

    monkeypatch.setattr(QProgressDialog, "setLabelText", set_label)

    def job():
        yield SessionNotice("Reading part 1 of 2")
        yield SessionNotice("Reading part 2 of 2")

    result: dict = {}
    run_with_progress(
        window, window.session, "Working…", job, result, title="A job", follow_notices=True
    )
    assert seen == [
        ("Working…\nReading part 1 of 2", True),
        ("Working…\nReading part 2 of 2", True),
    ]
    window.close()
