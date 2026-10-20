"""Markdown in the transcript (gui/markdown_text.py): the author (Oct 18
2026) found a simple chat showing **bold**, lists and headings as written;
stories, Questions and chapters had the same problem. Shown as Markdown
reads, never as HTML, with the text as written kept for editing and Find
finding what the reader sees. Offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.app import load_stylesheet  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.markdown_text import has_markdown, markdown_html, shown_text  # noqa: E402
from sealedlore.gui.transcript import (  # noqa: E402
    AsideWidget,
    MessageWidget,
    SummaryCardWidget,
    TranscriptView,
)
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402

REPLY = (
    "Here is the plan:\n\n"
    "## Before dark\n"
    "- **Check** the fences\n"
    "- Bring in _the_ wood\n\n"
    "Then `lock up`.\n"
    "Second line of the same paragraph."
)


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_a_reply_s_markdown_is_shown_as_it_reads_and_plain_prose_as_written(app):
    widget = MessageWidget("Assistant", REPLY, "assistant", node_id="a1")
    body = widget.body
    assert body.textFormat() == Qt.RichText
    shown = shown_text(REPLY)
    assert "**" not in shown and "##" not in shown and "`" not in shown and "- " not in shown
    assert "Check the fences" in shown and "Before dark" in shown
    # A line break inside a paragraph stays a line break.
    assert "Then lock up.\nSecond line" in shown
    html = body.text()
    assert "font-weight:700" in html and "font-style:italic" in html  # **bold**, _italics_
    assert "font-size:9pt" not in html and "<body>" in html  # the label's own font
    plain = "He waited by the door. Snow fell.\nNothing moved."
    widget = MessageWidget("Storyteller", plain, "assistant", node_id="a2")
    assert widget.body.textFormat() == Qt.PlainText and widget.body.text() == plain
    assert not has_markdown(plain) and shown_text(plain) == plain


def test_markdown_is_never_html_and_shows_no_picture_or_opens_no_link(app):
    text = "A *sign*: <b>CLOSED</b> <img src=x> ![map](/etc/hosts) [the docs](https://x.example)"
    html = markdown_html(text)
    assert html is not None
    assert "<img" not in html and "<a " not in html
    shown = shown_text(text)
    assert "<b>CLOSED</b>" in shown and "<img src=x>" in shown
    assert "map" in shown and "the docs (https://x.example)" in shown
    widget = MessageWidget("Assistant", text, "assistant", node_id="a1")
    flags = widget.body.textInteractionFlags()
    assert not flags & Qt.LinksAccessibleByMouse and not widget.body.openExternalLinks()


def test_editing_streaming_questions_and_chapters_keep_the_text_as_written(app):
    widget = MessageWidget("Assistant", REPLY, "assistant", node_id="a1")
    widget.begin_edit()
    assert widget._editor is not None and widget._editor.toPlainText() == REPLY
    aside = AsideWidget("Why?", "Because **she** asked.")
    assert aside.body.textFormat() == Qt.RichText and aside.body.source == "Because **she** asked."
    card = SummaryCardWidget("Chapter 1", "**Bonds**\n- Ruth trusts John")
    assert "Bonds" in shown_text(card.body.source) and card.body.textFormat() == Qt.RichText
    view = TranscriptView()
    view.begin_streaming()
    for piece in ("Some **bo", "ld** text", "\n\n- one"):
        view.append_streaming(piece)
    body = view._streaming.body
    assert body.source == "Some **bold** text\n\n- one"
    assert "**" not in shown_text(body.source)


@pytest.fixture
def window(tmp_path: Path, story, cast, app):
    before = app.styleSheet()
    app.setStyleSheet(load_stylesheet())
    nodes = make_exchange(3)
    nodes[-1].content = "**Ruth** waits at the _gate_.\nThe dog\nbarks."
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes)
    bundle.story.active_leaf_id = nodes[-1].id
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1366, 768)
    window.show()
    window.open_story(story.id)
    yield window
    window.close()
    app.setStyleSheet(before)


def test_find_finds_what_the_reader_sees_and_selects_it(app, window: MainWindow):
    window.find_action.trigger()
    for words in ("Ruth waits at the gate", "dog\nbarks"):
        window.find_bar.field.setText(words.replace("\n", " "))
        window.find_bar._timer.stop()
        window._on_find_changed(words)
        for _ in range(10):
            app.processEvents()
        assert window.find_bar.count.text() == "1 of 1"
        body = window.transcript.message_widget("a2").body
        assert body.selectedText().replace(" ", "\n") == words
    window._on_find_changed("**Ruth**")
    assert window.find_bar.count.text() != "1 of 1"


def test_a_message_that_is_only_a_picture_shows_its_words_or_its_address(app):
    """A reply that was one picture link showed as an empty message (the
    picture is never loaded; with no alt text nothing was left to read)."""
    bare = "![](https://example.com/cat.png)"
    assert shown_text(bare) == "(https://example.com/cat.png)"
    assert "<img" not in (markdown_html(bare) or "")
    assert shown_text("![a cat](https://example.com/cat.png)") == "a cat"
    assert shown_text("![   ](https://example.com/cat.png)") == "(https://example.com/cat.png)"
    widget = MessageWidget("Assistant", bare, "assistant", node_id="a1")
    assert widget.body.text().strip()
    # Inside a code span the link is text, shown as written.
    assert shown_text("Write `![](x.png)` *there*") == "Write ![](x.png) there"
