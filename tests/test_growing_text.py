"""Text boxes as tall as their text (gui/sizing.py), and the message editors."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

from sealedlore.gui.app import load_stylesheet
from sealedlore.gui.main_window import MainWindow
from sealedlore.gui.sizing import GrowingText
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.node import Node
from sealedlore.models.story import Story
from sealedlore.storage.repository import StoryBundle, save_config, save_story_bundle
from tests.test_ui_tidy import needs_real_fonts, settle


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def message(node_id: str, text: str, parent=None) -> Node:
    return Node(
        id=node_id, parent_id=parent, kind="assistant", speaker_id="__narrator__", content=text
    )


def save(root: Path, story_id: str, title: str, *nodes: Node) -> None:
    story = Story(id=story_id, title=title)
    if nodes:
        story.active_leaf_id = nodes[-1].id
    save_story_bundle(StoryBundle(story=story, nodes=list(nodes)), root=root)


@pytest.fixture
def window(app, tmp_path: Path):
    config = Config(
        providers=[ProviderConfig(name="p", base_url="https://e.example/v1", model="m/settings")],
        active_provider_name="p",
    )
    save_config(config, root=tmp_path)
    save(tmp_path, "s", "Story", message("n1", "Rain."))
    window = MainWindow(root=tmp_path, use_mock=True)
    window.show()
    window.open_story("s")
    settle(app)
    yield window
    window.close()


LONG = "\n\n".join(("Rain on the road to Hellsville, and no end to it. " * 12) for _ in range(5))


def test_a_box_is_as_tall_as_its_text_at_once_however_many_paragraphs(app):
    needs_real_fonts()
    host = QScrollArea()
    page = QWidget()
    layout = QVBoxLayout(page)
    box = GrowingText(LONG, lines=3, most=200)
    layout.addWidget(box)
    layout.addStretch(1)
    host.setWidget(page)
    host.setWidgetResizable(True)
    host.resize(600, 1000)
    host.show()
    settle(app)
    bar = box.verticalScrollBar()
    assert bar.maximum() == 0, "all of it shows, with no scroll bar of its own"
    # The document's own count had one line a paragraph until it painted.
    assert box.height() > 20 * box.fontMetrics().lineSpacing()
    box.setPlainText("Short.")
    settle(app)
    assert box.height() <= box.minimumSizeHint().height() + 2, "and shrinks again"
    host.close()


def test_a_box_in_a_scrolling_page_grows_to_most_of_its_view_then_scrolls(app):
    needs_real_fonts()
    host = QScrollArea()
    page = QWidget()
    layout = QVBoxLayout(page)
    box = GrowingText(LONG * 5, lines=3, most=4)
    layout.addWidget(box)
    layout.addStretch(1)
    host.setWidget(page)
    host.setWidgetResizable(True)
    host.resize(600, 500)
    host.show()
    settle(app)
    assert 0.7 * 500 < box.height() < 500, "past its `most`, and not past the view"
    assert box.verticalScrollBar().maximum() > 0
    host.close()


def test_a_page_follows_the_line_being_typed_in_a_box_that_has_grown_past_its_end(app, monkeypatch):
    host = QScrollArea()
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.addSpacing(900)
    box = GrowingText(LONG, lines=3, most=200)
    layout.addWidget(box)
    host.setWidget(page)
    host.setWidgetResizable(True)
    host.resize(600, 400)
    host.show()
    settle(app)
    bar = host.verticalScrollBar()
    bar.setValue(0)
    monkeypatch.setattr(box, "hasFocus", lambda: False)
    box._show_cursor()
    assert bar.value() == 0, "without the focus the page stays where the reader put it"
    monkeypatch.setattr(box, "hasFocus", lambda: True)
    box._show_cursor()
    at = box.viewport().mapTo(host.viewport(), box.cursorRect().center()).y()
    assert bar.value() > 0 and 0 <= at <= host.viewport().height()
    host.close()


def test_a_long_text_in_a_full_page_that_scrolls_keeps_its_height(app):
    """A resizable scroll area lays its page out from the minimum hints: once
    the page was full, every box in it was squeezed to its first few lines."""
    needs_real_fonts()
    host = QScrollArea()
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.addSpacing(900)
    box = GrowingText("Rain on the road to Hellsville, and no end to it. " * 12, lines=3, most=200)
    layout.addWidget(box)
    host.setWidget(page)
    host.setWidgetResizable(True)
    host.resize(600, 400)
    host.show()
    settle(app)
    assert box.height() > 6 * box.fontMetrics().lineSpacing()
    assert box.verticalScrollBar().maximum() == 0
    box.setPlainText("Short.")
    settle(app)
    assert box.height() < 5 * box.fontMetrics().lineSpacing() + 40, "and gives the room back"
    host.close()


def test_a_box_never_outgrows_the_view_of_a_small_page(app):
    """Its own `most` used to win: the lab's 1280x800 desktops had an editor
    taller than the view, with Save off the page."""
    needs_real_fonts()
    host = QScrollArea()
    page = QWidget()
    layout = QVBoxLayout(page)
    box = GrowingText(LONG * 5, lines=3, most=40)
    layout.addWidget(box)
    layout.addStretch(1)
    host.setWidget(page)
    host.setWidgetResizable(True)
    host.resize(600, 300)
    host.show()
    settle(app)
    assert box.height() <= host.viewport().height()
    assert box.verticalScrollBar().maximum() > 0
    host.close()


def test_a_box_outside_a_scrolling_page_stops_at_its_most(app):
    box = GrowingText(LONG * 5, lines=3, most=6)
    box.resize(400, 100)
    assert box.sizeHint().height() < 7 * box.fontMetrics().lineSpacing() + 40
    box.deleteLater()


def test_editing_a_long_message_shows_all_of_it_and_a_short_one_some_room(app, window):
    needs_real_fonts()
    app.setStyleSheet(load_stylesheet())  # unstyled, the column lays out zero high
    session = window.session
    long = message("n2", LONG, parent="n1")
    session.nodes.append(long)
    session.nodes[0].children.append("n2")
    session.story.active_leaf_id = "n2"
    window.reload_transcript()
    settle(app)
    view = window.transcript.viewport()
    widget = window.transcript.message_widget("n2")
    widget.begin_edit()
    settle(app)
    box = widget._editor
    if box.height() < 0.8 * view.height():
        assert box.verticalScrollBar().maximum() == 0
    assert box.height() > 200, "not the fixed box it was"
    short = window.transcript.message_widget("n1")
    short.begin_edit()
    settle(app)
    assert short._editor.height() >= short._editor.minimumSizeHint().height()
    assert box.height() > 200, "a second one open squeezes neither"
    short.cancel_edit()
    widget.cancel_edit()
    app.setStyleSheet("")


def test_every_message_in_a_story_open_in_an_editor_keeps_its_height(app, tmp_path):
    """Nothing in the column wraps then, so Qt's height-for-width said -1 and
    the column was squeezed into the view."""
    needs_real_fonts()
    save(tmp_path, "only", "Only", message("n1", LONG))
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1200, 500)
    window.show()
    app.setStyleSheet(load_stylesheet())
    window.open_story("only")
    settle(app)
    widget = window.transcript.message_widget("n1")
    widget.begin_edit()
    settle(app)
    column = window.transcript.widget()
    assert column.heightForWidth(column.width()) > 0
    assert widget._editor.height() >= widget._editor.sizeHint().height() - 1
    widget.cancel_edit()
    window.close()
    app.setStyleSheet("")
