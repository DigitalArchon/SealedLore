"""The window looked over for what could be trimmed and said more plainly (Nov 22
2026): the Story menu's pictures and plot in submenus of their own, one access key
per item in every menu, Setup's About without the placeholder rows a story never
uses, and the Media page's captions shown whole."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtGui import QIcon, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QListWidgetItem,
    QMenu,
    QStyleOptionViewItem,
)

from tests.test_background_queue import _window  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _keys_clash(menu: QMenu, where: str) -> list[str]:
    """Visible items of `menu` that share an access key, and so on down."""
    seen: dict[str, str] = {}
    clashes = []
    for action in menu.actions():
        if action.isSeparator() or not action.isVisible():
            continue
        found = re.search(r"&([^&])", action.text())
        if found:
            key = found.group(1).lower()
            if key in seen:
                clashes.append(f"{where}: {seen[key]!r} and {action.text()!r} share &{key}")
            seen[key] = action.text()
        if action.menu() is not None:
            clashes += _keys_clash(action.menu(), f"{where} → {action.text()}")
    return clashes


def test_no_two_items_of_a_menu_share_an_access_key(app, tmp_path: Path, story, cast):
    """Alt and a letter went to whichever came first: three of the Story menu's
    items answered to u, two each to c, p, v and l."""
    window = _window(tmp_path, story, cast, [])
    clashes = []
    for top in window.menuBar().actions():
        if top.menu() is not None:
            clashes += _keys_clash(top.menu(), top.text())
    assert not clashes, "\n".join(clashes)
    window.close()


def test_pictures_and_the_plot_have_submenus_of_their_own(app, tmp_path: Path, story, cast):
    """The Story menu had twenty items; its five for pictures and video and four
    for the plot are a submenu each."""
    window = _window(tmp_path, story, cast, [])
    pictures = window.pictures_menu.actions()
    for action in (
        window.image_action,
        window.portraits_action,
        window.restyle_action,
        window.place_action,
        window.video_action,
    ):
        assert action in pictures
        assert action not in window.story_menu.actions()
    plot = window.plot_menu.actions()
    for action in (
        window.write_plot_action,
        window.follow_up_plot_action,
        window.import_plot_action,
        window.edit_plot_action,
    ):
        assert action in plot
        assert action not in window.story_menu.actions()
    assert window.plot_menu.menuAction().isVisible()
    window.close()


def test_setup_offers_whom_placeholders_name_only_when_a_text_uses_them(app, story, cast):
    """{{user}} and {{char}} name nobody in a story none of whose texts writes
    them: three rows to read past in nearly every story."""
    from sealedlore.gui.setup_dialog import SetupDialog

    unused = SetupDialog(story, cast, placeholders_used=False)
    unused.show()
    assert not unused.user_is.isVisibleTo(unused)
    assert not unused.char_is.isVisibleTo(unused)
    unused._save()
    assert story.setup.placeholders.user_id is None  # kept as it was
    unused.deleteLater()

    used = SetupDialog(story, cast, placeholders_used=True)
    used.show()
    assert used.user_is.isVisibleTo(used) and used.char_is.isVisibleTo(used)
    used.deleteLater()


def test_the_window_tells_setup_whether_the_story_uses_placeholders(
    app, tmp_path: Path, story, cast, monkeypatch
):
    from sealedlore.gui import main_window as window_module

    seen = []

    def run(dialog) -> int:
        seen.append(dialog._placeholders_used)
        return 0

    monkeypatch.setattr(window_module.SetupDialog, "exec", run)
    window = _window(tmp_path, story, cast, [])
    window.open_setup()
    window.session.cast[0].full_description = "Loyal to {{user}} above all."
    window.open_setup()
    assert seen == [False, True]
    window.close()


def test_a_card_picture_s_caption_fits_its_cell(app):
    """Name, look and expression a line each, in a cell tall enough for three:
    on two lines, "winter coat · neutral" was cut to "winter coat · …"."""
    from sealedlore.gui.images_panel import CARD_THUMB, _Gallery

    gallery = _Gallery(CARD_THUMB)
    picture = QPixmap(CARD_THUMB, CARD_THUMB)
    picture.fill()
    gallery.addItem(QListWidgetItem(QIcon(picture), "John\nwinter coat\nneutral"))
    gallery.show()
    gallery.fit()
    option = QStyleOptionViewItem()
    gallery.initViewItemOption(option)
    wanted = gallery.itemDelegate().sizeHint(option, gallery.model().index(0, 0))
    assert gallery.gridSize().height() >= wanted.height()
    assert gallery.gridSize().width() >= wanted.width()
    gallery.deleteLater()


def test_the_stories_list_says_what_kind_each_is_in_words(tmp_path: Path) -> None:
    from sealedlore.storage.library import StoryListing

    fields = {"id": "s", "title": "S", "messages": 0, "updated": ""}
    assert StoryListing(**fields, chat=True).kind == "Simple chat"
    assert StoryListing(**fields, plot=True).kind == "Story with a plot"
    assert StoryListing(**fields).kind == "Story"
