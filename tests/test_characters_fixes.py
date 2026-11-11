"""Bugs a review found in Your characters, the cast board's cards, the plot
editor, Setup and the new-story guide, each driven as the author meets it:
real clicks (with Ctrl and Shift) on the lists, real keys typed into the
fields."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.storage import collection  # noqa: E402
from sealedlore.storage.repository import save_story_bundle  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def settle(app: QApplication) -> None:
    for _ in range(10):
        app.processEvents()


# --- Your characters: the list chooses as any list does ----------------------------------


NAMES = ("Ben", "Elena", "Jane", "Marcus")


@pytest.fixture
def kept_window(app, tmp_path: Path):
    """Your characters with four of them kept, the page shown in a window."""
    root = tmp_path / "data"
    bundle = collection.load_collection(root)
    for name in NAMES:
        bundle.cast.append(collection.kept(name))
    save_story_bundle(bundle, root=root)
    window = MainWindow(root=root, use_mock=True)
    window.resize(1600, 1000)
    window.show()
    window.show_collection(True)
    settle(app)
    yield window
    window.close()


def kept_id(window: MainWindow, name: str) -> str:
    return next(c.id for c in window._kept() if c.name == name)


def click(app, page, name: str, modifier=Qt.NoModifier) -> None:
    """A real click on their row in the list, with Ctrl or Shift if given."""
    card_id = next(c.id for c in page._characters if c.name == name)
    row = next(
        row
        for row in range(page.people.count())
        if page.people.item(row).data(Qt.UserRole) == card_id
    )
    rect = page.people.visualItemRect(page.people.item(row))
    QTest.mouseClick(page.people.viewport(), Qt.LeftButton, modifier, rect.center())
    settle(app)


def chosen(page) -> list[str]:
    return sorted(page._card(card_id).name for card_id in page.chosen_ids())


def test_ctrl_click_chooses_several_as_any_list_does(app, kept_window):
    """Ctrl+click left nothing chosen, and the choice ran a click behind: the
    page re-selected the row Qt had just made current, in the middle of the
    press, so the press's own toggle undid it."""
    page = kept_window.collection_page
    click(app, page, "Elena")
    assert chosen(page) == ["Elena"]
    click(app, page, "Marcus", Qt.ControlModifier)
    assert chosen(page) == ["Elena", "Marcus"]
    assert page.title.text() == "2 characters"
    click(app, page, "Ben", Qt.ControlModifier)
    assert chosen(page) == ["Ben", "Elena", "Marcus"]
    click(app, page, "Elena", Qt.ControlModifier)
    assert chosen(page) == ["Ben", "Marcus"], "Ctrl+click on one chosen lets go of them"


def test_a_plain_click_after_several_shows_and_edits_that_one(app, kept_window):
    """Click Ben, Shift+click Jane, then click Elena: the list showed Elena
    while the title and the forms were Jane's, and what was typed went to
    Jane."""
    window = kept_window
    page = window.collection_page
    click(app, page, "Ben")
    click(app, page, "Jane", Qt.ShiftModifier)
    assert chosen(page) == ["Ben", "Elena", "Jane"]
    click(app, page, "Elena")
    assert chosen(page) == ["Elena"]
    assert page.people.currentItem().data(Qt.UserRole) == kept_id(window, "Elena")
    assert page.current == kept_id(window, "Elena")
    assert page.title.text() == "Elena"
    assert page.card_form.character is page.current_character()
    page.show_tab("Card")
    page.card_form.summary.setFocus()
    QTest.keyClicks(page.card_form.summary, "A nurse")
    window._flush_collection()
    saved = {c.name: c.summary for c in collection.load_collection(window.root).cast}
    assert saved["Elena"] == "A nurse" and not saved["Jane"]


def test_delete_after_several_asks_about_the_one_the_list_shows(app, kept_window, monkeypatch):
    """Delete acted on the card the forms still held (Jane), not the one the
    list showed (Elena)."""
    page = kept_window.collection_page
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(page, "_act", lambda what: asked.append((what, page.current)))
    click(app, page, "Ben")
    click(app, page, "Jane", Qt.ShiftModifier)
    click(app, page, "Elena")
    page.people.setFocus()
    QTest.keyClick(page.people, Qt.Key_Delete)
    assert asked == [("delete", kept_id(kept_window, "Elena"))]


def test_shift_and_arrows_choose_and_let_go_as_a_list_does(app, kept_window):
    page = kept_window.collection_page
    click(app, page, "Ben")
    page.people.setFocus()
    QTest.keyClick(page.people, Qt.Key_Down, Qt.ShiftModifier)
    settle(app)
    assert chosen(page) == ["Ben", "Elena"] and page.title.text() == "2 characters"
    QTest.keyClick(page.people, Qt.Key_Down)
    settle(app)
    assert chosen(page) == ["Jane"] and page.title.text() == "Jane"
    assert page.current == kept_id(kept_window, "Jane")


# --- Your characters: a tag for several reaches the forms of the one shown ----------------


def test_a_tag_added_to_several_is_kept_by_the_card_whose_forms_were_loaded(
    app, kept_window, monkeypatch
):
    """Elena shown, Elena and Marcus chosen, Add a tag… "winter": back on
    Elena, a key typed in her maker's notes wrote the About tab's old tags
    back over it, and "winter" was gone from the disk."""
    from PySide6.QtWidgets import QInputDialog

    window = kept_window
    page = window.collection_page
    click(app, page, "Elena")
    page.show_tab("About")
    click(app, page, "Marcus", Qt.ControlModifier)
    assert page.title.text() == "2 characters"
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("winter", True))
    page._chosen("tag")
    click(app, page, "Elena")
    assert page.title.text() == "Elena"
    assert page.about_form.tags.text() == "winter"
    page.about_form.creator_notes.setFocus()
    QTest.keyClicks(page.about_form.creator_notes, "Drawn in winter")
    window._flush_collection()
    saved = {c.name: c for c in collection.load_collection(window.root).cast}
    assert saved["Elena"].tavern.tags == ["winter"]
    assert saved["Elena"].tavern.creator_notes == "Drawn in winter"
    assert saved["Marcus"].tavern.tags == ["winter"]


def test_a_tag_taken_off_several_doesnt_come_back_on_the_card_shown(app, kept_window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    window = kept_window
    page = window.collection_page
    for name in ("Elena", "Marcus"):
        window._kept_card(kept_id(window, name)).tavern.tags = ["survival", "road"]
    window._save_collection()
    window._refresh_collection()
    click(app, page, "Elena")
    page.show_tab("About")
    assert page.about_form.tags.text() == "survival, road"
    click(app, page, "Marcus", Qt.ControlModifier)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("survival", True))
    page._chosen("untag")
    click(app, page, "Elena")
    assert page.about_form.tags.text() == "road"
    page.about_form.creator.setFocus()
    QTest.keyClicks(page.about_form.creator, "me")
    window._flush_collection()
    saved = {c.name: c for c in collection.load_collection(window.root).cast}
    assert saved["Elena"].tavern.tags == ["road"] and saved["Elena"].tavern.creator == "me"
