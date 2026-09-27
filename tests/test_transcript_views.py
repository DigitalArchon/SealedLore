"""The transcript: built a page at a time, and viewable as the storyteller is sent it.

Offscreen. Building every message of a 498-message story took 11s of layout
on open and on every branch switch; only the newest page is built now.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.transcript import (  # noqa: E402
    PAGE,
    AsideWidget,
    MessageWidget,
    SummaryCardWidget,
    TranscriptView,
)
from sealedlore.models.aside import Aside  # noqa: E402
from sealedlore.models.summary import Summary  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    append_api_log,
    read_api_log,
    read_api_log_since,
    save_story_bundle,
)
from tests.conftest import make_exchange  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def flush(app: QApplication) -> None:
    for _ in range(3):
        app.processEvents()
        app.sendPostedEvents(None, QEvent.DeferredDelete)


def built_ids(view: TranscriptView) -> list[str]:
    layout = view._layout
    return [
        widget.node_id
        for widget in (layout.itemAt(i).widget() for i in range(layout.count()))
        if isinstance(widget, MessageWidget)
    ]


def test_only_the_newest_page_is_built(app, cast):
    nodes = make_exchange(50)  # 100 messages
    view = TranscriptView()
    view.show_path(nodes, cast)
    flush(app)
    assert built_ids(view) == [node.id for node in nodes[-PAGE:]]
    earlier = view.findChild(QPushButton, "earlierButton")
    assert earlier is not None and "60 more" in earlier.text()

    view.show_earlier()
    assert built_ids(view) == [node.id for node in nodes[-2 * PAGE :]]
    assert "20 more" in earlier.text()

    # Jumping to the first message builds back to it, and the row goes.
    assert view.message_widget(nodes[0].id) is not None
    assert built_ids(view) == [node.id for node in nodes]
    assert view._earlier is None


def test_a_short_story_is_built_whole_with_its_leading_asides(app, cast):
    nodes = make_exchange(3)
    early = Aside(anchor_node_id=None, question="Who is Serrik?", answer="A duellist.")
    view = TranscriptView()
    view.show_path(nodes, cast, asides_for=lambda anchor: [early] if anchor is None else [])
    flush(app)
    assert built_ids(view) == [node.id for node in nodes]
    assert len(view.findChildren(AsideWidget)) == 1
    assert view._earlier is None


def test_streaming_still_appends_at_the_bottom(app, cast):
    nodes = make_exchange(50)
    view = TranscriptView()
    view.show_path(nodes, cast)
    view.show_earlier()
    view.begin_streaming()
    view.append_streaming("The door gives.")
    layout = view._layout
    last = layout.itemAt(layout.count() - 2).widget()
    assert isinstance(last, MessageWidget) and last.body.text() == "The door gives."


# --- the storyteller's view ----------------------------------------------------------


@pytest.fixture
def window(app, tmp_path: Path, story, cast) -> MainWindow:
    nodes = make_exchange(6)
    bundle = StoryBundle(
        story=story,
        cast=cast,
        nodes=nodes,
        summaries=[
            Summary(covered_node_ids=[n.id for n in nodes[:4]], content="Serrik forced the door."),
            Summary(covered_node_ids=[n.id for n in nodes[4:8]], content="Maela found the key."),
        ],
        asides=[Aside(anchor_node_id="a5", question="Why?", answer="Because.")],
    )
    bundle.story.active_leaf_id = "a5"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    yield window
    window.close()


def test_the_storyteller_view_shows_summaries_then_what_is_sent(app, window: MainWindow):
    view = window.transcript
    flush(app)
    assert len(built_ids(view)) == 12
    assert len(view.findChildren(AsideWidget)) == 1

    window.set_storyteller_view(True)
    flush(app)
    cards = view.findChildren(SummaryCardWidget)
    assert [card.body.text() for card in cards] == [
        "Serrik forced the door.",
        "Maela found the key.",
    ]
    assert built_ids(view) == ["u4", "a4", "u5", "a5"]
    assert view.findChildren(AsideWidget) == []
    assert window.view_toggle.sent.isChecked()
    assert window.storyteller_view_action.isChecked()

    window.set_storyteller_view(False)
    flush(app)
    assert len(built_ids(view)) == 12
    assert view.findChildren(SummaryCardWidget) == []


def test_a_passage_the_budget_left_out_is_marked(app, window: MainWindow):
    window.session.last_prompt = SimpleNamespace(excluded_node_ids=("u4", "a4"))
    window.set_storyteller_view(True)
    flush(app)
    headers = {w.node_id: w.header.text() for w in window.transcript.findChildren(MessageWidget)}
    assert "not sent" in headers["u4"] and "not sent" in headers["a4"]
    assert "not sent" not in headers["a5"]


def test_the_view_survives_a_rebuild(app, window: MainWindow):
    window.set_storyteller_view(True)
    window.reload_transcript()
    flush(app)
    assert window.transcript.findChildren(SummaryCardWidget)


# --- the API log, read as it grows ------------------------------------------------------


def test_the_log_is_read_from_where_it_was_left(tmp_path: Path, story):
    save_story_bundle(StoryBundle(story=story), root=tmp_path)
    for index in range(3):
        append_api_log(story.id, {"id": str(index)}, root=tmp_path)
    entries, start, end = read_api_log_since(story.id, 0, root=tmp_path)
    assert [e["id"] for e in entries] == ["0", "1", "2"] and start == 0

    append_api_log(story.id, {"id": "3"}, root=tmp_path)
    log = tmp_path / "stories" / story.id / "api_log.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"id": "half')  # a line still being written
    more, start, later = read_api_log_since(story.id, end, root=tmp_path)
    assert [e["id"] for e in more] == ["3"] and start == end

    log.write_text('{"id": "new"}\n', encoding="utf-8")  # replaced, shorter
    again, start, _ = read_api_log_since(story.id, later, root=tmp_path)
    assert start == 0 and [e["id"] for e in again] == ["new"]
    assert read_api_log_since("missing", 0, root=tmp_path) == ([], 0, 0)


def test_the_story_cost_matches_a_full_read(app, window: MainWindow, tmp_path: Path):
    story_id = window.session.story.id
    append_api_log(
        story_id,
        {"id": "r1", "kind": "request", "payload": {"model": "m"}},
        root=tmp_path,
    )
    append_api_log(
        story_id,
        {"id": "x1", "kind": "response", "request_log_ref": "r1", "usage": {"cost": 0.25}},
        root=tmp_path,
    )
    window._refresh_story_cost()
    append_api_log(
        story_id,
        {"id": "x2", "kind": "response", "request_log_ref": "r1", "usage": {"cost": 0.5}},
        root=tmp_path,
    )
    window._refresh_story_cost()
    assert window._log_cache[2] == read_api_log(story_id, root=tmp_path)


# --- building fast (tools/bench/transcript_bench.py) ------------------------------------


def test_a_text_body_measures_again_when_its_text_font_or_style_changes(app):
    """The remembered height is always what a plain label would measure now."""
    from PySide6.QtWidgets import QLabel

    from sealedlore.gui.transcript import TextBody

    cached, plain = TextBody("Short."), QLabel("Short.")

    def same(step: str) -> int:
        for label in (cached, plain):
            label.ensurePolished()
        assert cached.heightForWidth(200) == plain.heightForWidth(200), step
        return cached.heightForWidth(200)

    for label in (cached, plain):
        label.setWordWrap(True)
    short = same("first")
    for label in (cached, plain):
        label.setText("A much longer line that has to wrap. " * 20)
    assert same("new text") > short
    font = cached.font()
    font.setPointSizeF(font.pointSizeF() * 2)
    for label in (cached, plain):
        label.setFont(font)
    same("new font")
    for label in (cached, plain):
        label.setStyleSheet("font-size: 60px; padding: 12px")  # the text size is a stylesheet
    same("new style")


def test_no_message_button_is_shown_before_it_has_a_parent(app, cast):
    """Shown while parentless, a widget becomes a window: on X11 each Edit
    button made and destroyed a native window, most of the time it took to
    build a long story on the display (and none of it offscreen)."""
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QAbstractButton

    shown_alone: list[str] = []

    class Watch(QObject):
        def eventFilter(self, watched, event):  # noqa: N802 - Qt naming
            if (
                event.type() == QEvent.Show
                and isinstance(watched, QAbstractButton)
                and watched.parentWidget() is None
            ):
                shown_alone.append(watched.text())
            return False

    watch = Watch()
    app.installEventFilter(watch)
    try:
        view = TranscriptView()
        view.show_path(make_exchange(2), cast)
        view.add_aside("Why?", "Because.", aside_id="x1")
    finally:
        app.removeEventFilter(watch)
    assert shown_alone == []


# --- the window: a bounded set of built messages ----------------------------------------


@pytest.fixture
def styled(app):
    """Scroll positions need the real stylesheet: without it, offscreen, the
    column lays its messages out zero high. It goes on each view (`styled_view`),
    not the app: restyling the app restyles every widget earlier tests left,
    3.5s a time. The views are deleted afterwards for the same reason."""
    yield app
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, TranscriptView):
            widget.deleteLater()
    flush(app)


def styled_view() -> TranscriptView:
    from sealedlore.gui.app import load_stylesheet

    view = TranscriptView()
    view.setStyleSheet(load_stylesheet())
    view.resize(900, 700)
    return view


def long_view(app, cast, exchanges: int, *, asides: dict[str, str] | None = None):
    from sealedlore.models.aside import Aside

    nodes = make_exchange(exchanges)
    for node in nodes:
        node.content = f"{node.content} " + "The station hummed through its night cycle. " * 6
    placed = [Aside(anchor_node_id=a, question=q, answer="A.") for a, q in (asides or {}).items()]
    view = styled_view()
    view.show_path(
        nodes, cast, asides_for=lambda anchor: [a for a in placed if a.anchor_node_id == anchor]
    )
    view.show()
    flush(app)
    return view, nodes


def built_messages(view: TranscriptView) -> int:
    return len(built_ids(view))


def test_a_jump_far_back_builds_a_window_not_the_whole_story(styled, cast):
    from sealedlore.gui.transcript import PAGE

    view, nodes = long_view(styled, cast, 100)  # 200 messages
    assert view.message_widget(nodes[0].id) is not None
    assert built_messages(view) == PAGE, "every message back to the first was built"
    assert view.unbuilt == 0 and view.unbuilt_later == 200 - PAGE
    later = view.findChild(QPushButton, "laterButton")
    assert later is not None and f"{200 - PAGE} more" in later.text()
    # And back: the newest page, followed at the bottom.
    view.jump_to_latest()
    flush(styled)
    assert view.at_latest and built_ids(view)[-1] == nodes[-1].id
    assert view.findChild(QPushButton, "laterButton") is None
    view.close()


def test_scrolling_up_keeps_the_window_bounded_and_the_reader_in_place(styled, cast):
    from sealedlore.gui.transcript import MAX_BUILT, PAGE

    view, _nodes = long_view(styled, cast, 150)  # 300 messages
    bar = view.verticalScrollBar()
    for _page in range(10):
        if not view.unbuilt:
            break
        bar.setValue(0)  # reaching the top builds the page above
        node_id, offset = view.place_of()
        flush(styled)
        assert built_messages(view) <= MAX_BUILT + PAGE // 2
        # The message the reader had at the top is still where they left it.
        widget = view._built_widget(node_id)
        assert widget is not None and widget.y() - bar.value() == offset
    assert view.unbuilt == 0 and view.unbuilt_later > 0
    assert built_messages(view) <= MAX_BUILT
    view.close()


def test_scrolling_down_an_old_window_reaches_the_newest(styled, cast):
    from sealedlore.gui.transcript import MAX_BUILT

    view, nodes = long_view(styled, cast, 150)
    view.jump_to_start()
    flush(styled)
    bar = view.verticalScrollBar()
    for _page in range(12):
        if view.at_latest:
            break
        bar.setValue(bar.maximum())
        flush(styled)
        assert built_messages(view) <= MAX_BUILT
    assert view.at_latest and built_ids(view)[-1] == nodes[-1].id
    assert view.findChild(QPushButton, "laterButton") is None
    view.close()


def test_writing_brings_the_window_back_to_the_newest(styled, cast):
    view, nodes = long_view(styled, cast, 100)
    view.jump_to_start()
    flush(styled)
    view.begin_streaming()
    view.append_streaming("The next passage.")
    flush(styled)
    assert view.at_latest
    layout = view._layout
    column = [layout.itemAt(i).widget() for i in range(layout.count() - 1)]
    assert column[-1] is view._streaming, "the passage streamed into the middle of the story"
    assert column[-2].node_id == nodes[-1].id
    view.end_streaming()

    view.clear()
    view, _nodes = long_view(styled, cast, 100)
    view.jump_to_start()
    view.begin_aside("Why?")
    assert view.at_latest
    view.close()


def test_asides_come_and_go_with_their_message(styled, cast):
    view, nodes = long_view(styled, cast, 100, asides={"a0": "EARLY-QUESTION"})

    def asides() -> list[str]:
        return [
            w.question.text()
            for w in view.findChildren(AsideWidget)
            if view._layout.indexOf(w) >= 0
        ]

    assert asides() == []
    view.jump_to_start()
    flush(styled)
    assert asides() == ["You asked: EARLY-QUESTION"]
    view.jump_to_latest()
    flush(styled)
    assert asides() == []
    view.close()


def test_the_storyteller_view_pages_and_jumps_the_same_way(styled, cast):
    from sealedlore.gui.transcript import ChapterCard

    nodes = make_exchange(100)
    view = styled_view()
    chapters = [ChapterCard(title="Chapter 1", text="What happened first.")]
    view.show_storyteller_view(chapters, nodes, cast)
    view.show()
    flush(styled)
    assert not view.findChildren(SummaryCardWidget) or all(
        view._layout.indexOf(c) < 0 for c in view.findChildren(SummaryCardWidget)
    )
    view.jump_to_start()
    flush(styled)
    cards = [c for c in view.findChildren(SummaryCardWidget) if view._layout.indexOf(c) >= 0]
    assert len(cards) == 1, "the chapter cards come with the start of the view"
    assert view.message_widget(nodes[-1].id) is not None and view.at_latest
    view.close()
