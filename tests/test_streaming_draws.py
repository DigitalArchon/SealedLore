"""A streaming reply is drawn a few times a second, its drawings never kept,
and every message's drawing let go of as its story, chat or private scene
ends (gui/transcript.py, gui/markdown_text.py).

Each piece of a reply used to draw the whole of it again, its Markdown read
from the start (a long reply's work grew with its square), and each of those
prefixes was kept in a cache of 2,048: the real messages were pushed out, and
the words of a memory-only chat or scene stayed in the process after it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui import markdown_text, transcript  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.markdown_text import markdown_html, shown_text  # noqa: E402
from sealedlore.gui.transcript import STREAM_DRAW_MS, TranscriptView  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402

PIECES = [f"**Word {n}** " if n % 7 == 0 else f"word {n} " for n in range(200)]


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _kept() -> int:
    return markdown_text._kept.cache_info().currsize


def test_a_streaming_reply_is_drawn_a_few_times_not_once_a_piece(app, monkeypatch):
    drawn: list[str] = []

    def counting(text: str, *, keep: bool = True):
        drawn.append(text)
        return markdown_html(text, keep=keep)

    monkeypatch.setattr(transcript, "markdown_html", counting)
    view = TranscriptView()
    view.begin_streaming()
    drawn.clear()  # the empty message it begins as
    for piece in PIECES:
        view.append_streaming(piece)
    body = view._streaming.body
    assert body.source == "".join(PIECES), "every piece is held, none lost"
    assert len(drawn) == 1, f"drawn {len(drawn)} times for {len(PIECES)} pieces"
    # What waits is drawn once the moment is up.
    deadline = time.monotonic() + 5
    while drawn[-1] != body.source:
        assert time.monotonic() < deadline, "what was held was never drawn"
        app.processEvents()
        time.sleep(STREAM_DRAW_MS / 1000 / 4)
    assert len(drawn) == 2
    # The reply's last pieces are drawn as it ends, the moment not up yet.
    view.append_streaming("the end")
    view.append_streaming(".")
    view.end_streaming()
    assert body.source.endswith("the end.") and drawn[-1] == body.source
    assert "Word 0" in body.text() and "**" not in shown_text(body.source)
    view.deleteLater()


def test_nothing_of_a_streaming_reply_is_kept(app):
    markdown_text.forget()
    view = TranscriptView()
    view.begin_streaming()
    for piece in PIECES:
        view.append_streaming(piece)
        app.processEvents()
    view.end_streaming()
    assert _kept() == 0, "the reply's prefixes were kept"
    view.deleteLater()


def test_what_is_kept_goes_as_the_story_closes(app, tmp_path: Path, story, cast):
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    markdown_html("A **secret** of the chat")
    shown_text("A **secret** of the chat")
    assert _kept() > 0
    window._close_story()
    assert _kept() == 0 and shown_text.cache_info().currsize == 0
    window.close()


def test_what_is_kept_goes_as_a_private_scene_ends(app, tmp_path: Path, story, cast, monkeypatch):
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)

    class Span:
        keep = "memory"
        model = "private/some-model"
        attestation = None

    open_span = [Span()]
    monkeypatch.setattr(type(window.session), "open_span", property(lambda _s: open_span[0]))
    window._sync_private_ui()
    markdown_html("Said in the **scene**")
    window._sync_private_ui()  # still open: kept
    assert _kept() > 0
    open_span[0] = None
    window._sync_private_ui()
    assert _kept() == 0, "the scene's words stayed after it ended"
    window.close()
