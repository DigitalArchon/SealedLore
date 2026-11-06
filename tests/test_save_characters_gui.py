"""A story's characters saved to Your characters (gui/save_characters.py,
gui/window_card_link.py): any of them or all at once, from the Cast page,
the cast board and Your characters' own page, each linked to the story's.
Before, only a lone button over one character's name did it, and the
board's Keep for new stories… read as if it did."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from sealedlore.gui import window_card_link  # noqa: E402
from sealedlore.gui.cast_board import KEEP_LABEL, SAVE_LABEL  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.save_characters import SaveCharactersDialog  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.models.supporting import SupportingCast  # noqa: E402
from sealedlore.storage import collection  # noqa: E402
from sealedlore.storage.images import REFS_DIR  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)
from tests.test_tavern import tiny_png  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path):
    root = tmp_path / "data"
    cast = [
        Character(id="john", name="John", summary="A farmer."),
        Character(id="jane", name="Jane", summary="His sister."),
    ]
    supporting = SupportingCast(characters=[Character(id="evan", name="Evan", summary="A bandit.")])
    story = Story(id="story-1", title="Doomsville", held_character_id="john")
    story.scene.present_character_ids = ["john"]
    save_story_bundle(StoryBundle(story=story, cast=cast, supporting=supporting), root=root)
    window = MainWindow(root=root, use_mock=True)
    window.open_story("story-1")
    window.session.pictures.write(f"{REFS_DIR}/john.png", tiny_png() + b"john")
    window.session.cast[0].reference_images.append(
        ImageRef(id="john-face", file=f"{REFS_DIR}/john.png", look="winter coat")
    )
    yield window
    window._private_allows_close = window._private_may_close = lambda: True
    window.close()


def answer(monkeypatch, *, tick: tuple[str, ...] | None = None, everyone: bool = False) -> list:
    """The dialog answered: everyone (All), or exactly `tick`, or as it opened;
    what it offered is recorded."""
    seen: list = []

    def exec_(dialog: SaveCharactersDialog) -> int:
        items = [dialog.list.item(row) for row in range(dialog.list.count())]
        seen.append(
            {
                "listed": [item.data(Qt.UserRole) for item in items],
                "offered": [item.data(Qt.UserRole) for item in dialog._offered()],
                "ticked": [c.id for c in dialog.chosen()],
            }
        )
        if everyone:
            dialog.all_button.click()
        elif tick is not None:
            dialog.none_button.click()
            for item in dialog._offered():
                if item.data(Qt.UserRole) in tick:
                    item.setCheckState(Qt.Checked)
        return QDialog.Accepted

    monkeypatch.setattr(SaveCharactersDialog, "exec", exec_)
    return seen


def kept_names(window) -> list[str]:
    return sorted(c.name for c in window._kept())


def test_the_cast_page_saves_everyone_at_once(app, window, monkeypatch):
    seen = answer(monkeypatch, everyone=True)
    window.cast_panel.select_character("jane")
    assert window.cast_panel.save_button.text() == SAVE_LABEL
    window.cast_panel.save_button.click()
    assert seen[0]["listed"] == ["john", "jane", "evan"]
    assert seen[0]["ticked"] == ["jane"], "the one shown, ticked to begin with"
    assert kept_names(window) == ["Evan", "Jane", "John"]
    session = window.session
    for character in (*session.cast, *session.supporting):
        card = window._kept_card(character.linked.card_id)
        assert card.name == character.name
        assert card.collection.came_from == "the story Doomsville"
    # John's picture went with him, labels and all, as Your characters' own file.
    john = session.cast[0]
    picture = window._kept_card(john.linked.card_id).reference_images[0]
    assert picture.look == "winter coat" and picture.file != john.reference_images[0].file
    assert window._collection_store().read(picture.file) == tiny_png() + b"john"
    # Both sides on disk.
    assert sorted(c.name for c in collection.load_collection(window.root).cast) == [
        "Evan",
        "Jane",
        "John",
    ]
    saved = load_story_bundle("story-1", root=window.root)
    assert all(c.linked is not None for c in (*saved.cast, *saved.supporting.characters))
    assert "Saved to Your characters: John, Jane, Evan." in window.statusBar().currentMessage()


def test_only_the_ones_ticked_are_saved(app, window, monkeypatch):
    answer(monkeypatch, tick=("evan",))
    window.cast_panel.save_button.click()
    assert kept_names(window) == ["Evan"]
    assert window.session.cast[0].linked is None
    assert window.session.supporting[0].linked is not None


def test_one_already_kept_is_listed_but_not_offered_again(app, window, monkeypatch):
    window.cast_panel.select_character("jane")
    window.cast_panel.link_button.click()  # Jane, saved from her own line
    seen = answer(monkeypatch, everyone=True)
    window.cast_panel.save_button.click()
    assert seen[0]["listed"] == ["john", "jane", "evan"]
    assert seen[0]["offered"] == ["john", "evan"]
    assert kept_names(window) == ["Evan", "Jane", "John"], "Jane once, not twice"


def test_one_whose_card_was_deleted_is_offered_again(app, window, monkeypatch):
    window.cast_panel.select_character("jane")
    window.cast_panel.link_button.click()
    jane = window.session.cast[1]
    collection.forget(window._collection_session().bundle, window._kept_card(jane.linked.card_id))
    seen = answer(monkeypatch, tick=("jane",))
    window.cast_panel.save_button.click()
    assert "jane" in seen[0]["offered"]
    assert window._kept_card(jane.linked.card_id).name == "Jane"


def test_the_line_says_where_a_character_is(app, window):
    window.cast_panel.select_character("john")
    panel = window.cast_panel
    assert panel.link_words.text() == window_card_link.ONLY_HERE
    assert panel.link_button.text() == window_card_link.KEEP_LABEL
    assert panel.link_button.toolTip() == window_card_link.KEEP_TIP
    panel.link_button.click()
    assert kept_names(window) == ["John"]
    assert "In step with their card" in panel.link_words.text()


def test_a_character_the_plot_hides_is_left_out_unless_spoilers_are_on(app, window, monkeypatch):
    monkeypatch.setattr(window.session, "hidden_ids", lambda: {"evan"})
    window.config.show_plot_spoilers = False
    seen = answer(monkeypatch, everyone=True)
    window.save_to_collection()
    assert seen[0]["listed"] == ["john", "jane"]
    assert "Evan" not in kept_names(window)
    window.config.show_plot_spoilers = True
    window.save_to_collection()
    assert seen[1]["listed"] == ["john", "jane", "evan"]


def test_the_cast_board_saves_the_one_shown_or_everyone(app, window, monkeypatch):
    seen = answer(monkeypatch, tick=())
    window.show_cast_board(True)
    board = window.cast_board
    assert board.save_button.isVisibleTo(board)
    assert board.keep_button.text() == KEEP_LABEL
    assert "Pictures only" in board.keep_button.toolTip()
    board.save_button.click()
    assert seen[-1]["ticked"] == ["john", "jane", "evan"], "everyone, when everyone is shown"
    board.show_character("jane")
    board.save_button.click()
    assert seen[-1]["ticked"] == ["jane"]
    window.show_cast_board(False)


def test_the_board_in_your_characters_offers_no_save(app, window):
    window.show_collection(True)
    board = window.collection_page.board
    assert not board.save_button.isVisibleTo(board)
    window.show_collection(False)


def test_your_characters_import_takes_the_open_storys(app, window, monkeypatch):
    answer(monkeypatch, tick=("jane", "evan"))
    window.show_collection(True)
    page = window.collection_page
    assert page.from_story_action.isVisible()
    page.from_story_action.trigger()
    assert kept_names(window) == ["Evan", "Jane"]
    assert page.current_character().name == "Jane", "the first of them shown"
    window.show_collection(False)


def test_your_characters_import_needs_a_story_open(app, window):
    window._close_story()
    window.show_collection(True)
    assert not window.collection_page.from_story_action.isVisible()
    window.show_collection(False)


def test_nothing_is_saved_in_a_private_scene(app, window, monkeypatch):
    monkeypatch.setattr(type(window.session), "in_private", property(lambda self: True))
    seen = answer(monkeypatch, everyone=True)
    window.cast_panel.save_button.click()
    assert seen == [] and window._kept() == []


def test_nobody_is_linked_when_your_characters_couldnt_be_kept(app, window, monkeypatch):
    answer(monkeypatch, everyone=True)
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(title))
    kept_session = window._collection_session()

    def fail() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(kept_session, "save", fail)
    window.cast_panel.save_button.click()
    assert warned == ["Couldn't keep your characters"]
    assert window._kept() == []
    assert all(c.linked is None for c in (*window.session.cast, *window.session.supporting))
    # No picture left behind in their folder.
    assert not any((window.root / "characters").rglob("*.png"))


def test_a_name_already_kept_is_said_and_both_are_kept(app, window, monkeypatch):
    window.cast_panel.select_character("john")
    window.cast_panel.link_button.click()
    window.session.cast[0].linked = None  # as if the story's John were another
    answer(monkeypatch, tick=("john",))
    window.cast_panel.save_button.click()
    assert kept_names(window) == ["John", "John"]
    assert "You already had a character named John" in window.statusBar().currentMessage()
