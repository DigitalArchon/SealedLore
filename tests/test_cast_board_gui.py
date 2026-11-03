"""The cast board (gui/cast_board.py, gui/window_board.py): every
character's pictures in the whole window, each as a sheet of looks and
expressions with its gaps ready to draw. Offscreen; nothing is sent to an
image model."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QItemSelectionModel  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.gui.fill_gaps import FillGapsDialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)
from tests.conftest import make_exchange  # noqa: E402


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
    window.open_story(story.id)
    session = window.session
    looks = [
        ("p", "normal clothes", "neutral"),
        ("h", "normal clothes", "happy"),
        ("a", "combat armour", "neutral"),
        ("u", "", ""),
    ]
    refs = []
    for ref_id, look, expression in looks:
        session.pictures.write(f"images/refs/{ref_id}.png", b"picture")
        refs.append(
            ImageRef(id=ref_id, file=f"images/refs/{ref_id}.png", look=look, expression=expression)
        )
    serrik(window).reference_images = refs
    session.full_path()[-1].meta.looks_wanted = {"char-serrik": "combat armour · angry"}
    yield window
    window.close()


def serrik(window: MainWindow):
    return next(c for c in window.session.cast if c.id == "char-serrik")


def cell_of(window: MainWindow, look: str, expression: str):
    board = window.cast_board
    sheet = board._sheet
    return board.sheet.cell_at(sheet.looks.index(look), sheet.expressions.index(expression))


def test_the_board_takes_the_window_and_gives_it_back_as_it_was(app, window):
    window.show()
    window.config.stories_list = False
    window._sync_stories_list()
    window.inspector_dock.setVisible(True)
    branch_bar = window.branch_bar.isVisible()
    window.cast_panel.board_button.click()
    assert window.board_shown()
    for widget in (window.inspector_dock, window.stories_dock, window.composer, window.find_button):
        assert not widget.isVisible()
    window._on_escape()
    assert not window.board_shown()
    assert window.inspector_dock.isVisible() and window.composer.isVisible()
    assert not window.stories_dock.isVisible()  # put away before, put away after
    assert window.branch_bar.isVisible() == branch_bar
    assert window.centre_stack.currentWidget() is window.transcript


def test_a_characters_sheet_shows_their_pictures_and_their_gaps(app, window):
    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    assert board.current == "char-serrik"
    assert board._sheet.looks == ["normal clothes", "combat armour"]
    assert board.sheet.rowCount() == 2 and board.sheet.columnCount() == 5
    shown = cell_of(window, "normal clothes", "neutral")
    assert shown.ref.id == "p" and shown.portrait_id == "p"
    gap = cell_of(window, "combat armour", "angry")
    assert gap.refs == [] and gap.gap.source.id == "a" and gap.gap.wanted == 1
    # Unlabelled pictures wait in the tray, to be labelled.
    assert board.tray.count() == 1 and board.tray.isVisibleTo(board)
    assert board.fill_button.text() == "Fill every gap (7)…"
    # Choosing gaps: the button says how many.
    sheet = board._sheet
    index = board.sheet.model().index(1, sheet.expressions.index("angry"))
    board.sheet.selectionModel().select(index, QItemSelectionModel.Select)
    assert board.fill_button.text() == "Draw 1 chosen…"


def test_filling_gaps_asks_for_only_what_differs_from_their_picture(app, window, monkeypatch):
    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    gaps = [board._sheet.gap("combat armour", "angry"), board._sheet.gap("normal clothes", "sad")]
    monkeypatch.setattr(window.image_catalog, "ensure", lambda *args, **kwargs: None)
    dialog = FillGapsDialog(window.session, serrik(window), gaps, catalog=None, browse=None)
    requests = dialog.requests()
    assert [(r.card_look, r.card_expression) for r in requests] == [
        ("combat armour", "angry"),
        ("normal clothes", "sad"),
    ]
    assert [r.references[0].ref_id for r in requests] == ["a", "p"]
    assert all(not r.card_first for r in requests)
    assert "Change their expression or pose to: angry." in requests[0].prompt
    assert "clothes" not in requests[0].prompt.split("Keep")[0]
    # Untick one and change a prompt: what is approved is what is sent.
    dialog.ticks[1].setChecked(False)
    dialog.prompts[0].setPlainText("Make him furious.")
    assert [r.prompt for r in dialog.requests()] == ["Make him furious."]
    assert dialog.draw_button.text() == "Draw 1"
    dialog.deleteLater()

    # From the board: the dialog's requests are started as card pictures.
    started: list = []
    monkeypatch.setattr(window, "_card_pictures_refused", lambda session: False)
    monkeypatch.setattr(window, "_start_card_pictures", started.extend)
    monkeypatch.setattr("sealedlore.gui.window_board.ask_fill_gaps", lambda *a, **k: ["request"])
    board.fill_requested.emit("char-serrik", gaps)
    assert started == ["request"]


def test_cells_being_drawn_say_so(app, window, monkeypatch):
    from types import SimpleNamespace

    request = SimpleNamespace(
        card_id="char-serrik", card_first=False, card_look="combat armour", card_expression="Angry"
    )
    monkeypatch.setattr(
        window.image_jobs, "pending", lambda story_id=None: [SimpleNamespace(request=request)]
    )
    window.show_cast_board(True, "char-serrik")
    assert cell_of(window, "combat armour", "angry").drawing == 1
    assert cell_of(window, "combat armour", "happy").drawing == 0
    assert window.cast_board.fill_button.text() == "Fill every gap (6)…"
    # Closing with pictures being drawn asks: none are, really.
    monkeypatch.undo()


def test_looks_and_expressions_of_ones_own_are_kept_on_the_card(app, window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("formal clothes", True))
    board._add_look()
    assert serrik(window).sheet_looks == ["formal clothes"]
    assert board._sheet.looks[-1] == "formal clothes"
    assert board._sheet.gap("formal clothes", "neutral").source is not None
    assert board._sheet.gap("formal clothes", "happy").source is None

    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("laughing", True))
    board._add_expression(everyone=True)
    for character in window.session.cast:
        assert "laughing" in character.sheet_expressions
    saved = load_story_bundle(window.session.story.id, root=window.root)
    card = next(c for c in saved.cast if c.id == "char-serrik")
    assert card.sheet_looks == ["formal clothes"] and "laughing" in card.sheet_expressions

    # A column renamed: every picture in it is relabelled.
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("smiling", True))
    board._rename(serrik(window), expression="happy")
    assert next(r for r in serrik(window).reference_images if r.id == "h").expression == "smiling"


def test_a_pictures_own_actions_from_the_sheet(app, window, monkeypatch):
    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    board.picture_action.emit("portrait", "char-serrik", "a")
    assert serrik(window).reference_images[0].id == "a"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    board.picture_action.emit("remove", "char-serrik", "h")
    assert "h" not in [r.id for r in serrik(window).reference_images]
    shown: list = []
    monkeypatch.setattr(window, "_show_look", lambda card_id, ref_id: shown.append(ref_id))
    board.picture_action.emit("show", "char-serrik", "a")
    assert shown == ["a"]
    # While the story's job runs, the cards are looked at, not changed.
    window._busy = True
    window._refresh_board()
    assert not board.add_look_button.isEnabled()
    board.picture_action.emit("portrait", "char-serrik", "p")
    assert serrik(window).reference_images[0].id == "a"
    window._busy = False


def test_everyone_is_on_the_overview_and_a_chat_has_no_board(app, window):
    window.show_cast_board(True)
    board = window.cast_board
    assert board.current is None and board.pages.currentIndex() == 0
    names = [board.people.item(i).text().split("\n")[0] for i in range(board.people.count())]
    assert names[0] == "Everyone"
    assert set(names[1:]) == {c.name for c in window.session.cast}
    assert len(board._overview_lists) == len(window.session.cast)
    board._overview_lists[0].itemClicked.emit(board._overview_lists[0].item(0))
    assert board.current == window.session.cast[0].id
    window.show_cast_board(False)
    window.session.story.mode = "chat"
    window._update_controls()
    assert not window.board_action.isVisible()
    window.show_cast_board(True)
    assert not window.board_shown()
