"""Room to read, and marks that show (Oct 18 2026; the author: dialogs opened
too small, a world written in a box that grew only wider, columns that
couldn't be dragged, a box to type in that couldn't be made taller, arrows
on spin boxes and menu buttons that could hardly be seen, and the map
flashing black while it drew). Offscreen, mocks only."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QFormLayout,
    QHeaderView,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from sealedlore.gui import theme  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.sizing import (  # noqa: E402
    MOST_OF_SCREEN,
    GrowingText,
    adjustable_columns,
    size_to_screen,
)
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def settle(app: QApplication) -> None:
    for _ in range(10):
        app.processEvents()


def test_a_text_box_grows_with_its_text_and_then_scrolls(app):
    page = QWidget()
    form = QFormLayout(page)
    box = GrowingText(lines=3, most=10)
    form.addRow("Description", box)
    page.resize(400, 700)
    page.show()
    settle(app)
    empty = box.height()
    box.setPlainText("word " * 400)
    settle(app)
    assert box.height() > empty * 2, "it grows with what is written"
    capped = box.height()
    box.setPlainText("word " * 4000)
    settle(app)
    assert box.height() == capped, "up to its most, then it scrolls"
    box.setPlainText("one line")
    settle(app)
    assert box.height() == empty, "and back to its least"
    # A page's main field takes the height the window gives it, too.
    main = GrowingText(lines=4, fill=True)
    form.addRow("World", main)
    settle(app)
    assert main.height() > main.minimumSizeHint().height() * 2
    page.close()


def test_a_dialog_opens_at_a_share_of_its_screen_never_past_it(app):
    dialog = QDialog()
    room = dialog.screen().availableGeometry().size()
    size_to_screen(dialog, 0.5, 0.5)
    assert dialog.width() == int(room.width() * 0.5)
    size_to_screen(dialog, 2.0, 2.0)
    assert dialog.width() == int(room.width() * MOST_OF_SCREEN)
    small = QDialog()
    size_to_screen(small, 0.01, 0.01, at_least=QSize(300, 200))
    assert small.width() >= 300 and small.height() >= 200


def test_columns_fit_their_contents_and_can_be_dragged(app):
    tree = QTreeWidget()
    tree.setColumnCount(3)
    adjustable_columns(tree, stretch=1, widest=200)
    header = tree.header()
    assert header.sectionResizeMode(0) == QHeaderView.Interactive, "draggable"
    assert header.sectionResizeMode(1) == QHeaderView.Stretch
    tree.addTopLevelItem(QTreeWidgetItem(["a much longer model id than fits", "name", "x" * 300]))
    tree.resize(800, 300)
    tree.show()
    settle(app)
    assert header.sectionSize(0) > 120, "fitted to what it holds"
    assert header.sectionSize(2) == 200, "no wider than its widest"
    tree.close()


def test_every_arrow_is_a_mark_in_the_theme_s_colours(app):
    for choice in theme.THEMES.values():
        sheet = theme.render(theme.load_template(), choice)
        for shape, colour in theme.ICONS.values():
            url = theme.icon_url(shape, choice.colours[colour])
            path = Path(re.match(r'url\("(.*)"\)', url).group(1))
            assert path.is_file() and choice.colours[colour] in path.read_text()
        assert "QSpinBox::up-arrow" in sheet and "QToolButton::menu-indicator" in sheet
    # Qt's palette follows the theme: arrows left to the style were black.
    palette = theme.palette(theme.THEMES["dark"])
    text = palette.color(QPalette.ButtonText)
    ground = palette.color(QPalette.Button)
    assert text.lightness() - ground.lightness() > 120
    assert palette.color(QPalette.Window) == QColor(theme.DARK["window"])


def test_the_box_to_type_in_can_be_made_taller_and_stays_so(app, tmp_path, story, cast):
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1600, 1000)
    window.show()
    window.open_story(story.id)
    settle(app)
    split = window.story_split
    assert split.widget(1) is window.composer
    before = window.composer.input.height()
    total = sum(split.sizes())
    split.setSizes([total - 420, 420])
    window._composer_resized()
    settle(app)
    assert window.composer.input.height() > before + 100
    window._remember_layout()
    assert window.config.window.composer_height == window.composer.height()
    window.close()
