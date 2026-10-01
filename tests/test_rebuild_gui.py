"""Rebuild from the banner runs in the background; the window stays usable, offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.summary import Summary  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402
from tests.test_background_queue import wait_for  # noqa: E402
from tests.test_rebuild import Background, Storyteller  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path, story, cast) -> MainWindow:
    nodes = make_exchange(6)
    summaries = [
        Summary(covered_node_ids=[n.id for n in nodes[:4]], content="Chapter one."),
        Summary(covered_node_ids=[n.id for n in nodes[4:8]], content="Chapter two."),
    ]
    story.active_leaf_id = nodes[-1].id
    save_story_bundle(
        StoryBundle(story=story, cast=cast, nodes=nodes, summaries=summaries), root=tmp_path
    )
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.suggest_characters = False
    window._provider = Storyteller(["A passage."], Background())
    window.open_story(story.id)
    return window


def test_the_banner_rebuilds_in_the_background(app, window: MainWindow):
    session = window.session
    background = window._provider.background
    window._edit_node("a0", "Serrik kept his sword.")
    window._edit_node("a2", "Maela opened the gate.")
    assert window.staleness.isVisibleTo(window)
    background.gate.clear()
    window.staleness.rebuild_button.click()
    # No progress dialog, no busy worker: the author can keep going.
    assert not window._busy and window._busy_dialog is None
    assert session.rebuilding_ids()
    assert "in the background" in window.staleness.message.text()
    assert not window.staleness.rebuild_button.isEnabled()
    background.gate.set()
    wait_for(app, lambda: not session.rebuilding_ids())
    assert not any(s.stale for s in session.summaries)
    assert [s.content for s in session.summaries] == [
        "Rebuilt in the background 1.",
        "Rebuilt in the background 2.",
    ]
    assert not window.staleness.isVisibleTo(window)


def test_keep_as_written_from_the_panel(window: MainWindow):
    panel = window.summaries_panel
    panel.chapters.setCurrentRow(0)
    panel.keep_box.setChecked(True)
    assert window.session.summaries[0].keep_as_written
    assert "kept as written" in panel.chapters.item(0).text()
