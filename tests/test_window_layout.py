"""The window's width shared between Stories, the story and the Inspector
(gui/window_layout.py), the story list's rows, and the spacing that keeps
controls from running into each other."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from sealedlore.gui.main_window import CENTRE_MARGIN, MainWindow
from sealedlore.gui.window_layout import INSPECTOR_ALONE_SHARE, INSPECTOR_WIDTHS, share_width
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.models.config import WindowLayout
from sealedlore.storage.repository import (
    StoryBundle,
    load_config,
    save_config,
    save_story_bundle,
)
from tests.conftest import make_exchange
from tests.test_ui_tidy import needs_real_fonts, settle


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1366, 768)
    window.show()
    # These measure the widths shared with the list beside the story.
    window.config.stories_list = True
    window.open_story(story.id)
    settle(app)
    yield window
    window.close()


@pytest.fixture
def alone(app, tmp_path: Path, story, cast):
    """A window opened on a story as a new setup has it: the list put away."""
    save_story_bundle(StoryBundle(story=story, cast=cast, nodes=make_exchange(2)), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1366, 768)
    window.show()
    settle(app)
    assert not window.stories_dock.isHidden(), "with no story open, the list opens one"
    window.open_story(story.id)
    settle(app)
    yield window
    window.close()


def test_a_story_open_puts_the_stories_list_away_and_the_inspector_takes_the_room(
    app, alone: MainWindow
):
    """The author (Oct 15 2026): the story gets the room, for every kind of
    story and chat; the list comes back when asked, and stays."""
    assert alone.stories_dock.isHidden() and not alone.stories_button.isChecked()
    assert alone.inspector_dock.width() >= round(1366 * INSPECTOR_ALONE_SHARE) - 40
    alone.stories_button.click()
    settle(app)
    assert not alone.stories_dock.isHidden() and alone.config.stories_list
    alone.stories_list_action.trigger()  # View → Stories list puts it away again
    settle(app)
    assert alone.stories_dock.isHidden() and not alone.config.stories_list
    assert not alone.stories_button.isChecked()
    alone.stories_button.click()
    alone.stories_dock.close()  # its own close button counts too
    assert not alone.config.stories_list
    alone._close_story()
    settle(app)
    assert not alone.stories_dock.isHidden(), "nothing open: the list is how one opens"
    assert not alone.stories_button.isEnabled()


def test_the_docks_take_what_they_want_when_there_is_room():
    assert share_width(1920, 800, 8, (320, 120), (480, 360)) == (320, 480)


def test_a_tight_window_takes_from_stories_before_the_inspector_goes_below_its_floor():
    floor = INSPECTOR_WIDTHS[0]
    # A 1366px laptop: 1366 - 766 - 8 leaves 592 for both docks.
    assert share_width(1366, 766, 8, (245, 120), (floor, 360)) == (592 - floor, floor)
    # Squeezed to its minimum, the Inspector jumped wider when a story filled
    # its forms: Stories gives way first, down to its own minimum.
    assert share_width(1300, 766, 8, (234, 120), (floor, 360)) == (1300 - 766 - 8 - floor, floor)
    # Then the Inspector, below its floor to its minimum.
    assert share_width(1100, 766, 8, (220, 120), (floor, 360)) == (120, 360)


def test_a_window_too_narrow_for_both_floors_keeps_the_minimums():
    assert share_width(900, 766, 8, (220, 120), (380, 360)) == (120, 360)


def test_on_a_laptop_the_stories_list_is_wide_enough_to_read(app, window: MainWindow):
    needs_real_fonts()
    window.resize(1366, 768)
    settle(app)
    assert window.stories_dock.width() >= 200
    assert window.inspector_dock.width() >= INSPECTOR_WIDTHS[0]
    # The composer keeps its two rows: who is in play on one line.
    composer = window.composer
    assert _same_line(composer.held, composer.scope)
    assert _same_line(composer.length, composer.ooc_toggle)
    assert not _same_line(composer.held, composer.length)


def _same_line(a: QWidget, b: QWidget) -> bool:
    """A line centres its controls, and a button may be a pixel or two
    shorter than a combo (on CI's fonts, 22px beside 24): on one line, their
    heights overlap."""
    first, second = a.geometry(), b.geometry()
    return first.top() <= second.bottom() and second.top() <= first.bottom()


def test_the_docks_grow_with_the_window_until_a_separator_is_dragged(app, window: MainWindow):
    window.resize(1366, 768)
    settle(app)
    narrow = window.stories_dock.width()
    window.resize(1900, 1000)
    settle(app)
    assert window.stories_dock.width() > narrow, "a maximised window shares its width out"

    # A press between a dock and the centre is on a separator.
    gap = QPoint(window.stories_dock.geometry().right() + 2, 300)
    assert window.childAt(gap) is None
    press = QMouseEvent(
        QEvent.MouseButtonPress,
        QPointF(gap),
        QPointF(gap),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )
    QApplication.sendEvent(window, press)
    dragged = window.stories_dock.width()
    window.resize(1500, 900)
    settle(app)
    assert window.stories_dock.width() == dragged, "the author's width is left alone"


def test_a_press_a_widget_passed_on_is_not_a_drag(app, window: MainWindow):
    label = window.findChild(QLabel)
    assert label is not None
    at = label.mapTo(window, label.rect().center())
    press = QMouseEvent(
        QEvent.MouseButtonPress,
        QPointF(at),
        QPointF(at),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )
    QApplication.sendEvent(window, press)
    assert not window._docks_dragged


def test_the_layout_is_kept_for_the_next_launch(app, tmp_path: Path, window: MainWindow):
    window.resize(1500, 900)
    settle(app)
    window._docks_dragged = True  # as a separator press sets it
    window.resizeDocks([window.stories_dock], [300], Qt.Horizontal)
    settle(app)
    kept_stories = window.stories_dock.width()
    window.close()
    saved = load_config(root=tmp_path).window
    assert saved is not None
    assert (saved.width, saved.height) == (1500, 900)
    assert saved.stories_width == kept_stories

    again = MainWindow(root=tmp_path, use_mock=True)
    try:
        again.place_on_screen()
        assert again.size().width() == min(1500, again.screen().availableGeometry().width())
        again.resize(1500, 900)
        again.show()
        settle(app)
        assert again.stories_dock.width() == kept_stories
    finally:
        again.close()


def test_a_saved_layout_never_opens_larger_than_the_screen(app, tmp_path: Path):
    from sealedlore.models.config import Config

    config = Config()
    config.window = WindowLayout(width=5000, height=4000, maximized=False)
    save_config(config, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    try:
        window.place_on_screen()
        room = window.screen().availableGeometry()
        assert window.width() <= room.width() and window.height() <= room.height()
    finally:
        window.close()


def test_the_centre_keeps_clear_of_the_docks(app, window: MainWindow):
    margins = window.centralWidget().layout().contentsMargins()
    assert margins.left() == margins.right() == CENTRE_MARGIN > 0
    composer = window.composer.mapTo(window.centralWidget(), QPoint(0, 0))
    assert composer.x() >= CENTRE_MARGIN


def test_a_long_title_wraps_to_two_lines_and_no_more(app, tmp_path: Path, cast):
    needs_real_fonts()
    from sealedlore.gui.stories import StoryListPanel
    from sealedlore.models.story import Story

    for story_id, title in (
        ("short", "Doomsville"),
        ("long", "A very long title " * 3),
        ("longer", "A very long title " * 8),
    ):
        story = Story(id=story_id, title=title.strip())
        save_story_bundle(StoryBundle(story=story, cast=cast, nodes=[]), root=tmp_path)
    panel = StoryListPanel(root=tmp_path)
    panel.resize(220, 600)
    panel.show()
    panel.refresh()
    settle(app)
    rows = panel.list
    heights = {}
    for row in range(rows.count()):
        item = rows.item(row)
        heights[item.data(Qt.UserRole)] = rows.visualItemRect(item).height()
    assert heights["long"] > heights["short"], "the title wraps rather than being cut"
    assert heights["longer"] == heights["long"], "past two lines it is cut with …"
    # The item's text is unchanged: the tooltip and the count read from it.
    assert rows.item(0).text().split("\n")[1] == "0 messages"
    panel.close()


def test_groups_stand_further_apart_than_a_caption_from_its_control(app):
    host = QWidget()
    row = WrapRow(host, spacing=6, group_spacing=12)
    a, b, c = (QLabel(text) for text in ("A", "B", "C"))
    row.add_group([a, b])
    row.add_group([c])
    host.resize(400, 40)
    host.show()
    settle(app)
    assert b.geometry().left() - a.geometry().right() - 1 == 6
    assert c.geometry().left() - b.geometry().right() - 1 == 12
    assert row.sizeHint().width() == sum(w.sizeHint().width() for w in (a, b, c)) + 6 + 12
    host.close()
